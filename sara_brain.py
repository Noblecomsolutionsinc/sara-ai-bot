# File: sara_brain.py
import logging
from sara_loader import SaraLoader

logger = logging.getLogger("sara-ai")

class SaraBrain:
    def __init__(self, data_folder="data"):
        self.loader = SaraLoader(data_folder)
        self.data = {}
        self.initialized = False
        self._init_brain()

    def _init_brain(self):
        """Initialize Sara’s brain by loading all knowledge files."""
        try:
            self.data = self.loader.load_all()
            self.initialized = True
            logger.info("SaraBrain initialized successfully. Loaded sections: %s", list(self.data.keys()))
        except Exception as e:
            logger.exception("Failed to initialize SaraBrain: %s", str(e))
            self.initialized = False

    def get_section(self, section_name):
        """Retrieve a specific section (e.g., 'Sara_Objections')."""
        if not self.initialized:
            logger.warning("SaraBrain not initialized. Cannot fetch section.")
            return None
        return self.data.get(section_name, None)

    def all_sections(self):
        """Return all loaded sections."""
        return self.data if self.initialized else {}

    def is_ready(self):
        """Check if Sara’s brain has been initialized successfully."""
        return self.initialized


if __name__ == "__main__":
    # Quick test
    brain = SaraBrain("data")
    if brain.is_ready():
        print("✅ SaraBrain ready. Sections loaded:")
        for section in brain.all_sections().keys():
            print(f" - {section}")
    else:
        print("❌ SaraBrain failed to initialize")
