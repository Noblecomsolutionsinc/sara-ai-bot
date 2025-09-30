import os
from dotenv import load_dotenv

print("=== ENVIRONMENT DIAGNOSTICS ===")
print(f"Current directory: {os.getcwd()}")
print(f"Python version: {os.sys.version}")

# Check if .env file exists and can be read
env_path = '.env'
print(f".env file exists: {os.path.exists(env_path)}")
print(f".env file size: {os.path.getsize(env_path) if os.path.exists(env_path) else 0} bytes")

try:
    with open(env_path, 'r') as f:
        content = f.read()
    print("✅ .env file can be read")
    print("First 3 lines of .env:")
    for i, line in enumerate(content.split('\n')[:3]):
        print(f"  {i+1}: {repr(line)}")
except Exception as e:
    print(f"❌ Error reading .env: {e}")

# Load environment
print("\n--- Loading environment ---")
load_dotenv()

# Check all expected variables
expected_vars = [
    'TWILIO_ACCOUNT_SID',
    'TWILIO_AUTH_TOKEN', 
    'TWILIO_PHONE_NUMBER',
    'SERVER_URL',
    'PUBLIC_STREAMING_URL',
    'OPENAI_API_KEY'
]

print("\n--- Environment Variables Status ---")
for var in expected_vars:
    value = os.getenv(var)
    if value:
        print(f"✅ {var}: FOUND (length: {len(value)})")
    else:
        print(f"❌ {var}: MISSING")

print("\n=== END DIAGNOSTICS ===")