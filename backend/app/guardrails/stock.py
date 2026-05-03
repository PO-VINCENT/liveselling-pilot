from __future__ import annotations
import time
from app.guardrails.base import GuardrailVerdict

class StockGuardrail:
    """Inventory mutation sanity check + claim-vs-reality check."""

    def check_adjust(self, listing: dict, delta: int, new_qty: int) -> GuardrailVerdict:
        t0 = time.perf_counter()
        reasons = []
        action = "allow"
        if new_qty < 0:
            reasons.append("would_go_negative")
            action = "block"
        if abs(delta) > 100:
            reasons.append("delta_exceeds_100_units")
            action = "human"
        return GuardrailVerdict(
            layer="stock",
            action=action,
            reasons=reasons,
            meta={"old_qty": int(listing["stock_qty"]), "delta": delta, "new_qty": new_qty},
            latency_ms=(time.perf_counter() - t0) * 1000,
        )

    def check_claim(self, listing: dict, claimed_qty: int | None) -> GuardrailVerdict:
        """If reply claims a specific stock count, it must match reality."""
        t0 = time.perf_counter()
        actual = int(listing.get("stock_qty", 0))
        reasons = []
        action = "allow"
        if claimed_qty is not None:
            if actual == 0 and claimed_qty > 0:
                reasons.append(f"claimed_in_stock_but_zero")
                action = "block"
            elif claimed_qty > actual:
                reasons.append(f"claimed_{claimed_qty}_but_have_{actual}")
                action = "block"
        return GuardrailVerdict(
            layer="stock",
            action=action,
            reasons=reasons,
            meta={"actual_qty": actual, "claimed_qty": claimed_qty},
            latency_ms=(time.perf_counter() - t0) * 1000,
        )
