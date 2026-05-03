"""
Intent extraction for buyer queries.

Decomposes a free-text buyer message into structured aspects so each can
drive its own retrieval path.

Two backends:
  1. Haiku (preferred) — strict-JSON structured output, ~150-300ms.
  2. Deterministic regex (fallback) — sub-1ms, no API key required.

The regex fallback is good enough that the smoke test passes against it;
Haiku materially helps on novel phrasings ("got the new pixel?",
"anything like the macbook pro but smaller").
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field

CATEGORIES = [
    "Smartphones", "Tablets", "Laptops", "Audio", "Wearables",
    "Gaming", "Cameras", "TVs/Displays", "Accessories",
]

# Subset of brands that show up in the eBay sample data, used by both
# the regex extractor and as the allow-list for the Haiku call.
COMMON_MANUFACTURERS = [
    "Apple", "Samsung", "Sony", "Nintendo", "Microsoft", "Google",
    "Bose", "Beats", "Fitbit", "Garmin", "DJI", "LG", "Motorola",
    "Huawei", "Amazon", "PowerA", "Sennheiser", "JBL", "Anker", "GoPro",
]

CATEGORY_KEYWORDS = {
    "Smartphones": ["iphone", "phone", "galaxy", "pixel", "smartphone", "moto"],
    "Audio":       ["airpod", "headphone", "earbud", "speaker", "soundbar", "headset", "earphone"],
    "Wearables":   ["apple watch", "watch", "fitbit", "garmin", "tracker", "wristband"],
    "Gaming":      ["nintendo", "switch", "playstation", "ps4", "ps5", "xbox", "console", "controller", "joy-con"],
    "Tablets":     ["ipad", "tablet", "galaxy tab", "surface"],
    "Laptops":     ["macbook", "laptop", "notebook", "thinkpad", "chromebook"],
    "Cameras":     ["camera", "gopro", "drone", "dji", "lens"],
    "TVs/Displays":["tv", "monitor", "display"],
}

COLORS = [
    "black", "white", "gray", "grey", "silver", "gold", "rose gold",
    "blue", "navy", "red", "green", "purple", "pink", "yellow", "orange",
    "midnight", "starlight", "graphite",
]


@dataclass
class Intent:
    raw_query: str = ""
    intention: str = "general"     # availability | price_negotiation | policy_question | recommendation | comparison | general
    product_name: str | None = None
    manufacturer: str | None = None
    category: str | None = None
    price_target: float | None = None     # buyer's offered/desired price
    price_max: float | None = None        # explicit upper bound ("under $400")
    quantity_required: int | None = None  # "I'll take 2"
    color: str | None = None
    memory: str | None = None             # "256GB"

    def to_dict(self) -> dict:
        return asdict(self)

    def has_aspects(self) -> bool:
        return any([
            self.product_name, self.manufacturer, self.category,
            self.price_target is not None, self.price_max is not None,
            self.quantity_required, self.color, self.memory,
        ])

    def hard_constraints(self) -> dict:
        """Constraints the judge step uses for top-1 acceptance."""
        c = {}
        if self.manufacturer: c["manufacturer"] = self.manufacturer
        if self.category: c["category"] = self.category
        if self.quantity_required: c["min_stock_qty"] = self.quantity_required
        if self.price_max is not None: c["max_price"] = self.price_max
        if self.price_target is not None: c["price_target"] = self.price_target
        if self.memory: c["memory"] = self.memory
        return c


# --------- regex fallback ---------

_PRICE_RE = re.compile(r"\$\s?(\d+(?:\.\d{1,2})?)")
_UNDER_RE = re.compile(r"\b(under|below|less than|cheaper than)\s+\$?\s?(\d+(?:\.\d{1,2})?)\b", re.I)
_QTY_RE   = re.compile(r"\b(\d+)\s*(?:of|x|pcs|pieces|units)\b", re.I)
_TAKE_RE  = re.compile(r"\bi'?ll take (\d+)\b", re.I)
_MEM_RE   = re.compile(r"\b(\d+)\s?(gb|tb)\b", re.I)


_STOPWORDS = re.compile(
    r"\b("
    r"do|does|did|done|you|your|have|has|had|having|any|in|stock|available|got|the|a|an|is|am|are|was|were|be|been|being|on|of|at|to|for|with|from|by|"
    r"hi|hey|hello|please|thanks|thank|i|me|my|we|us|our|"
    r"can|could|would|will|wouldn|won|may|might|should|shall|"
    r"this|that|these|those|new|old|"
    r"like|something|anything|kind|sort|"
    r"sale|deal|deals|discount|cheaper|cheapest|lowest|best|good|nice|"
    r"price|match|"
    r"under|below|over|above|less|more|than|"
    r"about|near|"
    r"size|version|color|colour|model|"
    # interrogatives + filler — pollute BM25 if left in product_name
    r"what|which|who|whom|whose|when|where|why|how|"
    r"there|here|"
    r"so|just|really|actually|maybe|"
    r"much|many|"
    r"and|or|but|nor"
    r")\b",
    re.I,
)
_CONTRACTIONS = re.compile(r"\b(i'm|i'll|i'd|you're|you'll|don't|won't|can't|isn't|it's|we'll|we're|they're)\b", re.I)
_PUNCT = re.compile(r"[?,.!:;]+")


def _detect_intention(text: str) -> str:
    t = text.lower()
    if re.search(r"\b(can you do|how about|deal|discount|cheaper|price match)\b.*\$\d", t) \
       or re.search(r"\$\d+\b.*\b(deal|please)\b", t):
        return "price_negotiation"
    if re.search(r"\b(return|refund|policy|shipping|deliver|ship|warranty|authentic|fake|counterfeit)\b", t):
        return "policy_question"
    if re.search(r"\b(do you have|in stock|available|got any|left|inventory)\b", t):
        return "availability"
    # comparison BEFORE recommendation: "vs" / "versus" / "X or Y" / "better than" are
    # unambiguous comparison signals, even if the message also contains "which" / "best".
    if re.search(r"\b(vs\.?|versus|compared to|better than)\b", t):
        return "comparison"
    if re.search(r"\b(recommend|suggest|best|which|something like|anything like|alternative)\b", t):
        return "recommendation"
    return "general"


def _extract_regex(query: str) -> Intent:
    intent = Intent(raw_query=query)
    intent.intention = _detect_intention(query)

    # manufacturer
    for m in COMMON_MANUFACTURERS:
        if re.search(rf"\b{re.escape(m)}\b", query, re.I):
            intent.manufacturer = m
            break

    # category — first match wins; check more specific keywords first; tolerate plurals
    for cat in ("Audio", "Wearables", "Gaming", "Tablets", "Laptops", "Cameras", "TVs/Displays", "Smartphones"):
        for kw in CATEGORY_KEYWORDS[cat]:
            if re.search(rf"\b{re.escape(kw)}s?\b", query, re.I):
                intent.category = cat
                break
        if intent.category:
            break

    # price
    m_under = _UNDER_RE.search(query)
    if m_under:
        intent.price_max = float(m_under.group(2))
    m_price = _PRICE_RE.search(query)
    if m_price:
        intent.price_target = float(m_price.group(1))

    # quantity
    m_take = _TAKE_RE.search(query) or _QTY_RE.search(query)
    if m_take:
        try:
            intent.quantity_required = int(m_take.group(1))
        except (ValueError, IndexError):
            pass

    # memory
    m_mem = _MEM_RE.search(query)
    if m_mem:
        intent.memory = (m_mem.group(1) + m_mem.group(2)).upper()

    # color
    for c in sorted(COLORS, key=len, reverse=True):  # multi-word first
        if re.search(rf"\b{re.escape(c)}\b", query, re.I):
            intent.color = c.lower()
            break

    # product_name: strip prices/memory/contractions, then stopwords, then trim
    pn = _PRICE_RE.sub("", query)
    pn = re.sub(r"\b\d+\s?(gb|tb)\b", "", pn, flags=re.I)
    pn = _CONTRACTIONS.sub(" ", pn)
    pn = _STOPWORDS.sub(" ", pn)
    pn = _PUNCT.sub(" ", pn)
    pn = re.sub(r"\s+", " ", pn).strip(" -")
    if pn and len(pn) >= 3:
        intent.product_name = pn

    return intent


# --------- LLM extractor ---------

class IntentExtractor:
    """Wraps Haiku call with structured-JSON output; falls back to regex."""
    def __init__(self) -> None:
        self.api_key = os.getenv("ANTHROPIC_API_KEY")
        self.model = os.getenv("ANTHROPIC_GUARDRAIL_MODEL", "claude-haiku-4-5-20251001")

    def extract(self, query: str) -> Intent:
        if not query or not query.strip():
            return Intent(raw_query=query)
        if self.api_key:
            try:
                return self._extract_llm(query)
            except Exception as e:
                print(f"[intent] LLM fallback ({e})")
        return _extract_regex(query)

    def _extract_llm(self, query: str) -> Intent:
        from anthropic import Anthropic
        client = Anthropic(api_key=self.api_key)
        sys = (
            "Extract structured intent from a buyer message on an eBay live shopping stream.\n"
            "Output STRICTLY a single JSON object. Never invent values; use null if unsure.\n"
            "Schema:\n"
            "{\n"
            '  "intention": one of [availability, price_negotiation, policy_question, '
            'recommendation, comparison, general],\n'
            '  "product_name": short product reference string (e.g. "AirPods Pro", "iPhone 13") or null,\n'
            f'  "manufacturer": one of {COMMON_MANUFACTURERS} or null,\n'
            f'  "category": one of {CATEGORIES} or null,\n'
            '  "price_target": number (the price the buyer offered or wants) or null,\n'
            '  "price_max": number (explicit max like "under $400") or null,\n'
            '  "quantity_required": integer (e.g. "I\'ll take 2") or null,\n'
            '  "color": string or null,\n'
            '  "memory": string like "256GB" or null\n'
            "}"
        )
        resp = client.messages.create(
            model=self.model,
            max_tokens=300,
            temperature=0,
            system=sys,
            messages=[{"role": "user", "content": query}],
        )
        text = resp.content[0].text.strip()
        i, j = text.find("{"), text.rfind("}")
        if i < 0 or j <= i:
            return _extract_regex(query)
        data = json.loads(text[i:j + 1])
        return Intent(
            raw_query=query,
            intention=(data.get("intention") or "general"),
            product_name=data.get("product_name"),
            manufacturer=data.get("manufacturer"),
            category=data.get("category"),
            price_target=_safe_float(data.get("price_target")),
            price_max=_safe_float(data.get("price_max")),
            quantity_required=_safe_int(data.get("quantity_required")),
            color=data.get("color"),
            memory=(data.get("memory") or None),
        )


def _safe_float(v) -> float | None:
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _safe_int(v) -> int | None:
    if v in (None, ""):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# module-level singleton
_EXTRACTOR: IntentExtractor | None = None
def get_intent_extractor() -> IntentExtractor:
    global _EXTRACTOR
    if _EXTRACTOR is None:
        _EXTRACTOR = IntentExtractor()
    return _EXTRACTOR
