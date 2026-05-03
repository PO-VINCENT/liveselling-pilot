from __future__ import annotations
import re
import time
from app.guardrails.base import GuardrailVerdict

# Patterns that should trigger explicit policy reference
POLICY_TRIGGER_PATTERNS = {
    "returns":      re.compile(r"\b(returns?|refunds?|exchanges?|money back)\b", re.I),
    "shipping":     re.compile(r"\b(ships?|shipping|delivery|deliveries|tracking|delivers?|delivered|arrives?)\b", re.I),
    "authenticity": re.compile(r"\b(authentic|authenticity|fake|real|genuine|original|counterfeit|replica)\b", re.I),
}

# Banned claim patterns in the *seller's* reply
BANNED_CLAIMS = [
    (re.compile(r"\bguarantee[d]? to (arrive|be delivered) (by|on)\b", re.I), "specific_delivery_guarantee"),
    (re.compile(r"\b(price|this) won['’]?t go up\b", re.I), "permanent_price_promise"),
    (re.compile(r"\b(cure|treat|prevent|heal)s?\s+\w+", re.I), "health_claim"),
    (re.compile(r"\b(other (sellers|stores))\b.*\b(scam|fake|terrible|bad|worse)\b", re.I), "competitor_disparagement"),
]


class PolicyGuardrail:
    """
    Two responsibilities:
      1) If the conversation is on a policy topic, the reply must include a
         [policy:...] citation pulled from retrieved_policies.
      2) Reject banned claims regardless of input.
    """

    def check(self, text: str, retrieved_policies: list[dict] | None = None) -> GuardrailVerdict:
        t0 = time.perf_counter()
        retrieved_policies = retrieved_policies or []
        reasons: list[str] = []
        action: str = "allow"

        for code, (pat, label) in zip(range(len(BANNED_CLAIMS)), BANNED_CLAIMS):
            if pat.search(text):
                reasons.append(f"banned_claim:{label}")
                action = "block"

        # Did the seller's reply touch a policy topic?
        triggered = [name for name, pat in POLICY_TRIGGER_PATTERNS.items() if pat.search(text)]
        if triggered:
            cited_policies = {p.get("policy") for p in retrieved_policies if isinstance(p, dict)}
            text_cites_policy = bool(re.search(r"\[policy:[\w_]+\]", text))
            missing = [t for t in triggered if t not in cited_policies and not text_cites_policy]
            if missing and action != "block":
                reasons.append(f"missing_policy_citation:{','.join(missing)}")
                action = "human"  # downgrade rather than block — let operator review

        return GuardrailVerdict(
            layer="policy",
            action=action,
            reasons=reasons,
            meta={"triggered_topics": triggered},
            latency_ms=(time.perf_counter() - t0) * 1000,
        )
