"""Form registry — command → NinjaOne form mappings, persisted in form_registry.json."""

import json
import logging
import os
import shutil

logger = logging.getLogger("eng_assist_bot.registry")

# Canonical path: inside the mounted data volume so it survives image rebuilds.
_REGISTRY_FILENAME = "form_registry.json"


def _registry_path(data_dir_override: str | None = None) -> str:
    if data_dir_override is not None:
        d = data_dir_override
    else:
        from ninja_auth import data_dir as _default_data_dir
        d = _default_data_dir()
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, _REGISTRY_FILENAME)


def _legacy_path() -> str:
    """Original path (next to bot.py) — only used for one-time migration."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), _REGISTRY_FILENAME)


def load_registry(data_dir: str | None = None) -> dict:
    path = _registry_path(data_dir)
    try:
        with open(path, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        # Only attempt legacy migration when using the default path
        if data_dir is None:
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


def save_registry(registry: dict, data_dir: str | None = None) -> None:
    path = _registry_path(data_dir)
    with open(path, "w") as f:
        json.dump(registry, f, indent=2)
    logger.info("Registry saved to %s", path)
