import json
import time
import logging
import os
import threading
from datetime import datetime
from flask import Flask, request, jsonify, render_template, redirect, url_for, flash, session
import pyotp
from SmartApi import SmartConnect
import pandas as pd
from options_module import options_processor
from trade_monitor_service import trade_monitor_service as TradeMonitorService
from account_manager import AccountManager

from angel_websocket_manager import websocket_manager


# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("trading_app.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.urandom(24)  # For session management

# Dictionary to store active client instances
active_clients = {}
account_manager = AccountManager()
# Load configuration from config.json
def load_config():
    try:
        with open('config.json', 'r') as f:
            config = json.load(f)
        logger.info("Successfully loaded configuration from config.json")
        return config
    except FileNotFoundError:
        logger.error("config.json file not found, creating a default one")
        default_config = {
            "webhook_secret": "your_webhook_secret_key",
            "disable_ip_check": False,
            "port": 80,
            "admin_username": "admin",
            "admin_password": "admin"  # Should be changed
        }
        with open('config.json', 'w') as f:
            json.dump(default_config, f, indent=4)
        return default_config
    except json.JSONDecodeError:
        logger.error("Error parsing config.json file")
        return {
            "webhook_secret": "your_webhook_secret_key",
            "disable_ip_check": False,
            "port": 80
        }

# Global config
CONFIG = load_config()

# Load accounts from JSON file
def load_accounts():
    try:
        with open('accounts.json', 'r') as f:
            accounts = json.load(f)
        logger.info(f"Successfully loaded {len(accounts)} accounts from accounts.json")
        return accounts
    except FileNotFoundError:
        logger.error("accounts.json file not found")
        return []
    except json.JSONDecodeError:
        logger.error("Error parsing accounts.json file")
        return []

# Save accounts to JSON file
def save_accounts(accounts):
    try:
        with open('accounts.json', 'w') as f:
            json.dump(accounts, f, indent=4)
        logger.info(f"Successfully saved {len(accounts)} accounts to accounts.json")
        return True
    except Exception as e:
        logger.error(f"Error saving accounts: {str(e)}")
        return False

# Cache for symbol data
symbol_cache = {}

# Load Symbol Tokens
def load_symbol_tokens():
    try:
        if os.path.exists('symbols.json'):
            with open('symbols.json', 'r') as f:
                symbol_data = json.load(f)
            logger.info(f"Successfully loaded symbol data from symbols.json")
            return symbol_data
        else:
            logger.warning("symbols.json not found. Will fetch from API when needed.")
            return {}
    except Exception as e:
        logger.error(f"Error loading symbol data: {str(e)}")
        return {}

