export type Listing = {
  sku: string;
  title: string;
  manufacturer: string;
  model_name?: string;
  model_num?: string;
  category?: string;
  price: number;
  cost?: number;
  margin_floor_pct?: number;
  stock_qty: number;
  color_category?: string;
  internal_memory?: string;
  screen_size?: string;
  carrier?: string;
  pageurl?: string;
};

export type GuardrailVerdict = {
  layer: string;
  action: "allow" | "warn" | "human" | "block";
  reasons: string[];
  meta?: Record<string, any>;
  latency_ms?: number;
};

export type ReplyPayload = {
  ok: boolean;
  audit_id?: number;
  message_id?: number;
  delivered?: boolean;
  final_action?: "auto" | "human" | "block";
  guardrail?: { action: string; reasons: string[]; by_layer?: GuardrailVerdict[] };
  text?: string;
  citations?: string[];
};

export type StreamEvent =
  | { type: "buyer_message"; text: string; buyer_handle: string }
  | { type: "thinking"; text: string }
  | { type: "tool_use"; name: string; input: any }
  | { type: "tool_result"; name: string; result: any }
  | { type: "first_token_ms"; ms: number }
  | { type: "token"; text: string }
  | { type: "reply"; payload: ReplyPayload; circuit_break?: boolean }
  | { type: "latency"; first_token_ms?: number; total_ms?: number }
  | { type: "error"; detail: string }
  | { type: "pong" };

export type ChatItem = {
  id: string;
  buyer_handle: string;
  text: string;
  ts: number;
  draft?: string;
  reply?: ReplyPayload;
  toolTrace: { name: string; input: any; result?: any }[];
  firstTokenMs?: number;
  totalMs?: number;
  status: "incoming" | "drafting" | "ready" | "sent" | "blocked" | "needs_human";
  error?: string;
};

export type AuditRow = {
  audit_id: number;
  ts: string;
  session_id: string;
  tool_name: string;
  input: Record<string, any>;
  output: Record<string, any>;
  reversed?: number;
};
