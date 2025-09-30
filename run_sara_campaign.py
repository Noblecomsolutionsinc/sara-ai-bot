import os
import sys
from dotenv import load_dotenv
import logging
from datetime import datetime
import csv
import re
import asyncio

# ===== FORCE LOAD ENVIRONMENT FIRST =====
current_dir = os.path.dirname(os.path.abspath(__file__))
env_path = os.path.join(current_dir, '.env')
load_dotenv(env_path, override=True)

# Manually ensure all required variables are in os.environ
required_vars = [
    'TWILIO_ACCOUNT_SID',
    'TWILIO_AUTH_TOKEN', 
    'TWILIO_PHONE_NUMBER',
    'SERVER_URL',
    'PUBLIC_STREAMING_URL'
]

for var_name in required_vars:
    value = os.getenv(var_name)
    if value and var_name not in os.environ:
        os.environ[var_name] = value

# ===== NOW IMPORT OTHER MODULES =====
from twilio.rest import Client
import websockets
import json
import requests

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

class SARACampaign:
    def __init__(self):
        self.setup_twilio()
        self.websocket_url = os.getenv('PUBLIC_STREAMING_URL', 'wss://sara-ai-streaming.onrender.com/ws')
        self.server_url = os.getenv('SERVER_URL', 'https://sara-ai-bot.onrender.com')
        
    def setup_twilio(self):
        """Initialize Twilio client with environment variables"""
        self.account_sid = os.getenv('TWILIO_ACCOUNT_SID')
        self.auth_token = os.getenv('TWILIO_AUTH_TOKEN')
        self.twilio_phone = os.getenv('TWILIO_PHONE_NUMBER')
        
        missing_vars = []
        if not self.account_sid:
            missing_vars.append('TWILIO_ACCOUNT_SID')
        if not self.auth_token:
            missing_vars.append('TWILIO_AUTH_TOKEN')
        if not self.twilio_phone:
            missing_vars.append('TWILIO_PHONE_NUMBER')
            
        if missing_vars:
            logging.error(f"Missing required env vars: {missing_vars}")
            logging.warning("Some environment variables missing, but continuing...")
            self.twilio_client = None
        else:
            try:
                self.twilio_client = Client(self.account_sid, self.auth_token)
                logging.info("✅ Twilio client initialized successfully")
            except Exception as e:
                logging.error(f"Failed to initialize Twilio client: {e}")
                self.twilio_client = None

    async def connect_websocket(self):
        """Connect to WebSocket server"""
        try:
            logging.info(f"Using WebSocket URL: {self.websocket_url}")
            self.websocket = await websockets.connect(self.websocket_url)
            logging.info("✅ Connected to WebSocket server")
            return True
        except Exception as e:
            logging.error(f"WebSocket connection failed: {e}")
            return False

    def make_call(self, phone_number, business_name="Unknown Business", industry="general"):
        """Make outbound call using Twilio with business context"""
        if not self.twilio_client:
            logging.warning("Twilio client not initialized - missing credentials")
            return None
            
        try:
            # URL encode business name for the query parameter
            import urllib.parse
            encoded_business_name = urllib.parse.quote(business_name)
            encoded_industry = urllib.parse.quote(industry)
            
            # Use the business context for intelligent calling
            call_url = f"{self.server_url}/outbound?business_name={encoded_business_name}&business_type={encoded_industry}"
            
            call = self.twilio_client.calls.create(
                url=call_url,
                to=phone_number,
                from_=self.twilio_phone,
                method="GET",
                timeout=30
            )
            
            logging.info(f"✅ Call initiated: {call.sid} to {phone_number} for {business_name} ({industry})")
            return call.sid
            
        except Exception as e:
            logging.error(f"Failed to make call to {phone_number}: {e}")
            return None

    async def send_websocket_message(self, message_type, data):
        """Send message via WebSocket"""
        try:
            if hasattr(self, 'websocket') and self.websocket:
                message = {
                    "type": message_type,
                    "data": data,
                    "timestamp": datetime.now().isoformat()
                }
                await self.websocket.send(json.dumps(message))
                return True
        except Exception as e:
            logging.error(f"WebSocket send failed: {e}")
        return False

    def load_contacts_from_csv(self, csv_path="contacts.csv"):
        """Load contacts from CSV file with name, phone, and industry columns"""
        contacts = []
        
        if not os.path.exists(csv_path):
            logging.error(f"Contacts file not found: {csv_path}")
            print(f"❌ CSV file not found: {csv_path}")
            print("💡 Make sure contacts.csv is in the same folder as this script")
            return contacts
            
        try:
            with open(csv_path, 'r', encoding='utf-8') as file:
                # Try different encodings if utf-8 fails
                try:
                    file.read()
                    file.seek(0)
                except UnicodeDecodeError:
                    file.close()
                    with open(csv_path, 'r', encoding='latin-1') as file:
                        reader = csv.DictReader(file)
                        contacts = self._process_csv_reader(reader, csv_path)
                    return contacts
                
                reader = csv.DictReader(file)
                contacts = self._process_csv_reader(reader, csv_path)
                
        except Exception as e:
            logging.error(f"Error reading CSV file: {e}")
            print(f"❌ Error reading CSV: {e}")
            
        return contacts

    def _process_csv_reader(self, reader, csv_path):
        """Process CSV reader and extract contacts"""
        contacts = []
        
        if not reader.fieldnames:
            logging.error("CSV file has no headers")
            print("❌ CSV file has no headers")
            return contacts
        
        # Log available columns for debugging
        logging.info(f"CSV columns: {reader.fieldnames}")
        print(f"📋 CSV columns detected: {list(reader.fieldnames)}")
        
        # Check for required columns
        required_columns = ['name', 'phone', 'industry']
        missing_columns = [col for col in required_columns if col not in [f.lower() for f in reader.fieldnames]]
        
        if missing_columns:
            logging.error(f"Missing required columns: {missing_columns}")
            print(f"❌ Missing required columns in CSV: {missing_columns}")
            print("💡 Your CSV must have columns: name, phone, industry")
            return contacts
        
        # Map column names (case-insensitive)
        column_map = {}
        for field in reader.fieldnames:
            lower_field = field.lower()
            if lower_field in required_columns:
                column_map[lower_field] = field
        
        for row_num, row in enumerate(reader, 1):
            name = row.get(column_map['name'], '').strip()
            phone = row.get(column_map['phone'], '').strip()
            industry = row.get(column_map['industry'], '').strip()
            
            # Validate required fields
            if not name:
                logging.warning(f"Row {row_num}: No name found")
                continue
                
            if not phone:
                logging.warning(f"Row {row_num}: No phone number found for {name}")
                continue
                
            if not industry:
                logging.warning(f"Row {row_num}: No industry found for {name}, using 'general'")
                industry = 'general'
            
            # Clean and validate phone number
            cleaned_phone = self.clean_phone_number(phone)
            if not cleaned_phone:
                logging.warning(f"Row {row_num}: Invalid phone number format for {name}: {phone}")
                continue
            
            contacts.append({
                'name': name,
                'phone': cleaned_phone,
                'industry': industry,
                'original_row': row
            })
            
        logging.info(f"✅ Loaded {len(contacts)} contacts from {csv_path}")
        print(f"✅ Successfully loaded {len(contacts)} contacts from {csv_path}")
        
        return contacts

    def clean_phone_number(self, phone):
        """Clean and format phone number to E.164 format"""
        if not phone:
            return None
            
        # Remove all non-numeric characters except +
        cleaned = re.sub(r'[^\d+]', '', str(phone))
        
        # If it starts with +, assume it's already in E.164 format
        if cleaned.startswith('+'):
            # Validate it has enough digits after +
            if len(cleaned) >= 11:  # +1 followed by 10 digits
                return cleaned
            else:
                return None
            
        # If it's 10 digits, assume US number and add +1
        if len(cleaned) == 10:
            return '+1' + cleaned
            
        # If it's 11 digits and starts with 1, add +
        if len(cleaned) == 11 and cleaned.startswith('1'):
            return '+' + cleaned
            
        # If it doesn't match expected patterns, return None
        return None

    async def run_campaign(self, csv_path="contacts.csv", limit=None):
        """Run the outbound campaign with contacts from CSV"""
        print("🚀 SARA AI - STARTING OUTBOUND CAMPAIGN")
        print("=" * 50)
        
        # Check for missing critical variables
        missing_critical = []
        if not os.getenv('TWILIO_ACCOUNT_SID'):
            missing_critical.append('TWILIO_ACCOUNT_SID')
        if not os.getenv('TWILIO_AUTH_TOKEN'):
            missing_critical.append('TWILIO_AUTH_TOKEN')
        if not os.getenv('TWILIO_PHONE_NUMBER'):
            missing_critical.append('TWILIO_PHONE_NUMBER')
            
        if missing_critical:
            print("❌ MISSING ENVIRONMENT VARIABLES:")
            for var in missing_critical:
                print(f"   - {var}")
            print("\n💡 Make sure your .env file has these variables!")
            return

        # Load contacts from CSV
        contacts = self.load_contacts_from_csv(csv_path)
        if not contacts:
            logging.error("No valid contacts found to call")
            print("❌ No valid contacts found. Campaign stopped.")
            return

        # Apply limit if specified
        if limit:
            contacts = contacts[:limit]
            logging.info(f"Limiting campaign to {limit} contacts")
            print(f"📊 Limiting to first {limit} contacts")

        # Connect to WebSocket
        if not await self.connect_websocket():
            logging.error("Failed to connect to WebSocket, but continuing...")

        # Process contacts
        successful_calls = 0
        print(f"\n📞 STARTING CALLS TO {len(contacts)} CONTACTS")
        print("-" * 40)
        
        for i, contact in enumerate(contacts, 1):
            phone = contact['phone']
            business_name = contact['name']
            industry = contact['industry']
            
            print(f"\n[{i}/{len(contacts)}] 📞 Calling: {business_name}")
            print(f"   📱 Phone: {phone}")
            print(f"   🏢 Industry: {industry}")
            
            logging.info(f"Calling {phone} - {business_name} ({industry}) - {i}/{len(contacts)}")
            
            # Make the call
            call_sid = self.make_call(phone, business_name, industry)
            
            if call_sid:
                # Send WebSocket notification
                await self.send_websocket_message("call_initiated", {
                    "call_sid": call_sid,
                    "to_number": phone,
                    "from_number": self.twilio_phone,
                    "business_name": business_name,
                    "industry": industry
                })
                logging.info(f"✅ Call {call_sid} started successfully")
                successful_calls += 1
                print(f"   ✅ Call initiated successfully")
            else:
                logging.error(f"❌ Failed to start call to {phone}")
                print(f"   ❌ Failed to start call")
            
            # Wait between calls (avoid rate limiting)
            if i < len(contacts):
                wait_time = 5
                print(f"   ⏳ Waiting {wait_time} seconds before next call...")
                await asyncio.sleep(wait_time)

        # Close WebSocket connection
        if hasattr(self, 'websocket') and self.websocket:
            await self.websocket.close()
            logging.info("✅ WebSocket connection closed")

        print("\n" + "=" * 50)
        print("🎯 CAMPAIGN COMPLETED!")
        print(f"📊 Total contacts processed: {len(contacts)}")
        print(f"✅ Successful calls: {successful_calls}")
        print(f"❌ Failed calls: {len(contacts) - successful_calls}")
        print("=" * 50)

async def main():
    import argparse
    
    parser = argparse.ArgumentParser(description='Run SARA AI Outbound Campaign')
    parser.add_argument('--csv', default='contacts.csv', help='Path to CSV file with contacts')
    parser.add_argument('--limit', type=int, help='Limit number of calls to make')
    
    args = parser.parse_args()
    
    campaign = SARACampaign()
    await campaign.run_campaign(csv_path=args.csv, limit=args.limit)

if __name__ == "__main__":
    # Run the async main function
    asyncio.run(main())