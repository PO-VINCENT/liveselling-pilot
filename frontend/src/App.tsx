import { useEffect, useRef, useState } from "react";
import clsx from "clsx";
import { useChatStream, p95 } from "./useChatStream";
import { fetchListings, fetchAudit, rollback, applyMarkdown, adjustStock } from "./api";
import { Listing, ChatItem, AuditRow } from "./types";

export default function App() {
  const { items, connected, sendMessage, accept, reject, editDraft, latencies } = useChatStream();
  const [listings, setListings] = useState<Listing[]>([]);
  const [audit, setAudit] = useState<AuditRow[]>([]);
  const [search, setSearch] = useState("");
  const [activeSku, setActiveSku] = useState<string | null>(null);
  const [composer, setComposer] = useState("");
  const [composerHandle, setComposerHandle] = useState("@you");

  useEffect(() => { fetchListings().then(setListings); }, []);
  useEffect(() => {
    const id = setInterval(() => fetchAudit("demo1").then(setAudit), 1500);
    return () => clearInterval(id);
  }, []);
  useEffect(() => {
    const id = setInterval(() => fetchListings(search).then(setListings), 2500);
    return () => clearInterval(id);
  }, [search]);

  const firstP95 = p95(latencies.first);
  const totalP95 = p95(latencies.total);

  return (
    <div className="h-full flex flex-col">
      <Header connected={connected} firstP95={firstP95} totalP95={totalP95} />
      <div className="flex-1 grid grid-cols-12 gap-3 p-3 overflow-hidden">
        <ChatPane items={items} onSend={sendMessage} composer={composer} setComposer={setComposer}
                  handle={composerHandle} setHandle={setComposerHandle} />
        <ReplyQueue items={items} onAccept={accept} onReject={reject} onEdit={editDraft} />
        <RightRail listings={listings} search={search} setSearch={setSearch}
                   activeSku={activeSku} setActiveSku={setActiveSku}
                   audit={audit} onRollback={async (id) => {
                     await rollback(id); fetchAudit("demo1").then(setAudit); fetchListings(search).then(setListings);
                   }}
                   onMarkdown={async (sku, pct) => { await applyMarkdown(sku, pct); fetchListings(search).then(setListings); }}
                   onAdjustStock={async (sku, delta) => { await adjustStock(sku, delta); fetchListings(search).then(setListings); }}
        />
      </div>
    </div>
  );
}

function Header({ connected, firstP95, totalP95 }: { connected: boolean; firstP95: number | null; totalP95: number | null }) {
  return (
    <header className="px-4 py-2 border-b bg-white flex items-center gap-4 text-sm">
      <div className="font-semibold text-slate-800">🎙️ Liveselling Copilot</div>
      <div className={clsx("h-2 w-2 rounded-full", connected ? "bg-emerald-500" : "bg-rose-500")} />
      <div className="text-slate-500">{connected ? "live" : "reconnecting…"}</div>
      <div className="ml-auto flex gap-3 text-slate-600">
        <Stat label="first-token p95" value={firstP95 ? `${Math.round(firstP95)} ms` : "—"} good={firstP95 ? firstP95 < 1500 : true} />
        <Stat label="total p95" value={totalP95 ? `${Math.round(totalP95)} ms` : "—"} good={totalP95 ? totalP95 < 2000 : true} />
        <Stat label="latency budget" value="2.0 s" good />
      </div>
    </header>
  );
}

function Stat({ label, value, good }: { label: string; value: string; good?: boolean }) {
  return (
    <div className="flex items-baseline gap-1">
      <span className="text-xs uppercase tracking-wide text-slate-400">{label}</span>
      <span className={clsx("font-mono", good ? "text-emerald-700" : "text-rose-700")}>{value}</span>
    </div>
  );
}

