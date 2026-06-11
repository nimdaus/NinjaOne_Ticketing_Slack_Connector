"""Form registry — command → NinjaOne form mappings, persisted in form_registry.json."""

import json
import logging
import os

logger = logging.getLogger("eng_assist_bot.registry")


def _registry_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "form_registry.json")


def load_registry() -> dict:
    path = _registry_path()
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"commands": {}}


def save_registry(registry: dict) -> None:
    path = _registry_path()
    with open(path, "w") as f:
        json.dump(registry, f, indent=2)
    logger.info("Registry saved to %s", path)
