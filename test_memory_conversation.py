# File: test_memory_conversation.py
import json
from app import generate_gpt_response, generate_voice_file
from memory_manager import append_message, get_recent_history, build_gpt_messages_from_history
from datetime import datetime

print("Chat with Sara (type 'exit' to quit)\n")

conversation_id = "test_user"
system_prompt = "You are Sara Hayes, Senior Growth Consultant at Noblecom Solutions. Respond naturally."

while True:
    user_input = input("You: ")
    if user_input.lower() == "exit":
        break

    # Save user message
    append_message(conversation_id, "user", user_input)

    # Build history for GPT
    history = get_recent_history(conversation_id, limit=50)  # fetch recent + old for summarisation
    messages = build_gpt_messages_from_history(conversation_id, system_prompt, limit=50)

    # Check if there is a summary message
    summaries = [m for m in messages if m["role"] == "system" and m.get("content", "").startswith("[Summary of older conversation]")]
    if summaries:
        print(f"[System: {len(summaries)} summary of old messages included in context]")

    try:
        raw_response = generate_gpt_response(messages)
        sara_reply = raw_response.get("sara_text", "").strip()
        sara_action = raw_response.get("action")

        if not sara_reply:
            sara_reply = "[Sara had no valid reply]"

        print(f"Sara says: {sara_reply}")
        print(f"Sara action: {sara_action}")

        # Save Sara’s reply
        append_message(conversation_id, "assistant", {
            "sara_text": sara_reply,
            "action": sara_action
        })

        # Generate voice
        if sara_reply and sara_reply != "[Sara had no valid reply]":
            try:
                audio_path = generate_voice_file(sara_reply)
                print(f"[Audio saved to: {audio_path}]")
            except Exception as e:
                print("[Voice generation failed]", str(e))

    except Exception as e:
        print("Error from Sara:", str(e))