# Angle One API client using SmartAPI
class AngleOneClient:
    def __init__(self, api_key, client_id, password, totp_key):
        self.api_key = api_key
        self.client_id = client_id
        self.password = password
        self.totp_key = totp_key
        self.smart_api = SmartConnect(api_key=api_key)
        self.refresh_token = None
        self.jwt_token = None
        self.feed_token = None
        self.session_active = False
        self.last_login_time = None
        self.session_expiry = 24 * 60 * 60  # 24 hours in seconds
        self.profile_data = None
        self.auth_token = self.jwt_token
    def login(self):
        try:
            logger.info(f"Attempting login for client: {self.client_id}")
            
            # Generate TOTP
            totp = pyotp.TOTP(self.totp_key).now()
            
            # Generate session
            data = self.smart_api.generateSession(self.client_id, self.password, totp)
            
            if data['status']:
                self.refresh_token = data['data']['refreshToken']
                self.jwt_token = data['data']['jwtToken']
                self.auth_token = self.jwt_token
                self.feed_token = self.smart_api.getfeedToken()
                self.session_active = True
                self.last_login_time = time.time()
                
                # Fetch user profile to verify session and get available exchanges
                profile = self.smart_api.getProfile(self.refresh_token)
                if profile['status']:
                    self.profile_data = profile['data']
                    logger.info(f"User profile retrieved for {self.client_id}. Available exchanges: {profile['data'].get('exchanges', [])}")
                
                logger.info(f"Login successful for client: {self.client_id}")
                return True
            else:
                logger.error(f"Login failed for client {self.client_id}: {data.get('message', 'Unknown error')}")
                return False
        
        except Exception as e:
            logger.error(f"Exception during login for client {self.client_id}: {str(e)}")
            return False
    
    def refresh_session(self):
        """Refresh the session if it's about to expire"""
        if not self.session_active or not self.last_login_time:
            return self.login()
        
        # Check if session is close to expiry (within 30 minutes)
        elapsed_time = time.time() - self.last_login_time
        if elapsed_time > (self.session_expiry - 1800):  # 1800 seconds = 30 minutes
            logger.info(f"Session for client {self.client_id} is about to expire. Refreshing...")
            return self.login()
        
        return True
    
    def get_funds(self):
        """Get account funds and balance information"""
        if not self.session_active:
            if not self.login():
                logger.error(f"Cannot get funds for {self.client_id}: Not logged in")
                return None
        
        try:
            # Use rmsLimit instead of getRMS as per the AngelOne API documentation
            response = self.smart_api.rmsLimit()
            if isinstance(response, dict) and response.get('status'):
                return response.get('data', {})
            else:
                logger.error(f"Error fetching funds for {self.client_id}: {response}")
                return None
        except Exception as e:
            logger.error(f"Exception getting funds for {self.client_id}: {str(e)}")
            return None
        
    def get_positions(self):
        """Get current positions with PnL for trades that were closed today"""
        if not self.session_active:
            if not self.login():
                logger.error(f"Cannot get positions for {self.client_id}: Not logged in")
                return None
        
        try:
            # Get trade book which contains all executed trades
            response = self.smart_api.tradeBook()
            
            if isinstance(response, dict) and response.get('status'):
                trades = response.get('data', [])
                
                # Get current date in the format that matches the API response
                from datetime import datetime
                today = datetime.now().strftime("%d-%b-%Y")  # Format: "09-May-2025"
                
                # Filter for today's trades
                todays_trades = []
                for trade in trades:
                    # Check if trade contains filltime with today's date
                    # Adjust this condition based on actual API response format
                    if trade.get('filltime') and today in trade.get('filltime', ''):
                        todays_trades.append(trade)
                
                if not todays_trades:
                    logger.info(f"No trades found for today for {self.client_id}")
                    return []
                
                # Group trades by symbol to match buy and sell transactions
                symbol_trades = {}
                for trade in todays_trades:
                    symbol = trade.get('tradingsymbol')
                    if symbol not in symbol_trades:
                        symbol_trades[symbol] = []
                    symbol_trades[symbol].append(trade)
                
                # Calculate PnL for closed positions (where we have both buy and sell)
                positions = []
                
                for symbol, symbol_trades_list in symbol_trades.items():
                    buy_qty = 0
                    buy_value = 0
                    sell_qty = 0
                    sell_value = 0
                    
                    # Sum up buys and sells
                    for trade in symbol_trades_list:
                        qty = float(trade.get('fillsize', 0))
                        price = float(trade.get('fillprice', 0))
                        value = qty * price
                        
                        if trade.get('transactiontype') == 'BUY':
                            buy_qty += qty
                            buy_value += value
                        elif trade.get('transactiontype') == 'SELL':
                            sell_qty += qty
                            sell_value += value
                    
                    # Check if position is closed (equal buy and sell quantities)
                    min_qty = min(buy_qty, sell_qty)
                    if min_qty > 0:
                        # Calculate PnL for the closed portion
                        avg_buy_price = buy_value / buy_qty if buy_qty > 0 else 0
                        avg_sell_price = sell_value / sell_qty if sell_qty > 0 else 0
                        closed_pnl = (avg_sell_price - avg_buy_price) * min_qty
                        
                        # Format as a position object with the calculated PnL
                        position = {
                            'tradingsymbol': symbol,
                            'exchange': symbol_trades_list[0].get('exchange', ''),
                            'producttype': symbol_trades_list[0].get('producttype', ''),
                            'netqty': buy_qty - sell_qty,  # Net position
                            'closed_qty': min_qty,         # Quantity that was closed
                            'avgbuysprice': round(avg_buy_price, 2),
                            'avgsellprice': round(avg_sell_price, 2),
                            'pnl': round(closed_pnl, 2)    # PnL for closed portion
                        }
                        positions.append(position)
                
                # Sort by PnL (highest first)
                positions.sort(key=lambda x: x['pnl'], reverse=True)
                
                return positions
            else:
                logger.error(f"Error fetching trade book for {self.client_id}: {response}")
                return None
        
        except Exception as e:
            logger.error(f"Exception getting positions for {self.client_id}: {str(e)}")
            return None

    def get_holdings(self):
        """Get current holdings (delivery positions)"""
        if not self.session_active:
            if not self.login():
                logger.error(f"Cannot get holdings for {self.client_id}: Not logged in")
                return None
        
        try:
            # Use the Trade Book API endpoint as per documentation
            response = self.smart_api.tradeBook()  # Changed from getAllHolding to tradeBook
            if isinstance(response, dict) and response.get('status'):
                holdings_data = response.get('data', [])
                # Filter for delivery positions or process as needed
                delivery_holdings = [trade for trade in holdings_data if trade.get('producttype') == 'DELIVERY']
                return delivery_holdings
            else:
                logger.error(f"Error fetching holdings for {self.client_id}: {response}")
                return None
        except Exception as e:
            logger.error(f"Exception getting holdings for {self.client_id}: {str(e)}")
            return None
    
    def get_order_book(self):
        """Get order book"""
        if not self.session_active:
            if not self.login():
                logger.error(f"Cannot get order book for {self.client_id}: Not logged in")
                return None
        
        try:
            response = self.smart_api.orderBook()
            if isinstance(response, dict) and response.get('status'):
                return response.get('data', [])
            else:
                logger.error(f"Error fetching order book for {self.client_id}: {response}")
                return None
        except Exception as e:
            logger.error(f"Exception getting order book for {self.client_id}: {str(e)}")
            return None
    
    def get_trade_book(self):
        """Get trade book"""
        if not self.session_active:
            if not self.login():
                logger.error(f"Cannot get trade book for {self.client_id}: Not logged in")
                return None
        
        try:
            response = self.smart_api.tradeBook()
            if isinstance(response, dict) and response.get('status'):
                return response.get('data', [])
            else:
                logger.error(f"Error fetching trade book for {self.client_id}: {response}")
                return None
        except Exception as e:
            logger.error(f"Exception getting trade book for {self.client_id}: {str(e)}")
            return None
    
    def cancel_order(self, order_id):
        """Cancel an order"""
        if not self.session_active:
            if not self.login():
                logger.error(f"Cannot cancel order for {self.client_id}: Not logged in")
                return False
        
        try:
            response = self.smart_api.cancelOrder(order_id=order_id, variety="NORMAL")
            if isinstance(response, dict) and response.get('status'):
                logger.info(f"Order {order_id} cancelled successfully for {self.client_id}")
                return True
            else:
                logger.error(f"Error cancelling order {order_id} for {self.client_id}: {response}")
                return False
        except Exception as e:
            logger.error(f"Exception cancelling order {order_id} for {self.client_id}: {str(e)}")
            return False
    
    def place_order(self, order_params):
        if not self.session_active:
            if not self.login():
                logger.error(f"Cannot place order for {self.client_id}: Not logged in")
                return False, None
        
        try:
            # Log the order parameters for debugging
            logger.info(f"Placing order for client {self.client_id} with params: {order_params}")
            
            # Check the order variety
            is_bracket_order = order_params.get('variety') == 'ROBO'
            
            # Place order using the SmartAPI
            response = None
            try:
                response = self.smart_api.placeOrder(order_params)
                logger.info(f"Raw API response: {response}")
            except Exception as api_error:
                # Special handling for empty responses with bracket orders
                error_str = str(api_error)
                if is_bracket_order and "Couldn't parse the JSON response" in error_str and "b''" in error_str:
                    logger.warning(f"Empty response received for bracket order. This is sometimes normal for Angel One API.")
                    
                    # Check if the order was actually placed despite the error
                    try:
                        # Wait a moment for the order to be processed
                        time.sleep(2)
                        
                        # Attempt to get orders to verify if our order went through
                        orders = self.smart_api.orderBook()
                        if isinstance(orders, dict) and orders.get('status') and orders.get('data'):
                            # Look for recent orders with matching symbol
                            for order in orders['data']:
                                if (order.get('tradingsymbol') == order_params['tradingsymbol'] and 
                                    order.get('transactiontype') == order_params['transactiontype'] and
                                    order.get('producttype') == order_params['producttype']):
                                    
                                    order_id = order.get('orderid')
                                    logger.info(f"Found matching order in orderbook: {order_id}")
                                    return True, order_id
                    except Exception as verify_error:
                        logger.error(f"Error verifying order placement: {str(verify_error)}")
                
                # Re-raise the original exception if we couldn't find a matching order
                raise api_error
            
            # Handle different response types
            if isinstance(response, str):
                # If the response is a string, check if it looks like an order ID
                # Angel One sometimes returns just the order ID as a string
                if response and len(response) > 5 and not response.startswith('{'):
                    logger.info(f"Received order ID as direct string response: {response}")
                    return True, response
                
                # Try to parse the response as JSON if it looks like JSON
                logger.warning(f"Response from API is a string, attempting to parse as JSON: {response}")
                try:
                    response = json.loads(response)
                except json.JSONDecodeError:
                    logger.error(f"Failed to parse response as JSON: {response}")
                    # Check if response might be an empty string or error message
                    if not response:
                        return False, None
                    return False, None
            
            # Now handle the response as a dictionary
            if isinstance(response, dict):
                if response.get('status', False):
                    # Extract order ID safely
                    if isinstance(response.get('data'), dict) and 'orderid' in response['data']:
                        order_id = response['data']['orderid']
                        logger.info(f"Order placed successfully for client {self.client_id}: {order_id}")
                        return True, order_id
                    else:
                        logger.error(f"Unexpected response format, 'orderid' not found: {response}")
                        return False, None
                else:
                    error_message = response.get('message', 'Unknown error')
                    error_code = response.get('errorcode', 'Unknown')
                    logger.error(f"Order placement failed for client {self.client_id}: {error_message} (Code: {error_code})")
                    return False, None
            else:
                logger.error(f"Unexpected response type for client {self.client_id}: {type(response)}")
                return False, None
            
        except Exception as e:
            logger.error(f"Exception during order placement for client {self.client_id}: {str(e)}")
            return False, None
    
    def search_symbols(self, exchange, symbol_query):
        """Search for symbols in a specific exchange"""
        if not self.session_active:
            if not self.login():
                logger.error(f"Cannot search symbols for {self.client_id}: Not logged in")
                return []
        
        try:
            response = self.smart_api.searchScrip(exchange=exchange, searchtext=symbol_query)
            if isinstance(response, dict) and response.get('status'):
                return response.get('data', [])
            else:
                logger.error(f"Error searching symbols for {self.client_id}: {response}")
                return []
        except Exception as e:
            logger.error(f"Exception searching symbols for {self.client_id}: {str(e)}")
            return []

