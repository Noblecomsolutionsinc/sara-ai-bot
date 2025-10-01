import os
from openai import OpenAI

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")  # default to fast/light model

client = OpenAI(api_key=OPENAI_API_KEY)


def generate_reply(history: list) -> str:
    """
    Generate a GPT reply given conversation history.
    History is a list of dicts with {"role": "user"/"assistant", "content": str}.
    """
    try:
        response = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": "You are Sara, a friendly AI caller."}
            ]
            + history,
            max_tokens=int(os.getenv("OPENAI_MAX_TOKENS", "200")),
            temperature=0.7,
            timeout=int(os.getenv("OPENAI_TIMEOUT", "20")),
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"[GPT Error] {e}")
        return "I'm sorry, could you repeat that?"
