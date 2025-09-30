# File: app.py
import os
import csv
import logging
import time
import re
import urllib.parse
from flask import Flask, Response, jsonify, request
from twilio.rest import Client

# --- Logging ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sara-app")

# --- Environment Variables ---
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.environ.get("TWILIO_PHONE_NUMBER")
SERVER_URL = os.environ.get("SERVER_URL", "https://sara-ai-bot.onrender.com").rstrip("/")
PUBLIC_STREAMING_URL = os.environ.get("PUBLIC_STREAMING_URL", "wss://sara-ai-streaming.onrender.com/ws")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY")

# Validate critical environment variables
log.info("🔧 Configuration Check:")
log.info("   - Twilio Account SID: %s", "✅ Set" if TWILIO_ACCOUNT_SID else "❌ Missing")
log.info("   - Twilio Auth Token: %s", "✅ Set" if TWILIO_AUTH_TOKEN else "❌ Missing") 
log.info("   - Twilio Phone: %s", TWILIO_PHONE_NUMBER or "❌ Missing")
log.info("   - OpenAI API Key: %s", "✅ Set" if OPENAI_API_KEY else "❌ Missing")
log.info("   - ElevenLabs API Key: %s", "✅ Set" if ELEVENLABS_API_KEY else "❌ Missing")
log.info("   - Server URL: %s", SERVER_URL)
log.info("   - Streaming URL: %s", PUBLIC_STREAMING_URL)
log.info("   - GPT Model: gpt-5-mini")

client = None
if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
    try:
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        log.info("✅ Twilio client initialized successfully")
    except Exception as e:
        log.error("❌ Failed to initialize Twilio client: %s", e)
        client = None
else:
    log.error("❌ Twilio client not initialized - missing credentials")

app = Flask(__name__)

def detect_business_type(business_name):
    """Intelligently detect business type from company name"""
    if not business_name or business_name == "Unknown Business":
        return "general"
    
    name_lower = business_name.lower()
    
    # Industry detection logic
    industry_keywords = {
        "dermatology clinic": ['dermatology', 'dermatologist', 'derm', 'skin', 'cosmetic', 'aesthetic', 'laser', 'botox', 'filler'],
        "law firm": ['law', 'legal', 'attorney', 'lawyer', 'firm', 'advocate', 'counsel'],
        "dental practice": ['dental', 'dentist', 'teeth', 'smile', 'orthodontist', 'oral', 'implant'],
        "medical practice": ['medical', 'clinic', 'hospital', 'health', 'wellness', 'doctor', 'physician'],
        "accounting firm": ['accounting', 'accountant', 'tax', 'cpa', 'financial', 'bookkeeping', 'audit'],
        "real estate agency": ['real estate', 'realtor', 'property', 'housing', 'estate', 'broker'],
        "insurance agency": ['insurance', 'insurer', 'coverage', 'policy', 'assurance'],
        "technology company": ['tech', 'software', 'digital', 'it', 'computer', 'system', 'solution'],
        "marketing agency": ['marketing', 'media', 'advertising', 'brand', 'creative', 'agency'],
        "construction company": ['construction', 'contractor', 'build', 'renovation', 'remodel'],
        "restaurant": ['restaurant', 'cafe', 'bistro', 'grill', 'kitchen', 'food'],
        "retail store": ['shop', 'store', 'retail', 'boutique', 'market']
    }
    
    for industry, keywords in industry_keywords.items():
        if any(keyword in name_lower for keyword in keywords):
            return industry
    
    return "general"

@app.route("/health", methods=["GET", "HEAD"])
def health():
    return jsonify({
        "status": "ok", 
        "twilio_configured": bool(client),
        "openai_configured": bool(OPENAI_API_KEY),
        "elevenlabs_configured": bool(ELEVENLABS_API_KEY),
        "gpt_model": "gpt-5-mini"
    }), 200

@app.route("/outbound", methods=["GET", "POST"])
def outbound():
    """Twilio webhook for OUTBOUND calls - Sara calls people"""
    try:
        call_sid = request.form.get("CallSid", "unknown")
        from_number = request.form.get("From", "unknown")
        to_number = request.form.get("To", "unknown")
        
        log.info("📞 OUTBOUND CALL - Sara calling: %s, From: %s, To: %s", 
                call_sid, from_number, to_number)
        
    except Exception as e:
        log.error("Error parsing Twilio request: %s", e)
        call_sid = "error"

    # Get business context from URL parameters
    business_name = request.args.get("business_name", "Unknown Business")
    business_type = request.args.get("business_type", "general")
    
    # URL encode parameters for the stream URL
    encoded_business_name = urllib.parse.quote(business_name)
    encoded_business_type = urllib.parse.quote(business_type)
    
    log.info("🏢 Intelligent Call - Business: %s, Type: %s", business_name, business_type)
    
    # Build properly encoded stream URL
    stream_url = f"{PUBLIC_STREAMING_URL}?business_name={encoded_business_name}&business_type={encoded_business_type}"
    
    # Generate valid TwiML with proper XML formatting
    twiml = f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Start>
        <Stream url="{stream_url}"/>
    </Start>
    <Say>Please wait while we connect your call.</Say>
    <Pause length="3"/>
