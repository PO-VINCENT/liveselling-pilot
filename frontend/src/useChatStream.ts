import { useEffect, useRef, useState, useCallback } from "react";
import { ChatItem, StreamEvent } from "./types";

const SESSION_ID = "demo1";

export function useChatStream() {
  const [items, setItems] = useState<ChatItem[]>([]);
  const [connected, setConnected] = useState(false);
  const [latencies, setLatencies] = useState<{ first: number[]; total: number[] }>({ first: [], total: [] });
  const wsRef = useRef<WebSocket | null>(null);
  const currentIdRef = useRef<string | null>(null);
  // De-dup guard: if React StrictMode (or any other accident) opens two WS
  // connections, every buyer_message broadcast lands on both. We dedupe by a
  // short-window key of (handle + text) so the UI only appends once.
  const recentMsgKeysRef = useRef<Map<string, number>>(new Map());

  function handleEvent(ev: StreamEvent) {
    if (ev.type === "buyer_message") {
      // dedupe within a 2s window — protects against duplicate WS connections
      const key = `${ev.buyer_handle}|${ev.text}`;
      const now = Date.now();
      const prevTs = recentMsgKeysRef.current.get(key);
      if (prevTs && now - prevTs < 2000) return; // already saw this
      recentMsgKeysRef.current.set(key, now);
      // garbage-collect old keys
      if (recentMsgKeysRef.current.size > 100) {
        for (const [k, t] of recentMsgKeysRef.current) {
          if (now - t > 5000) recentMsgKeysRef.current.delete(k);
        }
      }
      const id = `m_${now}_${Math.random().toString(36).slice(2,6)}`;
      currentIdRef.current = id;
      setItems((prev) => [
        ...prev,
        {
          id, buyer_handle: ev.buyer_handle, text: ev.text, ts: now,
          toolTrace: [], status: "drafting", draft: "",
        },
      ]);
      return;
    }
    setItems((prev) => updateCurrent(prev, currentIdRef.current, ev));
  }

  const sendMessage = useCallback((text: string, buyer_handle = "@me") => {
    wsRef.current?.send(JSON.stringify({ type: "buyer_message", text, buyer_handle }));
  }, []);

  const recordLatency = useCallback((first?: number, total?: number) => {
    setLatencies((s) => ({
      first: first ? [...s.first.slice(-19), first] : s.first,
      total: total ? [...s.total.slice(-19), total] : s.total,
    }));
  }, []);

  function updateCurrent(prev: ChatItem[], id: string | null, ev: StreamEvent): ChatItem[] {
    if (!id) return prev;
    return prev.map((it) => {
      if (it.id !== id) return it;
      const next: ChatItem = { ...it };
      switch (ev.type) {
        case "thinking":
          break;
        case "tool_use":
          next.toolTrace = [...next.toolTrace, { name: ev.name, input: ev.input }];
          break;
        case "tool_result":
          next.toolTrace = next.toolTrace.map((t, i) =>
            i === next.toolTrace.length - 1 && t.name === ev.name && t.result === undefined
              ? { ...t, result: ev.result }
              : t
          );
          break;
        case "token":
          next.draft = (next.draft || "") + ev.text;
          break;
        case "first_token_ms":
          next.firstTokenMs = ev.ms;
          recordLatency(ev.ms, undefined);
          break;
        case "reply":
          next.reply = ev.payload;
          if (ev.payload.final_action === "auto" && ev.payload.delivered) next.status = "sent";
          else if (ev.payload.final_action === "block") next.status = "blocked";
          else next.status = "needs_human";
          if (ev.payload.text) next.draft = ev.payload.text;
          break;
        case "latency":
          if (ev.first_token_ms) next.firstTokenMs = ev.first_token_ms;
          if (ev.total_ms) next.totalMs = ev.total_ms;
          recordLatency(ev.first_token_ms, ev.total_ms);
          break;
        case "error":
          next.error = ev.detail;
          next.status = "blocked";
          break;
      }
      return next;
    });
  }

  const accept = useCallback((id: string) => {
    setItems((prev) => prev.map((it) => (it.id === id ? { ...it, status: "sent" } : it)));
  }, []);
  const reject = useCallback((id: string) => {
    setItems((prev) => prev.map((it) => (it.id === id ? { ...it, status: "blocked" } : it)));
  }, []);
  const editDraft = useCallback((id: string, text: string) => {
    setItems((prev) => prev.map((it) => (it.id === id ? { ...it, draft: text } : it)));
  }, []);

  // Single-WS lifecycle. Cancellation flag guards against StrictMode double-mount
  // and against the reconnect timer firing after unmount.
  useEffect(() => {
    let cancelled = false;
    let reconnectTimer: number | null = null;

    const open = () => {
      if (cancelled) return;
      // If there's already a live socket, don't open another.
      const existing = wsRef.current;
      if (existing && (existing.readyState === WebSocket.OPEN || existing.readyState === WebSocket.CONNECTING)) {
        return;
      }
      const ws = new WebSocket(
        (location.protocol === "https:" ? "wss://" : "ws://") +
          location.host +
          `/ws/chat?session_id=${SESSION_ID}&role=operator`
      );
      wsRef.current = ws;
      ws.onopen = () => { if (!cancelled) setConnected(true); };
      ws.onclose = () => {
        if (cancelled) return;
        setConnected(false);
        reconnectTimer = window.setTimeout(open, 1500);
      };
      ws.onmessage = (e) => {
        if (cancelled) return;
        let ev: StreamEvent;
        try { ev = JSON.parse(e.data); } catch { return; }
        handleEvent(ev);
      };
    };

    open();

    return () => {
      cancelled = true;
      if (reconnectTimer) window.clearTimeout(reconnectTimer);
      const ws = wsRef.current;
      wsRef.current = null;
      try { ws?.close(); } catch { /* ignore */ }
    };
  }, []);

  return { items, connected, sendMessage, accept, reject, editDraft, latencies };
}

export function p95(arr: number[]): number | null {
  if (!arr.length) return null;
  const sorted = [...arr].sort((a, b) => a - b);
  return sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * 0.95))];
}