def initialize_clients():
    accounts = account_manager.get_active_accounts() 
    
    if not accounts:
        logger.warning("No accounts found in accounts.json. Server starting without active clients.")
        return
    
    logger.info(f"Initializing {len(accounts)} client connections")
    
    for account in accounts:
        try:
            client = AngleOneClient(
                api_key=account["api_key"],
                client_id=account["client_id"],
                password=account["password"],
                totp_key=account["totp_key"]
            )
            
            if client.login():
                active_clients[account["client_id"]] = client
                logger.info(f"Client {account['client_id']} initialized successfully")
            else:
                logger.error(f"Failed to initialize client {account['client_id']}")
        except Exception as e:
            logger.error(f"Error initializing client {account['client_id']}: {str(e)}")
    
    # Initialize services with active clients
    options_processor.initialize(active_clients)
    
    # Initialize WebSocket with the first active client
    if active_clients:
        try:
            first_client = next(iter(active_clients.values()))
            
            # Make sure we have all required authentication parameters
            if (hasattr(first_client, 'auth_token') and first_client.auth_token and
                hasattr(first_client, 'api_key') and first_client.api_key and
                hasattr(first_client, 'client_id') and first_client.client_id and
                hasattr(first_client, 'feed_token') and first_client.feed_token):
                
                # Initialize WebSocket manager with credentials
                logger.info(f"Initializing WebSocket with client {first_client.client_id}")
                websocket_manager.initialize(
                    auth_token=first_client.auth_token,
                    api_key=first_client.api_key,
                    client_code=first_client.client_id,
                    feed_token=first_client.feed_token
                )
                
                # Connect to WebSockets
                websocket_manager.connect()
                logger.info("WebSocket connections initialized")
            else:
                logger.error("Cannot initialize WebSocket: Missing authentication parameters")
                # Log what's missing for debugging
                logger.error(f"Auth token: {hasattr(first_client, 'auth_token') and bool(first_client.auth_token)}")
                logger.error(f"API key: {hasattr(first_client, 'api_key') and bool(first_client.api_key)}")
                logger.error(f"Client code: {hasattr(first_client, 'client_id') and bool(first_client.client_id)}")
                logger.error(f"Feed token: {hasattr(first_client, 'feed_token') and bool(first_client.feed_token)}")
        except Exception as e:
            logger.error(f"Error initializing WebSocket: {str(e)}")
    
    # Initialize the trade monitor service after WebSocket setup
    TradeMonitorService.initialize(active_clients, options_processor, websocket_manager)
    TradeMonitorService.start_monitoring()
    
    logger.info(f"Client initialization complete. {len(active_clients)}/{len(accounts)} clients active")





def session_refresh_task():
    while True:
        try:
            logger.info("Performing periodic session refresh for all clients")
            
            # First refresh client sessions
            for client_id, client in active_clients.items():
                try:
                    if client.refresh_session():
                        logger.info(f"Session refreshed successfully for client: {client_id}")
                    else:
                        logger.error(f"Failed to refresh session for client: {client_id}")
                except Exception as e:
                    logger.error(f"Error refreshing session for client {client_id}: {str(e)}")
            
            # Check if WebSocket is connected, reconnect if needed
            if not websocket_manager.is_connected():
                logger.warning("WebSocket connection lost, attempting to reconnect")
                if active_clients:
                    try:
                        first_client = next(iter(active_clients.values()))
                        
                        # Verify we have all required authentication parameters
                        if (hasattr(first_client, 'auth_token') and first_client.auth_token and
                            hasattr(first_client, 'api_key') and first_client.api_key and
                            hasattr(first_client, 'client_id') and first_client.client_id and
                            hasattr(first_client, 'feed_token') and first_client.feed_token):
                            
                            # Reinitialize WebSocket
                            websocket_manager.initialize(
                                auth_token=first_client.auth_token,
                                api_key=first_client.api_key,
                                client_code=first_client.client_id,
                                feed_token=first_client.feed_token
                            )
                            websocket_manager.connect()
                            logger.info("WebSocket connection reestablished")
                        else:
                            logger.error("Cannot reconnect WebSocket: Missing authentication parameters")
                    except Exception as e:
                        logger.error(f"Error reconnecting WebSocket: {str(e)}")

            # Check for externally exited trades
            TradeMonitorService.check_for_external_changes()                    
        except Exception as e:
            logger.error(f"Error in session refresh task: {str(e)}")
        
        # Sleep for 30 minutes before next refresh check
        time.sleep(30 * 60)

# Process webhook data and place orders for all accounts
def process_trading_signal(webhook_data):
    # Validate the webhook data
    required_fields = ["action", "symbol", "exchange", "product_type", "order_type", "quantity"]
    for field in required_fields:
        if field not in webhook_data:
            logger.error(f"Missing required field in webhook data: {field}")
            return False
    
    # Check if we have active clients
    if not active_clients:
        logger.error("No active clients available to process orders")
        
        # Try to initialize clients if they're not already active
        initialize_clients()
        
        if not active_clients:
            logger.error("Failed to initialize any clients")
            return False
    
    # Map TradingView actions to Angle One transaction types
    action_map = {
        "BUY": "BUY",
        "SELL": "SELL",
        "buy": "BUY",
        "sell": "SELL"
    }
    
    # Check if this is a bracket order
    is_bracket_order = False
    if (webhook_data.get("take_profit") and webhook_data.get("stop_loss")) or \
       (webhook_data.get("takeProfit") and webhook_data.get("stopLoss")) or \
       (webhook_data.get("target_price") and webhook_data.get("stoploss_price")):
        is_bracket_order = True
        logger.info("Detected target and stop loss parameters, processing as bracket order")
    
    # Extract target and stop loss values, supporting multiple naming conventions
    target_price = webhook_data.get("target_price") or webhook_data.get("takeProfit") or webhook_data.get("take_profit") or "0"
    stoploss_price = webhook_data.get("stoploss_price") or webhook_data.get("stopLoss") or webhook_data.get("stop_loss") or "0"
    
    # Convert target and stoploss to absolute price differences for bracket orders
    # Bracket orders require the difference between entry and exit, not absolute prices
    entry_price = float(webhook_data.get("price") or webhook_data.get("entryPrice") or "0")
    
    # Calculate price differences for bracket orders if using absolute prices
    if is_bracket_order and entry_price > 0:
        try:
            target_diff = abs(float(target_price) - entry_price)
            stoploss_diff = abs(entry_price - float(stoploss_price))
            
            # Convert back to strings
            target_price = str(target_diff)
            stoploss_price = str(stoploss_diff)
            
            logger.info(f"Calculated target diff: {target_price}, stoploss diff: {stoploss_price}")
        except (ValueError, TypeError) as e:
            logger.error(f"Error calculating price differences: {str(e)}")
    
    # Set product type for bracket orders
    product_type = webhook_data["product_type"]
    if is_bracket_order:
        # For bracket orders in Angel One, product type must be "BO"
        product_type = "BO"
    
    # Prepare order parameters for SmartAPI
    order_params = {
        "variety": "ROBO" if is_bracket_order else "NORMAL",  # ROBO is used for bracket orders in Angel One
        "tradingsymbol": webhook_data["symbol"],
        "symboltoken": webhook_data.get("symbol_token", ""),  # Symbol token should be provided
        "transactiontype": action_map.get(webhook_data["action"], webhook_data["action"]),
        "exchange": webhook_data["exchange"],
        "ordertype": webhook_data["order_type"],
        "producttype": product_type,
        "duration": webhook_data.get("validity", "DAY"),
        "price": webhook_data.get("price", "0"),
        "squareoff": target_price,  # Target price
        "stoploss": stoploss_price,  # Stop loss price
        "triggerprice": webhook_data.get("trigger_price", "0"),
        "quantity": str(webhook_data["quantity"])
    }
    
    # For limit orders, ensure price is set
    if webhook_data["order_type"] == "LIMIT" and float(order_params["price"]) <= 0:
        logger.error("Price must be specified for LIMIT orders")
        return False
    
    # For stop loss orders, ensure trigger price is set
    if webhook_data["order_type"] in ["SL", "SL-M", "STOPLOSS_LIMIT", "STOPLOSS_MARKET"] and float(order_params["triggerprice"]) <= 0:
        logger.error("Trigger price must be specified for stop loss orders")
        return False
    
    # For bracket orders, validate parameters
    if is_bracket_order:
        if float(order_params["squareoff"]) <= 0:
            logger.error("Target price must be specified for bracket orders")
            return False
        
        if float(order_params["stoploss"]) <= 0:
            logger.error("Stop loss price must be specified for bracket orders")
            return False
        
        # Bracket orders usually require a LIMIT type
        if order_params["ordertype"] == "MARKET":
            logger.warning("Converting MARKET order to LIMIT for bracket order compatibility")
            order_params["ordertype"] = "LIMIT"
            
            # If price is not set, get current market price or use a reasonable price
            if float(order_params["price"]) <= 0:
                logger.warning("Setting a placeholder price for bracket order")
                # Ideally we would fetch the current market price here
                order_params["price"] = webhook_data.get("entryPrice", "100")
    
    logger.info(f"Processing {webhook_data['action']} signal for {webhook_data['symbol']} across {len(active_clients)} clients")
    
    # Process orders for each active client in parallel
    threads = []
    results = []
    
    def place_order_for_client(client_id, client):
        # First ensure the session is still active
        if not client.session_active:
            if not client.login():
                logger.error(f"Failed to re-authenticate client {client_id} before placing order")
                results.append({
                    "client_id": client_id,
                    "success": False,
                    "order_id": None,
                    "message": "Authentication failed"
                })
                return
        
        # Place the order
        success, order_id = client.place_order(order_params)
        results.append({
            "client_id": client_id,
            "success": success,
            "order_id": order_id if success else None
        })
    
    # Create threads for each client
    for client_id, client in active_clients.items():
        thread = threading.Thread(target=place_order_for_client, args=(client_id, client))
        threads.append(thread)
        thread.start()
    
    # Wait for all threads to complete
    for thread in threads:
        thread.join()
    
    # Log the results
    successful_orders = sum(1 for r in results if r["success"])
    logger.info(f"Order placement complete: {successful_orders}/{len(active_clients)} successful")
    
    # Detailed logging of results
    for result in results:
        if result["success"]:
            logger.info(f"Order placed successfully for client {result['client_id']}: {result['order_id']}")
        else:
            logger.error(f"Order placement failed for client {result['client_id']}")
    
    return successful_orders > 0

