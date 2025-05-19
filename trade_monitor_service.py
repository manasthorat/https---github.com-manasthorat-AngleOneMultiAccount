import json
import os
import time
import logging
import threading
from datetime import datetime, timedelta
import uuid

# Import WebSocket manager
from angel_websocket_manager import websocket_manager, get_exchange_type_id, get_exchange_name

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("trade_monitor_service.log")
    ]
)
logger = logging.getLogger("TradeMonitorService")

class TradeMonitorService:
    """
    Service to monitor options trades and manage their lifecycle.
    This class watches the options_trades.json file and handles
    trade exits based on target/stop loss conditions.
    
    Enhanced with WebSocket support for real-time monitoring.
    """
    
    def __init__(self, 
                 trades_file="option_trades.json", 
                 completed_trades_file="completed_option_trades.json",
                 monitor_interval=60):  # Increased interval as fallback when WebSocket is down
        self.trades_file = trades_file
        self.completed_trades_file = completed_trades_file
        self.active_trades = {}
        self.clients = {}
        self.price_fetcher = None
        self.monitor_interval = monitor_interval  # seconds
        self.last_modified_time = 0
        self.monitor_thread = None
        self.is_monitoring = False
        self.file_lock = threading.Lock()
        self.web_data = {
            "active_trades": [],
            "last_update": None,
            "market_status": "CLOSED",
            "websocket_status": "DISCONNECTED"
        }
        
        # WebSocket support
        self.websocket_manager = None
        self.using_websocket = False
        self.fallback_to_polling = True
        
        # Market data cache (token -> price)
        self.price_cache = {}
        self.price_cache_ttl = 5  # Cache TTL in seconds
    
    def initialize(self, clients, price_fetcher=None, websocket_manager=None):
        """Initialize the monitor service with client connections and price fetcher"""
        self.clients = clients
        self.price_fetcher = price_fetcher
        self.websocket_manager = websocket_manager
        self.load_trades()
        
        # Set up WebSocket event handlers
        if self.websocket_manager:
            self.websocket_manager.on_market_data = self._handle_market_data
            self.websocket_manager.on_order_update = self._handle_order_update
            self.websocket_manager.on_stream_connected = self._handle_stream_connected
            self.websocket_manager.on_order_connected = self._handle_order_connected
            self.websocket_manager.on_stream_disconnected = self._handle_stream_disconnected
            self.websocket_manager.on_order_disconnected = self._handle_order_disconnected
            
            # Check if WebSocket is already connected
            if self.websocket_manager.stream_connected or self.websocket_manager.order_connected:
                self.using_websocket = True
                self.web_data["websocket_status"] = "CONNECTED"
                
                # Subscribe to market data for active trades
                self._subscribe_to_active_trades()
                
                logger.info("Initialized with active WebSocket connection")
        
        logger.info(f"TradeMonitorService initialized with {len(self.active_trades)} active trades")
    
    def load_trades(self):
        """Load trades from the JSON file"""
        try:
            if not os.path.exists(self.trades_file):
                logger.info(f"No trades file found at {self.trades_file}, starting with empty trade list")
                return
            
            # Get file modification time
            file_mod_time = os.path.getmtime(self.trades_file)
            
            # Only reload if file has been modified since last check
            if file_mod_time <= self.last_modified_time:
                return
                
            with self.file_lock:
                with open(self.trades_file, 'r') as f:
                    loaded_trades = json.load(f)
            
            # Convert string datetime back to datetime objects
            for key, trade in loaded_trades.items():
                if isinstance(trade.get("entry_time"), str):
                    try:
                        trade["entry_time"] = datetime.strptime(trade["entry_time"], '%Y-%m-%d %H:%M:%S')
                    except ValueError:
                        # If parsing fails, keep as string
                        pass
            
            # Update active trades
            self.active_trades = loaded_trades
            self.last_modified_time = file_mod_time
            logger.info(f"Successfully loaded {len(loaded_trades)} trades from {self.trades_file}")
            
            # Update web data
            self.update_web_data()
            
            # Subscribe to market data for new trades if using WebSocket
            if self.websocket_manager and self.websocket_manager.stream_connected:
                self._subscribe_to_active_trades()
                
        except Exception as e:
            logger.error(f"Error loading trades from JSON: {str(e)}")
    
    def save_trades(self):
        """Save active trades to the JSON file"""
        try:
            # Convert datetime objects to strings for JSON serialization
            serializable_trades = {}
            for key, trade in self.active_trades.items():
                # Create a copy of the trade to avoid modifying the original
                trade_copy = trade.copy()
                
                # Convert datetime objects to strings
                if isinstance(trade_copy.get("entry_time"), datetime):
                    trade_copy["entry_time"] = trade_copy["entry_time"].strftime('%Y-%m-%d %H:%M:%S')
                
                serializable_trades[key] = trade_copy
            
            # Create directory if it doesn't exist
            os.makedirs(os.path.dirname(os.path.abspath(self.trades_file)), exist_ok=True)
            
            # Write to JSON file with file lock to prevent concurrent writes
            with self.file_lock:
                with open(self.trades_file, 'w') as f:
                    json.dump(serializable_trades, f, indent=2)
            
            # Update the last modified time
            self.last_modified_time = os.path.getmtime(self.trades_file)
            logger.debug(f"Successfully saved {len(serializable_trades)} trades to {self.trades_file}")
            
            # Update web data
            self.update_web_data()
            return True
        except Exception as e:
            logger.error(f"Error saving trades to JSON: {str(e)}")
            return False
    
    def start_monitoring(self):
        """Start the monitoring thread"""
        if self.monitor_thread is None or not self.monitor_thread.is_alive():
            self.is_monitoring = True
            self.monitor_thread = threading.Thread(target=self.monitor_trades_loop, daemon=True)
            self.monitor_thread.start()
            logger.info("Trade monitoring thread started")
            return True
        else:
            logger.warning("Monitoring thread already running")
            return False
    
    def stop_monitoring(self):
        """Stop the monitoring thread"""
        if self.monitor_thread and self.monitor_thread.is_alive():
            self.is_monitoring = False
            self.monitor_thread.join(timeout=30)
            if self.monitor_thread.is_alive():
                logger.warning("Monitoring thread did not terminate within timeout")
                return False
            else:
                logger.info("Monitoring thread stopped")
                return True
        else:
            logger.info("No monitoring thread running")
            return True
    
    def monitor_trades_loop(self):
        """
        Main monitoring loop that checks trades for target/stop loss
        Used as fallback when WebSocket is not available
        """
        logger.info(f"Starting trade monitoring loop with interval of {self.monitor_interval} seconds")
        
        while self.is_monitoring:
            try:
                # Check if it's market hours (Mon-Fri, 9:15 AM - 3:30 PM)
                current_time = datetime.now()
                weekday = current_time.weekday()  # 0-4 for Mon-Fri, 5-6 for Sat-Sun
                market_hours = (
                    weekday < 5 and  # Monday to Friday
                    current_time.time() >= datetime.strptime("09:15:00", "%H:%M:%S").time() and 
                    current_time.time() <= datetime.strptime("15:30:00", "%H:%M:%S").time()
                )
                
                if market_hours:
                    # Update market status
                    self.web_data["market_status"] = "OPEN"
                    
                    # Check if WebSocket is being used
                    if not self.using_websocket or self.fallback_to_polling:
                        # If WebSocket is down or we need backup polling
                        
                        # Reload trades to check for external updates
                        self.load_trades()
                        
                        # Check reconciliation with broker positions
                        self.check_for_external_changes()
                        
                        # Check trades for target/stop loss hits if not using WebSocket
                        # or as a backup verification
                        if self.active_trades and self.clients:
                            self.check_all_trades()
                            logger.info("Completed polling-based trade check")
                    else:
                        # Just do reconciliation when using WebSocket
                        # This ensures trades manually exited outside our system are detected
                        self.check_for_external_changes()
                        logger.debug("Using WebSocket for real-time monitoring, skipping polling-based check")
                else:
                    # Market is closed
                    self.web_data["market_status"] = "CLOSED"
                    logger.debug("Market is closed, skipping trade check")
                
                # Update last check time
                self.web_data["last_update"] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                
                # Sleep to avoid excessive API calls
                time.sleep(self.monitor_interval)
            
            except Exception as e:
                logger.error(f"Error in monitoring loop: {str(e)}")
                time.sleep(5)  # Short sleep on error
    
    def check_all_trades(self):
        """Check all active trades for target/stop loss conditions"""
        if not self.active_trades or not self.clients:
            return
        
        # Get first available client for price checks
        check_client = next(iter(self.clients.values()))
        
        # Group trades by underlying symbol for efficient monitoring
        trades_by_underlying = {}
        for trade_key, trade in list(self.active_trades.items()):
            underlying = trade.get("underlying_symbol")
            if underlying:
                if underlying not in trades_by_underlying:
                    trades_by_underlying[underlying] = []
                trades_by_underlying[underlying].append((trade_key, trade))
        
        # Monitor each underlying asset
        for underlying, trades in trades_by_underlying.items():
            try:
                # Get current price of underlying
                current_price = self.price_fetcher.get_underlying_price(
                    check_client, 
                    underlying, 
                    trades[0][1].get("underlying_exchange", "NSE")
                )
                
                if current_price is None:
                    logger.warning(f"Failed to get current price for {underlying}")
                    continue
                
                # Check each trade against the underlying's price
                for trade_key, trade in trades:
                    self.check_single_trade(trade_key, trade, current_price)
                
            except Exception as e:
                logger.error(f"Error monitoring underlying {underlying}: {str(e)}")
    
    def check_single_trade(self, trade_key, trade, current_price=None):
        """Check a single trade for target/stop loss conditions"""
        try:
                    # Skip trades that are already being processed for exit
            if trade.get("status") == "EXITING":
                logger.debug(f"Trade {trade_key} already being processed for exit, skipping")
                return False
            client_id = trade["client_id"]
            client = self.clients.get(client_id)
            
            if not client:
                logger.warning(f"Client {client_id} not found for trade {trade_key}")
                # Don't remove the trade to avoid data loss
                trade["status"] = "CLIENT_MISSING"
                return False
            
            # Get underlying details
            underlying = trade.get("underlying_symbol")
            
            # If current price wasn't provided, fetch it or check cache
            if current_price is None:
                # Look in price cache first
                cache_key = f"{trade.get('underlying_exchange', 'NSE')}:{underlying}"
                
                if cache_key in self.price_cache:
                    cache_entry = self.price_cache[cache_key]
                    if time.time() - cache_entry['timestamp'] < self.price_cache_ttl:
                        current_price = cache_entry['price']
                        logger.debug(f"Using cached price for {underlying}: {current_price}")
                
                # If not in cache, fetch from price fetcher
                if current_price is None:
                    exchange = trade.get("underlying_exchange", "NSE")
                    current_price = self.price_fetcher.get_underlying_price(client, underlying, exchange)
                    
                    if current_price is None:
                        logger.warning(f"Failed to get current price for {underlying}")
                        return False
                    
                    # Update cache
                    self.price_cache[cache_key] = {
                        'price': current_price,
                        'timestamp': time.time()
                    }
            
            # Get price thresholds
            stop_loss = trade["underlying_stop_loss"]
            target = trade["underlying_target"]
            option_type = trade["option_type"]
            exit_reason = None
            
            # Check if current price has hit target or stop loss
            if option_type == "CE":  # Call option - bullish
                if current_price >= target:
                    exit_reason = "TARGET_HIT"
                    logger.info(f"Target hit for {underlying} at {current_price}, exiting {trade['symbol']}")
                
                elif current_price <= stop_loss:
                    exit_reason = "STOP_LOSS_HIT"
                    logger.info(f"Stop loss hit for {underlying} at {current_price}, exiting {trade['symbol']}")
                    
            else:  # Put option - bearish
                if current_price <= target:
                    exit_reason = "TARGET_HIT"
                    logger.info(f"Target hit for {underlying} at {current_price}, exiting {trade['symbol']}")
                
                elif current_price >= stop_loss:
                    exit_reason = "STOP_LOSS_HIT"
                    logger.info(f"Stop loss hit for {underlying} at {current_price}, exiting {trade['symbol']}")
            
            # Check for expiry - if today is expiry day, exit before market close
            if not exit_reason and trade.get("expiry"):
                try:
                    expiry_date = datetime.strptime(trade["expiry"], '%d%b%Y')
                    today = datetime.now().date()
                    
                    if expiry_date.date() == today:
                        current_time = datetime.now().time()
                        # If after 3:00 PM on expiry day, exit the position
                        if current_time.hour >= 15 and current_time.minute >= 0:
                            exit_reason = "EXPIRY_CLOSING"
                            logger.info(f"Expiry day closing for {trade['symbol']}, exiting position")
                except Exception as e:
                    logger.warning(f"Error checking expiry for {trade['symbol']}: {str(e)}")
            
            # Exit the position if needed
            if exit_reason:


                # Mark the trade as being processed for exit to prevent duplicate orders
                self.active_trades[trade_key]["status"] = "EXITING"
                # Save changes immediately to prevent other processes from also exiting
                self.save_trades()
                
                success = self.exit_option_position(client, trade, exit_reason)
                if success:
                    # Remove from active trades
                    self.active_trades.pop(trade_key, None)
                    # Save changes to JSON after trade exit
                    self.save_trades()
                    
                    # Unsubscribe if using WebSocket
                    if self.websocket_manager and self.websocket_manager.stream_connected:
                        self._unsubscribe_trade(trade)
                        
                    return True
                else:
                    # If exit failed, reset the status
                    self.active_trades[trade_key]["status"] = "ACTIVE"
                    self.save_trades()
            else:
                # Update trade with current price information 
                # (useful for the web interface)
                self.update_trade_status(trade_key, trade, current_price)
            
            return False
                
        except Exception as e:
            logger.error(f"Error checking trade {trade_key}: {str(e)}")
            return False
    
    def update_trade_status(self, trade_key, trade, current_price):
        """Update trade with current market data"""
        try:
            # Only proceed if the trade is still active
            if trade_key not in self.active_trades:
                return
                
            # Get option current price if possible
            current_option_price = None
            client_id = trade["client_id"]
            client = self.clients.get(client_id)
            
            if client:
                try:
                    current_option_price = self.get_option_price(client, trade["symbol"], trade["token"])
                except Exception as e:
                    logger.debug(f"Could not get option price for {trade['symbol']}: {str(e)}")
            
            # Update the trade with current prices
            self.active_trades[trade_key]["current_underlying_price"] = current_price
            
            # Calculate P&L if we have both entry and current prices
            if current_option_price and "entry_price" in trade:
                entry_price = trade["entry_price"]
                self.active_trades[trade_key]["current_option_price"] = current_option_price
                pnl = (current_option_price - entry_price) * trade["quantity"]
                pnl_percent = (current_option_price - entry_price) / entry_price * 100 if entry_price > 0 else 0
                
                self.active_trades[trade_key]["pnl"] = pnl
                self.active_trades[trade_key]["pnl_percent"] = pnl_percent
                self.active_trades[trade_key]["last_updated"] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        except Exception as e:
            logger.error(f"Error updating trade status for {trade_key}: {str(e)}")
    
    def exit_option_position(self, client, trade, reason):
        """Exit an option position"""
        try:
            # Get the order type to use for exit
            exit_ordertype = trade.get("exit_ordertype", "MARKET")
            
            # Prepare order parameters for exiting
            order_params = {
                "variety": "NORMAL",
                "tradingsymbol": trade["symbol"],
                "symboltoken": trade["token"],
                "transactiontype": "SELL",  # Always SELL to exit a long option
                "exchange": "NFO",
                "ordertype": exit_ordertype,  # Use stored exit_ordertype
                "producttype": trade.get("producttype", "INTRADAY"),  # Use stored producttype
                "duration": "DAY",
                "quantity": str(trade["quantity"])
            }
            
            # For limit orders, we need a valid price
            if exit_ordertype == "LIMIT" or exit_ordertype == "SL":
                # Try to get current option price
                current_price = self.get_option_price(client, trade["symbol"], trade["token"])
                if current_price:
                    # For SELL orders in LIMIT type, set price slightly lower for better execution
                    if exit_ordertype == "LIMIT":
                        limit_price = round(current_price * 0.99 / 0.05) * 0.05
                        order_params["price"] = str(limit_price)
                    elif exit_ordertype == "SL":
                        # For SL orders, set trigger slightly higher than current price
                        stop_price = round(current_price * 1.01 / 0.05) * 0.05 
                        order_params["triggerprice"] = str(stop_price)
                        order_params["price"] = str(current_price)
                else:
                    # Fallback if we can't get current price
                    logger.warning(f"Could not determine current price for {trade['symbol']}, using default for exit order")
                    # Set a low limit price to ensure sell execution
                    if exit_ordertype == "LIMIT":
                        order_params["price"] = "0.1"  # Low price to ensure execution
                    elif exit_ordertype == "SL":
                        order_params["triggerprice"] = "1000.0"  # High trigger price
                        order_params["price"] = "0.1"  # Low limit price
            else:
                # For MARKET and SL-M orders
                order_params["price"] = "0"  # Will be ignored for MARKET orders
            
            # Place the exit order - try different client structures
            success = False
            order_id = None
            
            try:
                if hasattr(client, 'place_order'):
                    success, order_id = client.place_order(order_params)
                elif hasattr(client, 'smart_api') and hasattr(client.smart_api, 'placeOrder'):
                    response = client.smart_api.placeOrder(order_params)
                    success = response.get('status') == True
                    order_id = response.get('data', {}).get('orderid')
                elif hasattr(client, 'placeOrder'):
                    response = client.placeOrder(order_params)
                    success = response.get('status') == True
                    order_id = response.get('data', {}).get('orderid')
            except Exception as e:
                logger.error(f"Error placing exit order: {str(e)}")
                success = False
            
            if success:
                logger.info(f"Successfully exited option position {trade['symbol']} ({reason}): {order_id}")
                
                # Calculate P&L if possible
                try:
                    # Get option current price
                    current_price = self.get_option_price(client, trade["symbol"], trade["token"])
                    if current_price is not None:
                        entry_price = trade.get("entry_price")  # This might not be available if we didn't track it
                        if entry_price:
                            pnl = (current_price - entry_price) * trade["quantity"]
                            pnl_percent = (current_price - entry_price) / entry_price * 100
                            logger.info(f"P&L for {trade['symbol']}: Rs.{pnl:.2f} ({pnl_percent:.2f}%)")
                            
                            # Record exit trade details in a completed trades file
                            self.record_completed_trade(trade, current_price, pnl, pnl_percent, reason, order_id)
                except Exception as e:
                    logger.error(f"Error calculating P&L for exited position: {str(e)}")
                
                return True
            else:
                logger.error(f"Failed to exit option position {trade['symbol']} ({reason})")
                return False
        except Exception as e:
            logger.error(f"Error exiting option position {trade['symbol']}: {str(e)}")
            return False
    
    def get_option_price(self, client, symbol, token):
        """Get current price of an option"""
        try:
            # Check cache first
            cache_key = f"NFO:{symbol}"
            if cache_key in self.price_cache:
                cache_entry = self.price_cache[cache_key]
                if time.time() - cache_entry['timestamp'] < self.price_cache_ttl:
                    return cache_entry['price']
            
            # If not in cache, fetch from broker
            # Handle different client structures
            try:
                response = client.smart_api.ltpData("NFO", symbol, token)
            except AttributeError:
                # Try direct access if smart_api access fails
                response = client.ltpData("NFO", symbol, token)
                
            if response.get('status'):
                price = float(response['data']['ltp'])
                
                # Update cache
                self.price_cache[cache_key] = {
                    'price': price,
                    'timestamp': time.time()
                }
                
                return price
            else:
                logger.error(f"Failed to get LTP for {symbol}: {response.get('message')}")
                return None
        except Exception as e:
            logger.error(f"Error getting option price for {symbol}: {str(e)}")
            return None
    
    def record_completed_trade(self, trade, exit_price, pnl, pnl_percent, exit_reason, exit_order_id):
        """Record details of a completed trade to a separate JSON file"""
        try:
            # Create a record of the completed trade
            completed_trade = {
                "trade_id": trade.get("trade_id", str(uuid.uuid4())),
                "client_id": trade["client_id"],
                "symbol": trade["symbol"],
                "underlying_symbol": trade.get("underlying_symbol", ""),
                "option_type": trade.get("option_type", ""),
                "strike_price": trade.get("strike_price", 0),
                "entry_time": trade.get("entry_time", datetime.now()).strftime('%Y-%m-%d %H:%M:%S') if isinstance(trade.get("entry_time"), datetime) else trade.get("entry_time", ""),
                "exit_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                "entry_price": trade.get("entry_price", 0),
                "exit_price": exit_price,
                "quantity": trade.get("quantity", 0),
                "pnl": pnl,
                "pnl_percent": pnl_percent,
                "exit_reason": exit_reason,
                "entry_order_id": trade.get("order_id", ""),
                "exit_order_id": exit_order_id,
                "expiry": trade.get("expiry", ""),
                "underlying_entry_price": trade.get("underlying_entry_price", 0),
                "underlying_stop_loss": trade.get("underlying_stop_loss", 0),
                "underlying_target": trade.get("underlying_target", 0)
            }
            
            # File to store completed trades
            completed_trades_file = self.completed_trades_file
            
            # Load existing completed trades
            completed_trades = []
            if os.path.exists(completed_trades_file):
                try:
                    with open(completed_trades_file, 'r') as f:
                        completed_trades = json.load(f)
                except:
                    completed_trades = []
            
            # Add new completed trade
            completed_trades.append(completed_trade)
            
            # Save back to file
            with open(completed_trades_file, 'w') as f:
                json.dump(completed_trades, f, indent=2)
            
            logger.info(f"Recorded completed trade for {trade['symbol']} in {completed_trades_file}")
        except Exception as e:
            logger.error(f"Error recording completed trade: {str(e)}")
    
    def manually_exit_trade(self, trade_id):
        """Manually exit a trade by its ID"""
        try:
            # Find the trade by ID
            trade_key = None
            trade = None
            
            for key, t in self.active_trades.items():
                if t.get("trade_id") == trade_id:
                    trade_key = key
                    trade = t
                    break
            
            if not trade:
                logger.warning(f"Trade with ID {trade_id} not found")
                return False, "Trade not found"
            
            # Find the client
            client_id = trade["client_id"]
            client = self.clients.get(client_id)
            
            if not client:
                logger.warning(f"Client {client_id} not found")
                return False, "Client not found"
            
            # Exit the position
            success = self.exit_option_position(client, trade, "MANUAL_EXIT")
            
            if success:
                # Remove from active trades
                self.active_trades.pop(trade_key, None)
                # Save changes
                self.save_trades()
                
                # Unsubscribe if using WebSocket
                if self.websocket_manager and self.websocket_manager.stream_connected:
                    self._unsubscribe_trade(trade)
                
                return True, "Trade successfully exited"
            else:
                return False, "Failed to exit trade"
            
        except Exception as e:
            logger.error(f"Error manually exiting trade {trade_id}: {str(e)}")
            return False, str(e)
    
    def update_web_data(self):
        """Update data for web interface"""
        try:
            active_trades_data = []
            total_pnl = 0
            
            for trade_key, trade in self.active_trades.items():
                # Create web-friendly trade data
                trade_data = {
                    "trade_id": trade.get("trade_id", ""),
                    "client_id": trade.get("client_id", ""),
                    "symbol": trade.get("symbol", ""),
                    "option_type": trade.get("option_type", ""),
                    "underlying_symbol": trade.get("underlying_symbol", ""),
                    "underlying_entry_price": trade.get("underlying_entry_price", 0),
                    "underlying_current_price": trade.get("current_underlying_price", 0),
                    "underlying_stop_loss": trade.get("underlying_stop_loss", 0),
                    "underlying_target": trade.get("underlying_target", 0),
                    "entry_price": trade.get("entry_price", 0),
                    "current_price": trade.get("current_option_price", 0),
                    "quantity": trade.get("quantity", 0),
                    "entry_time": trade.get("entry_time", datetime.now()).strftime('%Y-%m-%d %H:%M:%S') 
                              if isinstance(trade.get("entry_time"), datetime) else trade.get("entry_time", ""),
                    "expiry": trade.get("expiry", ""),
                    "pnl": trade.get("pnl", 0),
                    "pnl_percent": trade.get("pnl_percent", 0),
                    "status": trade.get("status", "ACTIVE"),
                    "last_updated": trade.get("last_updated", "")
                }
                
                active_trades_data.append(trade_data)
                
                # Add to total P&L
                if "pnl" in trade:
                    total_pnl += trade["pnl"]
            
            # Sort by entry time (newest first)
            active_trades_data.sort(key=lambda x: x["entry_time"], reverse=True)
            
            # Update WebSocket status
            websocket_status = "CONNECTED" if (self.websocket_manager and 
                                             (self.websocket_manager.stream_connected or 
                                              self.websocket_manager.order_connected)) else "DISCONNECTED"
            
            # Update the web data
            self.web_data = {
                "active_trades": active_trades_data,
                "total_pnl": total_pnl,
                "trade_count": len(active_trades_data),
                "last_update": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                "market_status": self.web_data.get("market_status", "UNKNOWN"),
                "websocket_status": websocket_status
            }
            
        except Exception as e:
            logger.error(f"Error updating web data: {str(e)}")
    
    def get_web_data(self):
        """Get data for web interface"""
        # Update real-time data first
        self.update_web_data()
        return self.web_data
    
    def check_for_external_changes(self):
        """
        Check if trades were manually exited in the broker platform
        This should be called periodically to sync with external changes
        """
        try:
            if not self.active_trades or not self.clients:
                return
            
            trades_to_remove = []
            
            # For each client, get active positions
            for client_id, client in self.clients.items():
                # Get only trades for this client
                client_trades = {k: v for k, v in self.active_trades.items() if v["client_id"] == client_id}
                
                if not client_trades:
                    continue
                
                # Get positions from broker
                try:
                    positions = self.get_client_positions(client)
                    
                    # Create a set of active symbols from broker positions
                    active_symbols = set()
                    for position in positions:
                        # Extract symbol and other details based on broker API response
                        symbol = position.get("tradingsymbol", "")
                        if symbol:
                            active_symbols.add(symbol)
                    
                    # Check each trade to see if it's still in active positions
                    for trade_key, trade in client_trades.items():
                        trade_symbol = trade["symbol"]
                        
                        # If the trade symbol is not in active positions, it was likely exited externally
                        if trade_symbol not in active_symbols:
                            logger.info(f"Trade {trade_symbol} appears to have been exited externally")
                            trades_to_remove.append(trade_key)
                
                except Exception as e:
                    logger.error(f"Error getting positions for client {client_id}: {str(e)}")
            
            # Remove trades that were exited externally
            if trades_to_remove:
                for trade_key in trades_to_remove:
                    trade = self.active_trades.get(trade_key)
                    if trade:
                        # Record it as a completed trade
                        try:
                            self.record_completed_trade(
                                trade, 
                                0,  # We don't know the exit price
                                0,  # We don't know the P&L
                                0,  # We don't know the P&L percent
                                "EXTERNAL_EXIT",
                                ""  # We don't have the order ID
                            )
                        except Exception as e:
                            logger.error(f"Error recording externally exited trade: {str(e)}")
                        
                        # Unsubscribe from WebSocket if active
                        if self.websocket_manager and self.websocket_manager.stream_connected:
                            self._unsubscribe_trade(trade)
                        
                        # Remove from active trades
                        self.active_trades.pop(trade_key, None)
                
                # Save changes
                self.save_trades()
                logger.info(f"Removed {len(trades_to_remove)} trades that were exited externally")
        
        except Exception as e:
            logger.error(f"Error checking for external changes: {str(e)}")
    
    def get_client_positions(self, client):
        """Get client positions from broker using the Angel One API"""
        try:
            # Try to get position data if the client supports it
            if hasattr(client, 'smart_api') and hasattr(client.smart_api, 'getPosition'):
                response = client.smart_api.getPosition()
                if response and response.get('status'):
                    return response.get('data', [])
            
            # If getPosition is not available, try to derive positions from orderBook and tradeBook
            positions = []
            
            # Try orderBook first to get active orders
            try:
                if hasattr(client, 'smart_api') and hasattr(client.smart_api, 'orderBook'):
                    response = client.smart_api.orderBook()
                    if response and response.get('status'):
                        # Filter for filled/active orders that represent positions
                        for order in response.get('data', []):
                            status = order.get('status', '').lower()
                            if status in ['complete', 'filled'] and order.get('filledshares', 0) > 0:
                                positions.append({
                                    'tradingsymbol': order.get('tradingsymbol'),
                                    'exchange': order.get('exchange'),
                                    'quantity': int(order.get('filledshares', 0)),
                                    'netqty': int(order.get('filledshares', 0)) * (1 if order.get('transactiontype') == 'BUY' else -1),
                                    'source': 'orderBook'
                                })
            except Exception as e:
                logger.warning(f"Error getting orderBook: {str(e)}")
            
            # Then try tradeBook to ensure all executed trades are considered
            try:
                if hasattr(client, 'smart_api') and hasattr(client.smart_api, 'tradeBook'):
                    response = client.smart_api.tradeBook()
                    if response and response.get('status'):
                        # Process trades and aggregate by symbol
                        symbol_positions = {}
                        
                        for trade in response.get('data', []):
                            symbol = trade.get('tradingsymbol')
                            if not symbol:
                                continue
                                
                            qty = int(trade.get('fillsize', 0))
                            is_buy = trade.get('transactiontype') == 'BUY'
                            net_qty = qty if is_buy else -qty
                            
                            if symbol not in symbol_positions:
                                symbol_positions[symbol] = {
                                    'tradingsymbol': symbol,
                                    'exchange': trade.get('exchange'),
                                    'quantity': 0,
                                    'netqty': 0,
                                    'source': 'tradeBook'
                                }
                            
                            symbol_positions[symbol]['quantity'] += qty
                            symbol_positions[symbol]['netqty'] += net_qty
                        
                        # Add unique positions from tradeBook
                        for symbol, pos in symbol_positions.items():
                            # Only consider as a position if net quantity is not zero
                            if pos['netqty'] != 0:
                                positions.append(pos)
            except Exception as e:
                logger.warning(f"Error getting tradeBook: {str(e)}")
            
            return positions
        except Exception as e:
            logger.error(f"Error getting client positions: {str(e)}")
            return []
    
    # WebSocket related methods
    def _handle_stream_connected(self):
        """Handle market data WebSocket connection"""
        logger.info("Market data WebSocket connected, subscribing to active trades")
        self.using_websocket = True
        self.web_data["websocket_status"] = "CONNECTED"
        
        # Subscribe to active trades
        self._subscribe_to_active_trades()
    
    def _handle_order_connected(self):
        """Handle order status WebSocket connection"""
        logger.info("Order status WebSocket connected")
        self.using_websocket = True
        self.web_data["websocket_status"] = "CONNECTED"
    
    def _handle_stream_disconnected(self):
        """Handle market data WebSocket disconnection"""
        logger.warning("Market data WebSocket disconnected, falling back to polling")
        self.using_websocket = False
        self.web_data["websocket_status"] = "DISCONNECTED"
    
    def _handle_order_disconnected(self):
        """Handle order status WebSocket disconnection"""
        logger.warning("Order status WebSocket disconnected")
        self.web_data["websocket_status"] = "DISCONNECTED"
    
    def _handle_market_data(self, binary_data):
        """Process market data updates from WebSocket"""
        if not self.websocket_manager:
            return
            
        # Parse binary data
        parsed_data = self.websocket_manager.parse_binary_market_data(binary_data)
        
        if not parsed_data:
            return
        
        # If mode is not LTP (1), we don't process it for price updates
        if parsed_data.get("mode") != 1:
            return
            
        # Extract fields from parsed data
        exchange_type = parsed_data.get("exchange_type")
        token = parsed_data.get("token")
        ltp = parsed_data.get("ltp")
        
        if not (exchange_type and token and ltp):
            return
        
        # Map exchange type back to exchange name
        exchange = get_exchange_name(exchange_type)
        
        # Update price cache
        cache_key = f"{exchange}:{token}"
        self.price_cache[cache_key] = {
            'price': ltp,
            'timestamp': time.time()
        }
        
        # Find trades that use this underlying for monitoring
        for trade_key, trade in list(self.active_trades.items()):
            try:
                underlying_symbol = trade.get("underlying_symbol")
                underlying_exchange = trade.get("underlying_exchange", "NSE")
                
                if not underlying_symbol:
                    continue
                
                # Get token for the underlying
                underlying_token = self.price_fetcher.get_symbol_token(underlying_symbol, underlying_exchange)
                
                if not underlying_token:
                    continue
                
                # Check if this update matches the trade's underlying
                if token == underlying_token and exchange == underlying_exchange:
                    # We have a price update for one of our monitored underlyings
                    # Process the trade with new price
                    logger.debug(f"WebSocket price update for {underlying_symbol}: {ltp}")
                    self.check_single_trade(trade_key, trade, ltp)
            except Exception as e:
                logger.error(f"Error processing WebSocket update for trade {trade_key}: {str(e)}")
    
    def _handle_order_update(self, order_data):
        """Process order status updates from WebSocket"""
        try:
            # Check if this is an actual order update or just the initial connection message
            order_status = order_data.get("order-status")
            if order_status == "AB00":
                # This is just the initial connection message
                logger.info("WebSocket order status connected, received initial message")
                return
                
            # Extract order details
            order_details = order_data.get("orderData", {})
            order_id = order_details.get("orderid")
            
            if not order_id:
                return
                
            # Find relevant trade that matches this order ID
            matching_trade_key = None
            matching_trade = None
            
            # Check both entry and exit orders
            for trade_key, trade in list(self.active_trades.items()):
                if trade.get("order_id") == order_id:
                    matching_trade_key = trade_key
                    matching_trade = trade
                    logger.info(f"Order update for entry order: {order_id} - Status: {order_status}")
                    break
            
            if not matching_trade:
                # This might be an exit order, which we don't track in active trades
                # But we could log it for debugging
                logger.debug(f"Received order update for unknown order: {order_id} - Status: {order_status}")
                return
            
            # Process based on order status
            if order_status in ["AB05"]:  # Complete
                # Order has been fully executed
                logger.info(f"Order {order_id} for {matching_trade.get('symbol')} completed")
                
                # Update trade with execution details if available
                avg_price = float(order_details.get("averageprice", 0))
                filled_qty = order_details.get("filledshares", "0")
                
                if avg_price > 0:
                    # The entry price might be more accurate from the order
                    self.active_trades[matching_trade_key]["entry_price"] = avg_price
                    
                # Save changes
                self.save_trades()
                
            elif order_status in ["AB02", "AB03"]:  # Cancelled or Rejected
                # Order has been cancelled or rejected
                logger.warning(f"Order {order_id} for {matching_trade.get('symbol')} {order_status.lower()}: {order_details.get('text', '')}")
                
                # Mark the trade status
                self.active_trades[matching_trade_key]["status"] = "REJECTED" if order_status == "AB03" else "CANCELLED"
                self.active_trades[matching_trade_key]["status_reason"] = order_details.get("text", "")
                
                # You might want to remove it or keep it with a special status
                # For now, we'll keep it with the status flagged
                self.save_trades()
                
        except Exception as e:
            logger.error(f"Error processing order update: {str(e)}")
    
    def _subscribe_to_active_trades(self):
        """Subscribe to market data for all active trades"""
        if not self.websocket_manager or not self.active_trades:
            return
            
        # Group tokens by exchange for WebSocket subscription
        token_list = []
        tokens_by_exchange = {}
        
        for trade_key, trade in self.active_trades.items():
            underlying_symbol = trade.get("underlying_symbol")
            exchange_name = trade.get("underlying_exchange", "NSE")
            
            if not underlying_symbol:
                continue
                
            # Convert exchange name to exchange type ID for WebSocket
            exchange_type = get_exchange_type_id(exchange_name)
            
            if not exchange_type:
                logger.warning(f"Unknown exchange: {exchange_name}")
                continue
                
            # Get token for underlying
            token = self.price_fetcher.get_symbol_token(underlying_symbol, exchange_name)
            
            if not token:
                logger.warning(f"Could not get token for {underlying_symbol} on {exchange_name}")
                continue
                
            # Add to exchange group
            if exchange_type not in tokens_by_exchange:
                tokens_by_exchange[exchange_type] = []
                
            if token not in tokens_by_exchange[exchange_type]:
                tokens_by_exchange[exchange_type].append(token)
        
        # Prepare token list for subscription
        for exchange_type, tokens in tokens_by_exchange.items():
            token_list.append({
                "exchangeType": exchange_type,
                "tokens": tokens
            })
        
        # Subscribe if we have tokens
        if token_list:
            token_count = sum(len(exchange["tokens"]) for exchange in token_list)
            logger.info(f"Subscribing to {token_count} underlying tokens for active trades")
            self.websocket_manager.subscribe_market_data(token_list, mode=1)  # LTP mode
        
    def _unsubscribe_trade(self, trade):
        """Unsubscribe from market data for a trade that is being removed"""
        if not self.websocket_manager or not self.websocket_manager.stream_connected:
            return
            
        underlying_symbol = trade.get("underlying_symbol")
        exchange_name = trade.get("underlying_exchange", "NSE")
        
        if not underlying_symbol:
            return
            
        # Check if there are other active trades for this underlying
        other_trades_with_same_underlying = False
        for other_key, other_trade in self.active_trades.items():
            if (other_trade.get("underlying_symbol") == underlying_symbol and 
                other_trade.get("underlying_exchange") == exchange_name and
                other_trade != trade):
                other_trades_with_same_underlying = True
                break
        
        # Only unsubscribe if this is the last trade for this underlying
        if not other_trades_with_same_underlying:
            # Convert exchange name to exchange type ID
            exchange_type = get_exchange_type_id(exchange_name)
            
            if not exchange_type:
                return
                
            # Get token for underlying
            token = self.price_fetcher.get_symbol_token(underlying_symbol, exchange_name)
            
            if not token:
                return
                
            # Prepare token list for unsubscription
            token_list = [{
                "exchangeType": exchange_type,
                "tokens": [token]
            }]
            
            # Unsubscribe
            logger.info(f"Unsubscribing from {underlying_symbol} as all trades for it are closed")
            self.websocket_manager.unsubscribe_market_data(token_list, mode=1)  # LTP mode
    
    def shutdown(self):
        """Safely shutdown the service"""
        logger.info("Shutting down TradeMonitorService...")
        # Stop monitoring thread
        self.stop_monitoring()
        # Save current trades
        self.save_trades()
        logger.info("TradeMonitorService shutdown complete")

# Create a singleton instance
trade_monitor_service = TradeMonitorService()