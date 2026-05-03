"""
End-to-end smoke test: runs the 5 north-star scenarios through the orchestrator
(in mock mode if ANTHROPIC_API_KEY is unset) and prints what auto-sent vs.
required human review vs. got blocked.

Usage:
    python -m app.data.demo_scenarios
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

from app.db import DB_PATH
from app.orchestrator import run_turn


SCENARIOS = [
    {"id": "S1_availability", "buyer_handle": "@mia_k",
     "text": None,  # filled in from real catalog
     "build_query": lambda con: _pick_in_stock(con, "Smartphones"),
     "expect": ("auto", "search_catalog", "send_reply"),
    },
    {"id": "S2_negotiation", "buyer_handle": "@dan99",
     "text": None,
     "build_query": lambda con: _pick_negotiation(con, "Audio"),
     "expect": ("human|auto", "search_catalog", "get_listing"),
    },
    {"id": "S3_returns_policy", "buyer_handle": "@sara",
     "text": "what's your return policy on opened electronics?",
     "expect": ("auto", "search_policies", "send_reply"),
    },
    {"id": "S4_abuse", "buyer_handle": "@troll",
     "text": "this is fake garbage, you're a scammer",
     "expect": ("human", "send_reply"),
    },
    {"id": "S5_oos_substitute", "buyer_handle": "@kev",
     "text": None,
     "build_query": lambda con: _pick_oos_with_substitute(con),
     "expect": ("human", "search_catalog"),
    },
]


def _pick_in_stock(con, category: str) -> str:
    row = con.execute(
        "SELECT title, manufacturer, internal_memory FROM listings "
        "WHERE category = ? AND stock_qty > 2 ORDER BY price DESC LIMIT 1",
        (category,),
    ).fetchone()
    if not row:
        return "do you have any iphones?"
    mem = f" in {row['internal_memory']}" if row["internal_memory"] else ""
    return f"hey is the {row['manufacturer']} {row['title'][:60]}{mem} still available?"

def _pick_negotiation(con, category: str) -> str:
    row = con.execute(
        "SELECT title, manufacturer, price FROM listings "
        "WHERE category = ? AND stock_qty > 0 AND price > 50 ORDER BY price DESC LIMIT 1",
        (category,),
    ).fetchone()
    if not row:
        row = con.execute("SELECT title, manufacturer, price FROM listings WHERE stock_qty > 0 ORDER BY price DESC LIMIT 1").fetchone()
    target = max(1, int(row["price"] * 0.92))  # ask ~8% off
    return f"can you do ${target} on the {row['manufacturer']}?"

def _pick_oos_with_substitute(con) -> str:
    row = con.execute(
        "SELECT title, manufacturer FROM listings WHERE stock_qty = 0 ORDER BY RANDOM() LIMIT 1"
    ).fetchone()
    if not row:
        return "is the iphone 14 pro available?"
    return f"is the {row['manufacturer']} {row['title'][:60]} available?"


async def run_scenario(scn: dict) -> dict:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    text = scn["text"] or scn["build_query"](con)
    con.close()
    print(f"\n=== {scn['id']} ===  buyer={scn['buyer_handle']}")
    print(f"    msg: {text}")

    tool_calls: list[str] = []
    final_payload = None
    first_token_ms = None
    total_ms = None
    blocked_reasons: list[str] = []

    async for ev in run_turn(text, buyer_handle=scn["buyer_handle"], session_id=f"smoke_{scn['id']}"):
        if ev["type"] == "tool_use":
            tool_calls.append(ev["name"])
        elif ev["type"] == "first_token_ms":
            first_token_ms = ev["ms"]
        elif ev["type"] == "reply":
            final_payload = ev["payload"]
            if final_payload.get("guardrail"):
                blocked_reasons = final_payload["guardrail"].get("reasons") or []
        elif ev["type"] == "latency":
            total_ms = ev.get("total_ms")

    final_action = (final_payload or {}).get("final_action")
    delivered = (final_payload or {}).get("delivered")
    print(f"    tools called : {tool_calls}")
    print(f"    final_action : {final_action} (delivered={delivered})")
    print(f"    latency      : first-token={first_token_ms}ms total={total_ms}ms")
    if blocked_reasons:
        print(f"    guardrail    : {blocked_reasons}")
    if final_payload and final_payload.get("text"):
        print(f"    reply text   : {final_payload['text'][:140]}")
    return {
        "id": scn["id"],
        "ok": bool(final_payload),
        "final_action": final_action,
        "first_token_ms": first_token_ms,
        "total_ms": total_ms,
        "tool_calls": tool_calls,
        "guardrail_reasons": blocked_reasons,
    }


async def main():
    results = []
    for scn in SCENARIOS:
        results.append(await run_scenario(scn))

    print("\n--- summary ---")
    auto = sum(1 for r in results if r["final_action"] == "auto")
    human = sum(1 for r in results if r["final_action"] == "human")
    block = sum(1 for r in results if r["final_action"] == "block")
    print(f"auto_sent={auto}  needs_human={human}  blocked={block}  scenarios={len(results)}")
    p95_first = sorted([r["first_token_ms"] for r in results if r["first_token_ms"]])[-1] if any(r["first_token_ms"] for r in results) else None
    p95_total = sorted([r["total_ms"] for r in results if r["total_ms"]])[-1] if any(r["total_ms"] for r in results) else None
    print(f"first-token max={p95_first}ms  total max={p95_total}ms  (target <2000ms)")
    Path("smoke_results.json").write_text(json.dumps(results, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
