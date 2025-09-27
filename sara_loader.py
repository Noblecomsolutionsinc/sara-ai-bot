import os
import json

class SaraLoader:
    def __init__(self, data_folder="data"):
        self.data_folder = data_folder
        self.sara_data = {}

    def load_files(self):
        """Load all JSON files from the data folder into a dictionary."""
        for filename in os.listdir(self.data_folder):
            if filename.endswith(".json"):
                file_path = os.path.join(self.data_folder, filename)
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        key = filename.replace(".json", "")
                        self.sara_data[key] = data
                        print(f"✅ {filename} loaded successfully")
                except Exception as e:
                    print(f"❌ Error loading {filename}: {e}")

    def get(self, section_name):
        """Retrieve a specific file's data (e.g., 'Sara_Objections')."""
        return self.sara_data.get(section_name, None)

    def all_data(self):
        """Return everything loaded into Sara's brain."""
        return self.sara_data

    def load_all(self):
        """
        Alias for load_files + return dictionary.
        This ensures compatibility with sara_brain.py.
        """
        self.load_files()
        return self.all_data()


if __name__ == "__main__":
    loader = SaraLoader("data")
    all_data = loader.load_all()

    # Quick test: print all top-level keys loaded
    print("\nSara’s knowledge files loaded:")
    for key in all_data.keys():
        print(f" - {key}")
