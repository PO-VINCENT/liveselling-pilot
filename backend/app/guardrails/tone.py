from __future__ import annotations
import os
import re
import time

from app.guardrails.base import GuardrailVerdict

_BAD_WORDS = re.compile(
    r"\b(idiot|stupid|moron|dumb|shut up|f[*u]ck|sh[*i]t|damn|piss off|bitch|asshole|crap)\b",
    re.I,
)
_BANNED_PHRASES = [
    re.compile(r"\babsolutely\b", re.I),
    re.compile(r"\bto be honest\b", re.I),
    re.compile(r"\bliterally\b", re.I),
]
# run of >=6 capital letters NOT inside a hyphenated/numeric token (model numbers like SD-1DESKTOP-250 are fine)
_ALL_CAPS_RUN = re.compile(r"(?:^|[^A-Z0-9\-])([A-Z]{6,})(?:$|[^A-Z0-9\-])")


class ToneGuardrail:
    """
    Two-stage tone check:
      1) Cheap deterministic regex layer (sub-1ms).
      2) Optional Haiku classifier for the gray zone (passive-aggressive,
         disparaging, off-brand). Skipped if ANTHROPIC_API_KEY is unset.

    The deterministic layer alone is enough for the demo's banned-phrase + caps + profanity story.
    """

    def __init__(self) -> None:
        self.api_key = os.getenv("ANTHROPIC_API_KEY")
        self.model = os.getenv("ANTHROPIC_GUARDRAIL_MODEL", "claude-haiku-4-5-20251001")

    def check(self, text: str) -> GuardrailVerdict:
        t0 = time.perf_counter()
        reasons: list[str] = []
        action = "allow"

        if _BAD_WORDS.search(text):
            reasons.append("profanity")
            action = "block"
        for pat in _BANNED_PHRASES:
            if pat.search(text):
                reasons.append(f"banned_phrase:{pat.pattern}")
                if action == "allow":
                    action = "warn"
        if _ALL_CAPS_RUN.search(text):
            reasons.append("all_caps_run")
            if action == "allow":
                action = "warn"

        # Optional model-based check for off-brand tone (skipped if no key).
        # Kept simple: only call model if no deterministic block, and only on suspicious length.
        if action != "block" and self.api_key and len(text) > 40:
            verdict = self._classify_with_haiku(text)
            if verdict:
                if verdict.get("action") in ("warn", "human", "block"):
                    reasons.append(f"haiku:{verdict.get('label', 'off_brand')}")
                    action = verdict["action"]

        return GuardrailVerdict(
            layer="tone",
            action=action,
            reasons=reasons,
            latency_ms=(time.perf_counter() - t0) * 1000,
        )

    def _classify_with_haiku(self, text: str) -> dict | None:
        try:
            from anthropic import Anthropic
            client = Anthropic(api_key=self.api_key)
            resp = client.messages.create(
                model=self.model,
                max_tokens=80,
                temperature=0,
                system=(
                    "You are a tone classifier for an eBay seller assistant. "
                    "Classify the SELLER's reply. Output strictly JSON: "
                    '{"label":"on_brand|off_brand|disparaging|sarcastic|abusive","action":"allow|warn|human|block"}'
                ),
                messages=[{"role": "user", "content": text}],
            )
            import json
            blob = resp.content[0].text.strip()
            # Trim to first {...}
            i, j = blob.find("{"), blob.rfind("}")
            if i >= 0 and j > i:
                return json.loads(blob[i:j+1])
        except Exception:
            return None
        return None
