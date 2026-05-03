"""
Orchestrator — Anthropic tool-use loop with streaming.

Flow:
  buyer message in
    -> system prompt + brand_tone + history
    -> Claude with tools (search_catalog, get_listing, search_policies, send_reply, ...)
    -> tool-use loop (max 4 turns)
    -> final send_reply call carries citations + auto flag
    -> guardrail stack inside send_reply may downgrade auto -> human-required

Yields events for SSE streaming:
  {"type":"thinking", "text":"..."}        # visible "what the model is doing now"
  {"type":"tool_use", "name":..., "input":...}
  {"type":"tool_result", "name":..., "result":...}
  {"type":"token", "text":"..."}           # streamed reply tokens (when model writes user-visible text)
  {"type":"reply", "payload": {...}}       # final send_reply tool call result
  {"type":"latency", "first_token_ms": N, "total_ms": N}
  {"type":"error", "detail": "..."}
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import AsyncIterator, Iterable

from app.tools import ANTHROPIC_TOOL_SCHEMAS, dispatch_tool

DATA_DIR = Path(__file__).resolve().parent / "data"
BRAND_TONE_PATH = DATA_DIR / "brand_tone.md"

REPLY_MODEL = os.getenv("ANTHROPIC_REPLY_MODEL", "claude-sonnet-4-6")
MAX_TOOL_TURNS = 4

SYSTEM_PROMPT_TEMPLATE = """You are the AI seller assistant for an eBay Live seller.

Your job: read incoming buyer messages from a live chat, draft a reply grounded in real catalog and policy data, and either auto-send (low-risk) or surface a suggested reply for the operator (higher-risk). You have function-call tools.

CORE RULES
1. Always call `search_catalog` first when the buyer mentions a product. Never speculate.
2. Always call `check_inventory` before promising availability or quoting stock counts.
3. Always call `search_policies` for any return/shipping/authenticity question.
4. When asked about price negotiation, you MAY call `apply_markdown` ONLY for amounts the operator has pre-approved (under {auto_pct_global:.0%}); otherwise propose a counter-offer in the reply and let the operator approve.
5. Every factual claim in the final reply must be grounded by something you retrieved. Cite using `[sku-prefix]` (first 8 chars of sku) for product facts and `[policy:NAME]` for policy facts.
6. Set `auto=true` only when: (a) you used live data, (b) no negotiation, (c) tone is neutral/friendly, (d) buyer is not abusive. Otherwise set `auto=false` (suggested-only).
7. The final action MUST be exactly one `send_reply` tool call. Do not write user-visible text outside `send_reply`.
8. Do NOT lead the reply with "Hi @handle" or any greeting. Open with the answer. The chat UI already shows who is replying.

BRAND TONE
{brand_tone}

