import asyncio
import json
import logging
import time
import eth_account
import lighter

logging.basicConfig(level=logging.DEBUG)

BASE_URL = "https://testnet.zklighter.elliot.ai"
ETH_PRIVATE_KEY = "1234567812345678123456781234567812345678123456781234567812345678"
API_KEY_INDEX = 253


async def setup_account(eth_address, eth_private_key, account_index, base_url, api_key_index):
    private_key, public_key, err = lighter.create_api_key()
    if err is not None:
        return None, f"Failed to create API key for account {account_index}: {err}"

    tx_client = lighter.SignerClient(
        url=base_url,
        private_key=private_key,
        account_index=account_index,
        api_key_index=api_key_index,
    )

    response, err = await tx_client.change_api_key(
        eth_private_key=eth_private_key,
        new_pubkey=public_key,
    )
    if err is not None:
        await tx_client.close()
        return None, f"Failed to change API key for account {account_index}: {err}"

    time.sleep(5)

    err = tx_client.check_client()
    if err is not None:
        await tx_client.close()
        return None, f"Failed to verify API key for account {account_index}: {err}"

    await tx_client.close()

    return {
        "api_key_private_key": private_key,
        "account_index": account_index,
        "api_key_index": api_key_index,
    }, None


async def main():
    api_client = lighter.ApiClient(configuration=lighter.Configuration(host=BASE_URL))
    eth_acc = eth_account.Account.from_key(ETH_PRIVATE_KEY)
    eth_address = eth_acc.address

    try:
        response = await lighter.AccountApi(api_client).accounts_by_l1_address(
            l1_address=eth_address
        )
    except lighter.ApiException as e:
        if e.data.message == "account not found":
            print(f"error: account not found for {eth_address}", file=__import__('sys').stderr)
            await api_client.close()
            return
        else:
            await api_client.close()
            raise e

    if len(response.sub_accounts) == 0:
        print(f"error: no accounts found for {eth_address}", file=__import__('sys').stderr)
        await api_client.close()
        return

    logging.info(f"Found {len(response.sub_accounts)} account(s)")

    tasks = []
    for sub_account in response.sub_accounts:
        logging.info(f"Setting up account index: {sub_account.index}")
        tasks.append(
            setup_account(
                eth_address,
                ETH_PRIVATE_KEY,
                sub_account.index,
                BASE_URL,
                API_KEY_INDEX,
            )
        )

    results = await asyncio.gather(*tasks, return_exceptions=True)

    accounts = []
    errors = []
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            errors.append(f"Account {response.sub_accounts[i].index}: {str(result)}")
        elif result[1] is not None:
            errors.append(result[1])
        else:
            accounts.append(result[0])

    if errors:
        for error in errors:
            logging.error(error)

    if not accounts:
        print("error: failed to setup any accounts", file=__import__('sys').stderr)
        await api_client.close()
        return

    output = {
        "BASE_URL": BASE_URL,
        "ACCOUNTS": accounts,
    }

    print(json.dumps(output, indent=2))

    await api_client.close()


if __name__ == "__main__":
    asyncio.run(main())
