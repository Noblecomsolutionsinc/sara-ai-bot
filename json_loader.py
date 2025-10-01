# json_loader.py
import json
from pathlib import Path
import logging

LOG = logging.getLogger("json_loader")
DATA_DIR = Path("data")

FILES = {
    "system_prompt": ["Sara_SystemPrompt_Production.json", "Sara_SystemPrompt.json", "Sara_SystemPrompt.txt"],
    "knowledge": ["Sara_KnowledgeBase.json", "Sara_Knowledgebase.json"],
    "objections": ["Sara_Objections.json"],
    "opening": ["Sara_Opening.json"],
    "callflow": ["Sara_CallFlow.json"],
    "playbook": ["Sara_Playbook.json"]
}

def _load_first(paths):
    for p in paths:
        fp = DATA_DIR / p
        if fp.exists():
            try:
                if fp.suffix.lower() == ".json":
                    return json.loads(fp.read_text(encoding="utf-8"))
                else:
                    return {"text": fp.read_text(encoding="utf-8")}
            except Exception as e:
                LOG.exception("Failed loading %s: %s", fp, e)
    LOG.warning("No files found for %s", paths)
    return {}

class SaraStore:
    def __init__(self):
        self.reload()

    def reload(self):
        self.system_prompt = _load_first(FILES["system_prompt"])
        self.knowledge = _load_first(FILES["knowledge"])
        self.objections = _load_first(FILES["objections"])
        self.opening = _load_first(FILES["opening"])
        self.callflow = _load_first(FILES["callflow"])
        self.playbook = _load_first(FILES["playbook"])

    def get(self, key):
        return getattr(self, key, {})

sara_store = SaraStore()
