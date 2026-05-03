from __future__ import annotations
import json
import time
from pathlib import Path

from app.guardrails.base import GuardrailVerdict

_RULES_PATH = Path(__file__).resolve().parent.parent / "data" / "pricing_rules.json"

class PriceGuardrail:
    """
    Deterministic check on apply_markdown / discount-in-reply requests.

    A markdown is allowed iff:
      - new_price >= cost * (1 + margin_floor_pct)         (per-listing margin floor)
      - pct       <= category.max_markdown_pct             (category cap)
      - pct       <= global.max_markdown_pct               (hard global cap)

    Auto-send only if pct <= category.auto_send_max_markdown_pct, else action=human.
    """
    def __init__(self) -> None:
        try:
            self.rules = json.loads(_RULES_PATH.read_text())
        except FileNotFoundError:
            self.rules = {"global": {"max_markdown_pct": 0.20, "auto_send_max_markdown_pct": 0.10},
                          "by_category": {}}

    def check(self, listing: dict, markdown_pct: float) -> GuardrailVerdict:
        t0 = time.perf_counter()
        reasons: list[str] = []
        cat = listing.get("category", "Accessories")
        cat_rules = self.rules.get("by_category", {}).get(cat, {})
        global_rules = self.rules.get("global", {})

        max_pct = min(
            cat_rules.get("max_markdown_pct", global_rules.get("max_markdown_pct", 0.20)),
            global_rules.get("max_markdown_pct", 0.20),
        )
        auto_pct = cat_rules.get("auto_send_max_markdown_pct", global_rules.get("auto_send_max_markdown_pct", 0.10))

        price = float(listing["price"])
        cost = float(listing["cost"])
        floor_pct = float(listing.get("margin_floor_pct", 0.10))
        new_price = round(price * (1 - markdown_pct), 2)
        floor_price = round(cost * (1 + floor_pct), 2)

        action = "allow"
        if markdown_pct < 0:
            reasons.append("negative_markdown")
            action = "block"
        if markdown_pct > max_pct:
            reasons.append(f"exceeds_max_markdown_{cat}({max_pct:.0%})")
            action = "block"
        if new_price < floor_price:
            reasons.append(f"below_margin_floor:new=${new_price}<floor=${floor_price}")
            action = "block"
        if action == "allow" and markdown_pct > auto_pct:
            action = "human"
            reasons.append(f"requires_human_above_{auto_pct:.0%}")

        return GuardrailVerdict(
            layer="price",
            action=action,
            reasons=reasons,
            meta={
                "category": cat,
                "old_price": price, "cost": cost, "floor_price": floor_price,
                "max_pct": max_pct, "auto_pct": auto_pct,
                "requested_pct": markdown_pct, "new_price": new_price,
            },
            latency_ms=(time.perf_counter() - t0) * 1000,
        )
