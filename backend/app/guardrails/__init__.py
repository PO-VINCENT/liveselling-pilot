"""
Guardrail stack — every write tool runs through one or more of these.

Verdict actions:
  - "allow"  : proceed normally
  - "warn"   : proceed but surface a warning to the operator
  - "human"  : require operator click, do not auto-send
  - "block"  : refuse outright

Layers:
  - PriceGuardrail     : margin floor, max markdown %, per-category caps
  - StockGuardrail     : non-negative inventory, large-delta sanity check
  - PolicyGuardrail    : reply must reference policy text when buyer asked policy questions; banned-claim detection
  - ToneGuardrail      : Haiku-classified tone (default keyword fallback if no API key)
  - GroundingGuardrail : every $ amount, stock count, and SKU mention in reply must trace to retrieved context
"""
from app.guardrails.base import GuardrailVerdict
from app.guardrails.price import PriceGuardrail
from app.guardrails.stock import StockGuardrail
from app.guardrails.policy import PolicyGuardrail
from app.guardrails.tone import ToneGuardrail
from app.guardrails.grounding import GroundingGuardrail

__all__ = [
    "GuardrailVerdict",
    "PriceGuardrail",
    "StockGuardrail",
    "PolicyGuardrail",
    "ToneGuardrail",
    "GroundingGuardrail",
]
