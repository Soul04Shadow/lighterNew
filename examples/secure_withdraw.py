#!/usr/bin/env python3
"""
Secure Withdraw - withdraw from Lighter L2 to Ethereum L1 (takes ~2 hours)
"""
import asyncio
import lighter

BASE_URL = "https://mainnet.zklighter.elliot.ai"
ACCOUNT_INDEX = 12345                               # Replace with your account index
API_KEY_INDEX = 2                                   # Replace with your API key index    
API_KEY_PRIVATE_KEY = "0xYourApiKeyPrivateKeyHere"  # Replace with your API key private key
AMOUNT_USDC = 5.0                                   # Amount of USDC to withdraw

async def main():
    client = lighter.SignerClient(
        url=BASE_URL,
        private_key=API_KEY_PRIVATE_KEY,
        account_index=ACCOUNT_INDEX,
        api_key_index=API_KEY_INDEX,
    )
    
    try:
        withdraw_info, response, err = await client.withdraw(usdc_amount=AMOUNT_USDC)
        if err:
            raise Exception(f"Withdraw failed: {err}")

        if response.code == 200:
            print(f"Secure withdraw submitted successfully!")
            print(f"Amount: {AMOUNT_USDC} USDC")
            print(f"TX hash: {response.tx_hash}")
            print(f"Note: Withdrawal to Ethereum L1 takes approximately 2 hours")
        else:
            print(f"Error: {response.message}")

    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
