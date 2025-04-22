from flask import Flask, render_template, request, jsonify
from coinbase_commerce.webhook import Client
from coinbase_commerce.webhook import Webhook
from dotenv import load_dotenv
import os
import qrcode
from io import BytesIO
import requests

load_dotenv()

app = Flask(__name__)

# Initialize Coinbase Commerce client
client = Client(api_key=os.getenv('COINBASE_API_KEY'))

# Exchange rate API endpoint
EXCHANGE_RATE_API = "https://api.exchangerate-api.com/v4/latest/USD"

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/create_payment', methods=['POST'])
def create_payment():
    try:
        amount = float(request.json.get('amount'))
        currency = request.json.get('currency', 'USD')
        
        # Create a charge
        charge = client.charge.create(
            name='Payment',
            description='Payment for goods/services',
            pricing_type='fixed_price',
            local_price={
                'amount': str(amount),
                'currency': currency
            },
            metadata={
                'customer_id': 'customer_1',
                'customer_email': 'customer@example.com'
            }
        )
        
        # Generate QR code
        qr = qrcode.QRCode(version=1, box_size=10, border=5)
        qr.add_data(charge['hosted_url'])
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        
        # Save QR code to bytes
        img_byte_arr = BytesIO()
        img.save(img_byte_arr, format='PNG')
        img_byte_arr = img_byte_arr.getvalue()
        
        return jsonify({
            'success': True,
            'payment_url': charge['hosted_url'],
            'qr_code': img_byte_arr.hex()
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/get_exchange_rate', methods=['GET'])
def get_exchange_rate():
    try:
        response = requests.get(EXCHANGE_RATE_API)
        rates = response.json()['rates']
        return jsonify({'success': True, 'rates': rates})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        event = Webhook.construct_event(
            request.data,
            request.headers.get('X-CC-Webhook-Signature'),
            os.getenv('COINBASE_WEBHOOK_SECRET')
        )
        
        if event.type == 'charge:confirmed':
            # Handle successful payment
            charge = event.data
            # Here you would typically update your database
            # and process the payment
            print(f"Payment confirmed: {charge['id']}")
        
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

if __name__ == '__main__':
    app.run(debug=True) 
