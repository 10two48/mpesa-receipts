from flask import Flask, render_template, request, jsonify
import os
import base64
from datetime import datetime
import requests
from dotenv import load_dotenv
import psycopg2

load_dotenv()

app = Flask(__name__)


# =========================
# DATABASE CONNECTION
# =========================

def get_db_connection():

    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        raise Exception("DATABASE_URL is not configured.")

    return psycopg2.connect(database_url)


def init_db():

    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id SERIAL PRIMARY KEY,
            customer_name TEXT,
            phone TEXT,
            amount NUMERIC,
            description TEXT,
            checkout_request_id TEXT UNIQUE,
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
            amount NUMERIC,
            description TEXT
        )
    """)

    conn.commit()

    cursor.close()
    conn.close()


# =========================
# MPESA ACCESS TOKEN
# =========================

def get_access_token():

    url = (
        "https://sandbox.safaricom.co.ke/"
        "oauth/v1/generate?grant_type=client_credentials"
    )

    response = requests.get(
        url,
        auth=(
            os.getenv("CONSUMER_KEY"),
            os.getenv("CONSUMER_SECRET")
        ),
        timeout=30
    )

    response.raise_for_status()

    return response.json()["access_token"]


# =========================
# HOME PAGE
# =========================

@app.route("/")
def home():

    return render_template("index.html")


# =========================
# INITIATE PAYMENT
# =========================

@app.route("/pay", methods=["POST"])
def pay():

    data = request.get_json() or {}

    phone = data.get("phone")
    amount = data.get("amount")
    description = data.get("description")

    if not phone or not amount:

        return jsonify({
            "success": False,
            "message": "Phone number and amount are required."
        }), 400

    # Clean phone number
    phone = phone.replace(" ", "").replace("+", "")

    if phone.startswith("0"):
        phone = "254" + phone[1:]

    if not phone.startswith("254") or len(phone) != 12:

        return jsonify({
            "success": False,
            "message": "Enter a valid Kenyan phone number."
        }), 400

    # Convert amount to integer
    try:

        amount = int(float(amount))

        if amount <= 0:
            raise ValueError

    except (ValueError, TypeError):

        return jsonify({
            "success": False,
            "message": "Enter a valid amount."
        }), 400

    try:

        access_token = get_access_token()

        shortcode = os.getenv("MPESA_SHORTCODE")
        passkey = os.getenv("MPESA_PASSKEY")

        if not shortcode or not passkey:
            raise Exception(
                "M-Pesa credentials are not configured."
            )

        timestamp = datetime.now().strftime(
            "%Y%m%d%H%M%S"
        )

        password_string = (
            shortcode +
            passkey +
            timestamp
        )

        password = base64.b64encode(
            password_string.encode()
        ).decode()

        stk_url = (
            "https://sandbox.safaricom.co.ke/"
            "mpesa/stkpush/v1/processrequest"
        )

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        }

        callback_url = os.getenv(
            "CALLBACK_URL",
            "https://mpesa-receipts-1.onrender.com/callback"
        )

        payload = {

            "BusinessShortCode": shortcode,

            "Password": password,

            "Timestamp": timestamp,

            "TransactionType":
                "CustomerPayBillOnline",

            "Amount": amount,

            "PartyA": phone,

            "PartyB": shortcode,

            "PhoneNumber": phone,

            "CallBackURL": callback_url,

            "AccountReference": "PAYMENT",

            "TransactionDesc":
                description or "Payment"
        }

        response = requests.post(
            stk_url,
            json=payload,
            headers=headers,
            timeout=30
        )

        result = response.json()

        print("Safaricom response:", result)

        # Save pending payment
        if result.get("ResponseCode") == "0":

            checkout_request_id = result.get(
                "CheckoutRequestID"
            )

            conn = get_db_connection()
            cursor = conn.cursor()

            cursor.execute("""
                INSERT INTO pending_payments (
                    checkout_request_id,
                    customer_name,
                    phone,
                    amount,
                    description
                )
                VALUES (%s, %s, %s, %s, %s)

                ON CONFLICT (checkout_request_id)

                DO UPDATE SET
                    customer_name =
                        EXCLUDED.customer_name,

                    phone =
                        EXCLUDED.phone,

                    amount =
                        EXCLUDED.amount,

                    description =
                        EXCLUDED.description
            """, (
                checkout_request_id,
                data.get("customer_name"),
                phone,
                amount,
                description
            ))

            conn.commit()

            cursor.close()
            conn.close()

        return jsonify(
            result
        ), response.status_code

    except Exception as e:

        print("Payment error:", str(e))

        return jsonify({

            "success": False,

            "message":
                "Payment request failed.",

            "error":
                str(e)

        }), 500


# =========================
# MPESA CALLBACK
# =========================

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

            "ResultDesc":
                "No callback data received"

        })

    stk_callback = data.get(
        "Body", {}
    ).get(
        "stkCallback", {}
    )

    result_code = stk_callback.get(
        "ResultCode"
    )

    result_desc = stk_callback.get(
        "ResultDesc"
    )

    checkout_request_id = stk_callback.get(
        "CheckoutRequestID"
    )

    merchant_request_id = stk_callback.get(
        "MerchantRequestID"
    )

    conn = get_db_connection()
    cursor = conn.cursor()

    # Find original payment
    cursor.execute("""
        SELECT
            customer_name,
            phone,
            amount,
            description

        FROM pending_payments

        WHERE checkout_request_id = %s
    """, (
        checkout_request_id,
    ))

    pending_payment = cursor.fetchone()

    # Callback information
    mpesa_receipt_number = None
    transaction_date = None
    callback_phone = None
    callback_amount = None

    callback_metadata = stk_callback.get(
        "CallbackMetadata",
        {}
    )

    items = callback_metadata.get(
        "Item",
        []
    )

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

    status = (
        "SUCCESS"
        if result_code == 0
        else "FAILED"
    )

    # Use original payment information
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

    # Save transaction
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

        VALUES (
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s
        )

        ON CONFLICT (checkout_request_id)

        DO NOTHING

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

    # Remove pending payment
    cursor.execute("""
        DELETE FROM pending_payments

        WHERE checkout_request_id = %s
    """, (
        checkout_request_id,
    ))

    conn.commit()

    cursor.close()
    conn.close()

    return jsonify({

        "ResultCode": 0,

        "ResultDesc":
            "Callback received successfully"

    })


# =========================
# RECEIPT
# =========================

@app.route("/receipt/<int:transaction_id>")
def receipt(transaction_id):

    conn = get_db_connection()
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

        WHERE id = %s

    """, (
        transaction_id,
    ))

    transaction = cursor.fetchone()

    cursor.close()
    conn.close()

    if not transaction:

        return "Receipt not found.", 404

    return render_template(
        "receipt.html",
        transaction=transaction
    )


