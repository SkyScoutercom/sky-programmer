"""Config snapshots on disk. A write cannot start until its snapshot has been saved and read back."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path

from .errors import SafetyRefusal

FORMAT = 1

# What counts as "this board's settings" for the drift check. A pre-write backup doesn't: it records whatever was live
# just before a write, including a script's temporary changes if the write was a restore, or never finished.
BASELINE_REASONS = ("manual", "post-write")


def default_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(base) / "sky-programmer" / "backups"


def _pack(blob: bytes) -> dict:
    return {"b64": base64.b64encode(blob).decode(), "sha256": hashlib.sha256(blob).hexdigest()}


def _unpack(entry: dict, name: str, path) -> bytes:
    blob = base64.b64decode(entry["b64"])
    if hashlib.sha256(blob).hexdigest() != entry["sha256"]:
        raise SafetyRefusal(f"{path}: the {name} checksum does not match, so the file is damaged. Not using it.")
    return blob


def save(directory, ident: dict, reason: str, mc_blob: bytes, app_blob: bytes, mc_values: dict) -> Path:
    """Write atomically and read back. Any failure raises, so the caller stops before writing to the board."""
    folder = Path(directory) / ident["uuid"]
    stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{time.time_ns() % 10**9:09d}"
    path = folder / f"{stamp}-{reason}.json"
    doc = {"app": "SKY programmer", "format": FORMAT, "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
           "reason": reason, "hw": ident["hw"], "fw": ident["fw"], "uuid": ident["uuid"],
           "mcconf": _pack(mc_blob), "appconf": _pack(app_blob), "mcconf_values": mc_values}  # fmt: skip
    try:
        folder.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=folder, suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(doc, f, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        check = load(path)
    except (OSError, ValueError, KeyError) as e:
        raise SafetyRefusal(f"could not save a backup in {folder}: {e}. Nothing was written to the board.") from e
    if check["mc_blob"] != mc_blob:
        raise SafetyRefusal(f"backup {path} did not read back correctly. Nothing was written to the board.")
    return path


def load(path) -> dict:
    with open(path) as f:
        doc = json.load(f)
    if not isinstance(doc, dict) or doc.get("format") != FORMAT or doc.get("app") != "SKY programmer":
        raise SafetyRefusal(f"{path} is not a SKY programmer backup.")
    try:
        doc["mc_blob"] = _unpack(doc["mcconf"], "motor config", path)
        doc["app_blob"] = _unpack(doc["appconf"], "app config", path)
        if not all(isinstance(doc[k], str) for k in ("created", "reason", "hw", "fw", "uuid")):
            raise ValueError("a field has the wrong type")
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        raise SafetyRefusal(f"{path} is damaged, so it can't be used: {e}") from e
    doc["path"] = str(path)
    return doc


def list_backups(directory, uuid: str) -> list[Path]:
    folder = Path(directory) / uuid
    return sorted(folder.glob("*.json")) if folder.is_dir() else []


def latest(directory, uuid: str, fw: str, reasons=BASELINE_REASONS):
    """Newest readable baseline of this board on this firmware, or None."""
    for path in reversed(list_backups(directory, uuid)):
        try:
            doc = load(path)
        except (OSError, ValueError, SafetyRefusal):
            continue
        if doc["fw"] == fw and doc["reason"] in reasons:
            return doc
    return None
