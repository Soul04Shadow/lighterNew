# Read-Only Auth Token Pre-Generation

This example demonstrates how to pre-generate authentication tokens for read-only operations on the Lighter platform. By generating tokens ahead of time, you can avoid needing access to your API private keys during runtime for read-only queries.

## Overview

Authentication tokens on Lighter have a maximum expiry of 8 hours. This example allows you to:

1. Configure a dedicated API key (index 253) for all your accounts
2. Pre-generate authentication tokens for future time periods
3. Use these tokens for read-only operations without exposing your private keys

The tokens are generated at 6-hour intervals (aligned to Unix timestamp // 6 hours), with each token valid for 8 hours. This provides an overlap period ensuring continuous coverage.

## Setup

The setup script configures API key 253 for all accounts associated with your Ethereum private key.

### Running Setup

```bash
cd examples/read-only-auth
python3 setup.py > config.json
```

This will:
- Query all accounts for your L1 address
- Generate new API key pairs for each account
- Change API key 253 to use the new keys
- Output configuration in JSON format

### Configuration Variables

Edit the constants in `setup.py`:

```python
BASE_URL = "https://testnet.zklighter.elliot.ai"
ETH_PRIVATE_KEY = "your_ethereum_private_key_here"
API_KEY_INDEX = 253  # Using 253 as it's typically unused
```

### Output Format

```json
{
  "BASE_URL": "https://testnet.zklighter.elliot.ai",
  "ACCOUNTS": [
    {
      "api_key_private_key": "...",
      "account_index": 0,
      "api_key_index": 253
    },
    {
      "api_key_private_key": "...",
      "account_index": 1,
      "api_key_index": 253
    }
  ]
}
```

## Generating Tokens

The generation script creates authentication tokens for future time periods.

### Running Generation

```bash
python3 generate.py [config_file]
```

If no config file is specified, it defaults to `config.json`.

### Duration Configuration

You can specify the duration in your config file:

```json
{
  "BASE_URL": "https://testnet.zklighter.elliot.ai",
  "DURATION_IN_DAYS": 7,
  "ACCOUNTS": [...]
}
```

Or modify the default in `generate.py`:

```python
DURATION_IN_DAYS = 7  # Generate tokens for 7 days
```

This will generate `4 * DURATION_IN_DAYS` tokens (4 per day, one every 6 hours).

### Output Format

The script generates `auth-tokens.json`:

```json
{
  "0": {
    "1697184000": "auth_token_string_1",
    "1697205600": "auth_token_string_2",
    "1697227200": "auth_token_string_3"
  },
  "1": {
    "1697184000": "auth_token_string_1",
    "1697205600": "auth_token_string_2"
  }
}
```

Where:
- First level key: account index
- Second level key: Unix timestamp (aligned to 6-hour boundaries)
- Value: authentication token

## Usage

### Looking Up Tokens

Use this code to look up the appropriate token for the current time:

```python
import json
import time

# Load pre-generated tokens
with open('auth-tokens.json') as f:
    auth_tokens = json.load(f)

# Get current aligned timestamp (6-hour boundary)
current_timestamp = (int(time.time()) // (6 * 3600)) * (6 * 3600)

# Look up token for specific account
account_index = 0
auth_token = auth_tokens[str(account_index)][str(current_timestamp)]

# Use the token for authentication
# (implementation depends on your API client)
```

### Time Alignment

All timestamps are aligned to 6-hour boundaries:
- Timestamps are divisible by 21600 seconds (6 hours)
- Calculation: `unix_timestamp // (6 * 3600) * (6 * 3600)`
- This ensures consistent token lookup across different systems

### Token Expiry

Each token is valid for 8 hours from its timestamp:
- Token timestamp: aligned to 6-hour boundary
- Valid until: timestamp + 8 hours
- This provides 2 hours of overlap between consecutive tokens

## Security

### API Key 253

We use API key index 253 because:
- It's the last available index (0-255)
- It's not typically used by other applications
- Easy to remember for this specific use case

### Invalidating Tokens

To invalidate all existing tokens:

```bash
python3 setup.py > config.json
```

Re-running the setup script generates new API keys for index 253, which invalidates all previously generated authentication tokens. This is useful if:
- You suspect your tokens have been compromised
- You want to rotate your API keys periodically
- You need to revoke access immediately

### Best Practices

1. **Store tokens securely**: The `auth-tokens.json` file contains sensitive authentication data
2. **Regenerate regularly**: Set up a cron job to regenerate tokens periodically
3. **Monitor usage**: Keep track of which tokens are being used
4. **Separate keys**: Use different API keys for different purposes (253 for read-only)

## Example Workflow

Complete workflow for setting up and using pre-generated tokens:

```bash
# 1. Configure accounts (one-time setup)
cd examples/read-only-auth
python3 setup.py > config.json

# 2. Generate tokens for the next 7 days
python3 generate.py

# 3. Use the tokens in your application
python3 your_app.py  # Uses auth-tokens.json

# 4. Regenerate tokens when needed (e.g., daily cron job)
python3 generate.py
```

## Troubleshooting

### "Account not found" error

Make sure your Ethereum private key corresponds to an account registered on the Lighter platform.

### "Failed to change API key" error

This could happen if:
- The API key change transaction failed
- Network connectivity issues
- The account is not active

### "Token not found for timestamp" error

This means you don't have a token for the current time period. Run:

```bash
python3 generate.py
```

to generate fresh tokens.

## Additional Notes

- Tokens are specific to each account index
- Each account has its own set of time-aligned tokens
- The system uses the SignerClient's native `create_auth_token_with_expiry` method
- No modifications to the core lighter-python SDK are required