# =========================
# TRANSACTION DASHBOARD
# =========================

@app.route("/dashboard")
def dashboard():

    conn = get_db_connection()
    cursor = conn.cursor()

    # Get filters
    search = request.args.get(
        "search",
        ""
    ).strip()

    date_from = request.args.get(
        "date_from",
        ""
    ).strip()

    date_to = request.args.get(
        "date_to",
        ""
    ).strip()

    # Base query
    query = """
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

        WHERE 1=1
    """

    params = []

    # Search
    if search:

        query += """
            AND (
                customer_name ILIKE %s

                OR phone ILIKE %s

                OR mpesa_receipt_number ILIKE %s
            )
        """

        search_value = f"%{search}%"

        params.extend([
            search_value,
            search_value,
            search_value
        ])

    # Start date
    if date_from:

        query += """
            AND LEFT(transaction_date, 8) >= %s
        """

        params.append(
            date_from.replace("-", "")
        )

    # End date
    if date_to:

        query += """
            AND LEFT(transaction_date, 8) <= %s
        """

        params.append(
            date_to.replace("-", "")
        )

    query += """
        ORDER BY id DESC
    """

    cursor.execute(
        query,
        params
    )

    transactions = cursor.fetchall()

    # Calculate dashboard totals
    total_transactions = len(
        transactions
    )

    successful_transactions = sum(
        1
        for transaction in transactions
        if transaction[9] == "SUCCESS"
    )

    total_amount = sum(
        float(transaction[3] or 0)
        for transaction in transactions
        if transaction[9] == "SUCCESS"
    )

    cursor.close()
    conn.close()

    return render_template(

        "dashboard.html",

        transactions=transactions,

        total_transactions=
            total_transactions,

        successful_transactions=
            successful_transactions,

        total_amount=
            total_amount,

        search=search,

        date_from=date_from,

        date_to=date_to
    )


# =========================
# PAYMENT STATUS
# =========================

@app.route(
    "/payment-status/<checkout_request_id>"
)
def payment_status(
    checkout_request_id
):

    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT

            id,
            status

        FROM transactions

        WHERE checkout_request_id = %s

    """, (
        checkout_request_id,
    ))

    transaction = cursor.fetchone()

    cursor.close()
    conn.close()

    if transaction:

        return jsonify({

            "found": True,

            "transaction_id":
                transaction[0],

            "status":
                transaction[1]

        })

    return jsonify({

        "found": False,

        "status": "PENDING"

    })


# =========================
# HEALTH CHECK
# =========================

@app.route("/health")
def health():

    try:

        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute(
            "SELECT 1"
        )

        cursor.fetchone()

        cursor.close()
        conn.close()

        return jsonify({

            "status": "ok",

            "database":
                "connected"

        })

    except Exception as e:

        return jsonify({

            "status": "error",

            "database":
                "not connected",

            "error":
                str(e)

        }), 500


# =========================
# START APPLICATION
# =========================

# Initialize database when app starts
init_db()


if __name__ == "__main__":

    app.run(

        host="0.0.0.0",

        port=int(
            os.getenv(
                "PORT",
                5000
            )
        ),

        debug=True
    )