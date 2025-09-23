from dotenv import load_dotenv
import os
from openai import OpenAI

# Load environment variables from .env
load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

client = OpenAI(api_key=OPENAI_API_KEY)

resp = client.chat.completions.create(
    model="gpt-5-mini",
    messages=[{"role":"user","content":"Hello, is my OpenAI key working?"}]
)

# Access the response content using attribute syntax
print(resp.choices[0].message.content)