function ChatPane({ items, onSend, composer, setComposer, handle, setHandle }: any) {
  const scrollRef = useRef<HTMLDivElement | null>(null);
  // auto-scroll to latest
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [items.length, items.map((it: ChatItem) => (it.draft || "").length).join("|"), items.map((it: ChatItem) => it.status).join("|")]);

  return (
    <section className="col-span-4 bg-white rounded-md border flex flex-col overflow-hidden">
      <div className="px-3 py-2 border-b text-xs font-medium text-slate-600">Live conversation</div>
      <div ref={scrollRef} className="flex-1 overflow-y-auto scrollbar-thin p-3 space-y-3">
        {items.length === 0 && <div className="text-xs text-slate-400">Waiting for buyer messages… start the replayer or type below.</div>}
        {items.map((m: ChatItem) => <ConversationTurn key={m.id} m={m} />)}
      </div>
      <form
        className="border-t p-2 flex gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          if (!composer.trim()) return;
          onSend(composer, handle || "@you");
          setComposer("");
        }}
      >
        <input value={handle} onChange={(e) => setHandle(e.target.value)}
               className="w-20 px-2 py-1 text-xs border rounded" />
        <input value={composer} onChange={(e) => setComposer(e.target.value)}
               placeholder="Simulate a buyer message…"
               className="flex-1 px-2 py-1 text-sm border rounded" />
        <button className="px-3 py-1 text-sm rounded bg-slate-800 text-white">Send</button>
      </form>
    </section>
  );
}

/** Cleans a seller reply for display in the chat bubble.
 *  Underlying data (citations array, audit log, grounding guardrail) keeps
 *  the SKU references — we just hide them from the operator/buyer view. */
function cleanForDisplay(text: string): string {
  if (!text) return text;
  // 1. Drop leading "Hi @handle, " / "Hi @handle — " (tolerates partial during streaming)
  let t = text.replace(/^\s*hi\s+@[\w._-]+\s*[,—\-]\s*/i, "");
  t = t.replace(/^\s*(hi|hello|hey)\s*[,!]\s+/i, "");
  t = t.replace(/^\s*hi\s+@[\w._-]*$/i, "");
  // 2. Strip inline SKU prefix citations like [4a88e307] (8-char hex). Leaves
  //    [policy:returns] alone — those stay visible.
  t = t.replace(/\s?\[[0-9a-f]{6,8}\]/gi, "");
  // 3. Tidy double spaces and orphan punctuation left behind
  t = t.replace(/\s{2,}/g, " ");
  t = t.replace(/\s+([,.;:!?])/g, "$1");
  return t.trim();
}

/** A single buyer↔seller exchange in the chat pane.
 *  Buyer bubble (left, slate) + seller bubble (right, color-coded by status).
 *  Streams the draft as tokens come in; updates color when status flips to sent / human / blocked. */
