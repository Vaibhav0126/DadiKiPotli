"""Content-hash guards so a re-run never regenerates (or re-pays for) unchanged assets.

Every artifact lives at a deterministic path with a sibling `<name>.key` file
holding the hash of the inputs that produced it. `is_fresh` is the only check
callers need: if the inputs are unchanged and the file is still there, skip.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def key_for(**parts: Any) -> str:
    blob = json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _key_path(artifact: Path) -> Path:
    return artifact.with_suffix(artifact.suffix + ".key")


def is_fresh(artifact: Path, key: str) -> bool:
    if not artifact.exists() or artifact.stat().st_size == 0:
        return False
    kp = _key_path(artifact)
    return kp.exists() and kp.read_text(encoding="utf-8").strip() == key


def mark_fresh(artifact: Path, key: str) -> None:
    _key_path(artifact).write_text(key, encoding="utf-8")
