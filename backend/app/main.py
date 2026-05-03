"""
FastAPI entrypoint.

Endpoints:
  GET  /api/health
  GET  /api/listings?q=&category=&limit=
  GET  /api/listings/{sku}
  GET  /api/audit?session_id=&limit=
  POST /api/rollback/{audit_id}
  POST /api/markdown                    {sku, pct, reason}
  POST /api/swap                        {from_sku, to_sku}
  POST /api/stock                       {sku, delta, reason}
  GET  /api/messages?session_id=
  POST /api/chat                        body: {text, buyer_handle, session_id}  -> SSE
  WS   /ws/chat?session_id=...          buyer messages in, events out
"""
from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from app.audit import list_actions
from app.db import get_conn
from app.orchestrator import run_turn
from app.tools import dispatch_tool

load_dotenv()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # warm retrievers at boot so the first request isn't slow
    try:
        from app.retrieval.hybrid import get_retriever
        r = get_retriever()
        r.search_listings("startup warmup", k=1)
    except Exception as e:
        print(f"[warmup] {e}")
    yield


app = FastAPI(title="Liveselling Copilot", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health():
    return {"ok": True, "model": os.getenv("ANTHROPIC_REPLY_MODEL", "claude-sonnet-4-6"),
            "api_key_configured": bool(os.getenv("ANTHROPIC_API_KEY"))}


@app.get("/api/listings")
def listings(q: str | None = None, category: str | None = None, limit: int = 20):
    con = get_conn()
    if q:
        rows = con.execute(
            "SELECT * FROM listings WHERE title LIKE ? OR manufacturer LIKE ? LIMIT ?",
            (f"%{q}%", f"%{q}%", limit),
        ).fetchall()
    elif category:
        rows = con.execute("SELECT * FROM listings WHERE category = ? LIMIT ?", (category, limit)).fetchall()
    else:
        rows = con.execute("SELECT * FROM listings ORDER BY stock_qty DESC LIMIT ?", (limit,)).fetchall()
    con.close()
    return {"items": [dict(r) for r in rows]}


@app.get("/api/listings/{sku}")
def get_listing(sku: str):
    res = dispatch_tool("get_listing", {"sku": sku}, session_id="api")
    if not res.get("ok"):
        raise HTTPException(404, res.get("error", "not_found"))
    return res["listing"]


@app.get("/api/audit")
def audit(session_id: str | None = None, limit: int = 50):
    return {"items": list_actions(session_id=session_id, limit=limit)}


@app.post("/api/rollback/{audit_id}")
def rollback(audit_id: int):
    return dispatch_tool("rollback", {"audit_id": audit_id}, session_id="api")


@app.post("/api/markdown")
async def markdown(req: Request):
    body = await req.json()
    return dispatch_tool("apply_markdown", body, session_id="ops")


@app.post("/api/swap")
async def swap(req: Request):
    body = await req.json()
    return dispatch_tool("swap_listing", body, session_id="ops")


@app.post("/api/stock")
async def stock(req: Request):
    body = await req.json()
    return dispatch_tool("adjust_stock", body, session_id="ops")


@app.get("/api/messages")
def messages(session_id: str = "default", limit: int = 50):
    con = get_conn()
    rows = con.execute(
        "SELECT * FROM messages WHERE session_id = ? ORDER BY message_id DESC LIMIT ?",
        (session_id, limit),
    ).fetchall()
    con.close()
    return {"items": [dict(r) for r in rows][::-1]}


@app.post("/api/chat")
async def chat_sse(req: Request):
    """SSE endpoint for one-shot buyer messages (testing convenience)."""
    body = await req.json()
    buyer_handle = body.get("buyer_handle", "@buyer")
    text = body.get("text", "")
    session_id = body.get("session_id", "default")

    # log buyer message
    con = get_conn()
    from datetime import datetime, timezone
    con.execute(
        "INSERT INTO messages (session_id, ts, role, text, buyer_handle) VALUES (?, ?, 'buyer', ?, ?)",
        (session_id, datetime.now(timezone.utc).isoformat(), text, buyer_handle),
    )
    con.commit()
    con.close()

    async def event_gen():
        async for ev in run_turn(text, buyer_handle=buyer_handle, session_id=session_id):
            yield {"event": ev["type"], "data": json.dumps(ev)}

    return EventSourceResponse(event_gen())


# ---------- WebSocket: live chat firehose ----------

class WSHub:
    """Trivial per-session pub/sub so external chat replayers can push messages
    and the operator console can subscribe to events."""

    def __init__(self) -> None:
        self.subscribers: dict[str, set[WebSocket]] = {}

    def add(self, session_id: str, ws: WebSocket) -> None:
        self.subscribers.setdefault(session_id, set()).add(ws)

    def remove(self, session_id: str, ws: WebSocket) -> None:
        self.subscribers.get(session_id, set()).discard(ws)

    async def broadcast(self, session_id: str, event: dict) -> None:
        dead = []
        for ws in self.subscribers.get(session_id, set()):
            try:
                await ws.send_json(event)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.remove(session_id, ws)


hub = WSHub()


@app.websocket("/ws/chat")
async def ws_chat(ws: WebSocket):
    await ws.accept()
    qp = ws.query_params
    session_id = qp.get("session_id", "default")
    role = qp.get("role", "operator")  # "operator" subscribes; "producer" pushes
    hub.add(session_id, ws)
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except Exception:
                msg = {"type": "buyer_message", "text": raw, "buyer_handle": "@buyer"}
            mtype = msg.get("type", "buyer_message")
            if mtype == "buyer_message":
                # log + broadcast + run orchestrator
                from datetime import datetime, timezone
                buyer_handle = msg.get("buyer_handle", "@buyer")
                text = msg.get("text", "")
                con = get_conn()
                con.execute(
                    "INSERT INTO messages (session_id, ts, role, text, buyer_handle) VALUES (?, ?, 'buyer', ?, ?)",
                    (session_id, datetime.now(timezone.utc).isoformat(), text, buyer_handle),
                )
                con.commit()
                con.close()
                await hub.broadcast(session_id, {"type": "buyer_message", "text": text, "buyer_handle": buyer_handle})
                # run orchestrator and stream events to all subscribers
                asyncio.create_task(_run_and_broadcast(session_id, text, buyer_handle))
            elif mtype == "ping":
                await ws.send_json({"type": "pong"})
    except WebSocketDisconnect:
        hub.remove(session_id, ws)
    except Exception as e:
        print(f"[ws] error: {e}")
        hub.remove(session_id, ws)


async def _run_and_broadcast(session_id: str, text: str, buyer_handle: str) -> None:
    async for ev in run_turn(text, buyer_handle=buyer_handle, session_id=session_id):
        await hub.broadcast(session_id, ev)
