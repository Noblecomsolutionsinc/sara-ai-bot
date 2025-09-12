import pandas as pd
from twilio.rest import Client
import os

# Load environment variables
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.environ.get("TWILIO_PHONE_NUMBER")
SERVER_URL = os.environ.get("SERVER_URL")  # your Render URL

# Initialize Twilio client
client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# Function to read CSV and place the first call
def call_next_contact():
    csv_path = "contacts.csv"  # path on the server
    df = pd.read_csv(csv_path)

    if df.empty:
        return "No contacts left in CSV."

    # Get the first row
    row = df.iloc[0]
    name = row['name']
    phone = row['phone']
    business_type = row.get('type', '')

    # Twilio call
    call = client.calls.create(
        to=phone,
        from_=TWILIO_PHONE_NUMBER,
        url=f"{SERVER_URL}/voice?name={name}&type={business_type}"
    )

    return f"Calling {name} ({phone})..."
