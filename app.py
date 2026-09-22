from flask import Flask, render_template, request, jsonify
import os
import base64
from datetime import datetime
import requests
from dotenv import load_dotenv
import sqlite3

load_dotenv()

app = Flask(__name__)

def init_db():
    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_name TEXT,
            phone TEXT,
            amount REAL,
            description TEXT,
            checkout_request_id TEXT,
            merchant_request_id TEXT,
            mpesa_receipt_number TEXT,
            transaction_date TEXT,
            status TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS pending_payments (
            checkout_request_id TEXT PRIMARY KEY,
            customer_name TEXT,
            phone TEXT,
            amount REAL,
            description TEXT
        )
    """)

    conn.commit()
    conn.close()

def get_access_token():
    url = "https://sandbox.safaricom.co.ke/oauth/v1/generate?grant_type=client_credentials"

    response = requests.get(
        url,
        auth=(
            os.getenv("CONSUMER_KEY"),
            os.getenv("CONSUMER_SECRET")
        )
    )

    response.raise_for_status()

    return response.json()["access_token"]


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/pay", methods=["POST"])
def pay():
    data = request.get_json()

    phone = data.get("phone")
    amount = data.get("amount")
    description = data.get("description")

    if not phone or not amount:
        return jsonify({
            "success": False,
            "message": "Phone number and amount are required."
        }), 400

    phone = phone.replace(" ", "").replace("+", "")

    if phone.startswith("0"):
        phone = "254" + phone[1:]

    if not phone.startswith("254"):
        return jsonify({
            "success": False,
            "message": "Enter a valid Kenyan phone number."
        }), 400

    try:
        amount = int(float(amount))
    except ValueError:
        return jsonify({
            "success": False,
            "message": "Enter a valid amount."
        }), 400

    try:
        access_token = get_access_token()

        shortcode = os.getenv("MPESA_SHORTCODE")
        passkey = os.getenv("MPESA_PASSKEY")

        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")

        password_string = shortcode + passkey + timestamp
        password = base64.b64encode(
            password_string.encode()
        ).decode()

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
            "Amount": amount,
            "PartyA": phone,
            "PartyB": shortcode,
            "PhoneNumber": phone,
            "CallBackURL": "https://foothill-profound-winter.ngrok-free.dev/callback",
            "AccountReference": "PAYMENT",
            "TransactionDesc": description or "Payment"
        }

        response = requests.post(
            stk_url,
            json=payload,
            headers=headers
        )

        result = response.json()

        if result.get("ResponseCode") == "0":
            checkout_request_id = result.get("CheckoutRequestID")

            conn = sqlite3.connect("database.db")
            cursor = conn.cursor()

            cursor.execute("""
                INSERT OR REPLACE INTO pending_payments (
                    checkout_request_id,
                    customer_name,
                    phone,
                    amount,
                    description
                )
                VALUES (?, ?, ?, ?, ?)
            """, (
                checkout_request_id,
                data.get("customer_name"),
                phone,
                amount,
                description
            ))

            conn.commit()
            conn.close()

        return jsonify(result), response.status_code

    except Exception as e:
        return jsonify({
            "success": False,
            "message": "Payment request failed.",
            "error": str(e)
        }), 500


@app.route("/callback", methods=["GET", "POST"])
def callback():

    if request.method == "GET":
        return "Callback endpoint is working."

    data = request.get_json(silent=True)

    print("M-Pesa Callback Received:")
    print(data)

    if not data:
        return jsonify({
            "ResultCode": 1,
            "ResultDesc": "No callback data received"
        })

    stk_callback = data.get("Body", {}).get("stkCallback", {})

    result_code = stk_callback.get("ResultCode")
    result_desc = stk_callback.get("ResultDesc")

    checkout_request_id = stk_callback.get("CheckoutRequestID")
    merchant_request_id = stk_callback.get("MerchantRequestID")

    # Find the original payment details
    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            customer_name,
            phone,
            amount,
            description
        FROM pending_payments
        WHERE checkout_request_id = ?
    """, (checkout_request_id,))

    pending_payment = cursor.fetchone()

    # Extract M-Pesa callback information
    mpesa_receipt_number = None
    transaction_date = None
    callback_phone = None
    callback_amount = None

    callback_metadata = stk_callback.get("CallbackMetadata", {})
    items = callback_metadata.get("Item", [])

    for item in items:

        name = item.get("Name")
        value = item.get("Value")

        if name == "MpesaReceiptNumber":
            mpesa_receipt_number = value

        elif name == "TransactionDate":
            transaction_date = value

        elif name == "PhoneNumber":
            callback_phone = value

        elif name == "Amount":
            callback_amount = value

    status = "SUCCESS" if result_code == 0 else "FAILED"

    # Use original payment information when available
    if pending_payment:

        customer_name = pending_payment[0]
        phone = pending_payment[1]
        amount = pending_payment[2]
        description = pending_payment[3]

    else:

        customer_name = None
        phone = callback_phone
        amount = callback_amount
        description = result_desc

    # Save completed transaction
    cursor.execute("""
        INSERT INTO transactions (
            customer_name,
            phone,
            amount,
            description,
            checkout_request_id,
            merchant_request_id,
            mpesa_receipt_number,
            transaction_date,
            status
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        customer_name,
        phone,
        amount,
        description,
        checkout_request_id,
        merchant_request_id,
        mpesa_receipt_number,
        transaction_date,
        status
    ))

    # Remove the payment from pending payments
    cursor.execute("""
        DELETE FROM pending_payments
        WHERE checkout_request_id = ?
    """, (checkout_request_id,))

    conn.commit()
    conn.close()

    return jsonify({
        "ResultCode": 0,
        "ResultDesc": "Callback received successfully"
    })
@app.route("/receipt/<int:transaction_id>")
def receipt(transaction_id):

    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            id,
            customer_name,
            phone,
            amount,
            description,
            checkout_request_id,
            merchant_request_id,
            mpesa_receipt_number,
            transaction_date,
            status
        FROM transactions
        WHERE id = ?
    """, (transaction_id,))

    transaction = cursor.fetchone()

    conn.close()

    if not transaction:
        return "Receipt not found.", 404

    return render_template(
        "receipt.html",
        transaction=transaction
    )
@app.route("/payment-status/<checkout_request_id>")
def payment_status(checkout_request_id):

    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()

    cursor.execute("""
        SELECT id, status
        FROM transactions
        WHERE checkout_request_id = ?
    """, (checkout_request_id,))

    transaction = cursor.fetchone()

    conn.close()

    if transaction:
        return jsonify({
            "found": True,
            "transaction_id": transaction[0],
            "status": transaction[1]
        })

    return jsonify({
        "found": False,
        "status": "PENDING"
    })
@app.route("/history")
def history():

    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            id,
            customer_name,
            phone,
            amount,
            description,
            mpesa_receipt_number,
            transaction_date,
            status
        FROM transactions
        ORDER BY id DESC
    """)

    transactions = cursor.fetchall()

    conn.close()

    return render_template(
        "history.html",
        transactions=transactions
    )
if __name__ == "__main__":
    init_db()
    app.run(debug=True)