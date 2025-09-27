# File: memory_manager.py
import os
import json
import shutil
from datetime import datetime, timedelta

MEMORY_DIR = "memory"
os.makedirs(MEMORY_DIR, exist_ok=True)

# ---------------------------
# Config
# ---------------------------
MEMORY_RETENTION_DAYS = 30  # Messages older than this will be summarised or removed
SUMMARY_THRESHOLD_DAYS = 7  # Messages older than this will be summarised

# ---------------------------
# Helper functions
# ---------------------------
def get_memory_file(conversation_id: str) -> str:
    return os.path.join(MEMORY_DIR, f"{conversation_id}.json")

def append_message(conversation_id: str, role: str, content):
    """
    Save a message to the memory file with timestamp.
    Auto-backup and recovery if file is corrupted.
    """
    mem_file = get_memory_file(conversation_id)
    backup_file = mem_file + ".bak"

    entry = {
        "timestamp": datetime.utcnow().isoformat(),
        "role": role,
        "content": content
    }

    memory = []

    if os.path.exists(mem_file):
        try:
            with open(mem_file, "r", encoding="utf-8") as f:
                memory = json.load(f)
                if not isinstance(memory, list):
                    raise ValueError("Memory file is not a list")
        except Exception as e:
            # Backup corrupted file
            shutil.copy(mem_file, backup_file)
            print(f"[Warning] Memory file corrupted. Backup created: {backup_file}")
            memory = []

    memory.append(entry)

    with open(mem_file, "w", encoding="utf-8") as f:
        json.dump(memory, f, ensure_ascii=False, indent=2)

def get_recent_history(conversation_id: str, limit: int = 10):
    """
    Return the last `limit` messages for the conversation.
    Automatically removes messages older than MEMORY_RETENTION_DAYS.
    """
    mem_file = get_memory_file(conversation_id)
    if not os.path.exists(mem_file):
        return []

    try:
        with open(mem_file, "r", encoding="utf-8") as f:
            memory = json.load(f)
            if not isinstance(memory, list):
                memory = []
    except Exception:
        memory = []

    # Filter out very old messages
    cutoff = datetime.utcnow() - timedelta(days=MEMORY_RETENTION_DAYS)
    filtered_memory = [m for m in memory if datetime.fromisoformat(m["timestamp"]) >= cutoff]

    return filtered_memory[-limit:]

def build_gpt_messages_from_history(conversation_id: str, system_prompt: str, limit: int = 10):
    """
    Build messages array for GPT API.
    Summarises messages older than SUMMARY_THRESHOLD_DAYS to reduce token usage.
    """
    memory = get_recent_history(conversation_id, limit=1000)  # fetch lots, summarise older

    messages = [{"role": "system", "content": system_prompt}]

    summary_cutoff = datetime.utcnow() - timedelta(days=SUMMARY_THRESHOLD_DAYS)
    summary_parts = []

    for msg in memory:
        msg_time = datetime.fromisoformat(msg["timestamp"])
        role = msg["role"]
        content = msg["content"]

        if isinstance(content, dict):
            content_str = content.get("sara_text", str(content))
        else:
            content_str = str(content)

        if msg_time < summary_cutoff:
            summary_parts.append(f"{role}: {content_str}")
        else:
            messages.append({"role": role, "content": content_str})

    if summary_parts:
        summary_text = " ".join(summary_parts)
        # truncate to avoid token overload
        messages.insert(1, {
            "role": "system",
            "content": f"[Summary of older conversation]: {summary_text[:2000]}..."
        })

    return messages
