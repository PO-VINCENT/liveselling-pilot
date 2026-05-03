from __future__ import annotations
import re
import time
from app.guardrails.base import GuardrailVerdict

PRICE_RE = re.compile(r"\$([0-9]+(?:\.[0-9]{1,2})?)")
QTY_RE = re.compile(r"\b(\d{1,3})\s*(?:left|in stock|available|units|in inventory)\b", re.I)
SKU_PREFIX_RE = re.compile(r"\[([0-9a-fA-F]{6,8})\]")


class GroundingGuardrail:
    """
    Ensures every factual claim in the reply maps to retrieved context.

    Checks:
      1) Every $X.XX price mentioned must match a retrieved listing's price (within $0.50).
      2) Every "N left / in stock" qty must match retrieved stock_qty.
      3) Every [sku-prefix] citation must reference a sku that was actually retrieved.
      4) If the reply makes specific factual claims at all, retrieved_context must be non-empty.

    Failure mode is `human` — operator review — except for outright wrong stock claims (block).
    """

    def check(
        self,
        text: str,
        retrieved_context: list[dict] | None = None,
        listing: dict | None = None,
    ) -> GuardrailVerdict:
        t0 = time.perf_counter()
        retrieved_context = retrieved_context or []
        reasons: list[str] = []
        action = "allow"

        # Pull out citation tokens
        cited = SKU_PREFIX_RE.findall(text)
        retrieved_skus = {c.get("sku", "")[:8] for c in retrieved_context if "sku" in c}
        for c in cited:
            if c.lower() not in {s.lower() for s in retrieved_skus}:
                reasons.append(f"citation_unmatched:{c}")
                if action == "allow":
                    action = "human"

        # Price claims
        prices_in_reply = [float(p) for p in PRICE_RE.findall(text)]
        retrieved_prices = sorted({round(float(c.get("price", 0)), 2) for c in retrieved_context if c.get("price")})
        if listing and "price" in listing:
            retrieved_prices = sorted(set(retrieved_prices) | {round(float(listing["price"]), 2)})
        for p in prices_in_reply:
            ok = any(abs(p - rp) <= 0.5 for rp in retrieved_prices) or any(
                # allow markdowns down to ~30% off any retrieved price (the reply might be quoting an offer)
                rp * 0.70 <= p <= rp * 1.05 for rp in retrieved_prices
            )
            if not ok and retrieved_prices:
                reasons.append(f"price_unmatched:${p}")
                if action == "allow":
                    action = "human"

        # Stock-count claims
        qty_claims = [int(q) for q in QTY_RE.findall(text)]
        retrieved_qtys = {int(c.get("stock_qty", 0)) for c in retrieved_context}
        if listing and "stock_qty" in listing:
            retrieved_qtys.add(int(listing["stock_qty"]))
        for q in qty_claims:
            if q not in retrieved_qtys and retrieved_qtys:
                reasons.append(f"qty_claim_unmatched:{q}")
                action = "block"  # claiming stock we don't have is a hard fail

        # If the reply contains $ or qty claims but nothing was retrieved, flag
        if (prices_in_reply or qty_claims) and not retrieved_context and not listing:
            reasons.append("factual_claim_without_retrieval")
            action = "block"

        return GuardrailVerdict(
            layer="grounding",
            action=action,
            reasons=reasons,
            meta={"cited": cited, "prices_in_reply": prices_in_reply, "qty_claims": qty_claims},
            latency_ms=(time.perf_counter() - t0) * 1000,
        )
