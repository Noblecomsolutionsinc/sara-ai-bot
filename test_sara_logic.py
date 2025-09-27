import json
import random
from pathlib import Path

# Load JSON files
data_path = Path("data")

with open(data_path / "Sara_Opening.json", "r", encoding="utf-8") as f:
    openings = json.load(f)

with open(data_path / "Sara_Objections.json", "r", encoding="utf-8") as f:
    objections = json.load(f)

with open(data_path / "Sara_SystemPrompt_Production.json", "r", encoding="utf-8") as f:
    system_prompt = json.load(f)


def simulate_start():
    """Test if Sara picks an opener correctly"""
    print("\n--- START STAGE TEST ---")
    opener = random.choice(openings["openings"])
    response = {
        "sara_text": opener,
        "action": "book",
        "time_slots": ["2025-09-29 11:00 +05:00"],
        "hot_lead": False,
        "log_tags": ["dm", "start"]
    }
    print(json.dumps(response, indent=2))


def simulate_objection(objection_text):
    """Test objection handling using ARC method"""
    print("\n--- OBJECTION TEST ---")
    match = next((o for o in objections if o["objection"].lower() in objection_text.lower()), None)

    if match:
        response = {
            "sara_text": match["response"],
            "action": "book",
            "time_slots": ["2025-09-29 14:00 +05:00"],
            "hot_lead": False,
            "log_tags": ["dm", "mid", f"objection:{match['objection']}"]
        }
    else:
        response = {
            "sara_text": "Hmm, I hear you. Can you tell me a bit more about your concern?",
            "action": "log_only",
            "time_slots": [],
            "hot_lead": False,
            "log_tags": ["dm", "mid", "objection:unknown"]
        }
    print(json.dumps(response, indent=2))


def simulate_hot_lead(prospect_text):
    """Test hot lead detection"""
    print("\n--- HOT LEAD TEST ---")
    hot_triggers = system_prompt["hot_lead_management"]["triggers"]

    if any(trigger.lower() in prospect_text.lower() for trigger in hot_triggers):
        response = {
            "sara_text": system_prompt["hot_lead_management"]["response_script"],
            "action": "escalate",
            "time_slots": [],
            "hot_lead": True,
            "log_tags": ["dm", "mid", "hot_lead"]
        }
    else:
        response = {
            "sara_text": "Got it — let's explore that further tomorrow at 11?",
            "action": "book",
            "time_slots": ["2025-09-30 11:00 +05:00"],
            "hot_lead": False,
            "log_tags": ["dm", "mid"]
        }
    print(json.dumps(response, indent=2))


if __name__ == "__main__":
    simulate_start()
    simulate_objection("Can you send me info?")
    simulate_hot_lead("What's your pricing?")
