import asyncio
import logging
import time
import eth_account
import lighter

from typing import Optional

logging.basicConfig(level=logging.DEBUG)

# this is a dummy private key which is registered on Testnet.
# It serves as a good example
ETH_PRIVATE_KEY = "1234567812345678123456781234567812345678123456781234567812345678" 

# API Endpoint URL
BASE_URL = "https://testnet.zklighter.elliot.ai" # Use appropriate URL (testnet/mainnet)
API_KEY_INDEX = 1


async def main():
    # verify that the account exists & fetch sub-accounts
    api_client = lighter.ApiClient(configuration=lighter.Configuration(host=BASE_URL))
    eth_acc = eth_account.Account.from_key(ETH_PRIVATE_KEY)
    eth_address = eth_acc.address

    print(f"Fetching sub-accounts for L1 address: {eth_address}...")
    try:
        response = await lighter.AccountApi(api_client).accounts_by_l1_address(l1_address=eth_address)
    except lighter.ApiException as e:
        if hasattr(e, 'data') and hasattr(e.data, 'message') and "account not found" in str(e.data.message).lower():
            print(f"Error: Account not found for {eth_address}")
        else:
            print(f"An API error occurred while fetching accounts: {e}")
            if hasattr(e, 'status'): print(f"Status: {e.status}")
            if hasattr(e, 'reason'): print(f"Reason: {e.reason}")
            if hasattr(e, 'body'): print(f"Body: {e.body}")
        await api_client.close()
        return 
    except Exception as e:
        print(f"An unexpected error occurred during account fetching: {e}")
        await api_client.close()
        raise e 

    #  Logic for handling subaccounts 
    if not response.sub_accounts:
        print(f"Error: No sub-accounts found for L1 address {eth_address}.")
        await api_client.close()
        return

    account_index: Optional[int] = None
    if len(response.sub_accounts) == 1:
        selected_account = response.sub_accounts[0]
        account_index = getattr(selected_account, 'index', None)
        if account_index is not None:
             print(f"Found single sub-account with index: {account_index}. Using this account.")
        else:
             print("Error: Found single sub-account but could not read its index.")
             await api_client.close()
             return

    else:
        # If multiple subaccounts, prompt the user
        print("\nMultiple sub-accounts found:")
        valid_accounts = []
        for i, sub_acc in enumerate(response.sub_accounts):
            idx = getattr(sub_acc, 'index', None)
            if idx is not None:
                valid_accounts.append(sub_acc)
                print(f"  {len(valid_accounts)}. Index: {idx}")
            else:
                print(f"  - Skipping account at list position {i} (could not read index)")

        if not valid_accounts:
            print("Error: Multiple sub-accounts listed, but none had a readable index.")
            await api_client.close()
            return

        while account_index is None:
            try:
                prompt_message = (f"Enter the number (1-{len(valid_accounts)}) or the exact index "
                                  f"of the account to register the API key for (API Key Index Slot: {API_KEY_INDEX}): ")
                choice = input(prompt_message)
                selected_account = None
                try:
                    choice_num = int(choice)
                    if 1 <= choice_num <= len(valid_accounts):
                        selected_account = valid_accounts[choice_num - 1]
                        print(f"Selected account by number: {choice_num}")
                    else:
                         print(f"Invalid number. Please enter between 1 and {len(valid_accounts)}.")

                except ValueError:
                    # If not a valid number, try matching by index value (as string)
                    matched_by_index = False
                    for sub_acc in valid_accounts:
                         if str(getattr(sub_acc, 'index', '')) == str(choice):
                             selected_account = sub_acc
                             print(f"Selected account by index value: {choice}")
                             matched_by_index = True
                             break
                    if not matched_by_index:
                         print("Invalid input. Please enter a valid list number or account index.")

                if selected_account:
                    account_index = getattr(selected_account, 'index', None)
                    if account_index is None:
                         print("Error: Selected account has no readable index. Please try again.")
                         selected_account = None

            except EOFError:
                print("\nOperation cancelled by user.")
                await api_client.close()
                return
            except Exception as e:
                print(f"An error occurred during input: {e}")

    
    if account_index is None:
        print("Could not determine target account index. Exiting.")
        await api_client.close()
        return

    print(f"\nProceeding with Account Index: {account_index} and API Key Index Slot: {API_KEY_INDEX}")
   


    # create a private/public key pair for the new API key
    print("Generating new API key pair...")
    private_key, public_key, err = lighter.create_api_key()
    if err is not None:
        await api_client.close()
        raise Exception(f"Error creating API key: {err}")
    print("API key pair generated successfully.")

    tx_client = lighter.SignerClient(
        url=BASE_URL,
        private_key=private_key, 
        account_index=account_index,
        api_key_index=API_KEY_INDEX,
    )
    print(f"Initializing client for account {account_index}, API key index {API_KEY_INDEX} with the new private key...")


    
    print(f"Attempting to register the new public key ({public_key[:10]}...) for account index {account_index} at API key index slot {API_KEY_INDEX}...")
    try:
        
        response, err = await tx_client.change_api_key(
            eth_private_key=ETH_PRIVATE_KEY, 
            new_pubkey=public_key,          
        )
        if err is not None:
            error_message = f"Error changing API key: {err}"
            if isinstance(err, lighter.ApiException):
                if hasattr(err, 'status'): error_message += f" (Status: {err.status})"
                if hasattr(err, 'reason'): error_message += f" (Reason: {err.reason})"
                if hasattr(err, 'body'): error_message += f" Body: {err.body}"
            print(error_message)
            await tx_client.close()
            await api_client.close()
            raise Exception(error_message)

        print("Change API key transaction submitted successfully.")
        if hasattr(response, 'tx_hash'):
            print(f"Transaction Hash: {response.tx_hash}") # Print Tx Hash if available

    except Exception as e:
        print(f"An unexpected error occurred during API key change: {e}")
        await tx_client.close()
        await api_client.close()
        raise e


    # Wait for the change to propagate on the server side
    print("Waiting 10 seconds for API key change to propagate...")
    time.sleep(10)

    # Verify that the server now recognizes the new API key using the client configured with it.
    print("Verifying new API key with the server...")
    err = tx_client.check_client()
    if err is not None:
        error_message = f"Error verifying new API key: {err}. The key might not have been registered correctly or propagation takes longer."
        if isinstance(err, lighter.ApiException) and hasattr(err, 'status') and err.status == 401:
             error_message += "\nThis is often due to the server not yet recognizing the new key, or an issue with the key registration transaction."
        elif isinstance(err, Exception): 
             error_message += f" ({type(err).__name__})"

        print(error_message)
        await tx_client.close()
        await api_client.close()
        # Raise an exception to signal failure
        raise Exception(error_message)

    print("Successfully verified the new API key with the server.")

    print("\n--- Setup Complete ---")
    print("Save these details securely. The API_KEY_PRIVATE_KEY is sensitive.")
    print(
        f"""
# == Configuration for Lighter SDK Client ==
BASE_URL = '{BASE_URL}'
API_KEY_PRIVATE_KEY = '{private_key}' 
ACCOUNT_INDEX = {account_index} 
API_KEY_INDEX = {API_KEY_INDEX} 
# =======================================
"""
    )

    # Clean up clients
    await tx_client.close()
    await api_client.close()
    print("Clients closed.")


if __name__ == "__main__":
    asyncio.run(main())