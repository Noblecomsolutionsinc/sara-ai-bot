from gpt_client import call_gpt

def main():
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Say hello and tell me your name in one sentence."}
    ]

    response = call_gpt(messages, max_completion_tokens=500)
    if response:
        print("GPT response:", response)
    else:
        print("GPT response: None")

if __name__ == "__main__":
    main()