</Response>'''
    
    log.info("📋 Returning intelligent TwiML for: %s", business_name)
    return Response(twiml, mimetype="text/xml")

def safe_initiate_call(to_number, name="Unknown Business", business_type="general"):
    """Make Sara call someone with intelligent business context"""
    if not client:
        log.error("Twilio client not initialized - cannot make call")
        return None
        
    # Detect business type from name
    detected_type = detect_business_type(name)
    
    log.info("🎯 SARA INITIATING INTELLIGENT CALL")
    log.info("   Business: %s", name)
    log.info("   Detected Type: %s", detected_type)
    log.info("   Phone: %s", to_number)
    
    try:
        # URL encode parameters
        encoded_name = urllib.parse.quote(name)
        encoded_type = urllib.parse.quote(detected_type)
        
        # Pass business context to the call
        call = client.calls.create(
            to=to_number,
            from_=TWILIO_PHONE_NUMBER,
            url=f"{SERVER_URL}/outbound?business_name={encoded_name}&business_type={encoded_type}",
            method="GET",
            timeout=30
        )
        log.info("✅ Sara intelligent call initiated - SID: %s", call.sid)
        return call
    except Exception as e:
        log.error("❌ Failed to initiate Sara call: %s", e)
        return None

def run_campaign(csv_path="contacts.csv", limit=None):
    """Run outbound calling campaign with intelligent business detection"""
    if not client:
        log.error("Twilio client not initialized - cannot run campaign")
        return 0
        
    if not os.path.exists(csv_path):
        log.error("contacts.csv not found at %s", csv_path)
        raise SystemExit("contacts.csv not found")

    log.info("🚀 Starting SARA INTELLIGENT OUTBOUND campaign with GPT-5-mini")
    with open(csv_path, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            log.error("contacts.csv has no headers")
            raise SystemExit("contacts.csv missing headers")
        
        log.info("📋 CSV headers: %s", reader.fieldnames)
        
        count = 0
        for row in reader:
            if limit and count >= limit:
                break
                
            # Clean row data
            rown = {k.strip().lower(): (v.strip() if isinstance(v, str) else v) for k, v in row.items()}
            name = rown.get("name", "Unknown Business")
            
            # ROBUST phone number extraction
            phone = None
            for field in ['phone', 'mobile', 'number', 'phonenumber']:
                if rown.get(field):
                    phone = str(rown[field]).strip()
                    break

            if not phone:
                log.warning("❌ Skipping %s - no phone number found. Available fields: %s", name, list(rown.keys()))
                continue

            # Clean phone number
            phone = phone.replace(' ', '').replace('-', '').replace('(', '').replace(')', '')
            if not phone.startswith('+'):
                if phone.startswith('1') and len(phone) == 11:
                    phone = '+' + phone
                else:
                    phone = '+1' + phone  # Default to US
            
            # Detect business type intelligently
            business_type = detect_business_type(name)
            
            log.info("📞 Sara intelligently dialing: %s", name)
            log.info("   📊 Detected as: %s", business_type)
            log.info("   📞 Phone: %s", phone)
            
            call = safe_initiate_call(phone, name, business_type)
            if call:
                count += 1
            time.sleep(2.0)
            
        log.info("🏁 Campaign finished — Sara made %d intelligent calls", count)
    return count

@app.route("/run_campaign", methods=["POST"])
def run_campaign_endpoint():
    token = os.environ.get("CAMPAIGN_TRIGGER_TOKEN")
    req_token = request.headers.get("X-Run-Token") or request.form.get("token")
    if token and req_token != token:
        log.warning("Unauthorized attempt to trigger campaign")
        return jsonify({"error": "unauthorized"}), 403
    limit = request.args.get("limit")
    limit = int(limit) if limit and limit.isdigit() else None
    try:
        count = run_campaign(limit=limit)
        return jsonify({"status": "started", "attempted": count}), 200
    except Exception as e:
        log.exception("Failed to start campaign")
        return jsonify({"error": str(e)}), 500

@app.route("/test_call/<phone_number>")
def test_call(phone_number):
    """Test endpoint to make Sara call a specific number"""
    if not phone_number.startswith('+'):
        return jsonify({"error": "Phone number must include country code (+1...)"}), 400
    
    business_name = request.args.get("business_name", "Test Business")
    call = safe_initiate_call(phone_number, business_name)
    if call:
        return jsonify({
            "status": "success", 
            "call_sid": call.sid, 
            "business_name": business_name,
            "detected_type": detect_business_type(business_name),
            "gpt_model": "gpt-5-mini"
        })
    else:
        return jsonify({"error": "Failed to initiate call"}), 500

@app.route("/detect_business/<business_name>")
def detect_business(business_name):
    """Test business type detection"""
    detected_type = detect_business_type(business_name)
    
    return jsonify({
        "business_name": business_name,
        "detected_type": detected_type,
        "gpt_model": "gpt-5-mini"
    })

if __name__ == "__main__":
    mode = os.environ.get("MODE", "server").lower()
    if mode == "campaign":
        run_campaign()
    else:
        port = int(os.environ.get("PORT", 5000))
        log.info("Starting Sara Intelligent Outbound Server on port %s", port)
        log.info("GPT Model: gpt-5-mini")
        app.run(host="0.0.0.0", port=port)