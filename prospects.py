# File: prospects.py
import csv

def load_contacts(csv_file="contacts.csv"):
    """
    Load contacts from CSV file.
    CSV must have: name, phone, Type
    Returns a list of dicts: [{name, phone, type}, ...]
    """
    contacts = []
    try:
        with open(csv_file, mode="r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                contact = {
                    "name": row.get("name", "").strip(),
                    "phone": row.get("phone", "").strip(),
                    "type": row.get("Type", "").strip()
                }
                if contact["name"] and contact["phone"]:
                    contacts.append(contact)
    except Exception as e:
        print(f"❌ Error reading {csv_file}: {e}")
    return contacts

if __name__ == "__main__":
    # Quick test
    contacts = load_contacts("contacts.csv")
    print(f"✅ Loaded {len(contacts)} contacts:")
    for c in contacts:
        print(f" - {c['name']} ({c['type']}) -> {c['phone']}")
