# File: app.py
import os
import csv
import logging
import time
import re
from flask import Flask, Response, jsonify, request
from twilio.rest import Client

# --- Logging ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sara-app")

# --- Required env vars ---
REQUIRED = [
    "TWILIO_ACCOUNT_SID",
    "TWILIO_AUTH_TOKEN", 
    "TWILIO_PHONE_NUMBER",
    "SERVER_URL",
    "PUBLIC_STREAMING_URL"
]

missing = [v for v in REQUIRED if not os.environ.get(v)]
if missing:
    log.error("Missing required env vars: %s", missing)
    log.warning("Some environment variables missing, but continuing...")

TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.environ.get("TWILIO_PHONE_NUMBER")
SERVER_URL = os.environ.get("SERVER_URL", "https://sara-ai-bot.onrender.com").rstrip("/")
PUBLIC_STREAMING_URL = os.environ.get("PUBLIC_STREAMING_URL", "https://sara-ai-streaming.onrender.com").rstrip("/")

# Ensure WebSocket URL uses wss://
if PUBLIC_STREAMING_URL.startswith('http://'):
    PUBLIC_STREAMING_URL = PUBLIC_STREAMING_URL.replace('http://', 'wss://', 1)
elif PUBLIC_STREAMING_URL.startswith('https://'):
    PUBLIC_STREAMING_URL = PUBLIC_STREAMING_URL.replace('https://', 'wss://', 1)
elif not PUBLIC_STREAMING_URL.startswith('wss://'):
    PUBLIC_STREAMING_URL = f"wss://{PUBLIC_STREAMING_URL}"

# Add WebSocket path if not present
if not PUBLIC_STREAMING_URL.endswith('/ws'):
    PUBLIC_STREAMING_URL = f"{PUBLIC_STREAMING_URL}/ws"

log.info("Using WebSocket URL: %s", PUBLIC_STREAMING_URL)

client = None
if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
else:
    log.warning("Twilio client not initialized - missing credentials")

app = Flask(__name__)

def detect_business_type(business_name):
    """
    Intelligently detect business type from company name using keyword analysis
    """
    if not business_name:
        return "general business"
    
    name_lower = business_name.lower()
    
    # Legal industry
    legal_keywords = ['law', 'legal', 'attorney', 'lawyer', 'firm', 'advocate', 'counsel', 'barrister', 'litigation', 'justice']
    if any(keyword in name_lower for keyword in legal_keywords):
        return "law firm"
    
    # Medical - Dermatology
    derm_keywords = ['dermatology', 'dermatologist', 'derm', 'skin', 'cosmetic', 'aesthetic', 'laser', 'botox', 'filler', 'rejuvenation']
    if any(keyword in name_lower for keyword in derm_keywords):
        return "dermatology clinic"
    
    # Medical - Dental
    dental_keywords = ['dental', 'dentist', 'teeth', 'smile', 'orthodontist', 'oral', 'implant', 'braces', 'invisalign']
    if any(keyword in name_lower for keyword in dental_keywords):
        return "dental practice"
    
    # Medical - General
    medical_keywords = ['medical', 'clinic', 'hospital', 'health', 'wellness', 'doctor', 'physician', 'care', 'surgery']
    if any(keyword in name_lower for keyword in medical_keywords):
        return "medical practice"
    
    # Accounting/Financial
    accounting_keywords = ['accounting', 'accountant', 'tax', 'cpa', 'financial', 'bookkeeping', 'audit', 'consulting', 'advisory']
    if any(keyword in name_lower for keyword in accounting_keywords):
        return "accounting firm"
    
    # Real Estate
    real_estate_keywords = ['real estate', 'realtor', 'property', 'housing', 'estate', 'broker', 'realty']
    if any(keyword in name_lower for keyword in real_estate_keywords):
        return "real estate agency"
    
    # Insurance
    insurance_keywords = ['insurance', 'insurer', 'coverage', 'policy', 'assurance', 'underwriter']
    if any(keyword in name_lower for keyword in insurance_keywords):
        return "insurance agency"
    
    # Technology
    tech_keywords = ['tech', 'software', 'digital', 'it', 'computer', 'system', 'solution', 'development', 'app', 'web']
    if any(keyword in name_lower for keyword in tech_keywords):
        return "technology company"
    
    # Marketing
    marketing_keywords = ['marketing', 'media', 'advertising', 'brand', 'creative', 'agency', 'communications', 'pr', 'public relations']
    if any(keyword in name_lower for keyword in marketing_keywords):
        return "marketing agency"
    
    # Construction
    construction_keywords = ['construction', 'contractor', 'build', 'renovation', 'remodel', 'contracting', 'builder']
    if any(keyword in name_lower for keyword in construction_keywords):
        return "construction company"
    
    # Restaurant/Food
    food_keywords = ['restaurant', 'cafe', 'bistro', 'grill', 'kitchen', 'food', 'eatery', 'dining', 'bar & grill']
    if any(keyword in name_lower for keyword in food_keywords):
        return "restaurant"
    
    # Retail
    retail_keywords = ['shop', 'store', 'retail', 'boutique', 'market', 'outlet', 'merchant']
    if any(keyword in name_lower for keyword in retail_keywords):
        return "retail store"
    
    # If no specific match, use general business classification
    business_keywords = ['company', 'corp', 'inc', 'llc', 'enterprise', 'ventures', 'group', 'partners', 'associates']
    if any(keyword in name_lower for keyword in business_keywords):
        return "professional services"
    
    return "local business"

