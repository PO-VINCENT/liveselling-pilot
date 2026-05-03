"""Append-only audit log helpers + rollback."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from app.db import get_conn


def log_action(
    *,
    session_id: str,
    tool_name: str,
    input: dict[str, Any],
    output: dict[str, Any],
    guardrail_verdict: dict[str, Any] | None = None,
    reverse_of: int | None = None,
) -> int:
    con = get_conn()
    cur = con.execute(
        """INSERT INTO audit_log (ts, session_id, tool_name, input_json, output_json, guardrail_verdict, reverse_of)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            datetime.now(timezone.utc).isoformat(),
            session_id,
            tool_name,
            json.dumps(input),
            json.dumps(output),
            json.dumps(guardrail_verdict) if guardrail_verdict else None,
            reverse_of,
        ),
    )
    audit_id = cur.lastrowid
    con.commit()
    con.close()
    return audit_id


def get_action(audit_id: int) -> dict | None:
    con = get_conn()
    row = con.execute("SELECT * FROM audit_log WHERE audit_id = ?", (audit_id,)).fetchone()
    con.close()
    if not row:
        return None
    d = dict(row)
    d["input"] = json.loads(d.pop("input_json"))
    d["output"] = json.loads(d.pop("output_json"))
    if d.get("guardrail_verdict"):
        try:
            d["guardrail_verdict"] = json.loads(d["guardrail_verdict"])
        except Exception:
            pass
    return d


def list_actions(session_id: str | None = None, limit: int = 50) -> list[dict]:
    con = get_conn()
    if session_id:
        rows = con.execute(
            "SELECT audit_id, ts, session_id, tool_name, input_json, output_json, reversed FROM audit_log WHERE session_id = ? ORDER BY audit_id DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
    else:
        rows = con.execute(
            "SELECT audit_id, ts, session_id, tool_name, input_json, output_json, reversed FROM audit_log ORDER BY audit_id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    con.close()
    out = []
    for r in rows:
        d = dict(r)
        d["input"] = json.loads(d.pop("input_json"))
        d["output"] = json.loads(d.pop("output_json"))
        out.append(d)
    return out


def mark_reversed(audit_id: int) -> None:
    con = get_conn()
    con.execute("UPDATE audit_log SET reversed = 1 WHERE audit_id = ?", (audit_id,))
    con.commit()
    con.close()
