"""Form registry — command → NinjaOne form mappings, persisted in form_registry.json."""

import json
import logging
import os
import shutil

from ninja_auth import data_dir

logger = logging.getLogger("eng_assist_bot.registry")

# Canonical path: inside the mounted data volume so it survives image rebuilds.
_REGISTRY_FILENAME = "form_registry.json"


def _registry_path() -> str:
    d = data_dir()
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, _REGISTRY_FILENAME)


def _legacy_path() -> str:
    """Original path (next to bot.py) — only used for one-time migration."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), _REGISTRY_FILENAME)


def load_registry() -> dict:
    path = _registry_path()
    try:
        with open(path, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        # One-time migration: copy from the old location if it exists.
        legacy = _legacy_path()
        if os.path.exists(legacy):
            try:
                shutil.copy2(legacy, path)
                logger.info("Migrated registry from %s to %s", legacy, path)
                with open(path, "r") as f:
                    return json.load(f)
            except Exception as exc:
                logger.warning("Registry migration failed: %s", exc)
        return {"commands": {}}
    except json.JSONDecodeError:
        return {"commands": {}}


def save_registry(registry: dict) -> None:
    path = _registry_path()
    with open(path, "w") as f:
        json.dump(registry, f, indent=2)
    logger.info("Registry saved to %s", path)
