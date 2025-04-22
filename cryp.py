# Import necessary libraries
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from coinbase_commerce.client import Client
from coinbase_commerce.webhook import Webhook
from dotenv import load_dotenv
import os
import qrcode
from io import BytesIO
import requests
from datetime import datetime, timedelta
import json
from functools import wraps
import pandas as pd
import logging
from logging.handlers import RotatingFileHandler
import jwt
import hashlib
import time
from typing import Dict, List, Optional, Union
from dataclasses import dataclass
from enum import Enum

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        RotatingFileHandler('app.log', maxBytes=1000000, backupCount=5),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Initialize Flask app
app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY', 'your-secret-key-here')

# Initialize Coinbase Commerce client
client = Client(api_key=os.getenv('COINBASE_API_KEY'))

# Exchange rate API configuration
EXCHANGE_RATE_API = "https://api.exchangerate-api.com/v4/latest/USD"
CACHE_DURATION = 3600  # Cache duration in seconds (1 hour)

# Define enums for better type safety
class PaymentStatus(Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    EXPIRED = "expired"

class CurrencyType(Enum):
    FIAT = "fiat"
    CRYPTO = "crypto"

# Data class for payment information
@dataclass
class PaymentInfo:
    id: str
    amount: float
    currency: str
    crypto: str
    status: PaymentStatus
    timestamp: datetime
    payment_url: str
    exchange_rate: Optional[float] = None
    customer_email: Optional[str] = None
    description: Optional[str] = None

# Cache for exchange rates
exchange_rate_cache = {
    'rates': {},
    'timestamp': 0
}

# Rate limiting configuration
RATE_LIMIT = {
    'requests': 100,  # Maximum requests
    'window': 3600,   # Time window in seconds (1 hour)
    'ip_requests': {}  # Track requests per IP
}

# Authentication decorator
def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get('Authorization')
        if not token:
            return jsonify({'message': 'Token is missing'}), 401
        try:
            data = jwt.decode(token, app.secret_key, algorithms=['HS256'])
            current_user = data['user']
        except:
            return jsonify({'message': 'Token is invalid'}), 401
        return f(current_user, *args, **kwargs)
    return decorated

# Rate limiting decorator
def rate_limit(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        ip = request.remote_addr
        current_time = time.time()
        
        # Clean up old entries
        RATE_LIMIT['ip_requests'] = {
            ip: timestamp for ip, timestamp in RATE_LIMIT['ip_requests'].items()
            if current_time - timestamp < RATE_LIMIT['window']
        }
        
        # Check rate limit
        if ip in RATE_LIMIT['ip_requests']:
            if len(RATE_LIMIT['ip_requests'][ip]) >= RATE_LIMIT['requests']:
                return jsonify({'error': 'Rate limit exceeded'}), 429
            RATE_LIMIT['ip_requests'][ip].append(current_time)
        else:
            RATE_LIMIT['ip_requests'][ip] = [current_time]
        
        return f(*args, **kwargs)
    return decorated

# Function to get exchange rates with caching
def get_exchange_rates() -> Dict[str, float]:
    current_time = time.time()
    if current_time - exchange_rate_cache['timestamp'] < CACHE_DURATION:
        return exchange_rate_cache['rates']
    
    try:
        response = requests.get(EXCHANGE_RATE_API)
        rates = response.json()['rates']
        exchange_rate_cache['rates'] = rates
        exchange_rate_cache['timestamp'] = current_time
        return rates
    except Exception as e:
        logger.error(f"Error fetching exchange rates: {str(e)}")
        return exchange_rate_cache['rates']  # Return cached rates if available

# Function to load payment history with error handling
def load_payment_history() -> List[Dict]:
    try:
        with open('payment_history.json', 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        return []
    except json.JSONDecodeError:
        logger.error("Error decoding payment history file")
        return []
    except Exception as e:
        logger.error(f"Error loading payment history: {str(e)}")
        return []

# Function to save payment history with error handling
def save_payment_history(history: List[Dict]) -> bool:
    try:
        with open('payment_history.json', 'w') as f:
            json.dump(history, f, default=str)
        return True
    except Exception as e:
        logger.error(f"Error saving payment history: {str(e)}")
        return False

# Function to validate payment amount
def validate_payment_amount(amount: float, currency: str) -> bool:
    try:
        amount = float(amount)
        if amount <= 0:
            return False
        # Add currency-specific validation if needed
        return True
    except (ValueError, TypeError):
        return False

# Function to generate payment hash for security
def generate_payment_hash(payment_info: PaymentInfo) -> str:
    data = f"{payment_info.id}{payment_info.amount}{payment_info.currency}{payment_info.crypto}"
    return hashlib.sha256(data.encode()).hexdigest()

# Route to get supported currencies and cryptos
@app.route('/get_currencies', methods=['GET'])
@rate_limit
def get_currencies():
    try:
        return jsonify({
            'success': True,
            'currencies': SUPPORTED_CURRENCIES,
            'cryptos': SUPPORTED_CRYPTOS
        })
    except Exception as e:
        logger.error(f"Error in get_currencies: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

# Route to create a new payment
@app.route('/create_payment', methods=['POST'])
@rate_limit
def create_payment():
    try:
        # Validate request data
        data = request.json
        amount = float(data.get('amount', 0))
        currency = data.get('currency', 'USD')
        crypto = data.get('crypto', 'BTC')
        customer_email = data.get('customer_email')
        description = data.get('description', f'Payment in {SUPPORTED_CRYPTOS[crypto]["name"]}')

        # Validate inputs
        if not validate_payment_amount(amount, currency):
            return jsonify({'success': False, 'error': 'Invalid amount'}), 400
        
        if crypto not in SUPPORTED_CRYPTOS:
            return jsonify({'success': False, 'error': 'Unsupported cryptocurrency'}), 400
        
        if currency not in SUPPORTED_CURRENCIES:
            return jsonify({'success': False, 'error': 'Unsupported currency'}), 400

        # Get current exchange rate
        exchange_rates = get_exchange_rates()
        exchange_rate = exchange_rates.get(currency, 1.0)

        # Create payment charge
        charge = client.charge.create(
            name='Payment',
            description=description,
            pricing_type='fixed_price',
            local_price={
                'amount': str(amount),
                'currency': currency
            },
            metadata={
                'customer_id': hashlib.md5(customer_email.encode()).hexdigest() if customer_email else 'anonymous',
                'customer_email': customer_email,
                'crypto': crypto,
                'currency': currency,
                'exchange_rate': exchange_rate
            }
        )

        # Generate QR code
        qr = qrcode.QRCode(
            version=1,
            box_size=10,
            border=5,
            error_correction=qrcode.constants.ERROR_CORRECT_L
        )
        qr.add_data(charge['hosted_url'])
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        
        # Save QR code to bytes
        img_byte_arr = BytesIO()
        img.save(img_byte_arr, format='PNG')
        img_byte_arr = img_byte_arr.getvalue()

        # Create payment info
        payment_info = PaymentInfo(
            id=charge['id'],
            amount=amount,
            currency=currency,
            crypto=crypto,
            status=PaymentStatus.PENDING,
            timestamp=datetime.now(),
            payment_url=charge['hosted_url'],
            exchange_rate=exchange_rate,
            customer_email=customer_email,
            description=description
        )

        # Save payment to history
        history = load_payment_history()
        history.append(payment_info.__dict__)
        save_payment_history(history)

        return jsonify({
            'success': True,
            'payment_url': charge['hosted_url'],
            'qr_code': img_byte_arr.hex(),
            'payment_info': payment_info.__dict__
        })

    except Exception as e:
        logger.error(f"Error in create_payment: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

# Route to get payment history
@app.route('/get_payment_history', methods=['GET'])
@rate_limit
@token_required
def get_payment_history(current_user):
    try:
        history = load_payment_history()
        return jsonify({
            'success': True,
            'history': history
        })
    except Exception as e:
        logger.error(f"Error in get_payment_history: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

# Route to handle webhook events
@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        # Verify webhook signature
        event = Webhook.construct_event(
            request.data,
            request.headers.get('X-CC-Webhook-Signature'),
            os.getenv('COINBASE_WEBHOOK_SECRET')
        )
        
        if event.type == 'charge:confirmed':
            # Handle successful payment
            charge = event.data
            history = load_payment_history()
            
            # Update payment status
            for payment in history:
                if payment['id'] == charge['id']:
                    payment['status'] = PaymentStatus.COMPLETED.value
                    payment['completed_at'] = datetime.now().isoformat()
                    break
            
            save_payment_history(history)
            logger.info(f"Payment confirmed: {charge['id']}")
        
        return jsonify({'success': True})
    except Exception as e:
        logger.error(f"Error in webhook: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

# Route to get exchange rates
@app.route('/get_exchange_rate', methods=['GET'])
@rate_limit
def get_exchange_rate():
    try:
        rates = get_exchange_rates()
        return jsonify({
            'success': True,
            'rates': rates,
            'timestamp': exchange_rate_cache['timestamp']
        })
    except Exception as e:
        logger.error(f"Error in get_exchange_rate: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

# Route to get cryptocurrency rates
@app.route('/get_crypto_rates', methods=['GET'])
@rate_limit
def get_crypto_rates():
    try:
        # This is a placeholder. In production, you would fetch real-time crypto rates
        # from a reliable API like Coinbase Pro or CoinGecko
        rates = {
            'BTC': 50000.0,
            'ETH': 3000.0,
            'USDC': 1.0,
            'LTC': 150.0,
            'BCH': 500.0,
            'XRP': 0.5,
            'DOGE': 0.1,
            'DOT': 20.0,
            'ADA': 1.5,
            'SOL': 100.0,
            'AVAX': 50.0,
            'MATIC': 2.0,
            'LINK': 20.0,
            'UNI': 10.0,
            'AAVE': 200.0
        }
        return jsonify({'success': True, 'rates': rates})
    except Exception as e:
        logger.error(f"Error in get_crypto_rates: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

# Main route
@app.route('/')
def home():
    return render_template('index.html')

if __name__ == '__main__':
    app.run(debug=True)
