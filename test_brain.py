import os
from dotenv import load_dotenv
from openai import OpenAI

# -----------------------------
# Load environment variables
# -----------------------------
load_dotenv()
OPENAI_KEY = os.getenv("OPENAI_API_KEY")

if not OPENAI_KEY:
    print("ERROR: OPENAI_API_KEY not found in .env")
    exit(1)

# Initialize OpenAI client
client = OpenAI(api_key=OPENAI_KEY)

# -----------------------------
# Load Sara Brain text files
# -----------------------------
TXT_FILES = [
    "Sara_SystemPrompt.txt",
    "Sara_Flow.txt",
    "Sara_Opening.txt",
    "Sara_Objections_Playbook_Full.txt",
    "Sara_Knowledgebase.txt",
    "Sara_MasterPrompt.txt"
]

SARA_BRAIN = {}
for f in TXT_FILES:
    path = os.path.join(os.getcwd(), f)
    if not os.path.exists(path):
        print(f"ERROR: {f} not found")
        exit(1)
    with open(path, "r", encoding="utf-8") as file:
        SARA_BRAIN[f] = file.read()

# -----------------------------
# GPT response function
# -----------------------------
def sara_gpt_response(prospect_input, conversation_history=[]):
    system_prompt = SARA_BRAIN["Sara_SystemPrompt.txt"] + "\n" + SARA_BRAIN["Sara_MasterPrompt.txt"]
    messages = [{"role": "system", "content": system_prompt}]
    for c in conversation_history:
        messages.append(c)
    messages.append({"role": "user", "content": prospect_input})

    resp = client.chat.completions.create(
        model="gpt-5-mini",
        messages=messages
        # temperature omitted because gpt-5-mini only supports default 1.0
    )

    return resp.choices[0].message.content

# -----------------------------
# Interactive test loop
# -----------------------------
if __name__ == "__main__":
    print("=== Sara Brain Test ===")
    conversation_history = []

    while True:
        user_input = input("\nProspect says: ")
        if user_input.lower() in ["exit", "quit"]:
            break

        try:
            sara_reply = sara_gpt_response(user_input, conversation_history)
        except Exception as e:
            print("ERROR calling GPT:", str(e))
            continue

        print("\nSara replies:", sara_reply)

        # Store conversation
        conversation_history.append({"role": "user", "content": user_input})
        conversation_history.append({"role": "assistant", "content": sara_reply})
