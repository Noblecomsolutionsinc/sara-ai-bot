import logging
from openai import OpenAI

logger = logging.getLogger("gpt_client")

# Initialize OpenAI client with global timeout
client = OpenAI(timeout=60)

def generate_reply(prompt: str) -> str:
    """
    Generate a reply from GPT model given a user prompt.
    """
    try:
        response = client.chat.completions.create(
            model="gpt-5-mini",
            messages=[{"role": "user", "content": prompt}],
            max_completion_tokens=1000,
            temperature=0.7,
        )
        reply = response.choices[0].message["content"]
        logger.info("GPT reply generated successfully")
        return reply
    except Exception as e:
        logger.error(f"GPT generation failed: {e}")
        return "I'm sorry, something went wrong."