# Authentication decorator
def login_required(f):
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    decorated_function.__name__ = f.__name__
    return decorated_function

# Load symbol data if available
symbol_data = load_symbol_tokens()

# Routes for Web UI
@app.route('/')
@login_required
def index():
    return render_template('index.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        
        if username == CONFIG.get('admin_username') and password == CONFIG.get('admin_password'):
            session['logged_in'] = True
            return redirect(url_for('index'))
        else:
            error = 'Invalid credentials. Please try again.'
    
    return render_template('login.html', error=error)

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect(url_for('login'))




@app.route('/accounts')
@login_required
def accounts():
    all_accounts = account_manager.get_all_accounts()
    
    # Add status information and active status
    for account in all_accounts:
        client_id = account['client_id']
        # Check if account is active for trading
        account['is_active'] = account_manager.is_account_active(client_id)
        
        # Check connection status
        if client_id in active_clients and active_clients[client_id].session_active:
            account['status'] = 'Active'
            account['last_login'] = datetime.fromtimestamp(active_clients[client_id].last_login_time).strftime('%Y-%m-%d %H:%M:%S')
        else:
            account['status'] = 'Inactive'
            account['last_login'] = 'Never'
    
    return render_template('accounts.html', accounts=all_accounts)

@app.route('/add_account', methods=['GET', 'POST'])
@login_required
def add_account():
    if request.method == 'POST':
        username = request.form.get('username', '')
        is_active = request.form.get('is_active') == 'on'
        
        if account_manager.add_account(
            client_id=request.form['client_id'],
            api_key=request.form['api_key'],
            password=request.form['password'],
            totp_key=request.form['totp_key'],
            username=username,
            is_active=is_active
        ):
            flash('Account added successfully', 'success')
            
            # Only initialize the new client if it's active
            if is_active:
                try:
                    client = AngleOneClient(
                        api_key=request.form["api_key"],
                        client_id=request.form["client_id"],
                        password=request.form["password"],
                        totp_key=request.form["totp_key"]
                    )
                    
                    if client.login():
                        active_clients[request.form["client_id"]] = client
                        logger.info(f"New client {request.form['client_id']} ({username}) initialized successfully")
                    else:
                        logger.error(f"Failed to initialize new client {request.form['client_id']} ({username})")
                except Exception as e:
                    logger.error(f"Error initializing new client {request.form['client_id']} ({username}): {str(e)}")
        else:
            flash('Failed to save account', 'danger')
        
        return redirect(url_for('accounts'))
    
    return render_template('add_account.html')

@app.route('/edit_account/<client_id>', methods=['GET', 'POST'])
@login_required
def edit_account(client_id):
    account = account_manager.get_account(client_id)
    
    if not account:
        flash('Account not found', 'danger')
        return redirect(url_for('accounts'))
    
    # Add active status
    account['is_active'] = account_manager.is_account_active(client_id)
    
    if request.method == 'POST':
        username = request.form.get('username', '')
        is_active = request.form.get('is_active') == 'on'
        
        # Update account
        if account_manager.update_account(
            client_id=client_id,
            api_key=request.form['api_key'],
            password=request.form['password'],
            totp_key=request.form['totp_key'],
            username=username,
            is_active=is_active
        ):
            flash('Account updated successfully', 'success')
            
            # Update active client
            if client_id in active_clients:
                # Remove old client instance
                del active_clients[client_id]
            
            # Initialize updated client if active
            if is_active:
                try:
                    client = AngleOneClient(
                        api_key=request.form["api_key"],
                        client_id=client_id,
                        password=request.form["password"],
                        totp_key=request.form["totp_key"]
                    )
                    
                    if client.login():
                        active_clients[client_id] = client
                        logger.info(f"Updated client {client_id} ({username}) initialized successfully")
                    else:
                        logger.error(f"Failed to initialize updated client {client_id} ({username})")
                except Exception as e:
                    logger.error(f"Error initializing updated client {client_id} ({username}): {str(e)}")
        else:
            flash('Failed to update account', 'danger')
        
        return redirect(url_for('accounts'))
    
    return render_template('edit_account.html', account=account)

@app.route('/toggle_account/<client_id>', methods=['POST'])
@login_required
def toggle_account(client_id):
    if account_manager.toggle_account_status(client_id):
        is_active = account_manager.is_account_active(client_id)
        account = account_manager.get_account(client_id)
        username = account.get('username', '') if account else ''
        
        flash(f"Account {client_id} is now {'activated' if is_active else 'deactivated'}", 'success')
        
        # If activation status changed to active and not already in active_clients, initialize it
        if is_active and client_id not in active_clients:
            try:
                client = AngleOneClient(
                    api_key=account["api_key"],
                    client_id=client_id,
                    password=account["password"],
                    totp_key=account["totp_key"]
                )
                
                if client.login():
                    active_clients[client_id] = client
                    logger.info(f"Client {client_id} ({username}) activated and initialized successfully")
                else:
                    logger.error(f"Failed to initialize client {client_id} ({username}) after activation")
            except Exception as e:
                logger.error(f"Error initializing client {client_id} ({username}) after activation: {str(e)}")
        
        # If deactivated, remove from active_clients
        elif not is_active and client_id in active_clients:
            del active_clients[client_id]
            logger.info(f"Client {client_id} ({username}) deactivated and removed from active clients")
    else:
        flash(f"Failed to update account status", 'danger')
    
    return redirect(url_for('accounts'))




@app.route('/delete_account/<client_id>', methods=['POST'])
@login_required
def delete_account(client_id):
    if account_manager.delete_account(client_id):
        flash('Account deleted successfully', 'success')
        
        # Remove from active clients
        if client_id in active_clients:
            del active_clients[client_id]
            logger.info(f"Removed client {client_id} from active clients")
    else:
        flash('Failed to delete account', 'danger')
    
    return redirect(url_for('accounts'))





@app.route('/orders')
@login_required
def orders():
    all_orders = []
    
    for client_id, client in active_clients.items():
        orders = client.get_order_book()
        if orders:
            for order in orders:
                order['client_id'] = client_id
                all_orders.append(order)
    
    return render_template('orders.html', orders=all_orders)

@app.route('/api/accounts/status', methods=['GET'])
@login_required
def api_accounts_status():
    status = []
    
    for client_id, client in active_clients.items():
        status.append({
            'client_id': client_id,
            'status': 'Active' if client.session_active else 'Inactive',
            'last_login': datetime.fromtimestamp(client.last_login_time).strftime('%Y-%m-%d %H:%M:%S') if client.last_login_time else "Never"
        })
    
    return jsonify(status)


@app.route('/api/accounts/summary', methods=['GET'])
@login_required
def api_accounts_summary():
    summary = {
        'total_accounts': len(active_clients),
        'total_balance': 0,
        'total_positions': 0,
        'total_pnl': 0
    }
    
    for client_id, client in active_clients.items():
        # Get funds
        funds = client.get_funds()
        if funds:
            summary['total_balance'] += float(funds.get('availablecash', 0))
        
        # Get positions
        positions = client.get_positions()
        if positions:
            summary['total_positions'] += len(positions)
            
            # Sum up day P&L
            for position in positions:
                summary['total_pnl'] += float(position.get('pnl', 0))
    
    return jsonify(summary)

@app.route('/exit_all_positions', methods=['POST'])
@login_required
def exit_all_positions():
    success_count = 0
    fail_count = 0
    
    for client_id, client in active_clients.items():
        positions = client.get_positions()
        if positions:
            for position in positions:
                # Only exit non-zero positions
                if int(position.get('netqty', 0)) != 0:
                    # Determine action based on position direction
                    action = "SELL" if int(position.get('netqty', 0)) > 0 else "BUY"
                    
                    order_params = {
                        "variety": "NORMAL",
                        "tradingsymbol": position.get('tradingsymbol', ''),
                        "symboltoken": position.get('symboltoken', ''),
                        "transactiontype": action,
                        "exchange": position.get('exchange', ''),
                        "ordertype": "MARKET",
                        "producttype": position.get('producttype', 'INTRADAY'),
                        "duration": "DAY",
                        "price": "0",
                        "quantity": str(abs(int(position.get('netqty', 0))))
                    }
                    
                    success, order_id = client.place_order(order_params)
                    if success:
                        success_count += 1
                        logger.info(f"Successfully placed exit order for {position.get('tradingsymbol')} with client {client_id}")
                    else:
                        fail_count += 1
                        logger.error(f"Failed to place exit order for {position.get('tradingsymbol')} with client {client_id}")
    
    if success_count > 0:
        flash(f'Successfully exited {success_count} positions', 'success')
    if fail_count > 0:
        flash(f'Failed to exit {fail_count} positions', 'warning')
    if success_count == 0 and fail_count == 0:
        flash('No positions to exit', 'info')
    
    return redirect(url_for('dashboard'))




@app.route('/dashboard')
@login_required
def dashboard():
    # Collect summary data for dashboard
    summary = {
        'total_accounts': len(active_clients),
        'active_accounts': sum(1 for client in active_clients.values() if client.session_active),
        'total_positions': 0,
        'open_orders': 0,
        'account_balance': 0,
        'day_pnl': 0
    }
    
    account_data = []
    
    for client_id, client in active_clients.items():
        account_info = {
            'client_id': client_id,
            'status': 'Active' if client.session_active else 'Inactive',
            'positions': 0,
            'orders': 0,
            'balance': 0,
            'day_pnl': 0
        }
        
        # Get funds using rmsLimit API
        funds = client.get_funds()
        if funds:
            # Extract available cash from the funds response
            account_info['balance'] = float(funds.get('availablecash', 0))
            summary['account_balance'] += account_info['balance']
        
        # Get orders
        orders = client.get_order_book()
        if orders:
            # Count only open orders
            open_orders = sum(1 for order in orders if order.get('orderstatus') in ['open', 'trigger pending', 'validation pending'])
            account_info['orders'] = open_orders
            summary['open_orders'] += open_orders
        
        account_data.append(account_info)
    
    return render_template('dashboard.html', summary=summary, account_data=account_data)



@app.route('/webhook', methods=['POST'])
def tradingview_webhook():
    # Check if the request is from a valid TradingView IP
    client_ip = request.remote_addr
    if client_ip not in TRADINGVIEW_IPS and not CONFIG.get('disable_ip_check', False):
        logger.warning(f"Unauthorized webhook attempt from IP: {client_ip}")
        return jsonify({"status": "error", "message": "Unauthorized"}), 403
    
    # Get the webhook data
    if request.is_json:
        webhook_data = request.get_json()
    else:
        try:
            # Try to parse text as JSON
            webhook_data = json.loads(request.data.decode('utf-8'))
        except json.JSONDecodeError:
            logger.error("Invalid JSON in webhook data")
            return jsonify({"status": "error", "message": "Invalid JSON data"}), 400
    
    # Validate the webhook key if provided
    if webhook_data.get("webhook_key") != CONFIG.get("webhook_secret"):
        logger.warning("Invalid webhook key")
        return jsonify({"status": "error", "message": "Invalid webhook key"}), 403
    
    # Log the received webhook
    logger.info(f"Received webhook: {json.dumps(webhook_data)}")
    
    # Process the trading signal
    success = process_trading_signal(webhook_data)
    
    if success:
        return jsonify({"status": "success", "message": "Orders processed successfully"}), 200
    else:
        return jsonify({"status": "error", "message": "Failed to process orders"}), 500
    
    
@app.route('/test')
def test():
    return "Server is working!"





@app.route('/options-webhook', methods=['POST'])
def options_webhook():
    # Check if the request is from a valid TradingView IP
    client_ip = request.remote_addr
    if client_ip not in TRADINGVIEW_IPS and not CONFIG.get('disable_ip_check', False):
        logger.warning(f"Unauthorized webhook attempt from IP: {client_ip}")
        return jsonify({"status": "error", "message": "Unauthorized"}), 403
    
    # Get the webhook data
    if request.is_json:
        webhook_data = request.get_json()
    else:
        try:
            # Try to parse text as JSON
            webhook_data = json.loads(request.data.decode('utf-8'))
        except json.JSONDecodeError:
            logger.error("Invalid JSON in webhook data")
            return jsonify({"status": "error", "message": "Invalid JSON data"}), 400
    
    # Validate the webhook key if provided
    if webhook_data.get("webhook_key") != CONFIG.get("webhook_secret"):
        logger.warning("Invalid webhook key")
        return jsonify({"status": "error", "message": "Invalid webhook key"}), 403
    
    # Log the received webhook
    logger.info(f"Received options webhook: {json.dumps(webhook_data)}")
    
    # Filter active clients - only use clients that correspond to active accounts
    active_account_clients = {}
    active_account_ids = [acc['client_id'] for acc in account_manager.get_active_accounts()]
    
    for client_id, client in active_clients.items():
        if client_id in active_account_ids:
            active_account_clients[client_id] = client
    
    logger.info(f"Processing options webhook with {len(active_account_clients)} active clients")
    
    # Process the options trading signal with filtered clients
    success, results = options_processor.process_option_signal(webhook_data, active_account_clients)
    
    if success:
        return jsonify({"status": "success", "message": "Options orders processed successfully", "results": results}), 200
    else:
        return jsonify({"status": "error", "message": "Failed to process options orders", "results": results}), 500



@app.route('/options')
@login_required
def options_view():
    """
    Serve the interactive option chain viewer page.
    This page uses React without a build process.
    """
    # Pass the webhook secret to the template
    webhook_secret = CONFIG.get("webhook_secret", "")
    
    # Prepare common symbols for quick access
    common_symbols = [
        {"symbol": "NIFTY", "name": "Nifty 50", "exchange": "NSE"},
        {"symbol": "BANKNIFTY", "name": "Bank Nifty", "exchange": "NSE"},
        {"symbol": "FINNIFTY", "name": "Financial Services Nifty", "exchange": "NSE"},
        {"symbol": "SENSEX", "name": "BSE Sensex", "exchange": "BSE"}
    ]
    
    # Get active option trades
    active_trades = options_processor.get_active_option_trades()
    
    return render_template('option_chain_viewer.html', 
                          webhook_secret=webhook_secret,
                          common_symbols=common_symbols,
                          active_trades=active_trades)

@app.route('/update-options-settings', methods=['POST'])
@login_required
def update_options_settings():
    try:
        data = request.get_json()
        
        # Update config
        CONFIG["default_option_moneyness"] = data.get("moneyness", "ATM")
        CONFIG["default_expiry_preference"] = data.get("expiry", "weekly")
        CONFIG["default_lot_size"] = int(data.get("lot_size", 1))
        
        # Save config
        with open('config.json', 'w') as f:
            json.dump(CONFIG, f, indent=4)
        
        return jsonify({"status": "success", "message": "Options settings updated successfully"}), 200
    except Exception as e:
        logger.error(f"Error updating options settings: {str(e)}")
        return jsonify({"status": "error", "message": f"Failed to update settings: {str(e)}"}), 500

@app.route('/exit-option/<client_id>/<option_symbol>', methods=['POST'])
@login_required
def exit_option(client_id, option_symbol):
    try:
        trade_key = f"{client_id}_{option_symbol}"
        
        if trade_key not in options_processor.active_option_trades:
            return jsonify({"status": "error", "message": "Option position not found"}), 404
        
        trade = options_processor.active_option_trades[trade_key]
        client = active_clients.get(client_id)
        
        if not client:
            return jsonify({"status": "error", "message": "Client not active"}), 400
        
        # Exit the position
        if options_processor.exit_option_position(client, trade, "MANUAL_EXIT"):
            # Remove from active trades
            options_processor.active_option_trades.pop(trade_key, None)
            return jsonify({"status": "success", "message": "Option position exited successfully"}), 200
        else:
            return jsonify({"status": "error", "message": "Failed to exit option position"}), 500
    except Exception as e:
        logger.error(f"Error exiting option position: {str(e)}")
        return jsonify({"status": "error", "message": f"Error: {str(e)}"}), 500


# Add these routes to your Flask application

@app.route('/api/search-symbols', methods=['GET'])
@login_required
def api_search_symbols():
    """Search for symbols in Angle One"""
    query = request.args.get('query', '')
    exchange = request.args.get('exchange', 'NSE')
    
    if not query or len(query) < 2:
        return jsonify({"status": "error", "message": "Please provide a search query of at least 2 characters"}), 400
    
    # Get the first active client for API calls
    if not active_clients:
        return jsonify({"status": "error", "message": "No active clients available"}), 500
    
    client = next(iter(active_clients.values()))
    
    try:
        # Search for symbols in the specified exchange
        search_results = client.search_symbols(exchange, query)
        
        # Also search common indices if query matches
        if query.lower() in ['nifty', 'bank', 'fin', 'sensex']:
            indices = [
                {"tradingsymbol": "NIFTY", "name": "Nifty 50", "exchange": "NSE", "token": "26000", "instrumenttype": "Index"},
                {"tradingsymbol": "BANKNIFTY", "name": "Bank Nifty", "exchange": "NSE", "token": "26009", "instrumenttype": "Index"},
                {"tradingsymbol": "FINNIFTY", "name": "Financial Services Nifty", "exchange": "NSE", "token": "26037", "instrumenttype": "Index"}
            ]
            
            # Filter indices based on query
            filtered_indices = [idx for idx in indices if query.lower() in idx["tradingsymbol"].lower() or query.lower() in idx["name"].lower()]
            
            # Add to search results
            search_results = filtered_indices + search_results
        
        return jsonify({"status": "success", "data": search_results})
    
    except Exception as e:
        logger.error(f"Error searching symbols: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/get-expiry-dates', methods=['GET'])
@login_required
def api_get_expiry_dates():
    """Get available expiry dates for a symbol"""
    symbol = request.args.get('symbol', '')
    exchange = request.args.get('exchange', 'NSE')
    
    # Add debug logging
    logger.info(f"Fetching expiry dates for symbol: {symbol}, exchange: {exchange}")
    
    if not symbol:
        logger.error("No symbol provided for expiry dates")
        return jsonify({"status": "error", "message": "Please provide a symbol"}), 400
    
    # For index options, use NFO exchange
    if symbol.upper() in ["NIFTY", "BANKNIFTY", "FINNIFTY"]:
        exchange = "NFO"
        logger.info(f"Exchange adjusted to NFO for index: {symbol}")
    elif symbol.upper() == "SENSEX":
        exchange = "BFO"
        logger.info(f"Exchange adjusted to BFO for index: {symbol}")
    
    # Get the first active client for API calls
    if not active_clients:
        logger.error("No active clients available for expiry dates API")
        return jsonify({"status": "error", "message": "No active clients available"}), 500
    
    client = next(iter(active_clients.values()))
    logger.info(f"Using client {client.client_id} for expiry dates API")
    
    try:
        # Ensure client is logged in
        if not client.session_active:
            logger.warning(f"Client {client.client_id} session not active, attempting login")
            login_success = client.login()
            if not login_success:
                logger.error(f"Failed to login client {client.client_id}")
                return jsonify({"status": "error", "message": "Client authentication failed"}), 500
        
        # Use the options processor to get expiry dates
        logger.info(f"Calling get_expiry_dates for {symbol} on {exchange}")
        expiry_dates = options_processor.get_expiry_dates(client, symbol, exchange)
        
        if not expiry_dates:
            logger.warning(f"No expiry dates returned for {symbol}")
            
            # Provide hardcoded defaults for common indices if API fails
            if symbol.upper() in ["NIFTY", "BANKNIFTY", "FINNIFTY"]:
                # Get current date and generate likely expiry dates
                from datetime import datetime, timedelta
                
                current_date = datetime.now()
                # Create weekly Thursday expiry for next 4 weeks
                default_expiries = []
                
                # Find next Thursday
                days_until_thursday = (3 - current_date.weekday()) % 7
                if days_until_thursday == 0 and current_date.hour >= 15:  # After market close
                    days_until_thursday = 7
                
                next_thursday = current_date + timedelta(days=days_until_thursday)
                
                for i in range(4):  # Next 4 weekly expiries
                    expiry_date = next_thursday + timedelta(days=i*7)
                    expiry_str = expiry_date.strftime("%d%b%Y").upper()
                    default_expiries.append(expiry_str)
                
                logger.info(f"Using default expiry dates for {symbol}: {default_expiries}")
                return jsonify({"status": "success", "data": default_expiries})
            
            return jsonify({"status": "error", "message": "No expiry dates available for this symbol"}), 404
        
        logger.info(f"Retrieved {len(expiry_dates)} expiry dates for {symbol}")
        return jsonify({"status": "success", "data": expiry_dates})
    
    except Exception as e:
        logger.error(f"Error getting expiry dates: {str(e)}")
        # Return a more detailed error
        error_details = {
            "status": "error", 
            "message": f"Error fetching expiry dates: {str(e)}",
            "details": {
                "symbol": symbol,
                "exchange": exchange,
                "client_active": client.session_active if hasattr(client, 'session_active') else False
            }
        }
        return jsonify(error_details), 500
    
@app.route('/api/get-current-price', methods=['GET'])
@login_required
def api_get_current_price():
    """Get current price for a symbol"""
    symbol = request.args.get('symbol', '')
    exchange = request.args.get('exchange', 'NSE')
    token = request.args.get('token', '')
    
    if not symbol:
        return jsonify({"status": "error", "message": "Please provide a symbol"}), 400
    
    # Get the first active client for API calls
    if not active_clients:
        return jsonify({"status": "error", "message": "No active clients available"}), 500
    
    client = next(iter(active_clients.values()))
    
    try:
        # For indices
        if symbol.upper() in ["NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"]:
            price = options_processor.get_underlying_price(client, symbol, exchange)
        else:
            # For stocks
            if not token:
                # Try to get token
                token = options_processor.get_symbol_token(symbol, exchange)
            
            if not token:
                return jsonify({"status": "error", "message": "Could not find token for symbol"}), 404
            
            # Get price using ltpData
            response = client.smart_api.ltpData(exchange, symbol, token)
            if response.get('status'):
                price = float(response['data']['ltp'])
            else:
                return jsonify({"status": "error", "message": "Failed to get current price"}), 500
        
        return jsonify({
            "status": "success", 
            "data": {
                "symbol": symbol,
                "exchange": exchange,
                "price": price
            }
        })
    
    except Exception as e:
        logger.error(f"Error getting current price: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/active-option-trades', methods=['GET'])
@login_required
def api_active_option_trades():
    """Get active option trades for AJAX refresh"""
    try:
        # Get active trades from options processor
        active_trades = options_processor.get_active_option_trades()
        return jsonify({"status": "success", "data": active_trades})
    
    except Exception as e:
        logger.error(f"Error getting active option trades: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/download-options-json', methods=['POST'])
@login_required
def download_options_json():
    """Generate and download options JSON file"""
    try:
        # Get form data
        data = request.get_json()
        
        # Create webhook JSON
        webhook_json = {
            "webhook_key": CONFIG.get("webhook_secret", ""),
            "options_mode": True,
            "action": data.get("action"),
            "symbol": data.get("symbol"),
            "exchange": data.get("exchange", "NSE"),
            "quantity": int(data.get("quantity", 1)),
            "option_moneyness": data.get("option_moneyness", "ATM"),
            "expiry_preference": data.get("expiry"),
            "underlying_price": float(data.get("price", 0)),
            "underlying_stop_loss": float(data.get("stop_loss", 0)),
            "underlying_target": float(data.get("target", 0))
        }
        
        # Generate the file
        json_str = json.dumps(webhook_json, indent=2)
        
        # Create response with file download
        response = make_response(json_str)
        response.headers["Content-Disposition"] = f"attachment; filename={data.get('symbol')}_options.json"
        response.headers["Content-Type"] = "application/json"
        
        return response
    
    except Exception as e:
        logger.error(f"Error generating options JSON: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/get-option-chain', methods=['GET'])
@login_required
def api_get_option_chain():
    """Get option chain for a particular symbol and expiry"""
    symbol = request.args.get('symbol', '')
    expiry = request.args.get('expiry', '')
    exchange = request.args.get('exchange', 'NSE')
    
    # Add debug logging
    logger.info(f"Fetching option chain for symbol: {symbol}, expiry: {expiry}, exchange: {exchange}")
    
    if not symbol or not expiry:
        logger.error(f"Missing required parameters - symbol: {symbol}, expiry: {expiry}")
        return jsonify({"status": "error", "message": "Please provide symbol and expiry date"}), 400
    
    # Get the first active client for API calls
    if not active_clients:
        logger.error("No active clients available for option chain API")
        return jsonify({"status": "error", "message": "No active clients available"}), 500
    
    client = next(iter(active_clients.values()))
    logger.info(f"Using client {client.client_id} for option chain API")
    
    try:
        # Ensure client is logged in
        if not client.session_active:
            logger.warning(f"Client {client.client_id} session not active, attempting login")
            login_success = client.login()
            if not login_success:
                logger.error(f"Failed to login client {client.client_id}")
                return jsonify({"status": "error", "message": "Client authentication failed"}), 500
        
        # For index options, use NFO exchange
        if symbol.upper() in ["NIFTY", "BANKNIFTY", "FINNIFTY"]:
            exchange = "NFO"
            logger.info(f"Exchange adjusted to NFO for index: {symbol}")
        elif symbol.upper() == "SENSEX":
            exchange = "BFO"
            logger.info(f"Exchange adjusted to BFO for index: {symbol}")
        
        # Get underlying price
        logger.info(f"Getting underlying price for {symbol}")
        current_price = options_processor.get_underlying_price(
            client, 
            symbol, 
            "NSE" if symbol.upper() not in ["SENSEX"] else "BSE"
        )
        
        if current_price is None:
            logger.error(f"Failed to get current price for {symbol}")
            # For testing, provide a default price for common indices
            if symbol.upper() == "NIFTY":
                current_price = 22000
            elif symbol.upper() == "BANKNIFTY":
                current_price = 48000
            elif symbol.upper() == "FINNIFTY":
                current_price = 21000
            else:
                return jsonify({"status": "error", "message": "Could not get current price for underlying"}), 500
            
            logger.info(f"Using default price for {symbol}: {current_price}")
        
        # Get option chain from options processor
        logger.info(f"Fetching options for {symbol} with expiry {expiry}")
        options_data = options_processor.fetch_options_for_expiry(
            client, symbol, expiry, current_price, exchange
        )
        
        if not options_data or not options_data.get('calls') or not options_data.get('puts'):
            logger.warning(f"No option chain data available for {symbol}/{expiry}")
            
            # For testing/demo purposes, generate a mock option chain for common indices
            if symbol.upper() in ["NIFTY", "BANKNIFTY", "FINNIFTY"]:
                step = 100 if symbol.upper() == "NIFTY" or symbol.upper() == "FINNIFTY" else 500
                
                # Calculate ATM strike (round to nearest step)
                atm_strike = round(current_price / step) * step
                
                # Generate 5 strikes above and below ATM
                strikes = [atm_strike + (i - 5) * step for i in range(11)]
                
                # Create mock calls and puts
                mock_calls = []
                mock_puts = []
                
                for strike in strikes:
                    # Calculate moneyness
                    if abs(strike - current_price) / current_price <= 0.005:
                        moneyness = "ATM"
                    elif (option_type == "CE" and strike < current_price) or (option_type == "PE" and strike > current_price):
                        moneyness = "ITM"
                    else:
                        moneyness = "OTM"
                    
                    # Calculate mock prices (simplified)
                    if strike < current_price:
                        call_price = max(0.01, current_price - strike + 50)
                        put_price = max(0.01, 50)
                    else:
                        call_price = max(0.01, 50)
                        put_price = max(0.01, strike - current_price + 50)
                    
                    # Call option
                    call = {
                        'symbol': f"{symbol}{expiry[:5]}{strike}CE",
                        'token': f"1{str(strike).zfill(5)}1",
                        'expiry': expiry,
                        'strike_price': strike,
                        'option_type': 'CE',
                        'last_price': round(call_price, 2),
                        'moneyness': moneyness
                    }
                    mock_calls.append(call)
                    
                    # Put option
                    put = {
                        'symbol': f"{symbol}{expiry[:5]}{strike}PE",
                        'token': f"1{str(strike).zfill(5)}2",
                        'expiry': expiry,
                        'strike_price': strike,
                        'option_type': 'PE',
                        'last_price': round(put_price, 2),
                        'moneyness': moneyness
                    }
                    mock_puts.append(put)
                
                logger.info(f"Generated mock option chain for {symbol}/{expiry} with {len(mock_calls)} strikes")
                options_data = {
                    'calls': mock_calls,
                    'puts': mock_puts
                }
            else:
                return jsonify({"status": "error", "message": "No option chain available for this symbol and expiry"}), 404
        
        logger.info(f"Retrieved option chain with {len(options_data.get('calls', []))} calls and {len(options_data.get('puts', []))} puts")
        return jsonify({
            "status": "success", 
            "data": {
                "symbol": symbol,
                "expiry": expiry,
                "underlying_price": current_price,
                "calls": options_data.get('calls', []),
                "puts": options_data.get('puts', [])
            }
        })
    
    except Exception as e:
        logger.error(f"Error getting option chain: {str(e)}")
        # Return a more detailed error
        error_details = {
            "status": "error", 
            "message": f"Error fetching option chain: {str(e)}",
            "details": {
                "symbol": symbol,
                "expiry": expiry,
                "exchange": exchange,
                "client_active": client.session_active if hasattr(client, 'session_active') else False
            }
        }
        return jsonify(error_details), 500
# Add this route to your Flask application
# Add these new API routes to your Flask application

@app.route('/api/get-fno-symbols', methods=['GET'])
@login_required
def api_get_fno_symbols():
    """Get list of all F&O symbols"""
    # Get the first active client for API calls
    if not active_clients:
        logger.error("No active clients available for F&O symbols API")
        return jsonify({"status": "error", "message": "No active clients available"}), 500
    
    client = next(iter(active_clients.values()))
    logger.info(f"Using client {client.client_id} for F&O symbols API")
    
    try:
        # Ensure client is logged in
        if not client.session_active:
            logger.warning(f"Client {client.client_id} session not active, attempting login")
            login_success = client.login()
            if not login_success:
                logger.error(f"Failed to login client {client.client_id}")
                return jsonify({"status": "error", "message": "Client authentication failed"}), 500
        
        # Get F&O symbols
        fno_symbols = options_processor.get_fno_symbols(client)
        
        return jsonify({
            "status": "success",
            "data": fno_symbols
        })
    
    except Exception as e:
        logger.error(f"Error getting F&O symbols: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/search-fno-symbols', methods=['GET'])
@login_required
def api_search_fno_symbols():
    """Search F&O symbols"""
    query = request.args.get('query', '').upper()
    
    # Get the first active client for API calls
    if not active_clients:
        logger.error("No active clients available for symbol search API")
        return jsonify({"status": "error", "message": "No active clients available"}), 500
    
    client = next(iter(active_clients.values()))
    logger.info(f"Using client {client.client_id} for symbol search API")
    
    try:
        # Ensure client is logged in
        if not client.session_active:
            logger.warning(f"Client {client.client_id} session not active, attempting login")
            login_success = client.login()
            if not login_success:
                logger.error(f"Failed to login client {client.client_id}")
                return jsonify({"status": "error", "message": "Client authentication failed"}), 500
        
        # Get F&O symbols
        fno_symbols = options_processor.get_fno_symbols(client)
        
        # Filter symbols based on query
        if query:
            filtered_symbols = [
                symbol for symbol in fno_symbols
                if query in symbol['symbol'].upper() or query in symbol.get('name', '').upper()
            ]
        else:
            filtered_symbols = fno_symbols
        
        return jsonify({
            "status": "success",
            "data": filtered_symbols
        })
    
    except Exception as e:
        logger.error(f"Error searching F&O symbols: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/generate-webhook-template', methods=['POST'])
def generate_webhook_template():
    """Generate a webhook template JSON file with TradingView placeholders."""
    try:
        # Get JSON data from request
        data = request.json
        
        # Extract parameters
        symbol = data.get('symbol')
        expiry = data.get('expiry')
        moneyness = data.get('moneyness', 'ATM')
        depth = data.get('depth', 0)
        
        # Get the new parameters
        ordertype = data.get('ordertype', 'MARKET')
        producttype = data.get('producttype', 'INTRADAY')
        exit_ordertype = data.get('exit_ordertype', 'MARKET')
        
        # Validate required fields
        if not symbol or not expiry:
            return jsonify({"status": "error", "message": "Symbol and expiry are required"}), 400
        
        # Create template with TradingView variables
        template = {
            "webhook_key": app.config['WEBHOOK_SECRET'],
            "options_mode": True,
            "action": "{{strategy.order.action}}",
            "symbol": symbol,
            "exchange": "NSE",
            "quantity": "{{strategy.order.contracts}}",
            "option_moneyness": moneyness,
            "moneyness_depth": depth,
            "expiry_preference": expiry,
            "underlying_price": "{{close}}",
            "underlying_stop_loss": "{{strategy.order.stop_price}}",
            "underlying_target": "{{strategy.order.limit_price}}",
            "ordertype": ordertype,
            "producttype": producttype,
            "exit_ordertype": exit_ordertype
        }
        
        # Convert to JSON string
        json_data = json.dumps(template, indent=2)
        
        # Create a response with the file
        response = make_response(json_data)
        response.headers['Content-Type'] = 'application/json'
        response.headers['Content-Disposition'] = f'attachment; filename={symbol}_webhook_template.json'
        
        return response
        
    except Exception as e:
        app.logger.error(f"Error generating webhook template: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/positions')
@login_required
def positions_view():
    """Display active positions with monitoring status"""
    return render_template('positions.html')

@app.route('/api/positions', methods=['GET'])
@login_required
def api_positions():
    """Get active option trades with real-time data"""
    web_data = TradeMonitorService.get_web_data()
    return jsonify(web_data)

@app.route('/api/positions/exit/<trade_id>', methods=['POST'])
@login_required
def api_exit_position(trade_id):
    """Manually exit a position"""
    success, message = TradeMonitorService.manually_exit_trade(trade_id)
    return jsonify({"success": success, "message": message})

@app.route('/api/positions/check-all', methods=['POST'])
@login_required
def api_check_all_positions():
    """Manually trigger a check of all positions"""
    TradeMonitorService.check_all_trades()
    return jsonify({"success": True, "message": "All positions checked"})
    
@app.route('/api/websocket/status', methods=['GET'])
@login_required
def api_websocket_status():
    """Get the status of the WebSocket connections"""
    status = {
        "market_data_connected": websocket_manager.stream_connected,
        "order_status_connected": websocket_manager.order_connected,
        "last_update": TradeMonitorService.web_data.get("last_update")
    }
    return jsonify(status)  

@app.route('/debug/websocket-auth', methods=['GET'])
@login_required
def debug_websocket_auth():
    """Debug endpoint to check WebSocket authentication parameters"""
    auth_info = {}
    
    if active_clients:
        client = next(iter(active_clients.values()))
        auth_info = {
            "client_id": client.client_id,
            "auth_token_available": bool(getattr(client, 'auth_token', None)),
            "api_key_available": bool(getattr(client, 'api_key', None)),
            "feed_token_available": bool(getattr(client, 'feed_token', None)),
            "session_active": client.session_active,
            "last_login_time": datetime.fromtimestamp(client.last_login_time).strftime('%Y-%m-%d %H:%M:%S') if client.last_login_time else None
        }
    
    # Check WebSocket manager status
    ws_status = {
        "initialized": bool(websocket_manager.auth_token and websocket_manager.api_key and 
                         websocket_manager.client_code and websocket_manager.feed_token),
        "stream_connected": websocket_manager.stream_connected,
        "order_connected": websocket_manager.order_connected
    }
    
    return jsonify({
        "auth_info": auth_info,
        "websocket_status": ws_status
    })

# Define the allowed TradingView IPs
TRADINGVIEW_IPS = [
    '52.89.214.238',
    '34.212.75.30',
    '54.218.53.128',
    '52.32.178.7'
]

if __name__ == '__main__':
    # Create required directories
    os.makedirs('templates', exist_ok=True)
    os.makedirs('static', exist_ok=True)
    os.makedirs('static/js', exist_ok=True)
    os.makedirs('static/css', exist_ok=True)
    
    # Initialize all client connections
    initialize_clients()
    options_processor.initialize(active_clients)

    # Start session refresh thread
    refresh_thread = threading.Thread(target=session_refresh_task, daemon=True)
    refresh_thread.start()
    
    try:
        # Start the Flask server
        port = CONFIG.get('port', 5000)
        print(f"Starting server on http://localhost:{port}")
        app.run(host='0.0.0.0', port=port, debug=True)
    except KeyboardInterrupt:
        print("Shutting down...")
    finally:
        # Shutdown the trade monitor service
        TradeMonitorService.shutdown()
        websocket_manager.close()