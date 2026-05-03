"""
Pumps chat_replay.jsonl into the running backend over WebSocket.

Run from backend/:
    python -m app.data.chat_replayer
or with custom params:
    python -m app.data.chat_replayer --session demo1 --speed 2.0
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import websockets

DATA = Path(__file__).resolve().parent
REPLAY = DATA / "chat_replay.jsonl"


async def main(session_id: str, speed: float, ws_url: str) -> None:
    if not REPLAY.exists():
        print(f"[replayer] missing {REPLAY}; run `python -m app.data.ingest` first", file=sys.stderr)
        sys.exit(1)
    msgs = [json.loads(line) for line in REPLAY.read_text().splitlines() if line.strip()]
    url = f"{ws_url}?session_id={session_id}&role=producer"
    print(f"[replayer] connecting to {url}; {len(msgs)} messages, speed={speed}x")
    async with websockets.connect(url, ping_interval=20) as ws:
        for m in msgs:
            await ws.send(json.dumps({
                "type": "buyer_message",
                "text": m["text"],
                "buyer_handle": m["buyer_handle"],
            }))
            print(f"[replayer] -> {m['buyer_handle']}: {m['text'][:80]}")
            await asyncio.sleep(max(0.05, m["delay_ms"] / 1000.0 / speed))
        print("[replayer] done")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", default=os.getenv("SESSION_ID", "demo1"))
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--ws", default=os.getenv("WS_URL", "ws://localhost:8000/ws/chat"))
    args = ap.parse_args()
    asyncio.run(main(args.session, args.speed, args.ws))
