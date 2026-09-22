import os
import base64
from datetime import datetime
import requests
from dotenv import load_dotenv

load_dotenv()

consumer_key = os.getenv("CONSUMER_KEY")
consumer_secret = os.getenv("CONSUMER_SECRET")

# Get access token
oauth_url = "https://sandbox.safaricom.co.ke/oauth/v1/generate?grant_type=client_credentials"

response = requests.get(
    oauth_url,
    auth=(consumer_key, consumer_secret)
)

print("Authentication status:", response.status_code)

if response.status_code != 200:
    print(response.text)
    exit()

access_token = response.json()["access_token"]

print("Daraja authentication: SUCCESS")

# STK Push credentials
shortcode = "174379"
passkey = "bfb279f9aa9bdbcf158e97dd71a467cd2e0c893059b10f78e6b72ada1ed2c919"

timestamp = datetime.now().strftime("%Y%m%d%H%M%S")

password_string = shortcode + passkey + timestamp

password = base64.b64encode(
    password_string.encode()
).decode()

# STK Push request
stk_url = "https://sandbox.safaricom.co.ke/mpesa/stkpush/v1/processrequest"

headers = {
    "Authorization": f"Bearer {access_token}",
    "Content-Type": "application/json"
}

payload = {
    "BusinessShortCode": shortcode,
    "Password": password,
    "Timestamp": timestamp,
    "TransactionType": "CustomerPayBillOnline",
    "Amount": 1,
    "PartyA": "254708374149",
    "PartyB": shortcode,
    "PhoneNumber": "254708374149",
    "CallBackURL": "https://example.com/callback",
    "AccountReference": "TEST",
    "TransactionDesc": "Test Payment"
}

stk_response = requests.post(
    stk_url,
    json=payload,
    headers=headers
)

print("STK Push Status:", stk_response.status_code)
print("STK Push Response:")
print(stk_response.text)