function ConversationTurn({ m }: { m: ChatItem }) {
  const rawSeller = m.draft || m.reply?.text || "";
  const sellerText = cleanForDisplay(rawSeller);
  const showSeller = m.status === "drafting" || sellerText.length > 0 || m.reply;
  const sellerStyle =
    m.status === "sent"        ? "bg-sky-100 text-sky-900 border border-sky-200" :
    m.status === "blocked"     ? "bg-rose-100 text-rose-900 border border-rose-200" :
    m.status === "needs_human" ? "bg-amber-50 text-amber-900 border border-amber-200" :
                                  "bg-slate-50 text-slate-700 border border-slate-200";
  const sellerBadge =
    m.status === "sent"        ? <span className="text-emerald-600">✓ auto-sent</span> :
    m.status === "blocked"     ? <span className="text-rose-600">✗ blocked</span> :
    m.status === "needs_human" ? <span className="text-amber-700">⏵ awaiting operator</span> :
    m.status === "drafting"    ? <span className="text-slate-500 inline-flex items-center gap-1">drafting<span className="inline-flex gap-0.5"><Dot delay={0} /><Dot delay={120} /><Dot delay={240} /></span></span> :
                                  null;

  return (
    <div className="space-y-1.5">
      {/* buyer bubble — left */}
      <div className="flex flex-col items-start">
        <div className="text-[10px] text-slate-500 mb-0.5">{m.buyer_handle}</div>
        <div className="bg-slate-100 text-slate-800 rounded-2xl rounded-tl-sm px-3 py-2 text-sm max-w-[88%] whitespace-pre-wrap">{m.text}</div>
      </div>
      {/* seller bubble — right */}
      {showSeller && (
        <div className="flex flex-col items-end">
          <div className="text-[10px] text-slate-500 mb-0.5 flex items-center gap-2">
            <span>seller</span>
            {sellerBadge}
            {m.firstTokenMs && (
              <span className="text-slate-400">⚡ {Math.round(m.firstTokenMs)}ms{m.totalMs ? ` / ${Math.round(m.totalMs)}ms` : ""}</span>
            )}
          </div>
          <div className={clsx("rounded-2xl rounded-tr-sm px-3 py-2 text-sm max-w-[88%] whitespace-pre-wrap", sellerStyle)}>
            {sellerText || <span className="text-slate-400 italic">…</span>}
          </div>
          {m.reply?.citations && m.reply.citations.length > 0 && (
            <div className="text-[10px] text-slate-400 mt-0.5 font-mono">
              cites: {m.reply.citations.map((c) => `[${c}]`).join(" ")}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function Dot({ delay }: { delay: number }) {
  return (
    <span
      className="inline-block w-1 h-1 rounded-full bg-slate-400 animate-pulse"
      style={{ animationDelay: `${delay}ms` }}
    />
  );
}

function ReplyQueue({ items, onAccept, onReject, onEdit }: any) {
  return (
    <section className="col-span-5 bg-white rounded-md border flex flex-col overflow-hidden">
      <div className="px-3 py-2 border-b text-xs font-medium text-slate-600">Suggested replies / agent trace</div>
      <div className="flex-1 overflow-y-auto scrollbar-thin p-3 space-y-3">
        {items.map((m: ChatItem) => (
          <div key={m.id} className={clsx("border rounded-md p-3 text-sm",
              m.status === "sent" && "border-emerald-300 bg-emerald-50",
              m.status === "blocked" && "border-rose-300 bg-rose-50",
              m.status === "needs_human" && "border-amber-300 bg-amber-50")}>
            <div className="flex items-center gap-2 mb-2">
              <Pill label={badgeLabel(m)} tone={badgeTone(m)} />
              <span className="text-xs text-slate-500">{m.buyer_handle}</span>
              {m.firstTokenMs && <span className="text-[10px] text-slate-400 ml-auto">{Math.round(m.firstTokenMs)} / {Math.round(m.totalMs ?? 0)} ms</span>}
            </div>

            <details className="mb-2" open>
              <summary className="cursor-pointer text-xs text-slate-500 hover:text-slate-700">tool trace ({m.toolTrace.length})</summary>
              <div className="mt-1 space-y-1">
                {m.toolTrace.map((t: any, i: number) => (
                  <div key={i} className="text-xs bg-slate-50 rounded px-2 py-1.5 break-all">
                    <div className="font-mono">
                      <span className="text-slate-700 font-medium">{t.name}</span>
                      <span className="text-slate-500"> ({JSON.stringify(t.input).slice(0, 80)})</span>
                      {t.result && (
                        <span className={clsx("ml-2", t.result.ok ? "text-emerald-600" : "text-rose-600")}>
                          → {t.result.ok ? "ok" : "fail"}{t.result.audit_id ? ` #${t.result.audit_id}` : ""}
                        </span>
                      )}
                    </div>
                    {t.name === "search_catalog" && t.result?.ok && <SearchCatalogTrace r={t.result} />}
                    {t.name === "search_policies" && t.result?.ok && <PolicyTrace r={t.result} />}
                  </div>
                ))}
              </div>
            </details>

            <textarea
              value={m.draft || ""}
              onChange={(e) => onEdit(m.id, e.target.value)}
              className="w-full text-sm border rounded p-2 font-normal"
              rows={Math.max(2, Math.min(6, (m.draft || "").split("\n").length + 1))}
            />

            {m.reply?.guardrail && (
              <div className="mt-2 text-xs space-y-1">
                <div className="text-slate-500">guardrail verdicts:</div>
                <div className="flex flex-wrap gap-1">
                  {(m.reply.guardrail.by_layer || []).map((v: any, i: number) => (
                    <Pill key={i} label={`${v.layer}:${v.action}`} tone={toneFor(v.action)} />
                  ))}
                </div>
                {m.reply.guardrail.reasons.length > 0 && (
                  <div className="text-slate-500">{m.reply.guardrail.reasons.join(" · ")}</div>
                )}
              </div>
            )}

            <div className="mt-2 flex gap-2">
              {m.status !== "sent" && m.status !== "blocked" && (
                <>
                  <button onClick={() => onAccept(m.id)}
                          className="px-2 py-1 text-xs rounded bg-emerald-600 text-white hover:bg-emerald-700">
                    Accept & send
                  </button>
                  <button onClick={() => onReject(m.id)}
                          className="px-2 py-1 text-xs rounded bg-slate-200 hover:bg-slate-300">Reject</button>
                </>
              )}
              {m.reply?.audit_id && (
                <span className="text-xs text-slate-500 ml-auto">audit #{m.reply.audit_id}</span>
              )}
            </div>
          </div>
        ))}
        {items.length === 0 && <div className="text-xs text-slate-400">No suggestions yet.</div>}
      </div>
    </section>
  );
}

function badgeLabel(m: ChatItem) {
  if (m.status === "sent") return "auto-sent";
  if (m.status === "blocked") return "blocked";
  if (m.status === "needs_human") return "needs human";
  if (m.status === "drafting") return "drafting…";
  return m.status;
}
function badgeTone(m: ChatItem) {
  if (m.status === "sent") return "emerald";
  if (m.status === "blocked") return "rose";
  if (m.status === "needs_human") return "amber";
  return "slate";
}
function toneFor(action: string) {
  return action === "allow" ? "emerald" : action === "warn" ? "amber" : action === "human" ? "amber" : "rose";
}

function Pill({ label, tone = "slate" }: { label: string; tone?: string }) {
  const map: any = {
    emerald: "bg-emerald-100 text-emerald-700",
    rose: "bg-rose-100 text-rose-700",
    amber: "bg-amber-100 text-amber-800",
    slate: "bg-slate-100 text-slate-700",
    indigo: "bg-indigo-100 text-indigo-700",
    sky: "bg-sky-100 text-sky-700",
  };
  return <span className={clsx("px-2 py-0.5 rounded-full text-[10px] uppercase tracking-wide", map[tone])}>{label}</span>;
}

/** Renders the multipath retrieval result inline in the tool trace.
 *  Surfaces step 1 (intent) → step 2 (paths fired) → step 3 (top-3 with RRF) → step 4 (judge). */
function SearchCatalogTrace({ r }: { r: any }) {
  const intent = r.intent || {};
  const paths = r.paths || [];
  const hits = r.hits || [];
  const aspectChips: { label: string; tone: string }[] = [];
  if (intent.intention && intent.intention !== "general")
    aspectChips.push({ label: `intent:${intent.intention}`, tone: "indigo" });
  if (intent.product_name) aspectChips.push({ label: `name: ${intent.product_name}`, tone: "slate" });
  if (intent.manufacturer) aspectChips.push({ label: `mfr: ${intent.manufacturer}`, tone: "sky" });
  if (intent.category) aspectChips.push({ label: `cat: ${intent.category}`, tone: "sky" });
  if (intent.price_target != null) aspectChips.push({ label: `target: $${intent.price_target}`, tone: "amber" });
  if (intent.price_max != null) aspectChips.push({ label: `≤ $${intent.price_max}`, tone: "amber" });
  if (intent.quantity_required) aspectChips.push({ label: `qty: ${intent.quantity_required}`, tone: "amber" });
  if (intent.memory) aspectChips.push({ label: `mem: ${intent.memory}`, tone: "slate" });
  if (intent.color) aspectChips.push({ label: `color: ${intent.color}`, tone: "slate" });

  return (
    <div className="mt-1.5 pl-2 border-l-2 border-slate-200 space-y-1.5">
      {/* Step 1: intent */}
      <div className="flex flex-wrap gap-1 items-center">
        <span className="text-[10px] uppercase text-slate-400 mr-1">step 1 · intent</span>
        {aspectChips.length === 0 ? <span className="text-[11px] text-slate-400 italic">no structured aspects</span>
          : aspectChips.map((c, i) => <Pill key={i} label={c.label} tone={c.tone} />)}
      </div>
      {/* Step 2: paths fired */}
      <div className="flex flex-wrap gap-1 items-center">
        <span className="text-[10px] uppercase text-slate-400 mr-1">step 2 · paths</span>
        {paths.map((p: any, i: number) => (
          <Pill key={i} label={`${p.path}:${p.n_hits}`} tone={
            p.method === "tfidf" ? "emerald" : p.method === "vector" ? "indigo" : p.method === "filter" ? "sky" : "slate"
          } />
        ))}
      </div>
      {/* Step 3 + 4: ranked hits + judge */}
      <div>
        <div className="text-[10px] uppercase text-slate-400 mb-0.5 flex items-center gap-2">
          <span>step 3 · top {hits.length} (RRF)</span>
          <span className="text-slate-300">|</span>
          <span>step 4 · judge:</span>
          <Pill
            label={r.top1_meets_requirements ? "top1 meets ✓" : (r.alternatives_offered ? "alts offered" : "no match")}
            tone={r.top1_meets_requirements ? "emerald" : "amber"}
          />
        </div>
        <div className="space-y-1">
          {hits.map((h: any, i: number) => (
            <div key={i} className="text-[11px] bg-white border border-slate-200 rounded px-1.5 py-1">
              <div className="flex items-center gap-1.5">
                <span className={clsx("font-mono", h.meets_hard ? "text-emerald-700" : "text-rose-600")}>
                  {h.meets_hard ? "✓" : "✗"} #{i + 1}
                </span>
                <span className="font-mono text-slate-400">[{h.sku.slice(0, 8)}]</span>
                <span className="font-mono">${h.price.toFixed(2)}</span>
                <span className="text-slate-500">stock {h.stock_qty}</span>
                <span className="text-slate-400 ml-auto">rrf {h.rrf_score.toFixed(4)}</span>
              </div>
              <div className="text-slate-700 line-clamp-1">{h.title}</div>
              <div className="flex flex-wrap gap-0.5 mt-0.5">
                {(h.matched_paths || []).map((p: string, j: number) => (
                  <span key={j} className="text-[9px] font-mono text-slate-500 bg-slate-100 rounded px-1">{p}</span>
                ))}
              </div>
              {h.miss_reasons?.length > 0 && (
                <div className="text-[10px] text-rose-600 mt-0.5">misses: {h.miss_reasons.join(" · ")}</div>
              )}
            </div>
          ))}
        </div>
        {r.judge_note && (
          <div className="text-[10px] text-slate-500 mt-1 italic">{r.judge_note}</div>
        )}
      </div>
    </div>
  );
}

function PolicyTrace({ r }: { r: any }) {
  const hits = r.hits || [];
  if (!hits.length) return null;
  return (
    <div className="mt-1.5 pl-2 border-l-2 border-slate-200">
      <div className="flex flex-wrap gap-1 items-center mb-1">
        {hits.map((h: any, i: number) => <Pill key={i} label={`policy:${h.policy}`} tone="indigo" />)}
      </div>
      <div className="text-[11px] text-slate-600 line-clamp-2">{(hits[0].text || "").slice(0, 200)}</div>
    </div>
  );
}

function RightRail({
  listings, search, setSearch, activeSku, setActiveSku, audit, onRollback, onMarkdown, onAdjustStock,
}: any) {
  const active = listings.find((l: Listing) => l.sku === activeSku);
  return (
    <section className="col-span-3 bg-white rounded-md border flex flex-col overflow-hidden">
      <div className="px-3 py-2 border-b text-xs font-medium text-slate-600 flex items-center gap-2">
        Catalog
        <input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="search…"
               className="ml-auto px-2 py-1 text-xs border rounded w-40" />
      </div>
      <div className="overflow-y-auto scrollbar-thin border-b max-h-[35%]">
        {listings.map((l: Listing) => (
          <button key={l.sku} onClick={() => setActiveSku(l.sku)}
                  className={clsx("w-full text-left px-3 py-2 text-xs border-b hover:bg-slate-50",
                                  activeSku === l.sku && "bg-slate-100")}>
            <div className="font-mono text-[10px] text-slate-400">{l.sku.slice(0, 8)}</div>
            <div className="line-clamp-1">{l.title}</div>
            <div className="text-slate-500 mt-0.5">${l.price.toFixed(2)} · stock {l.stock_qty}</div>
          </button>
        ))}
      </div>
      {active && <ListingActions listing={active} onMarkdown={onMarkdown} onAdjustStock={onAdjustStock} />}
      <div className="flex-1 overflow-y-auto scrollbar-thin">
        <div className="px-3 py-2 text-xs font-medium text-slate-600 border-t border-b sticky top-0 bg-white">Audit log</div>
        {audit.map((a: AuditRow) => (
          <div key={a.audit_id} className="px-3 py-2 text-xs border-b">
            <div className="flex items-center gap-2">
              <span className="font-mono text-slate-400">#{a.audit_id}</span>
              <span className="font-medium">{a.tool_name}</span>
              {a.reversed ? <Pill label="reversed" tone="slate" /> : null}
              {!a.reversed && (a.tool_name === "apply_markdown" || a.tool_name === "adjust_stock" || a.tool_name === "swap_listing") && (
                <button className="ml-auto text-rose-600 hover:underline" onClick={() => onRollback(a.audit_id)}>rollback</button>
              )}
            </div>
            <div className="text-slate-500 break-all">{JSON.stringify(a.input).slice(0, 100)}</div>
          </div>
        ))}
        {audit.length === 0 && <div className="px-3 py-2 text-xs text-slate-400">No actions yet.</div>}
      </div>
    </section>
  );
}

function ListingActions({ listing, onMarkdown, onAdjustStock }: any) {
  const [pct, setPct] = useState(0.05);
  const [delta, setDelta] = useState(1);
  return (
    <div className="px-3 py-2 text-xs border-b bg-slate-50">
      <div className="font-medium line-clamp-2 mb-1">{listing.title}</div>
      <div className="text-slate-500 mb-2">{listing.manufacturer} · {listing.category} · floor {(listing.margin_floor_pct! * 100).toFixed(0)}%</div>
      <div className="flex items-center gap-2 mb-2">
        <input type="number" step="0.01" value={pct} onChange={(e) => setPct(parseFloat(e.target.value))}
               className="w-16 px-1 border rounded" />
        <button className="px-2 py-1 rounded bg-slate-800 text-white text-xs"
                onClick={() => onMarkdown(listing.sku, pct)}>Apply markdown</button>
      </div>
      <div className="flex items-center gap-2">
        <input type="number" value={delta} onChange={(e) => setDelta(parseInt(e.target.value || "0"))}
               className="w-16 px-1 border rounded" />
        <button className="px-2 py-1 rounded bg-slate-800 text-white text-xs"
                onClick={() => onAdjustStock(listing.sku, delta)}>Adjust stock</button>
      </div>
    </div>
  );
}
