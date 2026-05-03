import { Listing, AuditRow } from "./types";

export async function fetchListings(q?: string, category?: string, limit = 30): Promise<Listing[]> {
  const params = new URLSearchParams();
  if (q) params.set("q", q);
  if (category) params.set("category", category);
  params.set("limit", String(limit));
  const r = await fetch(`/api/listings?${params}`);
  const j = await r.json();
  return j.items || [];
}

export async function fetchAudit(session_id?: string, limit = 30): Promise<AuditRow[]> {
  const params = new URLSearchParams();
  if (session_id) params.set("session_id", session_id);
  params.set("limit", String(limit));
  const r = await fetch(`/api/audit?${params}`);
  const j = await r.json();
  return j.items || [];
}

export async function rollback(audit_id: number) {
  const r = await fetch(`/api/rollback/${audit_id}`, { method: "POST" });
  return r.json();
}

export async function applyMarkdown(sku: string, pct: number, reason = "operator") {
  const r = await fetch(`/api/markdown`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ sku, pct, reason }),
  });
  return r.json();
}

export async function adjustStock(sku: string, delta: number, reason = "operator") {
  const r = await fetch(`/api/stock`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ sku, delta, reason }),
  });
  return r.json();
}
