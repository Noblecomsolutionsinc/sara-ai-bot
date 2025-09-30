# File: check_twiml.py
import requests

def check_twiml():
    url = "https://sara-ai-bot.onrender.com/outbound"
    
    try:
        response = requests.post(url, data={
            "CallSid": "test123",
            "From": "+15555555555", 
            "To": "+15555555556"
        })
        
        print("📞 TwiML Response:")
        print("=" * 40)
        print(response.text)
        print("=" * 40)
        
        # Check for critical elements
        if "wss://" in response.text:
            print("✅ WebSocket URL uses wss:// - CORRECT")
        else:
            print("❌ WebSocket URL missing wss://")
            
        if "/ws" in response.text:
            print("✅ WebSocket endpoint includes /ws - CORRECT")
        else:
            print("❌ WebSocket endpoint missing /ws")
            
    except Exception as e:
        print(f"❌ Error: {e}")

if __name__ == "__main__":
    check_twiml()