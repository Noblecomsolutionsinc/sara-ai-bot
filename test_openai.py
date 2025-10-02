from openai import OpenAI
import os
from dotenv import load_dotenv

# Load variables from .env
load_dotenv()

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

resp = client.chat.completions.create(
    model="gpt-5-mini",
    messages=[
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Introduce yourself politely as GPT-5-mini."},
    ],
    max_completion_tokens=50,
)

print("✅ GPT-5-mini Response:", resp.choices[0].message.content.strip())