def get_business_context(business_name, detected_type):
    """
    Generate intelligent context based on detected business type
    """
    context_templates = {
        "law firm": {
            "opening": f"I'm calling {business_name} because I specialize in helping law firms stop losing 7-9 potential clients each week to competitors who are simply easier to find online.",
            "value_prop": "We help recover $10K-$20K monthly in missed case revenue through better online visibility and intake optimization."
        },
        "dermatology clinic": {
            "opening": f"I'm reaching out to {business_name} about the current surge in cosmetic demand. Many dermatology practices miss 20-30 high-value patients monthly due to online visibility gaps.",
            "value_prop": "We help recapture $30K-$50K in monthly missed revenue from cosmetic patients seeking treatments like Botox and fillers."
        },
        "dental practice": {
            "opening": f"I'm calling {business_name} because for aesthetic dentists, a strong before-and-after presence is everything. Many practices lose 15-25 patients monthly because their online smile isn't as good as their work.",
            "value_prop": "We help capture $3K-$5K per patient in cosmetic dentistry revenue through better online presentation."
        },
        "medical practice": {
            "opening": f"I'm contacting {business_name} to discuss how medical practices often miss patients due to online visibility issues and scheduling barriers.",
            "value_prop": "We help medical practices fill appointment gaps and attract the right patients through optimized online presence."
        },
        "accounting firm": {
            "opening": f"I'm calling {business_name} about tax season visibility. Many accounting firms miss dozens of clients because they aren't top-of-mind when tax searches happen in February.",
            "value_prop": "We ensure you capture peak season demand with clients worth $1K-$2.5K annually through better online positioning."
        },
        "real estate agency": {
            "opening": f"I'm reaching out to {business_name} because in real estate, being found first is everything. Many agencies lose listings to competitors with stronger online presence.",
            "value_prop": "We help real estate agencies capture more listings and qualified buyers through dominant online visibility."
        },
        "insurance agency": {
            "opening": f"I'm calling {business_name} about the competitive insurance market. Many agencies lose clients to larger carriers with bigger marketing budgets.",
            "value_prop": "We help independent insurance agencies compete effectively and capture more local clients online."
        },
        "technology company": {
            "opening": f"I'm contacting {business_name} because even tech companies can struggle with client acquisition. Many miss opportunities due to poor online conversion.",
            "value_prop": "We help technology companies generate more qualified leads and improve their sales pipeline through better online positioning."
        },
        "marketing agency": {
            "opening": f"I'm calling {business_name} with an interesting observation - many marketing agencies are so busy helping clients that they neglect their own online presence.",
            "value_prop": "We help marketing agencies showcase their expertise and attract higher-value clients through strategic online visibility."
        },
        "construction company": {
            "opening": f"I'm reaching out to {business_name} because in construction, homeowners start their search online. Many contractors lose projects due to poor digital presence.",
            "value_prop": "We help construction companies win more bids and premium projects through better online credibility and visibility."
        },
        "restaurant": {
            "opening": f"I'm calling {business_name} about the importance of online presence for restaurants. Many establishments lose customers to competitors with better reviews and visibility.",
            "value_prop": "We help restaurants attract more diners and increase reservations through improved online reputation and visibility."
        },
        "retail store": {
            "opening": f"I'm contacting {business_name} because local retail has changed - customers now research online before visiting. Many stores lose sales due to poor digital presence.",
            "value_prop": "We help retail stores drive more foot traffic and local sales through better online visibility and customer engagement."
        },
        "professional services": {
            "opening": f"I'm calling {business_name} because I specialize in helping professional service firms stop losing potential clients due to online visibility gaps.",
            "value_prop": "We identify and plug revenue leakage that's costing you clients every week through simple, proven fixes."
        },
        "local business": {
            "opening": f"I'm reaching out to {business_name} because I help local businesses capture more customers who are actively searching for their services online.",
            "value_prop": "We help businesses like yours stop losing potential clients to competitors with better online presence and visibility."
        }
    }
    
    return context_templates.get(detected_type, context_templates["local business"])

