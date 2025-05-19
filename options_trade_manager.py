import logging
import json
import os
import uuid
from datetime import datetime, timedelta
import time

# Import WebSocket manager
from angel_websocket_manager import websocket_manager as websocket_manager

logger = logging.getLogger(__name__)

class OptionsTradeManager:
    def __init__(self, json_file_path="option_trades.json"):
        self.active_option_trades = {}
        self.active_clients = {}
        self.json_file_path = json_file_path
        self.completed_trades_file = "completed_option_trades.json"
        self.last_save_time = datetime.now()
        self.auto_save_interval = timedelta(minutes=1)  # Auto-save every minute
        self.file_lock = None  # Will be initialized if needed for thread-safety
        
        # WebSocket related members
        self.websocket_manager = None
    
    def initialize(self, active_clients, price_fetcher, websocket_manager=None):
        """Initialize the trade manager with active clients and price fetcher functions"""
        self.active_clients = active_clients
        self.price_fetcher = price_fetcher
        self.websocket_manager = websocket_manager
        
        # Load any existing trades from JSON file
        self.load_trades_from_json()
        
        # Reconcile with actual positions to ensure accuracy
        self.reconcile_with_broker_positions()
        
        # Connect to WebSocket if provided
        if self.websocket_manager and self.websocket_manager.is_connected():
            # Set up order status callback
            self.websocket_manager.on_order_update = self._handle_order_update
        
        logger.info(f"OptionsTradeManager initialized with {len(self.active_option_trades)} active trades")
    
    def save_trades_to_json(self):
        """Save active trades to JSON file"""
        try:
            # Convert datetime objects to strings for JSON serialization
            serializable_trades = {}
            for key, trade in self.active_option_trades.items():
                # Create a copy of the trade to avoid modifying the original
                trade_copy = trade.copy()
                
                # Convert datetime objects to strings
                if isinstance(trade_copy.get("entry_time"), datetime):
                    trade_copy["entry_time"] = trade_copy["entry_time"].strftime('%Y-%m-%d %H:%M:%S')
                
                serializable_trades[key] = trade_copy
            
            # Create directory if it doesn't exist
            os.makedirs(os.path.dirname(os.path.abspath(self.json_file_path)), exist_ok=True)
            
            # Write to JSON file
            with open(self.json_file_path, 'w') as f:
                json.dump(serializable_trades, f, indent=2)
                
            logger.debug(f"Successfully saved {len(serializable_trades)} trades to {self.json_file_path}")
            return True
        except Exception as e:
            logger.error(f"Error saving trades to JSON: {str(e)}")
            return False
    
    def load_trades_from_json(self):
        """Load trades from JSON file"""
        try:
            if not os.path.exists(self.json_file_path):
                logger.info(f"No trades file found at {self.json_file_path}, starting with empty trade list")
                return False
                
            with open(self.json_file_path, 'r') as f:
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
            self.active_option_trades = loaded_trades
            logger.info(f"Successfully loaded {len(loaded_trades)} trades from {self.json_file_path}")
            return True
        except Exception as e:
            logger.error(f"Error loading trades from JSON: {str(e)}")
            return False
    
    def verify_order_execution(self, client, order_id, max_retries=3, delay=5):
        """
        Verify if an order was successfully executed
        Returns: (success, order_status, avg_price, filled_qty)
        """
        retries = 0
        while retries < max_retries:
            try:
                # Use orderBook to get order details
                response = None
                
                if hasattr(client, 'smart_api') and hasattr(client.smart_api, 'orderBook'):
                    response = client.smart_api.orderBook()
                elif hasattr(client, 'orderBook'):
                    response = client.orderBook()
                
                if response and response.get('status'):
                    orders = response.get('data', [])
                    
                    # Find our specific order
                    for order in orders:
                        if order.get('orderid') == order_id:
                            status = order.get('status', '').lower()
                            
                            # Check if order is complete or rejected
                            if status in ['complete', 'filled', 'cancelled', 'rejected']:
                                avg_price = float(order.get('averageprice', 0))
                                filled_qty = int(order.get('filledshares', 0))
                                return True, status, avg_price, filled_qty
                            
                            # If still pending, we'll retry
                            logger.info(f"Order {order_id} is still in {status} state, waiting...")
                            break
                
                # Wait before retrying
                time.sleep(delay)
                retries += 1
                
            except Exception as e:
                logger.error(f"Error verifying order execution: {str(e)}")
                time.sleep(delay)
                retries += 1
        
        logger.warning(f"Could not verify order {order_id} execution after {max_retries} attempts")
        return False, "unknown", 0, 0

    def reconcile_with_broker_positions(self):
        """
        Reconcile active trades with actual broker positions
        This ensures we're not tracking trades that were manually exited
        """
        if not self.active_option_trades or not self.active_clients:
            return
        
        logger.info("Starting reconciliation with broker positions...")
        trades_to_remove = []
        
        # Group trades by client
        trades_by_client = {}
        for trade_key, trade in self.active_option_trades.items():
            client_id = trade.get('client_id')
            if client_id not in trades_by_client:
                trades_by_client[client_id] = []
            trades_by_client[client_id].append((trade_key, trade))
        
        # Check each client's positions
        for client_id, trades in trades_by_client.items():
            client = self.active_clients.get(client_id)
            if not client:
                logger.warning(f"Client {client_id} not found, skipping reconciliation")
                continue
            
            # Get current positions from broker
            positions = self.get_client_positions(client)
            if not positions:
                logger.warning(f"Could not get positions for client {client_id}, skipping reconciliation")
                continue
            
            # Create a map of symbol -> net quantity from broker positions
            position_map = {}
            for position in positions:
                symbol = position.get('tradingsymbol', '')
                if symbol:
                    # Track net quantity (positive for long positions, negative for short)
                    qty = int(float(position.get('netqty', 0)))
                    position_map[symbol] = qty
            
            # Check if our trades exist in actual positions
            for trade_key, trade in trades:
                symbol = trade.get('symbol', '')
                expected_qty = trade.get('quantity', 0)
                
                # Check if position exists and has correct quantity
                if symbol not in position_map:
                    logger.info(f"Trade {symbol} not found in broker positions, marking for removal")
                    trades_to_remove.append((trade_key, trade))
                elif position_map[symbol] <= 0:
                    logger.info(f"Trade {symbol} has zero or negative quantity, marking for removal")
                    trades_to_remove.append((trade_key, trade))
                elif position_map[symbol] < expected_qty:
                    logger.info(f"Trade {symbol} has lower quantity ({position_map[symbol]}) than expected ({expected_qty}), marking for removal")
                    trades_to_remove.append((trade_key, trade))
        
        # Process trades that need to be removed
        for trade_key, trade in trades_to_remove:
            # Record it as completed with EXTERNAL_EXIT reason
            self.record_completed_trade(
                trade,
                0,  # Unknown exit price
                0,  # Unknown PnL
                0,  # Unknown PnL percent
                "EXTERNAL_EXIT",
                ""  # No order ID
            )
            
            # Remove from active trades
            self.active_option_trades.pop(trade_key, None)
        
        if trades_to_remove:
            logger.info(f"Reconciliation complete: removed {len(trades_to_remove)} trades that were manually exited")
            self.save_trades_to_json()
        else:
            logger.info("Reconciliation complete: all trades match broker positions")


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

    def process_option_signal(self, webhook_data, price_fetcher=None):
        """Process a trading signal for options with direct strike calculation using dynamic step size"""
        try:
            if not price_fetcher:
                price_fetcher = self.price_fetcher

            # Extract signal details
            symbol = webhook_data.get("symbol")
            action = webhook_data.get("action", "").upper()  # BUY or SELL signal
            quantity = int(webhook_data.get("quantity", 1))
            
            # Get parameters from webhook
            producttype = webhook_data.get("producttype", "INTRADAY")
            ordertype = webhook_data.get("ordertype", "MARKET")
            exit_ordertype = webhook_data.get("exit_ordertype", "MARKET")

            # Optional fields
            specific_option = webhook_data.get("option_details")
            option_moneyness = webhook_data.get("option_moneyness", "ATM").upper()  # ATM, ITM, OTM
            moneyness_depth = int(webhook_data.get("moneyness_depth", 0))  # Support depths up to 20
            expiry_preference = webhook_data.get("expiry_preference")
            
            # Validate moneyness_depth (cap at 20 for safety)
            if moneyness_depth > 20:
                logger.warning(f"Moneyness depth {moneyness_depth} is too high, capping at 20")
                moneyness_depth = 20
            
            # Get prices for monitoring
            try:
                underlying_price = float(webhook_data.get("underlying_price", 0))
            except (ValueError, TypeError):
                underlying_price = 0
                
            try:
                underlying_stop_loss = float(webhook_data.get("underlying_stop_loss", 0))
            except (ValueError, TypeError):
                underlying_stop_loss = 0
                
            try:
                underlying_target = float(webhook_data.get("underlying_target", 0))
            except (ValueError, TypeError):
                underlying_target = 0
            
            # Validate required fields
            if not symbol:
                logger.error("No symbol provided in webhook data")
                return False, [{"error": "No symbol provided"}]
            
            # Determine option type based on action
            # For BUY signal -> Buy Call options (bullish)
            # For SELL signal -> Buy Put options (bearish)
            option_type = "CE" if action == "BUY" else "PE"
            
            logger.info(f"Processing {action} signal for {symbol}, selecting {option_type} options with depth {moneyness_depth}")
            
            # Process for each client (or specific client if specified)
            target_clients = self.active_clients
            if webhook_data.get("client_id"):
                target_client_id = webhook_data.get("client_id")
                if target_client_id in self.active_clients:
                    target_clients = {target_client_id: self.active_clients[target_client_id]}
                else:
                    logger.error(f"Specified client {target_client_id} not found")
                    return False, [{"error": f"Client {target_client_id} not found"}]
                    
            # Only proceed if we have clients to process
            if not target_clients:
                logger.error("No active clients available")
                return False, [{"error": "No active clients available"}]
                
            # Get a reference client for price and option data lookup
            check_client = next(iter(target_clients.values()))
            
            # Get current price if not provided
            if underlying_price <= 0:
                underlying_price = price_fetcher.get_underlying_price(check_client, symbol)
                if not underlying_price:
                    logger.error(f"Could not determine current price for {symbol}")
                    return False, [{"error": f"Could not determine price for {symbol}"}]
                
                # Ensure underlying_price is a float
                try:
                    underlying_price = float(underlying_price)
                except (ValueError, TypeError):
                    logger.error(f"Invalid underlying price: {underlying_price}")
                    return False, [{"error": f"Invalid price format for {symbol}"}]
            
            # Validate stop loss and target (calculate defaults if needed)
            if underlying_stop_loss <= 0 or underlying_target <= 0:
                logger.warning("Stop loss or target not provided, calculating based on price")
                # Calculate default values (5% from current price)
                if action == "BUY":  # Bullish
                    underlying_stop_loss = round(underlying_price * 0.95, 2)
                    underlying_target = round(underlying_price * 1.05, 2)
                else:  # Bearish
                    underlying_stop_loss = round(underlying_price * 1.05, 2)
                    underlying_target = round(underlying_price * 0.95, 2)
            
            results = []
            
            # If specific option details provided, use them
            option_contract = None
            if specific_option:
                option_symbol = specific_option.get("symbol")
                option_token = specific_option.get("token")
                
                try:
                    strike_price = float(specific_option.get("strike_price", 0))
                except (ValueError, TypeError):
                    strike_price = 0
                
                if not option_symbol or not option_token:
                    logger.error("Incomplete option details provided")
                    return False, [{"error": "Incomplete option details provided"}]
                
                option_contract = {
                    "symbol": option_symbol,
                    "token": option_token,
                    "strike_price": strike_price,
                    "option_type": specific_option.get("option_type", option_type),
                    "expiry": expiry_preference or "unknown",
                    "lotsize": specific_option.get("lotsize", 1)
                }
            else:
                # Get expiry dates if not specified
                if not expiry_preference:
                    logger.info("No expiry preference specified, getting available expiries")
                    expiries = price_fetcher.get_expiry_dates(check_client, symbol)
                    if not expiries:
                        logger.error(f"No expiry dates available for {symbol}")
                        return False, [{"error": "No expiry dates available"}]
                    expiry_preference = expiries[0]  # Use nearest expiry
                
                # OPTIMIZED APPROACH: Direct strike calculation using dynamic step size - ONCE for all clients
                
                # 1. Determine the strike spacing (step size)
                step = None
                
                # Try to dynamically determine step size from option chain - fetch only the option type we need
                try:
                    # Fetch option chain to analyze strike patterns
                    temp_option_chain = price_fetcher.fetch_options_for_expiry(
                        check_client, 
                        symbol, 
                        expiry_preference, 
                        underlying_price, 
                        "NFO" if symbol.upper() != "SENSEX" else "BFO"
                    )
                    
                    # If we got an option chain, extract step size from it
                    if temp_option_chain:
                        # We only need the relevant options based on our action/option_type
                        relevant_options = temp_option_chain.get('calls' if option_type == "CE" else 'puts', [])
                        if relevant_options:
                            # Get all unique strikes
                            all_strikes = sorted(set(opt['strike_price'] for opt in relevant_options))
                            # Calculate differences between adjacent strikes
                            if len(all_strikes) >= 2:
                                diffs = [all_strikes[i+1] - all_strikes[i] for i in range(len(all_strikes)-1)]
                                # Find most common difference
                                from collections import Counter
                                step_counter = Counter(diffs)
                                if step_counter:
                                    step = step_counter.most_common(1)[0][0]
                                    logger.info(f"Dynamically determined step size: {step} for {symbol}")
                except Exception as e:
                    logger.warning(f"Error dynamically determining step size: {str(e)}")
                
                # If dynamic determination failed, try price_fetcher
                if step is None:
                    try:
                        if hasattr(price_fetcher, '_get_default_step_size'):
                            step = price_fetcher._get_default_step_size(symbol, underlying_price)
                            logger.info(f"Using default step size: {step} for {symbol}")
                    except Exception as e:
                        logger.warning(f"Could not determine step size using price_fetcher: {str(e)}")
                
                # If still no step size, use symbol-based logic as fallback
                if step is None:
                    if symbol.upper() == "NIFTY":
                        step = 50
                    elif symbol.upper() == "BANKNIFTY" or symbol.upper() == "BANKEX":
                        step = 100  # Changed from 500 to 100
                    elif symbol.upper() == "FINNIFTY":
                        step = 50
                    elif symbol.upper() == "MIDCPNIFTY":
                        step = 25
                    elif symbol.upper() == "SENSEX":
                        step = 100
                    elif underlying_price > 5000:
                        step = 100
                    elif underlying_price > 1000:
                        step = 50
                    elif underlying_price > 500:
                        step = 20
                    elif underlying_price > 100:
                        step = 10
                    elif underlying_price > 50:
                        step = 5
                    else:
                        step = 2.5
                    
                    logger.info(f"Using estimated step size: {step} for {symbol}")
                
                # 2. Calculate ATM strike (round to nearest step)
                atm_strike = round(underlying_price / step) * step
                logger.info(f"Calculated ATM strike: {atm_strike} for underlying price: {underlying_price}")
                
                # 3. Calculate the target strike based on moneyness and depth
                target_strike = atm_strike  # Default to ATM
                
                if option_moneyness == "ITM":
                    # For Calls (CE), ITM means lower strikes
                    # For Puts (PE), ITM means higher strikes
                    if option_type == "CE":
                        target_strike = atm_strike - (moneyness_depth * step)
                    else:
                        target_strike = atm_strike + (moneyness_depth * step)
                elif option_moneyness == "OTM":
                    # For Calls (CE), OTM means higher strikes
                    # For Puts (PE), OTM means lower strikes
                    if option_type == "CE":
                        target_strike = atm_strike + (moneyness_depth * step)
                    else:
                        target_strike = atm_strike - (moneyness_depth * step)
                
                logger.info(f"Selected {option_moneyness} strike: {target_strike} (depth {moneyness_depth})")
                
                # Add safety limit for extremely deep OTM options
                max_price_deviation = 0.20  # 20% max deviation from current price
                max_otm_strike_call = underlying_price * (1 + max_price_deviation)
                min_otm_strike_put = underlying_price * (1 - max_price_deviation)

                # Apply safety limits
                if option_type == "CE" and target_strike > max_otm_strike_call:
                    original_target = target_strike
                    target_strike = min(max_otm_strike_call, atm_strike + (5 * step))  # No more than 5 strikes OTM
                    logger.warning(f"Limited extremely deep OTM call strike from {original_target} to {target_strike} (20% max deviation)")
                elif option_type == "PE" and target_strike < min_otm_strike_put:
                    original_target = target_strike
                    target_strike = max(min_otm_strike_put, atm_strike - (5 * step))  # No more than 5 strikes OTM
                    logger.warning(f"Limited extremely deep OTM put strike from {original_target} to {target_strike} (20% max deviation)")
                
                # 4. Get the specific option contract for the target strike - ONCE for all clients
                try:
                    # Get contract details for the specific strike
                    option_contract = price_fetcher.get_option_contract(
                        check_client, 
                        symbol, 
                        expiry_preference, 
                        target_strike, 
                        option_type
                    )
                    
                    if not option_contract:
                        logger.error(f"Could not find option contract for {symbol} {expiry_preference} {target_strike} {option_type}")
                        return False, [{"error": f"Option contract not found for strike {target_strike}"}]
                    
                    logger.info(f"Found option contract: {option_contract['symbol']}")
                    
                except Exception as e:
                    logger.error(f"Error getting option contract: {str(e)}")
                    return False, [{"error": f"Error getting option contract: {str(e)}"}]
            
            # Now process each client with the same option contract
            for client_id, client in target_clients.items():
                # Calculate proper quantity based on lot size
                try:
                    lot_size = int(option_contract.get('lotsize', 1))
                except (ValueError, TypeError):
                    lot_size = 1
                    
                adjusted_quantity = quantity
                if lot_size > 1:
                    # Adjust quantity to be a multiple of lot size
                    if quantity < lot_size:
                        adjusted_quantity = lot_size
                    else:
                        # Round to nearest lot
                        adjusted_quantity = round(quantity / lot_size) * lot_size
                
                # Prepare order parameters
                order_params = {
                    "variety": "NORMAL",
                    "tradingsymbol": option_contract["symbol"],
                    "symboltoken": option_contract["token"],
                    "transactiontype": "BUY",  # Always BUY the option contract
                    "exchange": "NFO",  # Options are on NFO
                    "ordertype": ordertype,  # Use the ordertype from webhook
                    "producttype": producttype,  # Use the producttype from webhook
                    "duration": "DAY",
                    "quantity": str(adjusted_quantity)
                }
                
                # For limit orders, we need a valid price
                if ordertype == "LIMIT" or ordertype == "SL":
                    # Try to get current option price first
                    current_price = self.get_option_price(client, option_contract["symbol"], option_contract["token"])
                    if current_price:
                        # Ensure current_price is a float
                        try:
                            current_price = float(current_price)
                        except (ValueError, TypeError):
                            logger.warning(f"Invalid option price format: {current_price}, using default")
                            current_price = None
                            
                        # Use current price for limit orders
                        if current_price and ordertype == "LIMIT":
                            # For buy orders, set limit price slightly higher for better fill chance
                            limit_price = round(current_price * 1.01 * 20) / 20
                            order_params["price"] = str(limit_price)
                        elif current_price and ordertype == "SL":
                            # For stop loss orders, set trigger price slightly lower 
                            stop_price = round(current_price * 0.99, 2)  # 1% lower
                            order_params["triggerprice"] = str(stop_price)
                            order_params["price"] = str(current_price)
                    
                    # Fallback if we can't get current price
                    if not current_price:
                        logger.warning(f"Could not determine current price for {option_contract['symbol']}, using default values for limit order")
                        # Set a high limit price to ensure order execution (for LIMIT)
                        if ordertype == "LIMIT":
                            order_params["price"] = "1000.0"  
                        elif ordertype == "SL":
                            order_params["triggerprice"] = "0.1"  # Low trigger price
                            order_params["price"] = "1000.0"  # High limit price
                else:
                    # For MARKET and SL-M orders
                    order_params["price"] = "0"  # Will be ignored for MARKET orders
                
                # Place the order
                logger.info(f"Placing order for {option_contract['symbol']} with client {client_id}, quantity: {adjusted_quantity}, producttype: {producttype}, ordertype: {ordertype}")
                
                # Handle different client structures for placing orders
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
                    logger.error(f"Error placing order: {str(e)}")
                    success = False
                
                if success and order_id:
                    logger.info(f"Option order placed successfully for client {client_id}: {order_id}")
                    
                    # Verify order execution to make sure it wasn't rejected
                    verified, status, avg_price, filled_qty = self.verify_order_execution(client, order_id, max_retries=3)
                    
                    if not verified or status in ['rejected', 'cancelled']:
                        logger.error(f"Order {order_id} was not executed successfully: {status}")
                        results.append({
                            "client_id": client_id,
                            "success": False,
                            "message": f"Order was {status}. Please check funds and margins."
                        })
                        continue
                    
                    # Generate a unique trade ID
                    trade_id = str(uuid.uuid4())
                    
                    # Get option entry price
                    entry_price = avg_price
                    
                    # If we couldn't get the average price from order verification
                    if entry_price <= 0:
                        try:
                            entry_price = self.get_option_price(client, option_contract["symbol"], option_contract["token"])
                            # Convert to float if needed
                            if entry_price is not None:
                                entry_price = float(entry_price)
                        except Exception as e:
                            logger.warning(f"Could not get entry price for {option_contract['symbol']}: {str(e)}")
                    
                    # Add to active option trades for monitoring
                    trade_key = f"{client_id}_{option_contract['symbol']}"
                    self.active_option_trades[trade_key] = {
                        "trade_id": trade_id,
                        "client_id": client_id,
                        "symbol": option_contract["symbol"],
                        "token": option_contract["token"],
                        "option_type": option_contract.get("option_type", option_type),
                        "underlying_symbol": symbol,
                        "underlying_exchange": webhook_data.get("exchange", "NSE"),
                        "underlying_entry_price": underlying_price,
                        "underlying_stop_loss": underlying_stop_loss,
                        "underlying_target": underlying_target,
                        "quantity": adjusted_quantity,
                        "order_id": order_id,
                        "entry_time": datetime.now(),
                        "expiry": option_contract.get("expiry", expiry_preference or "unknown"),
                        "strike_price": option_contract.get("strike_price", 0),
                        "lotsize": lot_size,
                        "producttype": producttype,
                        "ordertype": ordertype,
                        "exit_ordertype": exit_ordertype
                    }
                    
                    # Add entry price if available
                    if entry_price:
                        self.active_option_trades[trade_key]["entry_price"] = entry_price
                    
                    # Save the updated trades to JSON
                    self.save_trades_to_json()
                    
                    results.append({
                        "client_id": client_id,
                        "success": True,
                        "trade_id": trade_id,
                        "order_id": order_id,
                        "option_details": {
                            "symbol": option_contract["symbol"],
                            "strike": option_contract.get("strike_price", 0),
                            "type": option_contract.get("option_type", option_type),
                            "expiry": option_contract.get("expiry", expiry_preference or "unknown"),
                            "lotsize": lot_size
                        },
                        "underlying": {
                            "symbol": symbol,
                            "entry_price": underlying_price,
                            "stop_loss": underlying_stop_loss,
                            "target": underlying_target
                        },
                        "order_details": {
                            "producttype": producttype,
                            "ordertype": ordertype,
                            "exit_ordertype": exit_ordertype
                        }
                    })
                else:
                    logger.error(f"Option order placement failed for client {client_id}")
                    results.append({
                        "client_id": client_id,
                        "success": False,
                        "message": "Order placement failed"
                    })
            
            # Return the results
            successful_orders = sum(1 for r in results if r.get("success", False))
            logger.info(f"Option order placement complete: {successful_orders}/{len(target_clients)} successful")
            
            return successful_orders > 0, results
            
        except Exception as e:
            logger.error(f"Error processing option signal: {str(e)}")
            return False, [{"error": str(e)}]


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
                        # Calculate 1% lower price and round to nearest 5 paisa
                        limit_price = round((current_price * 0.99) * 20) / 20  # Round to nearest 0.05
                        order_params["price"] = str(limit_price)
                    elif exit_ordertype == "SL":
                        # For SL orders, set trigger slightly higher than current price
                        # Round to nearest 5 paisa
                        stop_price = round((current_price * 1.01) * 20) / 20  # Round to nearest 0.05
                        order_params["triggerprice"] = str(stop_price)
                        order_params["price"] = str(round(current_price * 20) / 20)  # Round current price to nearest 0.05
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
                logger.info(f"Successfully placed exit order for {trade['symbol']} ({reason}): {order_id}")
                
                # Verify if order was executed
                verified, status, exit_price, filled_qty = self.verify_order_execution(client, order_id)
                
                # If we couldn't verify or order was rejected, return failure
                if not verified or status in ['rejected', 'cancelled']:
                    logger.error(f"Exit order {order_id} verification failed or order was {status}")
                    return False
                
                # If order was filled but we don't have exit price yet
                if exit_price <= 0:
                    # Try to get current market price
                    exit_price = self.get_option_price(client, trade["symbol"], trade["token"])
                
                # Calculate P&L
                entry_price = trade.get("entry_price", 0)
                if entry_price > 0 and exit_price > 0:
                    pnl = (exit_price - entry_price) * trade["quantity"]
                    pnl_percent = (exit_price - entry_price) / entry_price * 100
                    logger.info(f"P&L for {trade['symbol']}: Rs.{pnl:.2f} ({pnl_percent:.2f}%)")
                else:
                    pnl = 0
                    pnl_percent = 0
                
                # Record completed trade
                self.record_completed_trade(trade, exit_price, pnl, pnl_percent, reason, order_id)
                
                # Remove the trade from active trades
                trade_key = f"{trade['client_id']}_{trade['symbol']}"
                if trade_key in self.active_option_trades:
                    self.active_option_trades.pop(trade_key, None)
                    # Save changes to JSON after removing trade
                    self.save_trades_to_json()
                
                return True
            else:
                logger.error(f"Failed to exit option position {trade['symbol']} ({reason})")
                return False
        except Exception as e:
            logger.error(f"Error exiting option position {trade['symbol']}: {str(e)}")
            return False
    
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
            
            # Load existing completed trades
            completed_trades = []
            if os.path.exists(self.completed_trades_file):
                try:
                    with open(self.completed_trades_file, 'r') as f:
                        completed_trades = json.load(f)
                except:
                    completed_trades = []
            
            # Add new completed trade
            completed_trades.append(completed_trade)
            
            # Save back to file
            with open(self.completed_trades_file, 'w') as f:
                json.dump(completed_trades, f, indent=2)
            
            logger.info(f"Recorded completed trade for {trade['symbol']} in {self.completed_trades_file}")
        except Exception as e:
            logger.error(f"Error recording completed trade: {str(e)}")
    
    def get_option_price(self, client, symbol, token):
        """Get current price of an option"""
        try:
            # Handle different client structures
            try:
                response = client.smart_api.ltpData("NFO", symbol, token)
            except AttributeError:
                # Try direct access if smart_api access fails
                response = client.ltpData("NFO", symbol, token)
                
            if response and response.get('status'):
                return float(response['data']['ltp'])
            else:
                logger.error(f"Failed to get LTP for {symbol}: {response.get('message')}")
                return None
        except Exception as e:
            logger.error(f"Error getting option price for {symbol}: {str(e)}")
            return None
    
    def get_active_option_trades(self):
        """Get list of active option trades with current prices and P&L"""
        # First reconcile with broker positions to ensure accuracy
        self.reconcile_with_broker_positions()
        
        active_trades = []
        
        for trade_key, trade in self.active_option_trades.items():
            # Get current price of option and underlying for P&L calculation
            current_option_price = None
            current_underlying_price = None
            
            if self.active_clients:
                check_client = next(iter(self.active_clients.values()))
                
                # Get option price
                try:
                    current_option_price = self.get_option_price(check_client, trade["symbol"], trade["token"])
                except:
                    pass
                
                # Get underlying price
                try:
                    current_underlying_price = self.price_fetcher.get_underlying_price(
                        check_client, 
                        trade["underlying_symbol"], 
                        trade.get("underlying_exchange", "NSE")
                    )
                except:
                    pass
            
            # Create a dictionary with all trade information
            trade_info = {
                "trade_id": trade.get("trade_id", ""),
                "client_id": trade["client_id"],
                "symbol": trade["symbol"],
                "token": trade["token"],
                "option_type": trade.get("option_type", ""),
                "underlying_symbol": trade.get("underlying_symbol", ""),
                "underlying_exchange": trade.get("underlying_exchange", "NSE"),
                "underlying_entry_price": trade.get("underlying_entry_price", 0),
                "underlying_stop_loss": trade.get("underlying_stop_loss", 0),
                "underlying_target": trade.get("underlying_target", 0),
                "current_underlying_price": current_underlying_price,
                "quantity": trade.get("quantity", 0),
                "entry_time": trade.get("entry_time", datetime.now()).strftime('%Y-%m-%d %H:%M:%S') if isinstance(trade.get("entry_time"), datetime) else trade.get("entry_time", ""),
                "expiry": trade.get("expiry", ""),
                "lotsize": trade.get("lotsize", 1),
                "producttype": trade.get("producttype", "INTRADAY"),
                "ordertype": trade.get("ordertype", "MARKET"),
                "exit_ordertype": trade.get("exit_ordertype", "MARKET"),
                "status": trade.get("status", "ACTIVE"),
                "order_status": trade.get("order_status", "UNKNOWN")
            }
            
            # Add current prices and P&L if available
            if current_option_price:
                trade_info["current_option_price"] = current_option_price
                
                # If we have entry price for the option, calculate P&L
                if "entry_price" in trade:
                    entry_price = trade["entry_price"]
                    pnl = (current_option_price - entry_price) * trade["quantity"]
                    pnl_percent = (current_option_price - entry_price) / entry_price * 100 if entry_price > 0 else 0
                    
                    trade_info["entry_price"] = entry_price
                    trade_info["pnl"] = pnl
                    trade_info["pnl_percent"] = pnl_percent
            
            active_trades.append(trade_info)
        
        return active_trades
    
    def check_trade_status(self, underlying_symbol=None):
        """
        Manual check for trade status against current prices
        Use this to manually check if any trades need to be exited based on price
        """
        # First reconcile with broker positions
        self.reconcile_with_broker_positions()
        
        if not self.active_option_trades or not self.active_clients:
            logger.info("No active trades or clients to check")
            return []
        
        check_client = next(iter(self.active_clients.values()))
        results = []
        
        # Filter trades if underlying symbol specified
        trades_to_check = []
        for trade_key, trade in list(self.active_option_trades.items()):
            if underlying_symbol and trade.get("underlying_symbol") != underlying_symbol:
                continue
            trades_to_check.append((trade_key, trade))
        
        if not trades_to_check:
            logger.info(f"No active trades found for {underlying_symbol}" if underlying_symbol else "No active trades found")
            return []
        
        for trade_key, trade in trades_to_check:
            symbol = trade.get("underlying_symbol", "")
            exchange = trade.get("underlying_exchange", "NSE")
            
            try:
                # Get current price of underlying
                current_price = self.price_fetcher.get_underlying_price(check_client, symbol, exchange)
                
                if current_price is None:
                    logger.warning(f"Failed to get current price for {symbol}")
                    results.append({
                        "trade_id": trade.get("trade_id", ""),
                        "symbol": trade["symbol"],
                        "status": "PRICE_CHECK_FAILED"
                    })
                    continue
                
                # Get price thresholds
                stop_loss = trade["underlying_stop_loss"]
                target = trade["underlying_target"]
                option_type = trade["option_type"]
                
                # Check if current price has hit target or stop loss
                status = "ACTIVE"  # Default status
                
                if option_type == "CE":  # Call option - bullish
                    if current_price >= target:
                        status = "TARGET_HIT"
                        logger.info(f"Target hit for {symbol} at {current_price}, {trade['symbol']} should be exited")
                    elif current_price <= stop_loss:
                        status = "STOP_LOSS_HIT"
                        logger.info(f"Stop loss hit for {symbol} at {current_price}, {trade['symbol']} should be exited")
                else:  # Put option - bearish
                    if current_price <= target:
                        status = "TARGET_HIT"
                        logger.info(f"Target hit for {symbol} at {current_price}, {trade['symbol']} should be exited")
                    elif current_price >= stop_loss:
                        status = "STOP_LOSS_HIT"
                        logger.info(f"Stop loss hit for {symbol} at {current_price}, {trade['symbol']} should be exited")
                
                # Check for expiry
                if trade.get("expiry"):
                    try:
                        expiry_date = datetime.strptime(trade["expiry"], '%d%b%Y')
                        today = datetime.now().date()
                        
                        if expiry_date.date() == today:
                            current_time = datetime.now().time()
                            # If after 3:00 PM on expiry day, the position should be exited
                            if current_time.hour >= 15 and current_time.minute >= 0:
                                status = "EXPIRY_DAY"
                                logger.info(f"Expiry day closing for {trade['symbol']}, position should be exited")
                    except Exception as e:
                        logger.warning(f"Error checking expiry for {trade['symbol']}: {str(e)}")
                
                # Get current option price for P&L calculation
                current_option_price = None
                client = self.active_clients.get(trade["client_id"])
                if client:
                    try:
                        current_option_price = self.get_option_price(client, trade["symbol"], trade["token"])
                    except:
                        pass
                
                result = {
                    "trade_id": trade.get("trade_id", ""),
                    "symbol": trade["symbol"],
                    "underlying_symbol": symbol,
                    "underlying_current_price": current_price,
                    "underlying_entry_price": trade.get("underlying_entry_price", 0),
                    "underlying_stop_loss": stop_loss,
                    "underlying_target": target,
                    "option_type": option_type,
                    "status": status
                }
                
                # Add P&L if both prices available
                if current_option_price and "entry_price" in trade:
                    entry_price = trade["entry_price"]
                    pnl = (current_option_price - entry_price) * trade["quantity"]
                    pnl_percent = (current_option_price - entry_price) / entry_price * 100 if entry_price > 0 else 0
                    
                    result["current_option_price"] = current_option_price
                    result["entry_price"] = entry_price
                    result["pnl"] = pnl
                    result["pnl_percent"] = pnl_percent
                
                results.append(result)
                
            except Exception as e:
                logger.error(f"Error checking trade {trade['symbol']}: {str(e)}")
                results.append({
                    "trade_id": trade.get("trade_id", ""),
                    "symbol": trade["symbol"],
                    "status": "CHECK_ERROR",
                    "error": str(e)
                })
        
        return results
    
    def get_completed_trades(self, limit=50):
        """Get a list of completed trades"""
        try:
            if not os.path.exists(self.completed_trades_file):
                return []
                
            with open(self.completed_trades_file, 'r') as f:
                completed_trades = json.load(f)
            
            # Sort by exit time (newest first)
            completed_trades.sort(key=lambda x: x.get("exit_time", ""), reverse=True)
            
            # Limit the number of trades returned
            return completed_trades[:limit]
        except Exception as e:
            logger.error(f"Error getting completed trades: {str(e)}")
            return []
    
    def manually_exit_trade(self, trade_id):
        """Manually exit a trade by ID"""
        try:
            # First do a reconciliation to ensure we're not trying to exit trades that no longer exist
            self.reconcile_with_broker_positions()
            
            # Find the trade by ID
            trade_key = None
            trade = None
            for key, t in self.active_option_trades.items():
                if t.get("trade_id") == trade_id:
                    trade_key = key
                    trade = t
                    break
            
            if not trade:
                logger.warning(f"Trade with ID {trade_id} not found")
                return False, "Trade not found"
            
            # Get the client
            client_id = trade["client_id"]
            client = self.active_clients.get(client_id)
            
            if not client:
                logger.warning(f"Client {client_id} not found")
                return False, "Client not found"
            
            # Exit the position
            success = self.exit_option_position(client, trade, "MANUAL_EXIT")
            
            if success:
                # Remove from active trades (should already be done in exit_option_position)
                if trade_key in self.active_option_trades:
                    self.active_option_trades.pop(trade_key, None)
                    # Save changes
                    self.save_trades_to_json()
                return True, "Trade successfully exited"
            else:
                return False, "Failed to exit trade"
            
        except Exception as e:
            logger.error(f"Error manually exiting trade {trade_id}: {str(e)}")
            return False, str(e)
    
    def _handle_order_update(self, order_data):
        """Process order status updates from WebSocket"""
        try:
            # Check if this is just the initial connection message
            order_status = order_data.get("order-status")
            if order_status == "AB00":
                logger.info("Received initial WebSocket order status connection message")
                return
                
            # Extract order details
            order_details = order_data.get("orderData", {})
            order_id = order_details.get("orderid")
            
            if not order_id:
                return
                
            # Find all trades that match this order ID (entry orders)
            matching_trade_keys = []
            
            for trade_key, trade in list(self.active_option_trades.items()):
                if trade.get("order_id") == order_id:
                    matching_trade_keys.append(trade_key)
                    logger.info(f"Order update for trade {trade['symbol']}: {order_id} - Status: {order_status}")
            
            if not matching_trade_keys:
                # Could be an exit order or untracked order
                logger.debug(f"Order update for unknown order: {order_id} - Status: {order_status}")
                return
            
            # Process order status update
            status_text = order_details.get("status", "").lower()
            
            # Update each matching trade
            for trade_key in matching_trade_keys:
                # Update order status
                self.active_option_trades[trade_key]["order_status"] = order_status
                
                if order_status in ["AB03", "AB02"]:  # Rejected, Cancelled
                    # Mark the trade as rejected/cancelled
                    self.active_option_trades[trade_key]["status"] = "REJECTED" if order_status == "AB03" else "CANCELLED"
                    self.active_option_trades[trade_key]["status_reason"] = order_details.get("text", "")
                    
                    logger.warning(f"Order {order_id} for {self.active_option_trades[trade_key]['symbol']} {status_text}: {order_details.get('text', '')}")
                
                elif order_status in ["AB05"]:  # Complete
                    # Update trade with execution details
                    avg_price = float(order_details.get("averageprice", 0))
                    filled_qty = order_details.get("filledshares", "0")
                    fill_id = order_details.get("fillid", "")
                    fill_time = order_details.get("filltime", "")
                    
                    logger.info(f"Order {order_id} for {self.active_option_trades[trade_key]['symbol']} complete: {avg_price}")
                    
                    if avg_price > 0:
                        self.active_option_trades[trade_key]["entry_price"] = avg_price
                    
                    self.active_option_trades[trade_key]["status"] = "ACTIVE"
                    self.active_option_trades[trade_key]["fill_id"] = fill_id
                    self.active_option_trades[trade_key]["fill_time"] = fill_time
                
                elif order_status in ["AB01"]:  # Open
                    # Mark as open/active
                    self.active_option_trades[trade_key]["status"] = "ACTIVE"
            
            # Save changes to the JSON file
            self.save_trades_to_json()
            
        except Exception as e:
            logger.error(f"Error processing order update: {str(e)}")
    
    def shutdown(self):
        """Safely shutdown the trade manager"""
        logger.info("Shutting down OptionsTradeManager...")
        # Save active trades
        self.save_trades_to_json()
        logger.info("OptionsTradeManager shutdown complete")

# Create a singleton instance
trade_manager = OptionsTradeManager()