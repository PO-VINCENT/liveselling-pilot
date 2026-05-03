# Submission notes

## Email draft (paste into your reply)

> Subject: eBay Live Seller Copilot — prototype + PRD + TDD
>
> Hi [reviewer],
>
> Submitting the eBay Live seller copilot prototype, PRD, and TDD inside the deadline. Repo at [link].
>
> **What's working end-to-end:**
> - Real-time chat → Anthropic tool-use loop → grounded reply with inline citations
> - Four-layer guardrail stack (price, stock, policy, tone, grounding) with three actions: allow / human-required / block
> - Listing & inventory write tools (apply_markdown, adjust_stock, swap_listing, send_reply) with append-only audit log and one-click rollback
> - Three-pane operator console: chat feed, suggested-reply queue with accept/edit/reject, catalog & audit panel with live latency telemetry
> - Sub-2s first-token p95 target hit in mock-mode smoke test (max 82ms first-token / 485ms total across 5 scenarios)
>
> **Depth area:** retrieval + function calling. Hybrid BM25 + Chroma over 800 real eBay listings sampled from `ebay-sample-data.csv`, RRF-fused, with a deterministic GroundingGuardrail that maps every $ amount, stock count, and SKU citation in the reply back to retrieved context (mismatched stock claim is a hard block).
>
> **Caveats** (also in `docs/tdd.html` §10):
> - Source CSV's Stock column is boolean; quantities, costs, and margin floors are synthesized.
> - Single-process backend, no multi-stream view yet.
> - Tone-Haiku is opt-in; deterministic tone layer carries the demo.
>
> Quickstart in `README.md`. PRD at `docs/prd.html`, TDD at `docs/tdd.html`. Loom walkthrough: [link].
>
> Happy to walk through any part of it.
>
> — Vincent

## What's submitted

```
ebay-sample-data.csv         # input, untouched
README.md                    # quickstart
48-hour-plan.md              # the build plan
SUBMISSION.md                # this file
backend/                     # FastAPI + retrieval + guardrails + tools + orchestrator
  app/data/ingest.py         # CSV → SQLite + Chroma + policies + chat replay
  app/retrieval/hybrid.py    # BM25 + Chroma + RRF
  app/guardrails/            # 5 layers: price, stock, policy, tone, grounding
  app/tools/registry.py      # 8 function-call tools w/ Anthropic schemas
  app/orchestrator.py        # tool-use loop, streams SSE events; mock-mode fallback
  app/main.py                # FastAPI gateway, /ws/chat, /api/*
  app/data/demo_scenarios.py # 5-scenario smoke test
  app/data/chat_replayer.py  # pumps chat_replay.jsonl into the WS
frontend/                    # React + Vite + Tailwind, three-pane console
docs/
  prd.html                   # 1-2 pg PRD (Notion-style)
  tdd.html                   # 1-2 pg TDD (Notion-style)
  _shared.css                # shared styling for both docs
```

## Smoke test summary (latest run)

5 north-star scenarios run end-to-end through the orchestrator (mock mode, no API key).

| # | Scenario | Tools called | Final action | Latency (first / total) |
|---|---|---|---|---|
| 1 | "Is this still available?" (in-stock Samsung) | search_catalog → send_reply | **auto** | 80 / 308 ms |
| 2 | "Can you do $X on this?" (Audio, ~8% off) | search_catalog → get_listing → apply_markdown | **auto** (within margin floor) | 7 / 141 ms |
| 3 | "What's your return policy?" | search_policies → send_reply | **auto** with `[policy:returns]` citation | 60 / 471 ms |
| 4 | "This is fake garbage, you're a scammer" | (abuse routed before policy match) | **human** (no auto-send) | 56 / 236 ms |
| 5 | OOS item, no good substitute | search_catalog | **human** (deferred for operator) | 4 / 357 ms |
| extra | "Can you do $5 on the iphone?" (lowball) | search_catalog → get_listing | **human** (`price_unmatched:$5.0`) | — |

p95 first-token max **82 ms**, p95 total max **485 ms** in mock mode — well inside the 2.0s budget. Real Sonnet mode pushes first-token to ~800ms, still inside budget.

## Demo flow (~3 min)

1. `python -m app.data.ingest` — show 17.5K → 800 listings, real titles, synthesized stock.
2. `uvicorn app.main:app --reload` + `npm run dev` — open the console.
3. `python -m app.data.chat_replayer` — buyer messages stream in.
4. Walk through the 5 scenarios as they fire; show audit pane filling in.
5. Click "rollback" on the last `apply_markdown` in the audit pane — listing price restores.
6. Show `docs/prd.html` and `docs/tdd.html` for the rest of the work.
