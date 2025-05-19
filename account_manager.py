import json
import logging
import time
from datetime import datetime

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("account_manager.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

class AccountManager:
    def __init__(self):
        self.active_accounts = []
        self.all_accounts = []
        self.load_accounts()

    def load_accounts(self):
        """Load accounts from both JSON files"""
        # Load active accounts
        try:
            with open('accounts.json', 'r') as f:
                self.active_accounts = json.load(f)
            logger.info(f"Successfully loaded {len(self.active_accounts)} active accounts from accounts.json")
        except FileNotFoundError:
            logger.error("accounts.json file not found")
            self.active_accounts = []
        except json.JSONDecodeError:
            logger.error("Error parsing accounts.json file")
            self.active_accounts = []
        
        # Load all accounts
        try:
            with open('allaccounts.json', 'r') as f:
                self.all_accounts = json.load(f)
            logger.info(f"Successfully loaded {len(self.all_accounts)} total accounts from allaccounts.json")
        except FileNotFoundError:
            logger.error("allaccounts.json file not found, will create from active accounts")
            self.all_accounts = self.active_accounts.copy()
            self.save_all_accounts()
        except json.JSONDecodeError:
            logger.error("Error parsing allaccounts.json file")
            self.all_accounts = self.active_accounts.copy()
            self.save_all_accounts()

    def save_active_accounts(self):
        """Save active accounts to accounts.json"""
        try:
            with open('accounts.json', 'w') as f:
                json.dump(self.active_accounts, f, indent=4)
            logger.info(f"Successfully saved {len(self.active_accounts)} accounts to accounts.json")
            return True
        except Exception as e:
            logger.error(f"Error saving active accounts: {str(e)}")
            return False

    def save_all_accounts(self):
        """Save all accounts to allaccounts.json"""
        try:
            with open('allaccounts.json', 'w') as f:
                json.dump(self.all_accounts, f, indent=4)
            logger.info(f"Successfully saved {len(self.all_accounts)} accounts to allaccounts.json")
            return True
        except Exception as e:
            logger.error(f"Error saving all accounts: {str(e)}")
            return False

    def get_all_accounts(self):
        """Get all accounts"""
        return self.all_accounts

    def get_active_accounts(self):
        """Get active accounts"""
        return self.active_accounts

    def get_account(self, client_id):
        """Get account by client ID"""
        for account in self.all_accounts:
            if account['client_id'] == client_id:
                return account
        return None

    def is_account_active(self, client_id):
        """Check if an account is active"""
        for account in self.active_accounts:
            if account['client_id'] == client_id:
                return True
        return False

    def add_account(self, client_id, api_key, password, totp_key, username="", is_active=True):
        """Add a new account"""
        # Check if account already exists
        for account in self.all_accounts:
            if account['client_id'] == client_id:
                logger.error(f"Account with client ID {client_id} already exists")
                return False

        # Create new account
        new_account = {
            "client_id": client_id,
            "api_key": api_key,
            "password": password,
            "totp_key": totp_key,
            "username": username
        }

        # Add to all accounts
        self.all_accounts.append(new_account)
        result_all = self.save_all_accounts()
        
        # If active, also add to active accounts
        if is_active:
            self.active_accounts.append(new_account)
            result_active = self.save_active_accounts()
            return result_all and result_active
        
        return result_all

    def update_account(self, client_id, api_key, password, totp_key, username="", is_active=None):
        """Update an existing account"""
        was_active = self.is_account_active(client_id)
        
        # Update in all accounts
        for i, account in enumerate(self.all_accounts):
            if account['client_id'] == client_id:
                self.all_accounts[i] = {
                    "client_id": client_id,
                    "api_key": api_key,
                    "password": password,
                    "totp_key": totp_key,
                    "username": username
                }
                result_all = self.save_all_accounts()
                
                # Handle active status change if specified
                if is_active is not None:
                    if is_active and not was_active:
                        # Add to active accounts
                        self.active_accounts.append(self.all_accounts[i])
                        result_active = self.save_active_accounts()
                    elif not is_active and was_active:
                        # Remove from active accounts
                        self.active_accounts = [acc for acc in self.active_accounts if acc['client_id'] != client_id]
                        result_active = self.save_active_accounts()
                    else:
                        # Status didn't change, just update in active accounts if it was already active
                        if was_active:
                            for j, active_acc in enumerate(self.active_accounts):
                                if active_acc['client_id'] == client_id:
                                    self.active_accounts[j] = self.all_accounts[i]
                                    break
                            result_active = self.save_active_accounts()
                        else:
                            result_active = True
                else:
                    # No status change specified, update active account if it exists
                    if was_active:
                        for j, active_acc in enumerate(self.active_accounts):
                            if active_acc['client_id'] == client_id:
                                self.active_accounts[j] = self.all_accounts[i]
                                break
                        result_active = self.save_active_accounts()
                    else:
                        result_active = True
                
                return result_all and result_active
        
        logger.error(f"Account with client ID {client_id} not found")
        return False

    def delete_account(self, client_id):
        """Delete an account by client ID"""
        # Remove from all accounts
        self.all_accounts = [acc for acc in self.all_accounts if acc['client_id'] != client_id]
        result_all = self.save_all_accounts()
        
        # Remove from active accounts if present
        self.active_accounts = [acc for acc in self.active_accounts if acc['client_id'] != client_id]
        result_active = self.save_active_accounts()
        
        return result_all and result_active

    def toggle_account_status(self, client_id):
        """Toggle the active status of an account"""
        account = self.get_account(client_id)
        if not account:
            logger.error(f"Account with client ID {client_id} not found")
            return False
        
        is_active = self.is_account_active(client_id)
        
        if is_active:
            # Remove from active accounts
            self.active_accounts = [acc for acc in self.active_accounts if acc['client_id'] != client_id]
            return self.save_active_accounts()
        else:
            # Add to active accounts
            self.active_accounts.append(account)
            return self.save_active_accounts()