#!/usr/bin/env python3
"""
script to create an auth token using the Lighter SDK.
"""

import asyncio
import time
import lighter

# ===== CONFIGURATION =====

PRIVATE_KEY = "0xyourapikeyhere" # Replace with your actual private key
API_KEY_INDEX = 2 # Replace with your actual API key index
ACCOUNT_INDEX = 123 # Replace with your actual account index

# Base URL - Change this to switch between testnet and mainnet
# For Testnet use: "https://testnet.zklighter.elliot.ai"
# For Mainnet use: "https://mainnet.zklighter.elliot.ai"
BASE_URL = "https://mainnet.zklighter.elliot.ai"


async def create_auth_token():
    """Create an auth token using the Lighter SDK."""
    
    environment = "Mainnet" if "mainnet" in BASE_URL else "Testnet"
    
    print(f"Creating Auth Token")
    print(f"Environment: {environment}")
    print(f"URL: {BASE_URL}")
    print(f"Account Index: {ACCOUNT_INDEX}")
    print(f"API Key Index: {API_KEY_INDEX}")
    print("-" * 50)
    
    client = None
    try:
        client = lighter.SignerClient(
            url=BASE_URL,
            private_key=PRIVATE_KEY,
            account_index=ACCOUNT_INDEX,
            api_key_index=API_KEY_INDEX,
        )
        
        err = client.check_client()
        if err is not None:
            print(f"Error: Failed to verify client configuration - {err}")
            return
        
        
        auth_token, err = client.create_auth_token_with_expiry(lighter.SignerClient.DEFAULT_10_MIN_AUTH_EXPIRY)
        
        if err is not None:
            print(f"Error: Failed to create auth token - {err}")
            return
        
        
        expiry_time = int(time.time() + 10 * 60)  
        expiry_readable = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(expiry_time))
        
        print(f"Auth Token Successfully Created!")
        print(f"Token: {auth_token}")
        print(f"Expires at: {expiry_readable} (in 10 minutes)")
        
    except Exception as e:
        print(f"Error: {str(e)}")
    finally:
        if client:
            await client.close()


async def main():
    """Main function to create auth token."""
    await create_auth_token()


if __name__ == "__main__":
    asyncio.run(main())
