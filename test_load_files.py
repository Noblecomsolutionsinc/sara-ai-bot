import json
import os

DATA_FOLDER = "data"

def test_load_files():
    for filename in os.listdir(DATA_FOLDER):
        if filename.endswith(".json"):
            filepath = os.path.join(DATA_FOLDER, filename)
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                print(f"✅ {filename} loaded successfully with {len(data)} items")
            except Exception as e:
                print(f"❌ Error loading {filename}: {e}")

if __name__ == "__main__":
    test_load_files()
