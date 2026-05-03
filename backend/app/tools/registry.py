"""
Function-call tools exposed to the LLM.

Every write tool runs through a guardrail before mutation, writes an audit row,
and returns an `audit_id` so the UI can offer a one-click rollback.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app.audit import log_action, get_action, mark_reversed
from app.db import get_conn
from app.guardrails import (
    PriceGuardrail,
    StockGuardrail,
    PolicyGuardrail,
    ToneGuardrail,
    GroundingGuardrail,
    GuardrailVerdict,
)
from app.retrieval.hybrid import get_retriever
from app.retrieval.multipath import get_multipath_retriever


# ---------- read tools ----------

def get_listing(sku: str, **_: Any) -> dict:
    con = get_conn()
    row = con.execute("SELECT * FROM listings WHERE sku = ?", (sku,)).fetchone()
    con.close()
    if not row:
        return {"ok": False, "error": "sku_not_found", "sku": sku}
    return {"ok": True, "listing": dict(row)}


def search_catalog(query: str, k: int = 3, **_: Any) -> dict:
    """
    Multi-path retrieval (intent → per-aspect paths → RRF rerank → judge).

    Returns:
      ok, query, intent, paths, hits[top k with rrf_score + matched_paths +
      meets_hard + miss_reasons], top1_meets_requirements, alternatives_offered,
      judge_note.

    The LLM gets enough structure to (a) pitch top 1 when it meets all hard
    constraints, or (b) lead with top 1 as "closest" and offer top 2/3 as
    alternatives when it doesn't.
    """
    return get_multipath_retriever().search(query, top_k=k)


def check_inventory(sku: str, **_: Any) -> dict:
    con = get_conn()
    row = con.execute("SELECT sku, stock_qty, title FROM listings WHERE sku = ?", (sku,)).fetchone()
    con.close()
    if not row:
        return {"ok": False, "error": "sku_not_found"}
    return {"ok": True, "sku": sku, "stock_qty": int(row["stock_qty"]), "title": row["title"]}


def search_policies(query: str, k: int = 2, **_: Any) -> dict:
    hits = get_retriever().search_policies(query, k=k)
    return {
        "ok": True,
        "query": query,
        "hits": [{"policy": h.policy, "chunk_id": h.chunk_id, "text": h.text, "score": round(h.score, 3)} for h in hits],
    }


# ---------- write tools (guardrailed) ----------

def apply_markdown(sku: str, pct: float, reason: str = "", session_id: str = "default", **_: Any) -> dict:
    con = get_conn()
    row = con.execute("SELECT * FROM listings WHERE sku = ?", (sku,)).fetchone()
    if not row:
        con.close()
        return {"ok": False, "error": "sku_not_found"}
    listing = dict(row)
    verdict = PriceGuardrail().check(listing=listing, markdown_pct=pct)
    if verdict.action == "block":
        con.close()
        audit_id = log_action(
            session_id=session_id, tool_name="apply_markdown",
            input={"sku": sku, "pct": pct, "reason": reason},
            output={"ok": False, "blocked_by": "PriceGuardrail", "reason": verdict.reasons},
            guardrail_verdict=verdict.to_dict(),
        )
        return {"ok": False, "blocked": True, "audit_id": audit_id, "guardrail": verdict.to_dict()}
    new_price = round(listing["price"] * (1 - pct), 2)
    con.execute("UPDATE listings SET price = ? WHERE sku = ?", (new_price, sku))
    con.commit()
    con.close()
    audit_id = log_action(
        session_id=session_id, tool_name="apply_markdown",
        input={"sku": sku, "pct": pct, "reason": reason, "old_price": listing["price"]},
        output={"ok": True, "new_price": new_price, "old_price": listing["price"]},
        guardrail_verdict=verdict.to_dict(),
    )
    return {
        "ok": True,
        "audit_id": audit_id,
        "sku": sku,
        "old_price": listing["price"],
        "new_price": new_price,
        "guardrail": verdict.to_dict(),
    }


def adjust_stock(sku: str, delta: int, reason: str = "", session_id: str = "default", **_: Any) -> dict:
    con = get_conn()
    row = con.execute("SELECT * FROM listings WHERE sku = ?", (sku,)).fetchone()
    if not row:
        con.close()
        return {"ok": False, "error": "sku_not_found"}
    listing = dict(row)
    new_qty = max(0, int(listing["stock_qty"]) + int(delta))
    verdict = StockGuardrail().check_adjust(listing=listing, delta=int(delta), new_qty=new_qty)
    if verdict.action == "block":
        con.close()
        audit_id = log_action(
            session_id=session_id, tool_name="adjust_stock",
            input={"sku": sku, "delta": delta, "reason": reason},
            output={"ok": False, "blocked_by": "StockGuardrail", "reason": verdict.reasons},
            guardrail_verdict=verdict.to_dict(),
        )
        return {"ok": False, "blocked": True, "audit_id": audit_id, "guardrail": verdict.to_dict()}
    con.execute("UPDATE listings SET stock_qty = ? WHERE sku = ?", (new_qty, sku))
    con.commit()
    con.close()
    audit_id = log_action(
        session_id=session_id, tool_name="adjust_stock",
        input={"sku": sku, "delta": delta, "reason": reason, "old_qty": listing["stock_qty"]},
        output={"ok": True, "new_qty": new_qty, "old_qty": listing["stock_qty"]},
        guardrail_verdict=verdict.to_dict(),
    )
    return {"ok": True, "audit_id": audit_id, "sku": sku, "old_qty": listing["stock_qty"], "new_qty": new_qty,
            "guardrail": verdict.to_dict()}


def swap_listing(from_sku: str, to_sku: str, reason: str = "", session_id: str = "default", **_: Any) -> dict:
    """Mark from_sku stock=0 and surface to_sku as the new featured listing."""
    con = get_conn()
    a = con.execute("SELECT * FROM listings WHERE sku = ?", (from_sku,)).fetchone()
    b = con.execute("SELECT * FROM listings WHERE sku = ?", (to_sku,)).fetchone()
    if not a or not b:
        con.close()
        return {"ok": False, "error": "sku_not_found"}
    old_a_qty = int(a["stock_qty"])
    con.execute("UPDATE listings SET stock_qty = 0 WHERE sku = ?", (from_sku,))
    con.commit()
    con.close()
    audit_id = log_action(
        session_id=session_id, tool_name="swap_listing",
        input={"from_sku": from_sku, "to_sku": to_sku, "reason": reason, "old_a_qty": old_a_qty},
        output={"ok": True, "featured": to_sku, "deactivated": from_sku},
    )
    return {"ok": True, "audit_id": audit_id, "featured": dict(b), "deactivated": from_sku}


def send_reply(
    text: str,
    citations: list[str] | None = None,
    auto: bool = False,
    buyer_handle: str = "",
    session_id: str = "default",
    retrieved_context: list[dict] | None = None,
    markdown_pct: float | None = None,
    listing_for_grounding: dict | None = None,
    **_: Any,
) -> dict:
    """
    Compose-and-send. Runs the full guardrail stack: tone → policy → grounding.
    `auto=True` means the orchestrator wants to auto-send without operator click.
    Guardrails can downgrade auto→suggested or block entirely.
    """
    citations = citations or []
    retrieved_context = retrieved_context or []
    verdicts: list[GuardrailVerdict] = []
    verdicts.append(ToneGuardrail().check(text))
    verdicts.append(PolicyGuardrail().check(text=text, retrieved_policies=retrieved_context))
    verdicts.append(GroundingGuardrail().check(text=text, retrieved_context=retrieved_context, listing=listing_for_grounding))

    block = any(v.action == "block" for v in verdicts)
    require_human = any(v.action == "human" for v in verdicts) or not auto
    final_action = "block" if block else ("human" if require_human else "auto")

    combined = {
        "action": final_action,
        "reasons": [r for v in verdicts for r in v.reasons],
        "by_layer": [v.to_dict() for v in verdicts],
    }

    delivered = False
    if final_action == "auto":
        delivered = True

    con = get_conn()
    cur = con.execute(
        """INSERT INTO messages (session_id, ts, role, text, buyer_handle, citations_json, auto)
           VALUES (?, ?, 'seller', ?, ?, ?, ?)""",
        (
            session_id,
            datetime.now(timezone.utc).isoformat(),
            text,
            buyer_handle,
            json.dumps(citations),
            1 if delivered else 0,
        ),
    )
    message_id = cur.lastrowid
    con.commit()
    con.close()

    audit_id = log_action(
        session_id=session_id, tool_name="send_reply",
        input={"text": text, "citations": citations, "auto_requested": auto, "buyer_handle": buyer_handle},
        output={"ok": True, "message_id": message_id, "delivered": delivered, "final_action": final_action},
        guardrail_verdict=combined,
    )
    return {
        "ok": True,
        "audit_id": audit_id,
        "message_id": message_id,
        "delivered": delivered,
        "final_action": final_action,
        "guardrail": combined,
        "text": text,
        "citations": citations,
    }


def rollback(audit_id: int, session_id: str = "default", **_: Any) -> dict:
    """Reverse a prior write action by audit_id. Supports apply_markdown, adjust_stock, swap_listing."""
    action = get_action(audit_id)
    if not action:
        return {"ok": False, "error": "audit_not_found"}
    if action.get("reversed"):
        return {"ok": False, "error": "already_reversed"}
    tool = action["tool_name"]
    inp = action["input"]
    out = action["output"]
    con = get_conn()
    if tool == "apply_markdown":
        con.execute("UPDATE listings SET price = ? WHERE sku = ?", (inp["old_price"], inp["sku"]))
    elif tool == "adjust_stock":
        con.execute("UPDATE listings SET stock_qty = ? WHERE sku = ?", (inp["old_qty"], inp["sku"]))
    elif tool == "swap_listing":
        con.execute("UPDATE listings SET stock_qty = ? WHERE sku = ?", (inp["old_a_qty"], inp["from_sku"]))
    else:
        con.close()
        return {"ok": False, "error": "tool_not_reversible", "tool": tool}
    con.commit()
    con.close()
    mark_reversed(audit_id)
    new_audit = log_action(
        session_id=session_id, tool_name="rollback",
        input={"reverse_of": audit_id, "tool": tool},
        output={"ok": True, "reversed_audit_id": audit_id},
        reverse_of=audit_id,
    )
    return {"ok": True, "audit_id": new_audit, "reversed": audit_id, "tool": tool}


# ---------- registry ----------

TOOLS: dict[str, Callable[..., dict]] = {
    "get_listing": get_listing,
    "search_catalog": search_catalog,
    "check_inventory": check_inventory,
    "search_policies": search_policies,
    "apply_markdown": apply_markdown,
    "adjust_stock": adjust_stock,
    "swap_listing": swap_listing,
    "send_reply": send_reply,
    "rollback": rollback,  # not exposed to LLM; used by /api/rollback
}


def dispatch_tool(name: str, args: dict, *, session_id: str) -> dict:
    fn = TOOLS.get(name)
    if not fn:
        return {"ok": False, "error": "unknown_tool", "tool": name}
    try:
        return fn(session_id=session_id, **args)
    except TypeError as e:
        return {"ok": False, "error": "bad_arguments", "detail": str(e)}
    except Exception as e:
        return {"ok": False, "error": "tool_failure", "detail": str(e)}


# Anthropic tool-use schemas
ANTHROPIC_TOOL_SCHEMAS = [
    {
        "name": "search_catalog",
        "description": (
            "Search the seller's catalog. Returns:\n"
            "  • intent: structured aspects extracted from the query "
            "(intention, manufacturer, category, product_name, price_target, price_max, "
            "quantity_required, color, memory)\n"
            "  • paths: which retrieval paths fired (TFIDF, vector, manufacturer/category/price filters)\n"
            "  • hits: top 3 ranked listings with rrf_score, matched_paths, meets_hard, miss_reasons\n"
            "  • top1_meets_requirements: bool — true means top hit satisfies all hard constraints\n"
            "  • alternatives_offered: bool — true when judge step says you should surface 2nd/3rd\n"
            "  • judge_note: human-readable summary\n"
            "When alternatives_offered is true: lead with top 1 as the closest match, name the gap "
            "(e.g. 'this one is $429 vs your $400 target'), then offer hits[1] and hits[2] as alternatives.\n"
            "ALWAYS call this first when the buyer asks about a product."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Buyer's natural-language message (verbatim is fine)."},
                "k": {"type": "integer", "description": "Top hits to return (default 3)", "default": 3},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_listing",
        "description": "Fetch the full canonical listing for a SKU. Use after search_catalog when you need every field.",
        "input_schema": {
            "type": "object",
            "properties": {"sku": {"type": "string"}},
            "required": ["sku"],
        },
    },
    {
        "name": "check_inventory",
        "description": "Get current stock_qty for a SKU. Use this BEFORE promising availability.",
        "input_schema": {
            "type": "object",
            "properties": {"sku": {"type": "string"}},
            "required": ["sku"],
        },
    },
    {
        "name": "search_policies",
        "description": "Retrieve relevant policy text (returns, shipping, authenticity, prohibited claims) by query. Use for any policy/process question from the buyer.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "k": {"type": "integer", "default": 2},
            },
            "required": ["query"],
        },
    },
    {
        "name": "apply_markdown",
        "description": "Apply a percentage markdown to a SKU's price. Subject to PriceGuardrail (margin floor, max markdown). Returns audit_id for rollback.",
        "input_schema": {
            "type": "object",
            "properties": {
                "sku": {"type": "string"},
                "pct": {"type": "number", "description": "Fraction 0..1, e.g., 0.10 for 10% off"},
                "reason": {"type": "string"},
            },
            "required": ["sku", "pct"],
        },
    },
    {
        "name": "adjust_stock",
        "description": "Increment or decrement stock_qty by `delta`. Use to correct inventory or swap units.",
        "input_schema": {
            "type": "object",
            "properties": {
                "sku": {"type": "string"},
                "delta": {"type": "integer"},
                "reason": {"type": "string"},
            },
            "required": ["sku", "delta"],
        },
    },
    {
        "name": "swap_listing",
        "description": "Deactivate one SKU and feature another. Useful when from_sku is out of stock and to_sku is the closest substitute.",
        "input_schema": {
            "type": "object",
            "properties": {
                "from_sku": {"type": "string"},
                "to_sku": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["from_sku", "to_sku"],
        },
    },
    {
        "name": "send_reply",
        "description": (
            "Compose and send a reply to the buyer. Set auto=true to attempt auto-send; "
            "guardrails may downgrade to operator-approval. ALWAYS pass citations as a list of "
            "[sku-prefix] or [policy:name] tokens that ground each factual claim."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "citations": {"type": "array", "items": {"type": "string"}},
                "auto": {"type": "boolean", "default": False},
                "buyer_handle": {"type": "string"},
                "markdown_pct": {"type": "number", "description": "If reply is offering a discount, the markdown applied. Optional."},
            },
            "required": ["text"],
        },
    },
]
