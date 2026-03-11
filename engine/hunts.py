from pathlib import Path
import yaml


HUNTS_PATH = Path("config/hunts.yaml")


def load_hunts() -> list[dict]:
    with open(HUNTS_PATH, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    hunts = data.get("hunts", [])
    if not isinstance(hunts, list):
        raise ValueError("config/hunts.yaml must contain a top-level 'hunts' list")

    return [h for h in hunts if h.get("enabled", True)]