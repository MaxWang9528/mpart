"""Persistent user settings stored as JSON in the user's config folder."""

import json
import os
import sys
from pathlib import Path

DEFAULTS = {
    "quick_split_dir": "",  # empty means the app's folder
    "quick_join_dir": "",
    "part_size": "2",
    "part_unit": "GB",
}


def settings_path():
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "mpart" / "settings.json"


def app_dir():
    """Folder containing the executable (or the project, when run from source)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def load():
    settings = dict(DEFAULTS)
    try:
        data = json.loads(settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return settings
    if isinstance(data, dict):
        for key, default in DEFAULTS.items():
            if isinstance(data.get(key), type(default)):
                settings[key] = data[key]
    return settings


def save(settings):
    path = settings_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(settings, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass  # settings are a convenience; never crash over them
