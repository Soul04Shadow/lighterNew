"""Delta neutral volume bot for the Lighter exchange.

This script coordinates pairs of accounts to generate hedged trading
volume by opening offsetting positions. All runtime behaviour is
configured through a YAML file; see
``examples/delta_neutral_volume_bot.example.yaml`` for a template.

The bot keeps detailed logs and aggregates volume statistics for each
pair and for the overall session. It also attempts to gracefully
recover from partial failures by submitting reduce-only hedge orders.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from dataclasses import dataclass, field
from logging.config import dictConfig
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

import yaml

import lighter
from lighter.configuration import Configuration
from lighter import nonce_manager
from lighter.models.tx_hash import TxHash
from lighter.signer_client import CODE_OK
from lighter.transactions.create_order import CreateOrder


class ConfigError(Exception):
    """Raised when the provided YAML configuration is invalid."""


@dataclass
class AccountCredentials:
    """Authentication and signing information for a trading account."""

    name: str
    private_key: str
    account_index: int
    api_key_index: int
    max_api_key_index: int = -1
    nonce_management: str = "optimistic"
    starting_client_order_index: int = 0
    additional_api_keys: Dict[int, str] = field(default_factory=dict)


@dataclass
class AccountPairConfig:
    """Describes how two accounts should trade together."""

    name: str
    long_account: str
    short_account: str
    leverage: Optional[float] = None
    margin_mode: Optional[str] = None
    trade_amount: Optional[int] = None
    max_slippage_bps: Optional[int] = None
    reduce_only: Optional[bool] = None


@dataclass
class BotSettings:
    """Global runtime parameters for the volume bot."""

    base_url: str
    market_index: int
    trade_amount: int
    trade_interval_seconds: float
    max_slippage_bps: int
    base_precision: int
    price_precision: int
    reduce_only: bool
    retry_attempts: int
    retry_backoff_seconds: float
    log_level: str = "INFO"
    margin_mode: str = "cross"
    leverage: Optional[float] = None
    order_book_depth: int = 5
    once: bool = False


@dataclass
class BotConfiguration:
    settings: BotSettings
    accounts: Dict[str, AccountCredentials]
    account_pairs: List[AccountPairConfig]
    logging_config: Optional[Dict[str, Any]] = None


@dataclass
class MarketSnapshot:
    best_bid: int
    best_ask: int

    @property
    def spread(self) -> int:
        return self.best_ask - self.best_bid


@dataclass
class PairStats:
    trades: int = 0
    base_volume: float = 0.0
    quote_volume: float = 0.0


@dataclass
class OrderExecution:
    account_name: str
    side: str
    order: CreateOrder
    response: TxHash
    reduce_only: bool


@dataclass
class ExecutionReport:
    pair_name: str
    executions: List[OrderExecution]
    errors: Dict[str, str] = field(default_factory=dict)
    hedged: bool = False


class VolumeStats:
    """Tracks aggregate trading statistics for reporting."""

    def __init__(self, base_precision: int, price_precision: int) -> None:
        self.base_scale = float(10**base_precision)
        self.price_scale = float(10**price_precision)
        self.total_trades = 0
        self.total_base_volume = 0.0
        self.total_quote_volume = 0.0
        self.pair_stats: Dict[str, PairStats] = {}

    def record_execution(self, pair_name: str, execution: OrderExecution) -> Tuple[float, float, PairStats]:
        order = execution.order
        base_amount = float(order.base_amount or 0)
        price = float(order.price or 0)
        base = base_amount / self.base_scale
        quote = base * (price / self.price_scale)
        pair_summary = self.pair_stats.setdefault(pair_name, PairStats())
        pair_summary.trades += 1
        pair_summary.base_volume += base
        pair_summary.quote_volume += quote
        self.total_trades += 1
        self.total_base_volume += base
        self.total_quote_volume += quote
        return base, quote, pair_summary

    def summary(self) -> str:
        return (
            f"trades={self.total_trades} "
            f"base_volume={self.total_base_volume:.6f} "
            f"quote_volume={self.total_quote_volume:.2f}"
        )

    def pair_summary(self, pair_name: str) -> PairStats:
        return self.pair_stats.get(pair_name, PairStats())


def _parse_int(value: Any, field_name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"Field '{field_name}' must be an integer") from exc


def _parse_float(value: Any, field_name: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"Field '{field_name}' must be a number") from exc


def resolve_margin_mode(margin_mode: str) -> Tuple[str, int]:
    mode = margin_mode.lower()
    if mode in {"cross", "cross_margin"}:
        return "cross", lighter.SignerClient.CROSS_MARGIN_MODE
    if mode in {"isolated", "isolated_margin"}:
        return "isolated", lighter.SignerClient.ISOLATED_MARGIN_MODE
    raise ConfigError(f"Unsupported margin mode '{margin_mode}'")


def resolve_nonce_manager_type(name: str) -> nonce_manager.NonceManagerType:
    normalized = name.lower()
    if normalized in {"optimistic", "opt"}:
        return nonce_manager.NonceManagerType.OPTIMISTIC
    if normalized in {"api", "http"}:
        return nonce_manager.NonceManagerType.API
    raise ConfigError(f"Unsupported nonce management strategy '{name}'")


def load_config(path: Path) -> BotConfiguration:
    if not path.exists():
        raise ConfigError(f"Configuration file '{path}' does not exist")

    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}

    if not isinstance(raw, dict):
        raise ConfigError("Configuration must define a mapping at the top level")

    bot_section = raw.get("bot") or {}
    settings = BotSettings(
        base_url=str(bot_section.get("base_url")),
        market_index=_parse_int(bot_section.get("market_index", 0), "bot.market_index"),
        trade_amount=_parse_int(bot_section.get("trade_amount", 1000), "bot.trade_amount"),
        trade_interval_seconds=_parse_float(
            bot_section.get("trade_interval_seconds", 30), "bot.trade_interval_seconds"
        ),
        max_slippage_bps=_parse_int(bot_section.get("max_slippage_bps", 50), "bot.max_slippage_bps"),
        base_precision=_parse_int(bot_section.get("base_precision", 4), "bot.base_precision"),
        price_precision=_parse_int(bot_section.get("price_precision", 2), "bot.price_precision"),
        reduce_only=bool(bot_section.get("reduce_only", False)),
        retry_attempts=_parse_int(bot_section.get("retry_attempts", 3), "bot.retry_attempts"),
        retry_backoff_seconds=_parse_float(
            bot_section.get("retry_backoff_seconds", 1.0), "bot.retry_backoff_seconds"
        ),
        log_level=str(bot_section.get("log_level", "INFO")),
        margin_mode=str(bot_section.get("margin_mode", "cross")),
        leverage=(
            _parse_float(bot_section.get("leverage"), "bot.leverage")
            if bot_section.get("leverage") is not None
            else None
        ),
        order_book_depth=_parse_int(bot_section.get("order_book_depth", 5), "bot.order_book_depth"),
        once=bool(bot_section.get("once", False)),
    )

    if not settings.base_url:
        raise ConfigError("'bot.base_url' must be provided")

    accounts_raw = raw.get("accounts")
    if not isinstance(accounts_raw, dict) or not accounts_raw:
        raise ConfigError("At least one account must be defined under 'accounts'")

    accounts: Dict[str, AccountCredentials] = {}
    for name, account_cfg in accounts_raw.items():
        if not isinstance(account_cfg, dict):
            raise ConfigError(f"Account '{name}' must be a mapping of settings")
        required_keys = {"private_key", "account_index", "api_key_index"}
        missing = required_keys - account_cfg.keys()
        if missing:
            raise ConfigError(f"Account '{name}' is missing required keys: {', '.join(sorted(missing))}")

        additional_api_keys = account_cfg.get("additional_api_keys") or {}
        if not isinstance(additional_api_keys, dict):
            raise ConfigError(
                f"Account '{name}' -> additional_api_keys must be a mapping of index to private key"
            )
        parsed_additional_keys = {int(idx): str(key) for idx, key in additional_api_keys.items()}

        accounts[name] = AccountCredentials(
            name=name,
            private_key=str(account_cfg["private_key"]),
            account_index=_parse_int(account_cfg["account_index"], f"accounts.{name}.account_index"),
            api_key_index=_parse_int(account_cfg["api_key_index"], f"accounts.{name}.api_key_index"),
            max_api_key_index=_parse_int(
                account_cfg.get("max_api_key_index", -1), f"accounts.{name}.max_api_key_index"
            ),
            nonce_management=str(account_cfg.get("nonce_management", "optimistic")),
            starting_client_order_index=_parse_int(
                account_cfg.get("starting_client_order_index", 0),
                f"accounts.{name}.starting_client_order_index",
            ),
            additional_api_keys=parsed_additional_keys,
        )

    raw_pairs = raw.get("account_pairs")
    if not isinstance(raw_pairs, list) or not raw_pairs:
        raise ConfigError("'account_pairs' must define at least one pair configuration")

    account_pairs: List[AccountPairConfig] = []
    for entry in raw_pairs:
        if not isinstance(entry, dict):
            raise ConfigError("Each account pair must be a mapping of settings")
        required_pair_keys = {"name", "long_account", "short_account"}
        missing_keys = required_pair_keys - entry.keys()
        if missing_keys:
            raise ConfigError(
                f"Account pair missing required fields: {', '.join(sorted(missing_keys))}"
            )
        pair = AccountPairConfig(
            name=str(entry["name"]),
            long_account=str(entry["long_account"]),
            short_account=str(entry["short_account"]),
            leverage=(
                _parse_float(entry.get("leverage"), f"account_pairs.{entry['name']}.leverage")
                if entry.get("leverage") is not None
                else None
            ),
            margin_mode=str(entry.get("margin_mode")) if entry.get("margin_mode") else None,
            trade_amount=(
                _parse_int(entry.get("trade_amount"), f"account_pairs.{entry['name']}.trade_amount")
                if entry.get("trade_amount") is not None
                else None
            ),
            max_slippage_bps=(
                _parse_int(
                    entry.get("max_slippage_bps"), f"account_pairs.{entry['name']}.max_slippage_bps"
                )
                if entry.get("max_slippage_bps") is not None
                else None
            ),
            reduce_only=bool(entry["reduce_only"]) if entry.get("reduce_only") is not None else None,
        )
        account_pairs.append(pair)

    logging_config = raw.get("logging")
    if logging_config is not None and not isinstance(logging_config, dict):
        raise ConfigError("'logging' section must be a valid dict compatible with logging.config.dictConfig")

    return BotConfiguration(
        settings=settings,
        accounts=accounts,
        account_pairs=account_pairs,
        logging_config=logging_config,
    )


def setup_logging(config: BotConfiguration) -> None:
    if config.logging_config:
        dictConfig(config.logging_config)
    else:
        logging.basicConfig(
            level=getattr(logging, config.settings.log_level.upper(), logging.INFO),
            format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        )


class AccountSession:
    """Wraps a SignerClient instance with convenience helpers."""

    def __init__(
        self,
        credentials: AccountCredentials,
        settings: BotSettings,
        logger: logging.Logger,
    ) -> None:
        self.credentials = credentials
        self.logger = logger.getChild(f"account[{credentials.name}]")
        nonce_type = resolve_nonce_manager_type(credentials.nonce_management)
        max_api_key_index = credentials.max_api_key_index
        if max_api_key_index < 0:
            max_api_key_index = -1
        private_keys = credentials.additional_api_keys or None
        self.client = lighter.SignerClient(
            url=settings.base_url,
            private_key=credentials.private_key,
            account_index=credentials.account_index,
            api_key_index=credentials.api_key_index,
            max_api_key_index=max_api_key_index,
            private_keys=private_keys,
            nonce_management_type=nonce_type,
        )
        self._client_order_index = credentials.starting_client_order_index

    def _next_client_order_index(self) -> int:
        current = self._client_order_index
        self._client_order_index += 1
        return current

    async def update_leverage(
        self,
        market_index: int,
        margin_mode_code: int,
        leverage: float,
        margin_mode_name: str,
    ) -> None:
        tx_info, response, err = await self.client.update_leverage(
            market_index=market_index,
            margin_mode=margin_mode_code,
            leverage=leverage,
        )
        if err:
            raise RuntimeError(f"Failed to update leverage: {err}")
        if response is None or response.code != CODE_OK:
            message = response.message if response is not None else "unknown error"
            raise RuntimeError(
                f"Unexpected response updating leverage (code={getattr(response, 'code', None)}): {message}"
            )
        self.logger.info(
            "Set leverage to x%.2f using %s margin (tx=%s)",
            leverage,
            margin_mode_name,
            response.tx_hash,
        )

    async def place_market_order(
        self,
        *,
        market_index: int,
        base_amount: int,
        max_slippage: float,
        is_ask: bool,
        reduce_only: bool,
        ideal_price: Optional[int],
        max_retries: int,
        retry_backoff: float,
        context: str,
    ) -> OrderExecution:
        last_error: Optional[Exception] = None
        for attempt in range(1, max_retries + 1):
            client_order_index = self._next_client_order_index()
            try:
                order, response, err = await self.client.create_market_order_limited_slippage(
                    market_index=market_index,
                    client_order_index=client_order_index,
                    base_amount=base_amount,
                    max_slippage=max_slippage,
                    is_ask=is_ask,
                    reduce_only=reduce_only,
                    ideal_price=ideal_price,
                )
                if err:
                    raise RuntimeError(err)
                if order is None or response is None:
                    raise RuntimeError("Exchange did not return an order confirmation")
                if response.code != CODE_OK:
                    raise RuntimeError(response.message or f"Unexpected response code {response.code}")

                side = "sell" if is_ask else "buy"
                self.logger.info(
                    "%s submitted %s order idx=%s base=%s price=%s reduce_only=%s tx=%s",
                    context,
                    side,
                    client_order_index,
                    order.base_amount,
                    order.price,
                    reduce_only,
                    response.tx_hash,
                )
                return OrderExecution(
                    account_name=self.credentials.name,
                    side=side,
                    order=order,
                    response=response,
                    reduce_only=reduce_only,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - deliberate broad catch for retry logic
                last_error = exc
                self.logger.warning(
                    "%s order attempt %d/%d failed for account %s: %s",
                    context,
                    attempt,
                    max_retries,
                    self.credentials.name,
                    exc,
                )
                if attempt == max_retries:
                    break
                await asyncio.sleep(retry_backoff * (2 ** (attempt - 1)))

        raise RuntimeError(f"Failed to submit order for account {self.credentials.name}") from last_error

    async def close(self) -> None:
        await self.client.close()


class AccountPairSession:
    def __init__(
        self,
        pair_config: AccountPairConfig,
        settings: BotSettings,
        long_session: AccountSession,
        short_session: AccountSession,
        stats: VolumeStats,
        logger: logging.Logger,
    ) -> None:
        self.config = pair_config
        self.settings = settings
        self.long_session = long_session
        self.short_session = short_session
        self.logger = logger.getChild(f"pair[{pair_config.name}]")
        margin_mode_name = pair_config.margin_mode or settings.margin_mode
        self.margin_mode_name, self.margin_mode_code = resolve_margin_mode(margin_mode_name)
        self.leverage = pair_config.leverage if pair_config.leverage is not None else settings.leverage
        self.trade_amount = pair_config.trade_amount if pair_config.trade_amount is not None else settings.trade_amount
        self.max_slippage_bps = (
            pair_config.max_slippage_bps
            if pair_config.max_slippage_bps is not None
            else settings.max_slippage_bps
        )
        self.max_slippage = self.max_slippage_bps / 10_000
        self.reduce_only = (
            pair_config.reduce_only if pair_config.reduce_only is not None else settings.reduce_only
        )
        self.stats = stats
        self.retry_attempts = settings.retry_attempts
        self.retry_backoff_seconds = settings.retry_backoff_seconds

    @property
    def name(self) -> str:
        return self.config.name

    async def prepare(self, market_index: int) -> None:
        if self.leverage is None:
            return
        await asyncio.gather(
            self.long_session.update_leverage(
                market_index, self.margin_mode_code, self.leverage, self.margin_mode_name
            ),
            self.short_session.update_leverage(
                market_index, self.margin_mode_code, self.leverage, self.margin_mode_name
            ),
        )

    async def execute_trade_cycle(
        self,
        *,
        market_index: int,
        snapshot: MarketSnapshot,
        price_fetcher: Callable[[], Awaitable[MarketSnapshot]],
    ) -> ExecutionReport:
        context = f"pair={self.config.name}"
        executions: Dict[str, OrderExecution] = {}
        errors: Dict[str, str] = {}

        async def submit_long() -> OrderExecution:
            return await self.long_session.place_market_order(
                market_index=market_index,
                base_amount=self.trade_amount,
                max_slippage=self.max_slippage,
                is_ask=False,
                reduce_only=self.reduce_only,
                ideal_price=snapshot.best_ask,
                max_retries=self.retry_attempts,
                retry_backoff=self.retry_backoff_seconds,
                context=context,
            )

        async def submit_short() -> OrderExecution:
            return await self.short_session.place_market_order(
                market_index=market_index,
                base_amount=self.trade_amount,
                max_slippage=self.max_slippage,
                is_ask=True,
                reduce_only=self.reduce_only,
                ideal_price=snapshot.best_bid,
                max_retries=self.retry_attempts,
                retry_backoff=self.retry_backoff_seconds,
                context=context,
            )

        tasks = {
            "long": asyncio.create_task(submit_long()),
            "short": asyncio.create_task(submit_short()),
        }

        for side, task in tasks.items():
            try:
                executions[side] = await task
            except asyncio.CancelledError:
                for other in tasks.values():
                    other.cancel()
                raise
            except Exception as exc:  # noqa: BLE001 - broad for controlled error handling
                errors[side] = str(exc)

        fallback_executions: List[OrderExecution] = []
        hedged = False

        if errors:
            self.logger.error("Encountered errors while submitting orders: %s", errors)

        if "long" in errors and "short" in executions:
            base_amount = executions["short"].order.base_amount or self.trade_amount
            try:
                snapshot = await price_fetcher()
                hedge_execution = await self.short_session.place_market_order(
                    market_index=market_index,
                    base_amount=base_amount,
                    max_slippage=self.max_slippage,
                    is_ask=False,
                    reduce_only=True,
                    ideal_price=snapshot.best_ask,
                    max_retries=self.retry_attempts,
                    retry_backoff=self.retry_backoff_seconds,
                    context=f"{context}-hedge-short",
                )
                fallback_executions.append(hedge_execution)
                hedged = True
            except Exception as exc:  # noqa: BLE001 - fallback best-effort
                errors["hedge_short"] = str(exc)

        if "short" in errors and "long" in executions:
            base_amount = executions["long"].order.base_amount or self.trade_amount
            try:
                snapshot = await price_fetcher()
                hedge_execution = await self.long_session.place_market_order(
                    market_index=market_index,
                    base_amount=base_amount,
                    max_slippage=self.max_slippage,
                    is_ask=True,
                    reduce_only=True,
                    ideal_price=snapshot.best_bid,
                    max_retries=self.retry_attempts,
                    retry_backoff=self.retry_backoff_seconds,
                    context=f"{context}-hedge-long",
                )
                fallback_executions.append(hedge_execution)
                hedged = True
            except Exception as exc:  # noqa: BLE001 - fallback best-effort
                errors["hedge_long"] = str(exc)

        all_executions = list(executions.values()) + fallback_executions
        if not all_executions and not errors:
            self.logger.warning("No executions were recorded for pair %s", self.config.name)

        for execution in all_executions:
            base, quote, pair_stats = self.stats.record_execution(self.config.name, execution)
            self.logger.info(
                "Executed %s order on %s | base=%.6f | quote=%.2f | reduce_only=%s | tx=%s",
                execution.side,
                execution.account_name,
                base,
                quote,
                execution.reduce_only,
                execution.response.tx_hash,
            )
            self.logger.debug(
                "Pair %s cumulative stats -> trades=%d base=%.6f quote=%.2f",
                self.config.name,
                pair_stats.trades,
                pair_stats.base_volume,
                pair_stats.quote_volume,
            )

        return ExecutionReport(
            pair_name=self.config.name,
            executions=all_executions,
            errors=errors,
            hedged=hedged,
        )


class DeltaNeutralVolumeBot:
    def __init__(self, config: BotConfiguration) -> None:
        self.config = config
        self.logger = logging.getLogger("lighter.volume_bot")
        self.stats = VolumeStats(
            base_precision=config.settings.base_precision,
            price_precision=config.settings.price_precision,
        )
        self.account_sessions: Dict[str, AccountSession] = {
            name: AccountSession(credentials=credentials, settings=config.settings, logger=self.logger)
            for name, credentials in config.accounts.items()
        }
        self.market_client = lighter.ApiClient(configuration=Configuration(host=config.settings.base_url))
        self.order_api = lighter.OrderApi(self.market_client)
        self.account_pairs: List[AccountPairSession] = []
        for pair_cfg in config.account_pairs:
            try:
                long_session = self.account_sessions[pair_cfg.long_account]
                short_session = self.account_sessions[pair_cfg.short_account]
            except KeyError as exc:
                raise ConfigError(
                    f"Pair '{pair_cfg.name}' references unknown account '{exc.args[0]}'"
                ) from exc
            self.account_pairs.append(
                AccountPairSession(
                    pair_config=pair_cfg,
                    settings=config.settings,
                    long_session=long_session,
                    short_session=short_session,
                    stats=self.stats,
                    logger=self.logger,
                )
            )
        self.stop_event = asyncio.Event()

    async def close(self) -> None:
        await asyncio.gather(*(session.close() for session in self.account_sessions.values()))
        await self.market_client.close()

    async def _prepare(self) -> None:
        await asyncio.gather(
            *(pair.prepare(self.config.settings.market_index) for pair in self.account_pairs)
        )

    async def fetch_market_snapshot(self) -> MarketSnapshot:
        order_book = await self.order_api.order_book_orders(
            market_id=self.config.settings.market_index,
            limit=max(1, self.config.settings.order_book_depth),
        )
        if not order_book.bids or not order_book.asks:
            raise RuntimeError("Order book is empty; cannot determine prices")

        def parse_price(raw_price: str) -> int:
            return int(raw_price.replace(".", ""))

        best_bid = parse_price(order_book.bids[0].price)
        best_ask = parse_price(order_book.asks[0].price)
        return MarketSnapshot(best_bid=best_bid, best_ask=best_ask)

    def _register_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.stop_event.set)
            except NotImplementedError:
                # Windows does not support loop signal handlers.
                pass

    async def _sleep_with_stop(self, delay: float) -> None:
        try:
            await asyncio.wait_for(self.stop_event.wait(), timeout=max(0, delay))
        except asyncio.TimeoutError:
            pass

    async def run(self) -> None:
        self._register_signal_handlers()
        await self._prepare()
        self.logger.info(
            "Starting delta-neutral volume bot with %d account pairs", len(self.account_pairs)
        )

        cycle = 0
        try:
            while not self.stop_event.is_set():
                cycle += 1
                try:
                    snapshot = await self.fetch_market_snapshot()
                except Exception as exc:  # noqa: BLE001 - logged and retried
                    self.logger.exception("Failed to fetch market snapshot: %s", exc)
                    await self._sleep_with_stop(self.config.settings.trade_interval_seconds)
                    continue

                self.logger.debug(
                    "Cycle %d | best_bid=%s best_ask=%s spread=%s",
                    cycle,
                    snapshot.best_bid,
                    snapshot.best_ask,
                    snapshot.spread,
                )

                reports = await asyncio.gather(
                    *(
                        pair.execute_trade_cycle(
                            market_index=self.config.settings.market_index,
                            snapshot=snapshot,
                            price_fetcher=self.fetch_market_snapshot,
                        )
                        for pair in self.account_pairs
                    ),
                    return_exceptions=True,
                )

                for pair, report in zip(self.account_pairs, reports):
                    if isinstance(report, Exception):
                        self.logger.exception(
                            "Pair %s execution failed: %s", pair.name, report
                        )
                        continue

                    if report.errors:
                        self.logger.warning(
                            "Pair %s completed with warnings: %s", pair.name, report.errors
                        )
                    pair_stats = self.stats.pair_summary(pair.name)
                    self.logger.info(
                        "Pair %s cumulative -> trades=%d base=%.6f quote=%.2f",
                        pair.name,
                        pair_stats.trades,
                        pair_stats.base_volume,
                        pair_stats.quote_volume,
                    )

                self.logger.info("Global volume summary: %s", self.stats.summary())

                if self.config.settings.once:
                    self.logger.info("Single-cycle mode enabled; stopping bot")
                    break

                await self._sleep_with_stop(self.config.settings.trade_interval_seconds)
        finally:
            await self.close()
            self.logger.info("Bot stopped. Final summary: %s", self.stats.summary())


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Delta neutral volume bot for Lighter")
    default_config = Path(__file__).with_name("delta_neutral_volume_bot.example.yaml")
    parser.add_argument(
        "--config",
        type=Path,
        default=default_config,
        help="Path to the YAML configuration file",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single trading cycle and exit",
    )
    return parser.parse_args()


async def async_main(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    if args.once:
        config.settings.once = True
    setup_logging(config)
    bot = DeltaNeutralVolumeBot(config)
    await bot.run()


def main() -> None:
    args = parse_args()
    try:
        asyncio.run(async_main(args))
    except ConfigError as exc:
        logging.basicConfig(level=logging.ERROR)
        logging.error("Configuration error: %s", exc)
    except KeyboardInterrupt:
        logging.getLogger("lighter.volume_bot").info("Keyboard interrupt received. Exiting...")


if __name__ == "__main__":
    main()