OUTPUT
Use tools. End with a single send_reply call.
"""


def build_system_prompt() -> str:
    try:
        brand_tone = BRAND_TONE_PATH.read_text()
    except FileNotFoundError:
        brand_tone = "Warm, direct, knowledgeable. Don't disparage. Don't promise specific delivery dates."
    # auto_pct from pricing rules
    try:
        rules = json.loads((DATA_DIR / "pricing_rules.json").read_text())
        auto_pct = float(rules.get("global", {}).get("auto_send_max_markdown_pct", 0.10))
    except Exception:
        auto_pct = 0.10
    return SYSTEM_PROMPT_TEMPLATE.format(brand_tone=brand_tone.strip(), auto_pct_global=auto_pct)


async def run_turn(
    buyer_message: str,
    *,
    buyer_handle: str,
    session_id: str,
    history: Iterable[dict] | None = None,
) -> AsyncIterator[dict]:
    """
    Runs one buyer-message turn end-to-end. Yields SSE-shaped events.
    Falls back to a deterministic 'mock orchestrator' if ANTHROPIC_API_KEY is not set,
    so the prototype still demos without network.
    """
    t_start = time.perf_counter()
    if not os.getenv("ANTHROPIC_API_KEY"):
        async for ev in _mock_orchestrator(buyer_message, buyer_handle=buyer_handle, session_id=session_id):
            yield ev
        return

    try:
        from anthropic import AsyncAnthropic
    except ImportError:
        yield {"type": "error", "detail": "anthropic SDK not installed"}
        return

    client = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    system = build_system_prompt()
    msgs = list(history or [])
    msgs.append({"role": "user", "content": f"@{buyer_handle}: {buyer_message}"})

    retrieved_for_grounding: list[dict] = []
    listing_for_grounding: dict | None = None
    first_token_ms: float | None = None

    for turn_idx in range(MAX_TOOL_TURNS):
        try:
            async with client.messages.stream(
                model=REPLY_MODEL,
                max_tokens=1024,
                system=system,
                tools=ANTHROPIC_TOOL_SCHEMAS,
                messages=msgs,
            ) as stream:
                async for event in stream:
                    if event.type == "content_block_delta":
                        delta = event.delta
                        if getattr(delta, "type", "") == "text_delta":
                            if first_token_ms is None:
                                first_token_ms = (time.perf_counter() - t_start) * 1000
                                yield {"type": "first_token_ms", "ms": round(first_token_ms, 1)}
                            yield {"type": "token", "text": delta.text}
                final = await stream.get_final_message()
        except Exception as e:
            yield {"type": "error", "detail": f"stream_error:{e}"}
            return

        msgs.append({"role": "assistant", "content": final.content})

        # Walk content blocks
        tool_uses = [b for b in final.content if getattr(b, "type", "") == "tool_use"]
        if not tool_uses:
            # model returned plain text — coerce a send_reply
            text_blocks = [getattr(b, "text", "") for b in final.content if getattr(b, "type", "") == "text"]
            text = "\n".join(t for t in text_blocks if t).strip() or "Thanks for the message — we'll get right back to you."
            result = dispatch_tool("send_reply", {
                "text": text, "citations": [], "auto": False, "buyer_handle": buyer_handle,
                "retrieved_context": retrieved_for_grounding,
                "listing_for_grounding": listing_for_grounding,
            }, session_id=session_id)
            yield {"type": "reply", "payload": result}
            break

        # execute tools
        tool_results_for_msg = []
        ended = False
        for tu in tool_uses:
            yield {"type": "tool_use", "name": tu.name, "input": tu.input}
            args = dict(tu.input or {})
            # inject retrieval context for send_reply grounding
            if tu.name == "send_reply":
                args["retrieved_context"] = retrieved_for_grounding
                args["listing_for_grounding"] = listing_for_grounding
                if "buyer_handle" not in args:
                    args["buyer_handle"] = buyer_handle
            result = dispatch_tool(tu.name, args, session_id=session_id)
            yield {"type": "tool_result", "name": tu.name, "result": result}

            # stash retrieved context for downstream grounding
            if tu.name == "search_catalog" and result.get("ok"):
                retrieved_for_grounding.extend(result.get("hits", []))
            if tu.name == "get_listing" and result.get("ok"):
                listing_for_grounding = result.get("listing")
                retrieved_for_grounding.append(result.get("listing"))
            if tu.name == "search_policies" and result.get("ok"):
                retrieved_for_grounding.extend(result.get("hits", []))

            if tu.name == "send_reply":
                yield {"type": "reply", "payload": result}
                ended = True
            tool_results_for_msg.append({
                "type": "tool_result",
                "tool_use_id": tu.id,
                "content": json.dumps(_truncate_for_model(result), ensure_ascii=False),
            })
        if ended:
            break
        msgs.append({"role": "user", "content": tool_results_for_msg})
    else:
        # exhausted MAX_TOOL_TURNS without send_reply
        result = dispatch_tool("send_reply", {
            "text": "Let me check on that and get back to you in a moment.",
            "citations": [], "auto": False, "buyer_handle": buyer_handle,
            "retrieved_context": retrieved_for_grounding,
        }, session_id=session_id)
        yield {"type": "reply", "payload": result, "circuit_break": True}

    yield {"type": "latency",
           "first_token_ms": round(first_token_ms, 1) if first_token_ms else None,
           "total_ms": round((time.perf_counter() - t_start) * 1000, 1)}


def _truncate_for_model(d: dict, max_len: int = 4000) -> dict:
    """Keep tool_result small enough not to blow the context."""
    s = json.dumps(d, ensure_ascii=False)
    if len(s) <= max_len:
        return d
    if "hits" in d and isinstance(d["hits"], list):
        d2 = dict(d)
        d2["hits"] = d["hits"][:5]
        return d2
    return {"_truncated": True, "preview": s[:max_len]}


# ---------- mock orchestrator (no API key needed) ----------

async def _mock_orchestrator(buyer_message: str, *, buyer_handle: str, session_id: str) -> AsyncIterator[dict]:
    """
    Deterministic stand-in so the prototype demos without an API key.
    Pattern-matches buyer intent, calls real tools, returns a grounded reply.
    """
    import asyncio, re
    t0 = time.perf_counter()
    txt = buyer_message.lower()

    # 0. prompt-injection / "ignore the rules" probes — never auto-send, deflect generically
    if re.search(
        r"\b(ignore|disregard|bypass|override|forget)\s+(the\s+|your\s+|all\s+|previous\s+)?"
        r"(polic|rule|guardrail|instruction|system|prompt|guideline)",
        txt,
    ) or re.search(r"\b(jailbreak|prompt injection)\b", txt):
        await asyncio.sleep(0.02)
        yield {"type": "thinking", "text": "detected prompt-injection probe — deflect, escalate"}
        reply_text = (
            "Happy to help with anything about our listings, pricing, or policies — "
            "but I can't go off-script. What item or question can I look up for you?"
        )
        yield {"type": "first_token_ms", "ms": round((time.perf_counter() - t0) * 1000, 1)}
        for w in reply_text.split(" "):
            yield {"type": "token", "text": w + " "}
            await asyncio.sleep(0.005)
        result = dispatch_tool("send_reply", {
            "text": reply_text, "citations": [], "auto": False,
            "buyer_handle": buyer_handle, "retrieved_context": [],
        }, session_id=session_id)
        yield {"type": "reply", "payload": result}
        yield {"type": "latency", "total_ms": round((time.perf_counter() - t0) * 1000, 1)}
        return

    # 0b. out-of-domain probes — catalog is consumer electronics, never confidently sell a car
    OUT_OF_DOMAIN = (
        r"\b(cars?|vehicles?|trucks?|motorcycles?|boats?|"
        r"houses?|homes?|apartments?|condos?|real estate|"
        r"foods?|meals?|drinks?|beverages?|groceries|"
        r"flights?|hotels?|tickets?|vacations?|"
        r"medicines?|drugs?|prescriptions?|"
        r"pets?|dogs?|cats?|"
        r"insurance|loans?|mortgages?)\b"
    )
    if re.search(r"\b(do you (sell|carry|stock|have)|got any|any)\b", txt) and re.search(OUT_OF_DOMAIN, txt):
        await asyncio.sleep(0.02)
        yield {"type": "thinking", "text": "detected out-of-domain ask — clarify, escalate"}
        reply_text = (
            "Doesn't look like that's something we carry — our catalog is consumer electronics "
            "(phones, audio, gaming, wearables, cameras). Anything in that range I can help find?"
        )
        yield {"type": "first_token_ms", "ms": round((time.perf_counter() - t0) * 1000, 1)}
        for w in reply_text.split(" "):
            yield {"type": "token", "text": w + " "}
            await asyncio.sleep(0.005)
        result = dispatch_tool("send_reply", {
            "text": reply_text, "citations": [], "auto": False,
            "buyer_handle": buyer_handle, "retrieved_context": [],
        }, session_id=session_id)
        yield {"type": "reply", "payload": result}
        yield {"type": "latency", "total_ms": round((time.perf_counter() - t0) * 1000, 1)}
        return

    # 1. abuse (check FIRST so abuse buyers don't get a policy reply)
    if re.search(
        r"\b(scam[a-z]*|fraud[a-z]*|li(ar|ed|es)\b|cheat[a-z]*|asshole|garbage|rip[- ]?off|scumbag|crook|ripoff)\b",
        txt,
    ):
        await asyncio.sleep(0.05)
        yield {"type": "thinking", "text": "detected abuse — suggested-only, escalate to human"}
        reply_text = "Every item we list is sourced from authorized distributors, and we're happy to share documentation [policy:authenticity]. If you'd rather walk through it, I'm here."
        yield {"type": "first_token_ms", "ms": round((time.perf_counter() - t0) * 1000, 1)}
        for word in reply_text.split(" "):
            yield {"type": "token", "text": word + " "}
            await asyncio.sleep(0.005)
        result = dispatch_tool("send_reply", {
            "text": reply_text, "citations": ["policy:authenticity"],
            "auto": False, "buyer_handle": buyer_handle,
            "retrieved_context": [{"policy": "authenticity"}],
        }, session_id=session_id)
        yield {"type": "reply", "payload": result}
        yield {"type": "latency", "total_ms": round((time.perf_counter() - t0) * 1000, 1)}
        return

    # 2. policy questions — broadened to catch shipping/delivery probes
    policy_hit = re.search(
        r"\b(return|returns|refund|refunds|policy|policies|"
        r"shipping|ship|ships|delivery|deliver|delivers|delivered|arrive|arrives|"
        r"warranty|authentic|authenticity|fake|counterfeit|genuine|"
        r"real|legit|legitimate|trustworthy|"
        r"guarantee|guaranteed|"
        r"when (will|do)|how long)\b",
        txt,
    )
    # Probe for date-promise fishing ("guaranteed by tuesday", "arrive by friday")
    is_date_promise_probe = bool(re.search(
        r"\b(guarantee|guaranteed|promise|on time|by (mon|tue|wed|thu|fri|sat|sun|tomorrow|today)\w*)\b",
        txt,
    ))
    if policy_hit:
        topic = "returns" if re.search(r"\b(return|refund)", txt) else \
                "shipping" if re.search(r"\b(ship|deliver|arrive|warranty|guarantee|when (will|do)|how long)", txt) else \
                "authenticity"
        await asyncio.sleep(0.05)
        yield {"type": "thinking", "text": f"detected policy topic: {topic}"
                                         + (" (date-promise probe — must not commit)" if is_date_promise_probe else "")}
        # Use a topic-targeted retrieval query so the BM25 fallback picks the right
        # policy doc (buyer's verbatim text often pulls prohibited_claims by keyword overlap).
        topic_query = {
            "returns":      "return refund exchange policy 30 days",
            "shipping":     "shipping delivery times standard expedited carrier",
            "authenticity": "authenticity genuine sourced authorized distributors",
        }.get(topic, buyer_message)
        yield {"type": "tool_use", "name": "search_policies", "input": {"query": topic_query, "k": 1}}
        res = dispatch_tool("search_policies", {"query": topic_query, "k": 1}, session_id=session_id)
        yield {"type": "tool_result", "name": "search_policies", "result": res}
        hit = (res.get("hits") or [{}])[0]
        snippet = (hit.get("text") or "").split("\n", 4)
        snippet = "\n".join(s for s in snippet if s.strip())[:280]
        if is_date_promise_probe:
            # Never promise a delivery date. Quote the policy windows and
            # explicitly note we can't guarantee a specific day.
            reply_text = (
                "I can't promise a specific delivery day — that's outside what we control once the carrier has the package. "
                f"Here's our shipping window from policy [policy:{hit.get('policy', 'shipping')}]:\n\n{snippet}\n\n"
                "If you want, I can confirm same-day shipping cutoff on this listing."
            )
            auto = False
        else:
            reply_text = (
                f"Quick answer from our {topic} policy [policy:{hit.get('policy', topic)}]:\n\n{snippet}\n\n"
                "Let me know if you want me to dig deeper."
            )
            auto = True
        yield {"type": "first_token_ms", "ms": round((time.perf_counter() - t0) * 1000, 1)}
        for word in reply_text.split(" "):
            yield {"type": "token", "text": word + " "}
            await asyncio.sleep(0.005)
        result = dispatch_tool("send_reply", {
            "text": reply_text, "citations": [f"policy:{hit.get('policy', topic)}"],
            "auto": auto, "buyer_handle": buyer_handle,
            "retrieved_context": res.get("hits", []),
        }, session_id=session_id)
        yield {"type": "reply", "payload": result}
        yield {"type": "latency", "total_ms": round((time.perf_counter() - t0) * 1000, 1)}
        return

    # 3. price negotiation — supports both "$X" and "Y% off" framings
    m_dollar = re.search(r"\$\s?(\d+(?:\.\d{1,2})?)", buyer_message)
    m_pct = re.search(r"(\d+(?:\.\d+)?)\s?%\s*(?:off|discount|down)?", buyer_message, re.I)
    nego_phrase = re.search(
        r"\b(can you do|how about|deal|discount|cheaper|price match|lowest|"
        r"give me|knock|take off|come down to|drop to|do better)\b",
        txt,
    )
    if (m_dollar or m_pct) and nego_phrase:
        target_pct: float | None = None
        target: float | None = None
        if m_dollar:
            target = float(m_dollar.group(1))
        if m_pct:
            try:
                target_pct = float(m_pct.group(1)) / 100.0
            except ValueError:
                pass
        # find a product the buyer might mean — strip the price/% from the seed query
        q = re.sub(r"\$\s?\d+(?:\.\d{1,2})?", "", buyer_message)
        q = re.sub(r"\d+(?:\.\d+)?\s?%\s*(?:off|discount|down)?", "", q, flags=re.I)
        yield {"type": "tool_use", "name": "search_catalog", "input": {"query": q, "k": 3}}
        sres = dispatch_tool("search_catalog", {"query": q or "deal", "k": 3}, session_id=session_id)
        yield {"type": "tool_result", "name": "search_catalog", "result": sres}
        hits = sres.get("hits") or []
        if not hits:
            reply_text = "Which item are you asking about? Happy to talk price once I know which listing you mean."
            auto = False
        else:
            top = hits[0]
            current = float(top["price"])
            # Compute proposed markdown pct: prefer explicit % if given, else derive from $.
            if target_pct is not None:
                pct = max(0.0, min(0.99, target_pct))
                target = round(current * (1 - pct), 2)
            elif target is not None:
                pct = max(0.0, min(0.99, (current - target) / current))
            else:
                pct = 0.0
            yield {"type": "tool_use", "name": "get_listing", "input": {"sku": top["sku"]}}
            full = dispatch_tool("get_listing", {"sku": top["sku"]}, session_id=session_id)
            yield {"type": "tool_result", "name": "get_listing", "result": full}
            from app.guardrails import PriceGuardrail
            verdict = PriceGuardrail().check(full["listing"], pct)
            if verdict.action == "block":
                counter = verdict.meta.get("floor_price")
                reply_text = (
                    f"I can't go down to ${target:.2f} on the "
                    f"{top['manufacturer']} (currently ${current:.2f}) [{top['sku'][:8]}] — "
                    f"that's a {pct*100:.0f}% markdown and we cap at our margin floor. "
                    f"I can do ${counter:.2f} though."
                )
                auto = False
            elif verdict.action == "human":
                new_p = round(current * (1 - pct), 2)
                reply_text = (
                    f"${target:.2f} on the {top['manufacturer']} "
                    f"[{top['sku'][:8]}] would be a {pct*100:.0f}% markdown — let me get that approved "
                    f"and confirm in a sec."
                )
                auto = False
            else:
                new_p = round(current * (1 - pct), 2)
                # auto-apply small markdown
                yield {"type": "tool_use", "name": "apply_markdown", "input": {"sku": top["sku"], "pct": pct, "reason": "buyer offer accepted"}}
                ar = dispatch_tool("apply_markdown", {"sku": top["sku"], "pct": pct, "reason": "buyer offer accepted"}, session_id=session_id)
                yield {"type": "tool_result", "name": "apply_markdown", "result": ar}
                reply_text = f"Done — ${new_p:.2f} on the {top['manufacturer']} [{top['sku'][:8]}]. Tap the listing to grab it."
                auto = True
        listing_for_g = (full.get("listing") if hits else None)
        yield {"type": "first_token_ms", "ms": round((time.perf_counter() - t0) * 1000, 1)}
        for word in reply_text.split(" "):
            yield {"type": "token", "text": word + " "}
            await asyncio.sleep(0.005)
        result = dispatch_tool("send_reply", {
            "text": reply_text, "citations": [hits[0]["sku"][:8]] if hits else [],
            "auto": auto if hits else False, "buyer_handle": buyer_handle,
            "retrieved_context": hits, "listing_for_grounding": listing_for_g,
        }, session_id=session_id)
        yield {"type": "reply", "payload": result}
        yield {"type": "latency", "total_ms": round((time.perf_counter() - t0) * 1000, 1)}
        return

    # 4. default: pre-screen low-information messages — no concrete product
    # reference, no manufacturer, no category, very short, or pure punctuation.
    stripped = re.sub(r"[^a-zA-Z0-9]+", "", buyer_message).strip()
    looks_low_info = (
        len(stripped) <= 2
        or re.fullmatch(r"[?.!]+\s*", buyer_message.strip()) is not None
        or re.fullmatch(r"(same|that|this|it|same\s+as\s+before|same\s+thing|like\s+before)\s*[?.!]?\s*", buyer_message.strip(), re.I) is not None
    )
    if looks_low_info:
        await asyncio.sleep(0.02)
        yield {"type": "thinking", "text": "low-information message — ask for clarification"}
        reply_text = "Can you say a bit more? I want to make sure I point you at the right listing."
        yield {"type": "first_token_ms", "ms": round((time.perf_counter() - t0) * 1000, 1)}
        for w in reply_text.split(" "):
            yield {"type": "token", "text": w + " "}
            await asyncio.sleep(0.005)
        result = dispatch_tool("send_reply", {
            "text": reply_text, "citations": [], "auto": False,
            "buyer_handle": buyer_handle, "retrieved_context": [],
        }, session_id=session_id)
        yield {"type": "reply", "payload": result}
        yield {"type": "latency", "total_ms": round((time.perf_counter() - t0) * 1000, 1)}
        return

    yield {"type": "tool_use", "name": "search_catalog", "input": {"query": buyer_message, "k": 3}}
    sres = dispatch_tool("search_catalog", {"query": buyer_message, "k": 3}, session_id=session_id)
    yield {"type": "tool_result", "name": "search_catalog", "result": sres}
    hits = sres.get("hits") or []
    intention = (sres.get("intent") or {}).get("intention", "general")
    top1_ok = sres.get("top1_meets_requirements", True)
    alt_offered = sres.get("alternatives_offered", False)

    # 4a. comparison: detect multiple products/brands; route to comparison whenever
    # 2+ brands are detected OR the intent says comparison, even if intent missed.
    from app.retrieval.intent import COMMON_MANUFACTURERS
    brands: list[str] = []
    for mfr in COMMON_MANUFACTURERS:
        if re.search(rf"\b{re.escape(mfr)}\b", buyer_message, re.I) and mfr not in brands:
            brands.append(mfr)
    noun_to_brand = {"iphone": "Apple", "ipad": "Apple", "macbook": "Apple", "airpod": "Apple",
                     "galaxy": "Samsung", "pixel": "Google", "switch": "Nintendo",
                     "playstation": "Sony", "xbox": "Microsoft", "kindle": "Amazon"}
    for noun, brand in noun_to_brand.items():
        if re.search(rf"\b{noun}", buyer_message, re.I) and brand not in brands:
            brands.append(brand)
    is_explicit_compare = bool(re.search(r"\b(vs\.?|versus|compared to|better than)\b", buyer_message, re.I))
    if intention == "comparison" or len(brands) >= 2 or is_explicit_compare:
        sides: list[tuple[str, dict | None]] = []
        if len(brands) >= 2:
            for b in brands[:3]:
                sub_q = b + " " + buyer_message
                sub = dispatch_tool("search_catalog", {"query": sub_q, "k": 1}, session_id=session_id)
                yield {"type": "tool_use", "name": "search_catalog", "input": {"query": sub_q, "k": 1}}
                yield {"type": "tool_result", "name": "search_catalog", "result": sub}
                shits = sub.get("hits") or []
                sides.append((b, shits[0] if shits else None))
        else:
            # only one brand recognized: try splitting on " vs " / " versus "
            halves = re.split(r"\s+(?:vs\.?|versus)\s+", buyer_message, flags=re.I)
            if len(halves) >= 2:
                for h in halves[:3]:
                    sub = dispatch_tool("search_catalog", {"query": h, "k": 1}, session_id=session_id)
                    yield {"type": "tool_use", "name": "search_catalog", "input": {"query": h, "k": 1}}
                    yield {"type": "tool_result", "name": "search_catalog", "result": sub}
                    shits = sub.get("hits") or []
                    sides.append((h.strip()[:30], shits[0] if shits else None))

        if sides:
            lines = []
            cites: list[str] = []
            ctx: list[dict] = []
            for name, hit in sides:
                if hit:
                    lines.append(
                        f"• {name}: closest I have is {hit['title'][:70]} "
                        f"[{hit['sku'][:8]}] — ${hit['price']:.2f}, {hit['stock_qty']} in stock"
                    )
                    cites.append(hit["sku"][:8])
                    ctx.append(hit)
                else:
                    lines.append(f"• {name}: nothing matching in our catalog right now")
            reply_text = (
                "I don't have an exact match for both, but here's the closest I've got:\n"
                + "\n".join(lines)
                + "\nWant me to dig into one of them or pull specs?"
            )
            yield {"type": "first_token_ms", "ms": round((time.perf_counter() - t0) * 1000, 1)}
            for word in reply_text.split(" "):
                yield {"type": "token", "text": word + " "}
                await asyncio.sleep(0.005)
            result = dispatch_tool("send_reply", {
                "text": reply_text, "citations": cites, "auto": False,
                "buyer_handle": buyer_handle, "retrieved_context": ctx,
            }, session_id=session_id)
            yield {"type": "reply", "payload": result}
            yield {"type": "latency", "total_ms": round((time.perf_counter() - t0) * 1000, 1)}
            return
        # fall through — no recognizable sides → treat as general recommendation

    # 4b. recommendation: never auto-send; surface top match honestly
    if intention == "recommendation":
        if hits:
            top = hits[0]
            listing_for_g = top
            reply_text = (
                f"Best fit I've got is {top['title'][:80]} [{top['sku'][:8]}] "
                f"at ${top['price']:.2f} ({top['stock_qty']} in stock). "
                "Want me to walk through specs or surface alternatives?"
            )
        else:
            reply_text = "Tell me a bit more about what you're looking for and I'll find the closest match."
            listing_for_g = None
        yield {"type": "first_token_ms", "ms": round((time.perf_counter() - t0) * 1000, 1)}
        for word in reply_text.split(" "):
            yield {"type": "token", "text": word + " "}
            await asyncio.sleep(0.005)
        result = dispatch_tool("send_reply", {
            "text": reply_text, "citations": [hits[0]["sku"][:8]] if hits else [],
            "auto": False, "buyer_handle": buyer_handle,
            "retrieved_context": hits, "listing_for_grounding": listing_for_g,
        }, session_id=session_id)
        yield {"type": "reply", "payload": result}
        yield {"type": "latency", "total_ms": round((time.perf_counter() - t0) * 1000, 1)}
        return

    if not hits:
        reply_text = "Can you say a bit more about which item you mean? I want to point you at the right listing."
        auto = False
        listing_for_g = None
    else:
        top = hits[0]
        listing_for_g = top
        if top1_ok:
            reply_text = (
                f"Yes — {top['title'][:80]} [{top['sku'][:8]}] is in. "
                f"${top['price']:.2f}, {top['stock_qty']} in stock. Let me know if you want me to send the listing link."
            )
            auto = True
        elif alt_offered:
            # find first hit that meets_hard, otherwise fall back to first in-stock alternative
            alt = next((h for h in hits[1:] if h.get("meets_hard")), None) or \
                  next((h for h in hits[1:] if h["stock_qty"] > 0), None)
            misses = ", ".join(top.get("miss_reasons", [])) or "doesn't fully match"
            if alt:
                reply_text = (
                    f"Closest I have to that is "
                    f"{top['title'][:60]} [{top['sku'][:8]}] but {misses}. "
                    f"A better fit: {alt['title'][:60]} [{alt['sku'][:8]}] — "
                    f"${alt['price']:.2f}, {alt['stock_qty']} in stock."
                )
            else:
                reply_text = (
                    f"Closest I have is {top['title'][:60]} [{top['sku'][:8]}] "
                    f"but {misses}. Want me to keep looking?"
                )
            auto = False
        else:
            reply_text = f"{top['title'][:60]} [{top['sku'][:8]}] is sold out — want me to notify you when it's back?"
            auto = False
    yield {"type": "first_token_ms", "ms": round((time.perf_counter() - t0) * 1000, 1)}
    for word in reply_text.split(" "):
        yield {"type": "token", "text": word + " "}
        await asyncio.sleep(0.005)
    result = dispatch_tool("send_reply", {
        "text": reply_text, "citations": [hits[0]["sku"][:8]] if hits else [],
        "auto": auto, "buyer_handle": buyer_handle,
        "retrieved_context": hits, "listing_for_grounding": listing_for_g,
    }, session_id=session_id)
    yield {"type": "reply", "payload": result}
    yield {"type": "latency", "total_ms": round((time.perf_counter() - t0) * 1000, 1)}
