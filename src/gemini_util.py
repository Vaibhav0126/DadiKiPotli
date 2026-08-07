"""Shared Gemini error classification and resilient generate_content calls."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)


def is_missing_model(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "not_found" in text or "404" in text or "not available" in text


def is_capacity_error(exc: BaseException) -> bool:
    """True for overloaded / rate-limited / quota responses (e.g. 503 high demand)."""
    text = str(exc).upper()
    markers = (
        "503",
        "UNAVAILABLE",
        "HIGH DEMAND",
        "RESOURCE_EXHAUSTED",
        "RESOURCE HAS BEEN EXHAUSTED",
        "429",
        "TOO MANY REQUESTS",
        "QUOTA",
        "RATE LIMIT",
        "RATE_LIMIT",
    )
    return any(m in text for m in markers)


def is_retryable_model_error(exc: BaseException) -> bool:
    """Errors where trying the next model (or retrying) may succeed."""
    return is_missing_model(exc) or is_capacity_error(exc)


def generate_with_fallback(
    *,
    models: list[str],
    call: Callable[[str], Any],
    active_model: str | None = None,
    attempts_per_model: int = 3,
    label: str = "Gemini",
) -> tuple[Any, str]:
    """Try models in order; retry capacity errors; skip missing models.

    Returns (response, model_id_used). Raises the last error if all fail.
    """
    candidates = [active_model] if active_model else list(models)
    # If an active model fails capacity, still walk the full list afterward.
    seen: set[str] = set()
    ordered: list[str] = []
    for m in candidates + list(models):
        if m and m not in seen:
            seen.add(m)
            ordered.append(m)

    last_error: BaseException | None = None
    for model in ordered:
        for attempt in range(attempts_per_model):
            try:
                resp = call(model)
                return resp, model
            except Exception as exc:  # noqa: BLE001 - classified below
                last_error = exc
                if is_missing_model(exc):
                    log.debug("%s model %s missing, trying next", label, model)
                    break
                if is_capacity_error(exc):
                    wait = 2**attempt
                    log.warning(
                        "%s model %s capacity/unavailable (%s); "
                        "retry %d/%d in %ss",
                        label,
                        model,
                        exc,
                        attempt + 1,
                        attempts_per_model,
                        wait,
                    )
                    time.sleep(wait)
                    continue
                raise

    raise RuntimeError(
        f"{label} failed after trying {', '.join(ordered)}: {last_error}"
    ) from last_error
