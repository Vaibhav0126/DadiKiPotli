"""Spend ledger with a hard pre-flight cap.

Only motion_engine spends money. The cap is checked *before* each call so a
runaway loop cannot overspend, and the ledger is persisted so a resumed run
accounts for what earlier attempts already cost.
"""

from __future__ import annotations

import json
from pathlib import Path


class BudgetExceeded(RuntimeError):
    pass


class CostLedger:
    def __init__(self, path: Path, max_usd: float) -> None:
        self.path = path
        self.max_usd = max_usd
        self.entries: list[dict] = []
        if path.exists():
            try:
                self.entries = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                self.entries = []

    @property
    def spent(self) -> float:
        return round(sum(e["usd"] for e in self.entries), 4)

    @property
    def remaining(self) -> float:
        return round(self.max_usd - self.spent, 4)

    def check_affordable(self, usd: float, label: str) -> None:
        if self.spent + usd > self.max_usd + 1e-9:
            raise BudgetExceeded(
                f"{label} would cost ${usd:.3f}, taking the run to "
                f"${self.spent + usd:.3f} against a ${self.max_usd:.2f} cap. "
                f"Raise motion.max_cost_usd or lower motion.hero_scene_count."
            )

    def record(self, usd: float, label: str) -> None:
        self.entries.append({"label": label, "usd": round(usd, 4)})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.entries, indent=2), encoding="utf-8")
