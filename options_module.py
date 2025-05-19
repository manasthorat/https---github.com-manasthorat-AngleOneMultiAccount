import json
import logging
import time
import requests
import pandas as pd
from datetime import datetime, timedelta
import pyotp
from collections import Counter
from options_trade_manager import trade_manager
import concurrent.futures

logger = logging.getLogger(__name__)

class OptionsProcessor:
    def __init__(self):
        self.token_df = None  # Will store token DataFrame
        self.last_token_df_update = 0  # Last update timestamp
        self.active_clients = {}
        self.last_cache_clean = time.time()
        self.ltp_failures = 0
        self.ltp_failure_threshold = 10
        self.ltp_backoff_time = 1.0
        self.option_price_cache = {}  # Cache for option prices
        self.price_cache_ttl = 5  # Time in seconds for which cached prices are valid
        
        # Constants for option symbols
        self.INDEX_SYMBOLS = ["NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX", "MIDCPNIFTY"]
        self.INDEX_TOKENS = {
            "NSE": {
                "NIFTY": "26000",
                "BANKNIFTY": "26009",
                "FINNIFTY": "26037",
                "MIDCPNIFTY": "26074"
            },
            "BSE": {
                "SENSEX": "1"
            }
        }
    
    def initialize(self, active_clients):
        """Initialize the options processor with active client connections"""
        # Verify all clients and filter out any that fail verification
        verified_clients = {}
        for client_id, client in active_clients.items():
            verified_client, success = self.verify_client_connection(client)
            if success:
                verified_clients[client_id] = verified_client
            else:
                logger.warning(f"Client {client_id} failed verification and will be skipped")
        
        self.active_clients = verified_clients
        logger.info(f"Options processor initialized with {len(self.active_clients)} verified clients")
        
        # Initialize token map
        self.initialize_symbol_token_map()
        
        # Initialize the trade manager with verified clients and price fetching functions
        trade_manager.initialize(self.active_clients, self)
    
    def initialize_symbol_token_map(self):
        """Initialize symbol token map from Angel One OpenAPI JSON file"""
        try:
            current_time = time.time()
            
            # Only update once per day (86400 seconds)
            if self.token_df is None or (current_time - self.last_token_df_update > 86400):
                logger.info("Initializing symbol token map from Angel One OpenAPI")
                
                url = 'https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json'
                response = requests.get(url)
                
                if response.status_code == 200:
                    # Parse JSON and create DataFrame
                    self.token_df = pd.DataFrame.from_dict(response.json())
                    
                    # Process DataFrame
                    self.token_df['expiry'] = pd.to_datetime(self.token_df['expiry'], errors='coerce')
                    self.token_df['strike'] = pd.to_numeric(self.token_df['strike'], errors='coerce')
                    self.token_df = self.token_df.astype({'token': 'str'})
                    
                    self.last_token_df_update = current_time
                    logger.info(f"Successfully loaded {len(self.token_df)} symbols from OpenAPI")
                else:
                    logger.error(f"Failed to fetch symbol data: HTTP {response.status_code}")
                    if self.token_df is None:
                        # Create an empty DataFrame with required columns if we can't fetch
                        self.token_df = pd.DataFrame(columns=['token', 'symbol', 'name', 'expiry', 'strike', 
                                                            'lotsize', 'instrumenttype', 'exch_seg'])
            
            return True
        except Exception as e:
            logger.error(f"Error initializing symbol token map: {str(e)}")
            
            # Create an empty DataFrame with required columns if we had an error
            if self.token_df is None:
                self.token_df = pd.DataFrame(columns=['token', 'symbol', 'name', 'expiry', 'strike', 
                                                    'lotsize', 'instrumenttype', 'exch_seg'])
            
            return False
    
    def get_underlying_price(self, client, symbol, exchange="NSE"):
        """Get current price of an underlying asset (stock or index)"""
        try:
            logger.info(f"Getting price for {symbol} on {exchange}")
            
            # Check cache first for recent price
            cache_key = f"{exchange}:{symbol}"
            if cache_key in self.option_price_cache:
                cached_data = self.option_price_cache[cache_key]
                if time.time() - cached_data['timestamp'] < self.price_cache_ttl:
                    logger.info(f"Using cached price for {symbol}: {cached_data['price']}")
                    return cached_data['price']
            
            # For indices, use the index mapping
            if symbol.upper() in self.INDEX_SYMBOLS:
                actual_exchange = "NSE" if symbol.upper() != "SENSEX" else "BSE"
                token = self.get_index_token(symbol.upper(), actual_exchange)
                
                if not token:
                    logger.error(f"No token found for index {symbol}")
                    return None
                
                # Get LTP for index - try different client methods
                try:
                    response = client.smart_api.ltpData(actual_exchange, symbol.upper(), token)
                except AttributeError:
                    # Try direct access if smart_api access fails
                    response = client.ltpData(actual_exchange, symbol.upper(), token)
                    
                if response.get('status'):
                    price = float(response['data']['ltp'])
                    # Cache the successful result
                    self.option_price_cache[cache_key] = {'price': price, 'timestamp': time.time()}
                    logger.info(f"Got price for index {symbol}: {price}")
                    return price
                
                logger.error(f"Error getting price for index {symbol}: {response.get('message', 'Unknown error')}")
                return None
            
            # For stocks, find the token from DataFrame
            token = self.get_symbol_token(symbol, exchange)
            if not token:
                logger.error(f"No token found for {symbol}")
                return None
            
            # Get LTP for stock - try different client methods
            try:
                response = client.smart_api.ltpData(exchange, symbol, token)
            except AttributeError:
                # Try direct access if smart_api access fails
                response = client.ltpData(exchange, symbol, token)
                
            if response.get('status'):
                price = float(response['data']['ltp'])
                # Cache the successful result
                self.option_price_cache[cache_key] = {'price': price, 'timestamp': time.time()}
                logger.info(f"Got price for stock {symbol}: {price}")
                return price
            
            logger.error(f"Error getting price for {symbol}: {response.get('message', 'Unknown error')}")
            return None
            
        except Exception as e:
            logger.error(f"Exception getting price for {symbol}: {str(e)}")
            # Return a sensible default for testing
            if symbol.upper() == "NIFTY":
                return 22500
            elif symbol.upper() == "BANKNIFTY":
                return 47500
            elif symbol.upper() == "FINNIFTY":
                return 21000
            elif symbol.upper() == "MIDCPNIFTY":
                return 12000
            return None
    
    def get_index_token(self, symbol, exchange):
        """Get token for an index"""
        return self.INDEX_TOKENS.get(exchange, {}).get(symbol)
    
    def get_symbol_token(self, symbol, exchange="NSE"):
        """Get token for a symbol using the token DataFrame"""
        try:
            # For indices, use predefined mapping
            if symbol.upper() in self.INDEX_SYMBOLS:
                actual_exchange = "NSE" if symbol.upper() != "SENSEX" else "BSE"
                return self.get_index_token(symbol.upper(), actual_exchange)
            
            # For stocks, find in DataFrame
            if self.token_df is not None:
                # Filter DataFrame for the symbol and exchange
                filtered_df = self.token_df[(self.token_df['name'] == symbol) & 
                                           (self.token_df['exch_seg'] == exchange)]
                
                if not filtered_df.empty:
                    # Return the first matching token
                    return filtered_df.iloc[0]['token']
            
            logger.warning(f"Symbol {symbol} not found in token dataframe")
            return None
            
        except Exception as e:
            logger.error(f"Error getting token for symbol {symbol}: {str(e)}")
            return None
    
    def get_expiry_dates(self, client, symbol, exchange="NSE"):
        """Get available expiry dates for options using the token DataFrame"""
        try:
            logger.info(f"Getting expiry dates for {symbol} on {exchange}")
            
            # Generate fallback expiry dates (we'll use this if DataFrame lookup fails)
            fallback_dates = self._generate_fallback_expiry_dates()
            
            # Check if token_df is available
            if self.token_df is None or self.token_df.empty:
                logger.warning("Token DataFrame not available, using fallback dates")
                return fallback_dates
            
            # For indices, options are on NFO or BFO exchange
            options_exchange = "NFO"
            if symbol.upper() == "SENSEX":
                options_exchange = "BFO"
            
            # Filter DataFrame for the given symbol and instrument type (options)
            filtered_df = self.token_df[
                (self.token_df['name'] == symbol) & 
                (self.token_df['exch_seg'] == options_exchange) &
                (self.token_df['instrumenttype'].isin(['OPTSTK', 'OPTIDX']))
            ]
            
            if filtered_df.empty:
                logger.warning(f"No option contracts found for {symbol}, using fallback dates")
                return fallback_dates
            
            # Extract unique expiry dates and sort them
            expiry_dates = sorted(filtered_df['expiry'].dropna().unique())
            
            # Convert to string format
            expiry_strings = []
            for exp_date in expiry_dates:
                if pd.notna(exp_date):
                    # Format as 'DDMMMYYYY'
                    exp_str = exp_date.strftime('%d%b%Y').upper()
                    expiry_strings.append(exp_str)
            
            if not expiry_strings:
                logger.warning(f"No valid expiry dates found for {symbol}, using fallback dates")
                return fallback_dates
            
            logger.info(f"Found {len(expiry_strings)} expiry dates for {symbol}")
            return expiry_strings
            
        except Exception as e:
            logger.error(f"Error getting expiry dates for {symbol}: {str(e)}")
            # Return fallback dates
            return self._generate_fallback_expiry_dates()
    
    def _generate_fallback_expiry_dates(self):
        """Generate fallback expiry dates for testing or when API fails"""
        try:
            # Create weekly Thursday expiry for next 4 weeks
            expiry_dates = []
            
            # Get current date
            current_date = datetime.now()
            
            # Find next Thursday
            days_until_thursday = (3 - current_date.weekday()) % 7
            if days_until_thursday == 0 and current_date.hour >= 15:  # After market close
                days_until_thursday = 7
            
            next_thursday = current_date + timedelta(days=days_until_thursday)
            
            # Get next 4 Thursdays
            for i in range(4):
                expiry_date = next_thursday + timedelta(days=i*7)
                # Format as DDMMMYYYY (e.g., 25APR2024)
                expiry_str = expiry_date.strftime("%d%b%Y").upper()
                expiry_dates.append(expiry_str)
            
            logger.info(f"Generated fallback expiry dates: {expiry_dates}")
            return expiry_dates
        except Exception as e:
            logger.error(f"Error generating fallback expiry dates: {str(e)}")
            # Hardcoded fallback as last resort
            return ["25APR2025", "02MAY2025", "09MAY2025", "16MAY2025"]
    
    def calculate_moneyness(self, underlying_price, strike_price, option_type):
        """Calculate if option is ITM, ATM, or OTM"""
        try:
            price_diff_percent = abs(underlying_price - strike_price) / underlying_price * 100
            
            # Define ATM as within 0.5% of current price
            if price_diff_percent <= 0.5:
                return "ATM"
            
            if option_type == "CE":  # Call option
                if underlying_price > strike_price:
                    return "ITM"
                else:
                    return "OTM"
            else:  # Put option
                if underlying_price < strike_price:
                    return "ITM"
                else:
                    return "OTM"
        except Exception as e:
            logger.warning(f"Error calculating moneyness: {str(e)}")
            return "UNKNOWN"
    
    def _get_default_step_size(self, symbol, underlying_price):
        """Get default step size based on symbol"""
        symbol_upper = symbol.upper()
        if symbol_upper == "NIFTY" or symbol_upper == "FINNIFTY" or symbol_upper == "MIDCPNIFTY":
            return 100
        elif symbol_upper == "BANKNIFTY":
            return 500
        elif symbol_upper == "SENSEX":
            return 1000
        else:
            # For stocks, use 1% of price rounded to nearest 5
            return max(5, round(underlying_price * 0.01 / 5) * 5)
    
    def _get_option_price(self, client, options_exchange, row, max_retries=2):
        """Helper method to fetch the option price with retry logic and caching"""
        symbol = row['symbol']
        token = row['token']
        
        # Check cache first
        cache_key = f"{options_exchange}:{symbol}"
        if cache_key in self.option_price_cache:
            cached_data = self.option_price_cache[cache_key]
            if time.time() - cached_data['timestamp'] < self.price_cache_ttl:
                return cached_data['price']
        
        # Try to get live price
        for attempt in range(max_retries):
            try:
                # Get LTP data - try different access methods
                try:
                    ltp_data = client.smart_api.ltpData(options_exchange, symbol, token)
                except AttributeError:
                    # If client.smart_api.ltpData fails, try direct access
                    ltp_data = client.ltpData(options_exchange, symbol, token)
                
                if ltp_data.get('status'):
                    option_price = float(ltp_data['data']['ltp'])
                    # Cache the successful result
                    self.option_price_cache[cache_key] = {'price': option_price, 'timestamp': time.time()}
                    # Successful LTP call - decrease failure counter and backoff time
                    self.ltp_failures = max(0, self.ltp_failures - 1)
                    self.ltp_backoff_time = max(1.0, self.ltp_backoff_time / 2)
                    return option_price
                
                # If we got a response but status is False, count as failure
                self.ltp_failures += 1
                time.sleep(0.1)  # Short delay before retry
            
            except Exception as e:
                logger.warning(f"Error getting price for {symbol} (attempt {attempt+1}): {str(e)}")
                self.ltp_failures += 1
                time.sleep(0.1)  # Short delay before retry
        
        # Generate mock price if all retries fail
        return None
    
    def _batch_fetch_options(self, client, options_items, options_exchange, underlying_price, option_type):
        """Process a batch of options concurrently"""
        results = []
        max_workers = min(10, len(options_items))  # Cap maximum workers
        
        # Check if circuit breaker is active
        if self.ltp_failures >= self.ltp_failure_threshold:
            logger.warning(f"Circuit breaker active - backing off for {self.ltp_backoff_time}s")
            time.sleep(self.ltp_backoff_time)
            # Increase backoff time for next failure (up to 30s)
            self.ltp_backoff_time = min(30, self.ltp_backoff_time * 2)
            # Reset failure counter
            self.ltp_failures = 0
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = []
            for row in options_items:
                future = executor.submit(self._get_option_price, client, options_exchange, row)
                futures.append((future, row))
            
            for future, row in futures:
                try:
                    option_price = future.result()
                    
                    # If we couldn't get a real price, generate a mock one
                    if option_price is None:
                        strike = float(row['strike'])
                        if option_type == 'CE':
                            if strike < underlying_price:
                                option_price = max(0.1, underlying_price - strike + 50)
                            else:
                                option_price = max(0.1, 50 - (strike - underlying_price) * 0.1)
                        else:  # PE
                            if strike > underlying_price:
                                option_price = max(0.1, strike - underlying_price + 50)
                            else:
                                option_price = max(0.1, 50 - (underlying_price - strike) * 0.1)
                        logger.warning(f"Using mock price for {row['symbol']} after failed API calls")
                    
                    # Calculate moneyness
                    moneyness = self.calculate_moneyness(underlying_price, float(row['strike']), option_type)
                    
                    # Add to results list
                    results.append({
                        'symbol': row['symbol'],
                        'token': row['token'],
                        'expiry': row.get('expiry_str', ''),  # Added this field in filtered DFs
                        'strike_price': float(row['strike']),
                        'option_type': option_type,
                        'last_price': round(option_price, 2),
                        'moneyness': moneyness,
                        'lotsize': int(row['lotsize'])
                    })
                    
                except Exception as e:
                    logger.error(f"Error processing {option_type} option {row['symbol']}: {str(e)}")
        
        return results
    
    def fetch_options_for_expiry(self, client, symbol, expiry, underlying_price, exchange):
        """Fetch option chain for a specific expiry date using the token DataFrame - optimized to fetch only strikes near ATM"""
        try:
            logger.info(f"Fetching options for {symbol} with expiry {expiry}")
            
            # Clean up expired cache entries occasionally
            current_time = time.time()
            if current_time - self.last_cache_clean > 300:  # Every 5 minutes
                self._clean_price_cache()
                self.last_cache_clean = current_time
            
            # Check if token_df is available
            if self.token_df is None or self.token_df.empty:
                logger.warning("Token DataFrame not available, using mock data")
                calls, puts = self._create_mock_options(symbol, expiry, underlying_price, num_strikes=6)
                return {'calls': calls, 'puts': puts}
            
            # Determine which exchange to use for options
            options_exchange = "NFO"
            if symbol.upper() == "SENSEX":
                options_exchange = "BFO"
            
            # Convert expiry string to datetime for filtering
            try:
                expiry_date = pd.to_datetime(expiry, format='%d%b%Y')
            except:
                logger.error(f"Invalid expiry format: {expiry}")
                calls, puts = self._create_mock_options(symbol, expiry, underlying_price, num_strikes=6)
                return {'calls': calls, 'puts': puts}
            
            # Filter DataFrame for options of the symbol with the given expiry
            option_chain_df = self.token_df[
                (self.token_df['exch_seg'] == options_exchange) &
                (self.token_df['instrumenttype'].isin(['OPTSTK', 'OPTIDX'])) &
                (self.token_df['name'] == symbol) &
                (self.token_df['expiry'] == expiry_date)
            ]
            
            # Divide the strike prices by 100
            option_chain_df = option_chain_df.copy()
            option_chain_df.loc[:, 'strike'] = option_chain_df['strike'] / 100
            
            if option_chain_df.empty:
                logger.warning(f"No options found for {symbol} expiry {expiry}, using mock data")
                calls, puts = self._create_mock_options(symbol, expiry, underlying_price, num_strikes=6)
                return {'calls': calls, 'puts': puts}
            
            # Split into calls and puts
            calls_df = option_chain_df[option_chain_df['symbol'].str.contains('CE')]
            puts_df = option_chain_df[option_chain_df['symbol'].str.contains('PE')]
            
            logger.info(f"Found {len(calls_df)} calls and {len(puts_df)} puts in DataFrame")
            
            # Determine step size based on symbol and actual data
            all_strikes = sorted(option_chain_df['strike'].unique())
            
            # Calculate step size from the dataset if possible
            if len(all_strikes) >= 2:
                diffs = [all_strikes[i+1] - all_strikes[i] for i in range(len(all_strikes)-1)]
                # Find most common difference
                step_counter = Counter(diffs)
                if step_counter:
                    common_step = step_counter.most_common(1)[0][0]
                    step = common_step
                    logger.info(f"Dynamically determined step size: {step} from strike price data")
                else:
                    # Use symbol-based logic as fallback
                    step = self._get_default_step_size(symbol, underlying_price)
                    logger.info(f"Using default step size: {step} for {symbol}")
            else:
                # Use symbol-based logic as fallback
                step = self._get_default_step_size(symbol, underlying_price)
                logger.info(f"Using default step size: {step} for {symbol}")

            # Calculate ATM strike (round to nearest step)
            atm_strike = round(underlying_price / step) * step
            
            # Determine strikes to fetch (6 above and 6 below ATM)
            num_strikes = 6  # Number of strikes above and below ATM
            target_strikes = []
            for i in range(-num_strikes, num_strikes + 1):
                strike = atm_strike + (i * step)
                if strike > 0:  # Skip negative strikes
                    target_strikes.append(strike)
            
            # Filter calls and puts for target strikes only
            filtered_calls_df = calls_df[calls_df['strike'].isin(target_strikes)]
            filtered_puts_df = puts_df[puts_df['strike'].isin(target_strikes)]
            
            # Add expiry string for each row (needed for parallel processing)
            filtered_calls_df['expiry_str'] = expiry
            filtered_puts_df['expiry_str'] = expiry
            
            logger.info(f"Filtered to {len(filtered_calls_df)} calls and {len(filtered_puts_df)} puts around ATM strike {atm_strike}")
            
            # Process calls and puts in parallel batches
            calls = self._batch_fetch_options(client, filtered_calls_df.to_dict('records'), 
                                             options_exchange, underlying_price, 'CE')
            puts = self._batch_fetch_options(client, filtered_puts_df.to_dict('records'), 
                                            options_exchange, underlying_price, 'PE')
            
            # Sort by strike price
            calls = sorted(calls, key=lambda x: x['strike_price'])
            puts = sorted(puts, key=lambda x: x['strike_price'])
            
            # If no options found with prices, create mock data
            if not calls and not puts:
                logger.warning(f"No options found with prices for {symbol}/{expiry}, creating mock data")
                calls, puts = self._create_mock_options(symbol, expiry, underlying_price, num_strikes=6)
            
            logger.info(f"Prepared {len(calls)} calls and {len(puts)} puts for {symbol}/{expiry}")
            return {'calls': calls, 'puts': puts}
            
        except Exception as e:
            logger.error(f"Error fetching options for {symbol}/{expiry}: {str(e)}")
            # Return mock data for testing/fallback
            calls, puts = self._create_mock_options(symbol, expiry, underlying_price, num_strikes=6)
            return {'calls': calls, 'puts': puts}
    
    def _clean_price_cache(self):
        """Clean up expired cache entries"""
        current_time = time.time()
        expired_keys = []
        
        for key, entry in self.option_price_cache.items():
            if current_time - entry['timestamp'] > self.price_cache_ttl:
                expired_keys.append(key)
        
        for key in expired_keys:
            self.option_price_cache.pop(key, None)
        
        if expired_keys:
            logger.info(f"Cleaned {len(expired_keys)} expired entries from price cache")

 



    def get_option_contract(self, client, symbol, expiry, strike, option_type):
        """Get a specific option contract by symbol, expiry, strike and type with dynamic step calculation"""
        try:
            logger.info(f"Looking for option contract: {symbol} {expiry} {strike} {option_type}")
            
            # Check if token_df is available
            if self.token_df is None or self.token_df.empty:
                logger.warning("Token DataFrame not available, using constructed data")
                return self._construct_mock_option_contract(symbol, expiry, strike, option_type)
            
            # Determine which exchange to use for options
            options_exchange = "NFO"
            if symbol.upper() == "SENSEX":
                options_exchange = "BFO"
            
            # Convert expiry string to datetime for filtering
            try:
                expiry_date = pd.to_datetime(expiry, format='%d%b%Y')
            except:
                logger.error(f"Invalid expiry format: {expiry}")
                return self._construct_mock_option_contract(symbol, expiry, strike, option_type)
            
            # First, let's get all options for this symbol and expiry to determine the correct step size
            option_chain_df = self.token_df[
                (self.token_df['exch_seg'] == options_exchange) &
                (self.token_df['instrumenttype'].isin(['OPTSTK', 'OPTIDX'])) &
                (self.token_df['name'] == symbol) &
                (self.token_df['expiry'] == expiry_date)
            ]
            
            # Divide the strike prices by 100 (since they're stored multiplied by 100)
            option_chain_df['strike'] = option_chain_df['strike'] / 100
            
            if not option_chain_df.empty:
                # Get all strikes and sort them
                all_strikes = sorted(option_chain_df['strike'].unique())
                
                # Dynamically determine step size if possible
                step = None
                if len(all_strikes) >= 2:
                    # Calculate differences between adjacent strikes
                    diffs = [all_strikes[i+1] - all_strikes[i] for i in range(len(all_strikes)-1)]
                    # Find most common difference using Counter
                    from collections import Counter
                    step_counter = Counter(diffs)
                    if step_counter:
                        # Get the most common step size
                        common_step = step_counter.most_common(1)[0][0]
                        step = common_step
                        logger.info(f"Dynamically determined step size: {step} for {symbol}")
                        
                        # Find the nearest valid strike based on the actual available strikes
                        nearest_strike = min(all_strikes, key=lambda x: abs(x - float(strike)))
                        logger.info(f"Adjusted to nearest available strike: {nearest_strike} (original: {strike})")
                        
                        # Update strike to nearest valid value
                        strike = nearest_strike
            
            # Ensure strike is a number
            try:
                strike_value = float(strike)
            except (ValueError, TypeError):
                logger.error(f"Invalid strike value: {strike}")
                return self._construct_mock_option_contract(symbol, expiry, strike, option_type)
                
            # Now filter for the specific option contract
            contract_df = self.token_df[
                (self.token_df['exch_seg'] == options_exchange) &
                (self.token_df['instrumenttype'].isin(['OPTSTK', 'OPTIDX'])) &
                (self.token_df['name'] == symbol) &
                (self.token_df['expiry'] == expiry_date) &
                (self.token_df['strike'] == strike_value * 100) &  # Multiply by 100 for internal representation
                (self.token_df['symbol'].str.contains(option_type))
            ]
            
            if contract_df.empty:
                logger.warning(f"No option contract found for {symbol} {expiry} {strike} {option_type}")
                return self._construct_mock_option_contract(symbol, expiry, strike, option_type)
            
            # Get the first contract (should be only one)
            contract = contract_df.iloc[0]
            
            # Get lotsize safely
            lotsize = 1
            if 'lotsize' in contract:
                try:
                    lotsize = int(contract['lotsize'])
                except (ValueError, TypeError):
                    lotsize = self._get_default_lot_size(symbol)
            else:
                lotsize = self._get_default_lot_size(symbol)
                
            # Create the contract object
            option_contract = {
                "symbol": contract['symbol'],
                "token": str(contract['token']),  # Ensure token is a string
                "strike_price": strike_value,
                "option_type": option_type,
                "expiry": expiry,
                "lotsize": lotsize
            }
            
            return option_contract
        except Exception as e:
            logger.error(f"Error getting option contract: {str(e)}")
            return self._construct_mock_option_contract(symbol, expiry, strike, option_type)

    def _construct_mock_option_contract(self, symbol, expiry, strike, option_type):
        """Create a mock option contract when actual data is not available"""
        # Format example: NIFTY24MAY18500CE
        symbol_base = symbol.upper()
        
        # Convert expiry from format like "30May2024" to "24MAY30"
        try:
            exp_date = datetime.strptime(expiry, '%d%b%Y')
            exp_formatted = f"{exp_date.strftime('%y%b').upper()}{exp_date.day}"
        except:
            logger.warning(f"Could not parse expiry {expiry}, using as is")
            exp_formatted = expiry
        
        # Format the strike price - remove decimal for integers
        strike_formatted = str(int(strike)) if strike == int(strike) else str(strike)
        
        # Construct the option symbol
        option_symbol = f"{symbol_base}{exp_formatted}{strike_formatted}{option_type}"
        
        # Create a mock token - this is just for testing, real code should get actual tokens
        mock_token = abs(hash(option_symbol)) % 10000000
        
        return {
            "symbol": option_symbol,
            "token": str(mock_token),
            "strike_price": strike,
            "option_type": option_type,
            "expiry": expiry,
            "lotsize": self._get_default_lot_size(symbol)
        }

    def _get_default_lot_size(self, symbol):
        """Get default lot size for common indices/stocks"""
        symbol = symbol.upper()
        if symbol == "NIFTY":
            return 50
        elif symbol in ["BANKNIFTY", "BANKEX"]:
            return 25
        elif symbol == "FINNIFTY":
            return 40
        elif symbol == "SENSEX":
            return 10
        else:
            return 1  # Default for stocks





    def _create_mock_options(self, symbol, expiry, underlying_price, num_strikes=6):
        """Create mock option data for testing or when API fails"""
        try:
            calls = []
            puts = []
            
            # Get step size based on symbol
            step = self._get_default_step_size(symbol, underlying_price)
            
            # Determine lot size based on symbol
            lot_size = 50  # Default
            if symbol.upper() == "NIFTY":
                lot_size = 50
            elif symbol.upper() == "BANKNIFTY":
                lot_size = 15
            elif symbol.upper() == "MIDCPNIFTY":
                lot_size = 75
            elif symbol.upper() == "SENSEX":
                lot_size = 10
            else:
                lot_size = 1000  # Default lot size for stocks
            
            # Calculate ATM strike (round to nearest step)
            atm_strike = round(underlying_price / step) * step
            
            # Generate strikes above and below ATM
            for i in range(-num_strikes, num_strikes + 1):
                strike = atm_strike + (i * step)
                
                # Skip negative strikes
                if strike <= 0:
                    continue
                
                # Create call option
                call_price = 0.0
                if strike < underlying_price:
                    call_price = max(0.1, underlying_price - strike + 50)
                else:
                    call_price = max(0.1, 50 - (strike - underlying_price) * 0.1)
                
                # Calculate moneyness
                call_moneyness = self.calculate_moneyness(underlying_price, strike, "CE")
                
                # Create call token with special format
                call_token = f"1{str(int(strike)).zfill(5)}1"
                
                # Format expiry part for symbol (3-letter month + 2-digit day)
                expiry_date = datetime.strptime(expiry, '%d%b%Y')
                expiry_short = expiry_date.strftime("%b%d").upper()
                
                # Create call symbol
                call_symbol = f"{symbol}{expiry_short}CE{int(strike)}"
                
                calls.append({
                    'symbol': call_symbol,
                    'token': call_token,
                    'expiry': expiry,
                    'strike_price': strike,
                    'option_type': 'CE',
                    'last_price': round(call_price, 2),
                    'moneyness': call_moneyness,
                    'lotsize': lot_size
                })
                
                # Create put option
                put_price = 0.0
                if strike > underlying_price:
                    put_price = max(0.1, strike - underlying_price + 50)
                else:
                    put_price = max(0.1, 50 - (underlying_price - strike) * 0.1)
                
                # Calculate moneyness
                put_moneyness = self.calculate_moneyness(underlying_price, strike, "PE")
                
                # Create put token with special format
                put_token = f"1{str(int(strike)).zfill(5)}2"
                
                # Create put symbol
                put_symbol = f"{symbol}{expiry_short}PE{int(strike)}"
                
                puts.append({
                    'symbol': put_symbol,
                    'token': put_token,
                    'expiry': expiry,
                    'strike_price': strike,
                    'option_type': 'PE',
                    'last_price': round(put_price, 2),
                    'moneyness': put_moneyness,
                    'lotsize': lot_size
                })
            
            return calls, puts
            
        except Exception as e:
            logger.error(f"Error creating mock options: {str(e)}")
            return [], []
    
    def get_fno_symbols(self, client):
        """Get list of all F&O symbols using the token DataFrame"""
        try:
            # Start with indices
            fno_symbols = [
                {"symbol": "NIFTY", "name": "Nifty 50", "exchange": "NSE", "type": "Index"},
                {"symbol": "BANKNIFTY", "name": "Bank Nifty", "exchange": "NSE", "type": "Index"},
                {"symbol": "FINNIFTY", "name": "Financial Services Nifty", "exchange": "NSE", "type": "Index"},
                {"symbol": "MIDCPNIFTY", "name": "Midcap Nifty", "exchange": "NSE", "type": "Index"},
                {"symbol": "SENSEX", "name": "BSE Sensex", "exchange": "BSE", "type": "Index"}
            ]

            # Check if token_df is available
            if self.token_df is None or self.token_df.empty:
                logger.warning("Token DataFrame not available, using default list of symbols")
                return fno_symbols
            
            # Get unique stock option symbols from NFO
            try:
                # Filter for OPTSTK (stock options) only
                stock_options = self.token_df[(self.token_df['exch_seg'] == 'NFO') & 
                                             (self.token_df['instrumenttype'] == 'OPTSTK')]
                
                # Get unique stock names
                unique_stocks = stock_options['name'].unique()
                
                # Add to symbols list
                for stock in unique_stocks:
                    # Skip if already in the list
                    if any(s['symbol'] == stock for s in fno_symbols):
                        continue
                        
                    fno_symbols.append({
                        "symbol": stock,
                        "name": stock,
                        "exchange": "NSE",
                        "type": "Stock"
                    })
            except Exception as e:
                logger.warning(f"Error getting stock options from DataFrame: {str(e)}")
                # Add some default stocks as fallback
                default_stocks = [
                    "RELIANCE", "TCS", "HDFCBANK", "INFY", "HINDUNILVR", "ICICIBANK", "ITC", "KOTAKBANK",
                    "AXISBANK", "SBIN", "BAJFINANCE", "BHARTIARTL", "ASIANPAINT", "MARUTI", "HCLTECH",
                    "ADANIPORTS", "M&M", "TITAN", "ULTRACEMCO", "SUNPHARMA", "TATASTEEL", "JSWSTEEL", 
                    "HINDALCO", "NTPC", "POWERGRID", "ONGC", "COALINDIA", "BPCL", "WIPRO", "TATAMOTORS"
                ]
                
                for stock in default_stocks:
                    # Skip if already in the list
                    if any(s['symbol'] == stock for s in fno_symbols):
                        continue
                        
                    fno_symbols.append({
                        "symbol": stock,
                        "name": stock,
                        "exchange": "NSE",
                        "type": "Stock"
                    })
            
            logger.info(f"Prepared list of {len(fno_symbols)} F&O symbols")
            return fno_symbols
            
        except Exception as e:
            logger.error(f"Error getting F&O symbols: {str(e)}")
            # Return just indices as fallback
            return [
                {"symbol": "NIFTY", "name": "Nifty 50", "exchange": "NSE", "type": "Index"},
                {"symbol": "BANKNIFTY", "name": "Bank Nifty", "exchange": "NSE", "type": "Index"},
                {"symbol": "FINNIFTY", "name": "Financial Services Nifty", "exchange": "NSE", "type": "Index"},
                {"symbol": "MIDCPNIFTY", "name": "Midcap Nifty", "exchange": "NSE", "type": "Index"}
            ]



    def process_option_signal(self, webhook_data, active_clients=None):
        """Process a trading signal for options - delegate to trade manager"""
        # Use current active clients if none provided
        if active_clients:
            # Update active clients temporarily for this operation
            original_clients = self.active_clients
            self.active_clients = active_clients
            
            # Call trade manager with self as price fetcher
            result = trade_manager.process_option_signal(webhook_data, self)
            
            # Restore original clients
            self.active_clients = original_clients
            return result
        else:
            # Just use existing clients
            return trade_manager.process_option_signal(webhook_data, self)

        
    def get_active_option_trades(self):
        """Get list of active option trades - delegate to trade manager"""
        return trade_manager.get_active_option_trades()

    def verify_client_connection(self, client):
        """Verify that the client connection is working and return a properly configured client"""
        try:
            logger.info("Verifying client connection")
            
            # Check if client has the correct structure
            if hasattr(client, 'smart_api') and hasattr(client.smart_api, 'ltpData'):
                # Try to get the feed token as a test
                try:
                    feed_token = client.smart_api.getfeedToken()
                    if feed_token:
                        logger.info("Client connection verified with smart_api structure")
                        return client, True
                except Exception as e:
                    logger.warning(f"Error with smart_api structure: {str(e)}")
            
            # Check if client itself has the methods (like in test2.py)
            if hasattr(client, 'ltpData'):
                # Try a test call
                try:
                    # Test with Nifty token
                    test_result = client.ltpData("NSE", "NIFTY", "26000")
                    if test_result and test_result.get('status'):
                        logger.info("Client connection verified with direct structure")
                        # Create a wrapper so we can use consistent access pattern
                        class ClientWrapper:
                            def __init__(self, direct_client):
                                self.smart_api = direct_client
                        
                        return ClientWrapper(client), True
                except Exception as e:
                    logger.warning(f"Error with direct structure: {str(e)}")
            
            # If we get here, neither approach worked
            logger.error("Client verification failed - client structure not recognized")
            return client, False
            
        except Exception as e:
            logger.error(f"Error verifying client connection: {str(e)}")
            return client, False


# Create a singleton instance
options_processor = OptionsProcessor()