@app.route("/health", methods=["GET", "HEAD"])
def health():
    return jsonify({"status": "ok"}), 200

@app.route("/outbound", methods=["POST"])
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
    
    log.info("🏢 Intelligent Call - Business: %s, Detected Type: %s", business_name, business_type)
    
    # Pass context to streaming server via URL parameters
    stream_url = f"{PUBLIC_STREAMING_URL}?business_name={business_name}&business_type={business_type}"
    
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Start>
        <Stream url="{stream_url}"/>
    </Start>
</Response>"""
    
    log.info("📋 Returning intelligent TwiML for: %s", business_name)
    return Response(twiml, mimetype="text/xml")

def safe_initiate_call(to_number, name="unknown", business_type="general"):
    """Make Sara call someone with intelligent business context"""
    if not client:
        log.error("Twilio client not initialized - cannot make call")
        return None
        
    # Detect business type from name
    detected_type = detect_business_type(name)
    context = get_business_context(name, detected_type)
    
    log.info("🎯 SARA INITIATING INTELLIGENT CALL")
    log.info("   Business: %s", name)
    log.info("   Detected Type: %s", detected_type)
    log.info("   Opening: %s", context["opening"])
    log.info("   Value Prop: %s", context["value_prop"])
    
    try:
        # Pass business context to the call
        call = client.calls.create(
            to=to_number,
            from_=TWILIO_PHONE_NUMBER,
            url=f"{SERVER_URL}/outbound?business_name={name}&business_type={detected_type}",
            method="POST",
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

    log.info("🚀 Starting SARA INTELLIGENT OUTBOUND campaign")
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
            context = get_business_context(name, business_type)
            
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
            "detected_type": detect_business_type(business_name)
        })
    else:
        return jsonify({"error": "Failed to initiate call"}), 500

@app.route("/detect_business/<business_name>")
def detect_business(business_name):
    """Test business type detection"""
    detected_type = detect_business_type(business_name)
    context = get_business_context(business_name, detected_type)
    
    return jsonify({
        "business_name": business_name,
        "detected_type": detected_type,
        "opening_line": context["opening"],
        "value_proposition": context["value_prop"]
    })

if __name__ == "__main__":
    mode = os.environ.get("MODE", "server").lower()
    if mode == "campaign":
        run_campaign()
    else:
        port = int(os.environ.get("PORT", 5000))
        log.info("Starting Sara Intelligent Outbound Server on port %s", port)
        app.run(host="0.0.0.0", port=port)