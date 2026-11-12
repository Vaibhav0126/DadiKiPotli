"""The three recurring story families, and how to recognise them in free text."""

from __future__ import annotations

# Canonical names: these are the values stored in ideas_used.txt and uploads.json.
CHARACTER_NAMES = ("Akbar-Birbal", "Tenali Raman", "Vikram-Betaal")

# Human-readable forms for titles and descriptions.
DISPLAY_NAMES = {
    "Akbar-Birbal": "Akbar Birbal",
    "Tenali Raman": "Tenali Raman",
    "Vikram-Betaal": "Vikram Betaal",
}

_KEYWORDS = {
    "Akbar-Birbal": ("akbar", "birbal", "अकबर", "बीरबल"),
    "Tenali Raman": ("tenali", "तेनाली"),
    "Vikram-Betaal": ("vikram", "betaal", "vetal", "विक्रम", "बेताल"),
}


def detect_character(text: str) -> str | None:
    """Return the family a free-text idea or story belongs to, if any."""
    lowered = text.casefold()
    for name in CHARACTER_NAMES:
        if any(word in lowered for word in _KEYWORDS[name]):
            return name
    return None
