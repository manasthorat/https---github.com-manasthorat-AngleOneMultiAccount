import json
import logging
import time
import threading
import struct
import websocket
from datetime import datetime

logger = logging.getLogger(__name__)

class AngelOneWebSocketManager:
    """
    WebSocket manager for Angel One API
    Handles market data streaming and order status updates
    """
    def __init__(self):
        # WebSocket connections
        self.stream_socket = None  # For market data
        self.order_socket = None   # For order updates
        
        # Connection status
        self.stream_connected = False
        self.order_connected = False
        
        # Auth data
        self.auth_token = None
        self.api_key = None
        self.client_code = None
        self.feed_token = None
        
        # Callback handlers
        self.on_market_data = None
        self.on_order_update = None
        self.on_stream_connected = None
        self.on_order_connected = None
        self.on_stream_disconnected = None
        self.on_order_disconnected = None
        
        # Threading
        self.heartbeat_thread = None
        self.reconnect_thread = None
        self.is_running = False
        
        # Subscription history (to restore after reconnection)
        self.subscriptions = []
        
        # Reconnection parameters
        self.reconnect_delay = 5  # Start with 5 seconds delay
        self.max_reconnect_delay = 300  # Max 5 minutes
        self.reconnect_attempts = 0
        self.max_reconnect_attempts = 10  # Try 10 times before giving up
        
    def initialize(self, auth_token, api_key, client_code, feed_token):
        """Initialize with authentication parameters"""
        self.auth_token = auth_token
        self.api_key = api_key
        self.client_code = client_code
        self.feed_token = feed_token
        
    def connect(self):
        """Establish both WebSocket connections"""
        if not self.auth_token or not self.api_key or not self.client_code or not self.feed_token:
            logger.error("Cannot connect: Missing authentication parameters")
            return False
            
        # Reset connection attempts
        self.reconnect_attempts = 0
        
        # Connect to WebSockets
        stream_success = self._connect_stream()
        order_success = self._connect_order_status()
        
        # Start heartbeat thread if not already running
        if not self.is_running and (stream_success or order_success):
            self.is_running = True
            self.heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
            self.heartbeat_thread.start()
            logger.info("Heartbeat thread started")
        
        return stream_success or order_success
        
    def _connect_stream(self):
        """Connect to market data stream"""
        try:
            # For browser-based approach with query params
            ws_url = f"wss://smartapisocket.angelone.in/smart-stream?clientCode={self.client_code}&feedToken={self.feed_token}&apiKey={self.api_key}"
            
            # Create WebSocket connection
            self.stream_socket = websocket.WebSocketApp(
                ws_url,
                on_open=self._on_stream_open,
                on_message=self._on_stream_message,
                on_error=self._on_stream_error,
                on_close=self._on_stream_close
            )
            
            # Start WebSocket in a thread
            thread = threading.Thread(target=self.stream_socket.run_forever, daemon=True)
            thread.start()
            logger.info("Market data WebSocket connection initiated")
            return True
            
        except Exception as e:
            logger.error(f"Error connecting to market data WebSocket: {str(e)}")
            return False
    
    def _connect_order_status(self):
        """Connect to order status WebSocket"""
        try:
            ws_url = "wss://tns.angelone.in/smart-order-update"
            headers = {
                "Authorization": f"Bearer {self.auth_token}"
            }
            
            # Create WebSocket connection
            self.order_socket = websocket.WebSocketApp(
                ws_url,
                header=headers,
                on_open=self._on_order_open,
                on_message=self._on_order_message,
                on_error=self._on_order_error,
                on_close=self._on_order_close
            )
            
            # Start WebSocket in a thread
            thread = threading.Thread(target=self.order_socket.run_forever, daemon=True)
            thread.start()
            logger.info("Order status WebSocket connection initiated")
            return True
            
        except Exception as e:
            logger.error(f"Error connecting to order status WebSocket: {str(e)}")
            return False
    
    # WebSocket callbacks for market data
    def _on_stream_open(self, ws):
        logger.info("Market data WebSocket connection established")
        self.stream_connected = True
        self.reconnect_attempts = 0  # Reset reconnect attempts
        self.reconnect_delay = 5  # Reset delay
        
        # Restore subscriptions
        self._restore_subscriptions()
        
        # Notify
        if self.on_stream_connected:
            self.on_stream_connected()
    
    def _on_stream_message(self, ws, message):
        # Check if it's a heartbeat response
        if message == "pong":
            logger.debug("Received heartbeat response from market data WebSocket")
            return
            
        try:
            # Process binary data for market updates
            if self.on_market_data:
                self.on_market_data(message)
        except Exception as e:
            logger.error(f"Error processing market data message: {str(e)}")
    
    def _on_stream_error(self, ws, error):
        logger.error(f"Market data WebSocket error: {str(error)}")
    
    def _on_stream_close(self, ws, close_status_code, close_msg):
        logger.info(f"Market data WebSocket closed: {close_status_code} - {close_msg}")
        self.stream_connected = False
        
        # Notify
        if self.on_stream_disconnected:
            self.on_stream_disconnected()
        
        # Schedule reconnection
        self._schedule_reconnect("stream")

    # WebSocket callbacks for order status
    def _on_order_open(self, ws):
        logger.info("Order status WebSocket connection established")
        self.order_connected = True
        self.reconnect_attempts = 0  # Reset reconnect attempts
        self.reconnect_delay = 5  # Reset delay
        
        # Notify
        if self.on_order_connected:
            self.on_order_connected()
    
    def _on_order_message(self, ws, message):
        try:
            # Process JSON order status update
            data = json.loads(message)
            logger.debug(f"Order update received: {data}")
            
            if self.on_order_update:
                self.on_order_update(data)
        except json.JSONDecodeError:
            # Might be a ping response or other text-based message
            logger.debug(f"Non-JSON message from order WebSocket: {message}")
        except Exception as e:
            logger.error(f"Error processing order status message: {str(e)}")
    
    def _on_order_error(self, ws, error):
        logger.error(f"Order status WebSocket error: {str(error)}")
    
    def _on_order_close(self, ws, close_status_code, close_msg):
        logger.info(f"Order status WebSocket closed: {close_status_code} - {close_msg}")
        self.order_connected = False
        
        # Notify
        if self.on_order_disconnected:
            self.on_order_disconnected()
        
        # Schedule reconnection
        self._schedule_reconnect("order")
    
    def _schedule_reconnect(self, socket_type):
        """Schedule a reconnection attempt with exponential backoff"""
        if not self.is_running:
            logger.info(f"Not scheduling reconnect for {socket_type} as manager is shutting down")
            return
            
        self.reconnect_attempts += 1
        
        if self.reconnect_attempts > self.max_reconnect_attempts:
            logger.error(f"Max reconnect attempts reached for {socket_type} WebSocket. Giving up.")
            return
        
        # Calculate delay with exponential backoff
        delay = min(self.reconnect_delay * (2 ** (self.reconnect_attempts - 1)), self.max_reconnect_delay)
        
        logger.info(f"Scheduling {socket_type} WebSocket reconnection in {delay} seconds (attempt {self.reconnect_attempts})")
        
        # Schedule reconnection
        if self.reconnect_thread and self.reconnect_thread.is_alive():
            logger.info("Reconnect thread already running, not starting another")
            return
            
        self.reconnect_thread = threading.Thread(
            target=self._reconnect_after_delay, 
            args=(socket_type, delay),
            daemon=True
        )
        self.reconnect_thread.start()
    
    def _reconnect_after_delay(self, socket_type, delay):
        """Wait and then attempt to reconnect"""
        time.sleep(delay)
        
        if not self.is_running:
            return
            
        logger.info(f"Attempting to reconnect {socket_type} WebSocket")
        
        if socket_type == "stream" and not self.stream_connected:
            self._connect_stream()
        elif socket_type == "order" and not self.order_connected:
            self._connect_order_status()
    
    def _heartbeat_loop(self):
        """Send heartbeat messages to keep connections alive"""
        while self.is_running:
            try:
                # Send heartbeat to market data socket every 30 seconds
                if self.stream_connected and self.stream_socket and self.stream_socket.sock:
                    logger.debug("Sending heartbeat to market data WebSocket")
                    self.stream_socket.send("ping")
                
                # Order status WebSocket may also need ping to stay alive
                if self.order_connected and self.order_socket and self.order_socket.sock:
                    logger.debug("Sending ping to order status WebSocket")
                    self.order_socket.send("ping")
                    
            except Exception as e:
                logger.error(f"Error sending heartbeat: {str(e)}")
            
            # Wait before next heartbeat
            time.sleep(30)  # 30 seconds between heartbeats
    
    def subscribe_market_data(self, token_list, mode=1):
        """
        Subscribe to market data for specified tokens
        mode: 1 (LTP), 2 (Quote), 3 (Snap Quote)
        token_list: List of dictionaries with exchangeType and tokens
        Example:
        [
            {
                "exchangeType": 1,  # NSE
                "tokens": ["10626", "5290"]
            },
            {
                "exchangeType": 5,  # MCX
                "tokens": ["234230", "234235"]
            }
        ]
        """
        if not self.stream_connected:
            logger.error("Cannot subscribe: Market data WebSocket not connected")
            return False
        
        try:
            # Prepare subscription request
            request = {
                "correlationID": f"req_{int(time.time())}",
                "action": 1,  # Subscribe
                "params": {
                    "mode": mode,
                    "tokenList": token_list
                }
            }
            
            # Save subscription for reconnect
            self.subscriptions.append(request)
            
            # Send subscription request
            self.stream_socket.send(json.dumps(request))
            
            # Count total tokens
            token_count = sum(len(exchange["tokens"]) for exchange in token_list)
            logger.info(f"Subscribed to {token_count} tokens in mode {mode}")
            return True
            
        except Exception as e:
            logger.error(f"Error subscribing to market data: {str(e)}")
            return False
    
    def unsubscribe_market_data(self, token_list, mode=1):
        """Unsubscribe from market data for specified tokens"""
        if not self.stream_connected:
            logger.error("Cannot unsubscribe: Market data WebSocket not connected")
            return False
        
        try:
            # Prepare unsubscription request
            request = {
                "correlationID": f"req_{int(time.time())}",
                "action": 0,  # Unsubscribe
                "params": {
                    "mode": mode,
                    "tokenList": token_list
                }
            }
            
            # Remove from subscriptions list
            for sub in self.subscriptions[:]:
                if sub["params"]["mode"] == mode:
                    # Check each token list entry
                    for req_exchange in token_list:
                        for sub_exchange in sub["params"]["tokenList"]:
                            if req_exchange["exchangeType"] == sub_exchange["exchangeType"]:
                                # Remove tokens that match
                                sub_exchange["tokens"] = [t for t in sub_exchange["tokens"] 
                                                         if t not in req_exchange["tokens"]]
                    
                    # Clean up empty token lists
                    sub["params"]["tokenList"] = [e for e in sub["params"]["tokenList"] if e["tokens"]]
                    
                    # Remove subscription if empty
                    if not sub["params"]["tokenList"]:
                        self.subscriptions.remove(sub)
            
            # Send unsubscription request
            self.stream_socket.send(json.dumps(request))
            
            # Count total tokens
            token_count = sum(len(exchange["tokens"]) for exchange in token_list)
            logger.info(f"Unsubscribed from {token_count} tokens in mode {mode}")
            return True
            
        except Exception as e:
            logger.error(f"Error unsubscribing from market data: {str(e)}")
            return False
    
    def _restore_subscriptions(self):
        """Restore previous subscriptions after reconnect"""
        if not self.subscriptions:
            logger.info("No subscriptions to restore")
            return
            
        logger.info(f"Restoring {len(self.subscriptions)} subscription requests")
        
        for request in self.subscriptions:
            try:
                # Update correlation ID
                request["correlationID"] = f"req_{int(time.time())}"
                
                # Send subscription request
                self.stream_socket.send(json.dumps(request))
                
                # Small delay to avoid overwhelming the server
                time.sleep(0.1)
            except Exception as e:
                logger.error(f"Error restoring subscription: {str(e)}")
    
    def parse_binary_market_data(self, binary_data):
        """
        Parse binary market data according to Angel One documentation
        Returns a dictionary with parsed fields
        """
        try:
            # Ensure the data is long enough for basic parsing
            if len(binary_data) < 51:  # Minimum size for LTP mode
                logger.warning(f"Binary data too short: {len(binary_data)} bytes")
                return None
                
            # Parse fields according to documentation (Little Endian)
            mode = int.from_bytes(binary_data[0:1], byteorder='little')
            exchange_type = int.from_bytes(binary_data[1:2], byteorder='little')
            
            # Extract token (null-terminated string)
            token = ""
            for i in range(2, 27):
                if i >= len(binary_data):
                    break
                byte_val = binary_data[i:i+1]
                if byte_val == b'\x00':
                    break
                token += byte_val.decode('utf-8')
            
            # Continue parsing if we have enough data
            if len(binary_data) >= 51:
                sequence_number = int.from_bytes(binary_data[27:35], byteorder='little')
                exchange_timestamp = int.from_bytes(binary_data[35:43], byteorder='little')
                ltp = int.from_bytes(binary_data[43:51], byteorder='little') / 100.0  # Convert from paise
                
                result = {
                    "mode": mode,
                    "exchange_type": exchange_type,
                    "token": token,
                    "sequence_number": sequence_number,
                    "exchange_timestamp": exchange_timestamp,
                    "ltp": ltp
                }
                
                # For Quote mode (2), parse additional fields
                if mode == 2 and len(binary_data) >= 123:
                    # Extract additional fields for Quote mode
                    result.update({
                        "last_traded_quantity": int.from_bytes(binary_data[51:59], byteorder='little'),
                        "average_traded_price": int.from_bytes(binary_data[59:67], byteorder='little') / 100.0,
                        "volume": int.from_bytes(binary_data[67:75], byteorder='little'),
                        "total_buy_quantity": struct.unpack('<d', binary_data[75:83])[0],
                        "total_sell_quantity": struct.unpack('<d', binary_data[83:91])[0],
                        "open_price": int.from_bytes(binary_data[91:99], byteorder='little') / 100.0,
                        "high_price": int.from_bytes(binary_data[99:107], byteorder='little') / 100.0,
                        "low_price": int.from_bytes(binary_data[107:115], byteorder='little') / 100.0,
                        "close_price": int.from_bytes(binary_data[115:123], byteorder='little') / 100.0
                    })
                
                # For SnapQuote mode (3), parse even more fields
                if mode == 3 and len(binary_data) >= 379:
                    # First extract all the Quote mode fields
                    if "last_traded_quantity" not in result:
                        result.update({
                            "last_traded_quantity": int.from_bytes(binary_data[51:59], byteorder='little'),
                            "average_traded_price": int.from_bytes(binary_data[59:67], byteorder='little') / 100.0,
                            "volume": int.from_bytes(binary_data[67:75], byteorder='little'),
                            "total_buy_quantity": struct.unpack('<d', binary_data[75:83])[0],
                            "total_sell_quantity": struct.unpack('<d', binary_data[83:91])[0],
                            "open_price": int.from_bytes(binary_data[91:99], byteorder='little') / 100.0,
                            "high_price": int.from_bytes(binary_data[99:107], byteorder='little') / 100.0,
                            "low_price": int.from_bytes(binary_data[107:115], byteorder='little') / 100.0,
                            "close_price": int.from_bytes(binary_data[115:123], byteorder='little') / 100.0
                        })
                    
                    # Then extract SnapQuote specific fields
                    result.update({
                        "last_traded_timestamp": int.from_bytes(binary_data[123:131], byteorder='little'),
                        "open_interest": int.from_bytes(binary_data[131:139], byteorder='little'),
                        "open_interest_change_pct": struct.unpack('<d', binary_data[139:147])[0],
                        # Best 5 data is complex and parsed separately below
                        "upper_circuit": int.from_bytes(binary_data[347:355], byteorder='little') / 100.0,
                        "lower_circuit": int.from_bytes(binary_data[355:363], byteorder='little') / 100.0,
                        "52_week_high": int.from_bytes(binary_data[363:371], byteorder='little') / 100.0,
                        "52_week_low": int.from_bytes(binary_data[371:379], byteorder='little') / 100.0
                    })
                    
                    # Parse best 5 data (10 packets, each 20 bytes)
                    best_buy = []
                    best_sell = []
                    
                    for i in range(10):
                        start_idx = 147 + (i * 20)
                        
                        if start_idx + 20 <= len(binary_data):
                            buy_sell_flag = int.from_bytes(binary_data[start_idx:start_idx+2], byteorder='little')
                            quantity = int.from_bytes(binary_data[start_idx+2:start_idx+10], byteorder='little')
                            price = int.from_bytes(binary_data[start_idx+10:start_idx+18], byteorder='little') / 100.0
                            orders = int.from_bytes(binary_data[start_idx+18:start_idx+20], byteorder='little')
                            
                            # Create best price packet
                            packet = {
                                "quantity": quantity,
                                "price": price,
                                "orders": orders
                            }
                            
                            # Add to appropriate list
                            if buy_sell_flag == 1:
                                best_buy.append(packet)
                            else:
                                best_sell.append(packet)
                    
                    result["best_buy"] = best_buy
                    result["best_sell"] = best_sell
                
                return result
            else:
                logger.warning(f"Insufficient data for parsing: {len(binary_data)} bytes")
                return None
                
        except Exception as e:
            logger.error(f"Error parsing binary market data: {str(e)}")
            return None
    
    def is_connected(self):
        """Check if either WebSocket is connected"""
        return self.stream_connected or self.order_connected
    
    def close(self):
        """Close WebSocket connections and stop threads"""
        logger.info("Closing WebSocket connections")
        self.is_running = False
        
        if self.stream_socket:
            try:
                self.stream_socket.close()
            except:
                pass
            
        if self.order_socket:
            try:
                self.order_socket.close()
            except:
                pass
            
        # Clear subscriptions
        self.subscriptions = []
        
        logger.info("WebSocket connections closed")

# Map of exchange names to WebSocket exchange type IDs
EXCHANGE_TYPE_MAP = {
    "NSE": 1,  # nse_cm
    "NFO": 2,  # nse_fo
    "BSE": 3,  # bse_cm
    "BFO": 4,  # bse_fo
    "MCX": 5,  # mcx_fo
    "NCX": 7,  # ncx_fo
    "CDS": 13  # cde_fo
}

# Map of WebSocket exchange type IDs to exchange names
EXCHANGE_NAME_MAP = {
    1: "NSE",  # nse_cm
    2: "NFO",  # nse_fo
    3: "BSE",  # bse_cm
    4: "BFO",  # bse_fo
    5: "MCX",  # mcx_fo
    7: "NCX",  # ncx_fo
    13: "CDS"  # cde_fo
}

def get_exchange_type_id(exchange_name):
    """Map exchange name to WebSocket exchange type ID"""
    return EXCHANGE_TYPE_MAP.get(exchange_name.upper())

def get_exchange_name(exchange_type_id):
    """Map WebSocket exchange type ID to exchange name"""
    return EXCHANGE_NAME_MAP.get(exchange_type_id, "UNKNOWN")

# Create a singleton instance
websocket_manager = AngelOneWebSocketManager()