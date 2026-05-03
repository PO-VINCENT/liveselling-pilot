"""
Hybrid retrieval over the eBay catalog and policy docs.

- BM25 over listing text (tokenized title + manufacturer + model + specs).
- Chroma vector search (sentence-transformers/all-MiniLM-L6-v2) for semantic recall.
- Reciprocal Rank Fusion (RRF) to combine.

Two collections: 'listings' and 'policies'. Both queryable.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from app.db import get_conn

HERE = Path(__file__).resolve().parent
CHROMA_PATH = Path(os.getenv("CHROMA_PATH", HERE.parent / "data" / "chroma"))


def _tokenize(text: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", text.lower()) if t]


@dataclass
class ListingHit:
    sku: str
    title: str
    manufacturer: str
    category: str
    price: float
    stock_qty: int
    score: float
    snippet: str

    def cite(self) -> str:
        return f"[{self.sku[:8]}] {self.manufacturer} — {self.title[:80]}"


@dataclass
class PolicyHit:
    policy: str
    chunk_id: str
    text: str
    score: float

    def cite(self) -> str:
        return f"[policy:{self.policy}]"


class Retriever:
    """Hybrid BM25 + vector retriever. Lazy-loads everything."""

    def __init__(self) -> None:
        self._bm25 = None
        self._bm25_corpus_meta: list[dict] = []
        self._chroma = None
        self._listings_col = None
        self._policies_col = None

    # -------- BM25 --------

    def _ensure_bm25(self) -> None:
        if self._bm25 is not None:
            return
        try:
            from rank_bm25 import BM25Okapi
        except ImportError:
            print("[retrieval] WARN: rank_bm25 not installed; BM25 disabled")
            self._bm25 = "disabled"
            return
        con = get_conn()
        rows = con.execute(
            """SELECT sku, title, manufacturer, model_name, model_num, category,
                      price, stock_qty, color_category, internal_memory, screen_size, carrier
               FROM listings"""
        ).fetchall()
        con.close()
        corpus_tokens, corpus_meta = [], []
        for r in rows:
            blob = " ".join(str(r[k] or "") for k in (
                "title", "manufacturer", "model_name", "model_num",
                "category", "color_category", "internal_memory",
                "screen_size", "carrier",
            ))
            corpus_tokens.append(_tokenize(blob))
            corpus_meta.append(dict(r))
        self._bm25 = BM25Okapi(corpus_tokens)
        self._bm25_corpus_meta = corpus_meta

    # -------- Chroma --------

    def _ensure_chroma(self) -> None:
        if self._chroma is not None:
            return
        try:
            import chromadb
            from chromadb.utils import embedding_functions
            self._chroma = chromadb.PersistentClient(path=str(CHROMA_PATH))
            ef = embedding_functions.SentenceTransformerEmbeddingFunction(
                model_name="all-MiniLM-L6-v2"
            )
            try:
                self._listings_col = self._chroma.get_collection("listings", embedding_function=ef)
            except Exception:
                self._listings_col = None
            try:
                self._policies_col = self._chroma.get_collection("policies", embedding_function=ef)
            except Exception:
                self._policies_col = None
        except Exception as e:
            print(f"[retrieval] WARN: Chroma disabled ({e})")
            self._chroma = "disabled"

    # -------- public API --------

    def search_listings(self, query: str, k: int = 5) -> list[ListingHit]:
        self._ensure_bm25()
        self._ensure_chroma()
        ranked: dict[str, dict] = {}  # sku -> aggregated info

        # BM25 leg
        if self._bm25 not in (None, "disabled"):
            tokens = _tokenize(query)
            if tokens:
                scores = self._bm25.get_scores(tokens)
                top = sorted(range(len(scores)), key=lambda i: -scores[i])[:k * 4]
                for rank, i in enumerate(top):
                    if scores[i] <= 0:
                        continue
                    meta = self._bm25_corpus_meta[i]
                    ranked.setdefault(meta["sku"], {"meta": meta, "rrf": 0.0})
                    ranked[meta["sku"]]["rrf"] += 1.0 / (60 + rank)

        # Vector leg
        if self._chroma not in (None, "disabled") and self._listings_col is not None:
            try:
                res = self._listings_col.query(query_texts=[query], n_results=k * 4)
                ids = res["ids"][0]
                for rank, sku in enumerate(ids):
                    if sku not in ranked:
                        # need to hydrate from sqlite
                        con = get_conn()
                        row = con.execute("SELECT * FROM listings WHERE sku = ?", (sku,)).fetchone()
                        con.close()
                        if not row:
                            continue
                        ranked[sku] = {"meta": dict(row), "rrf": 0.0}
                    ranked[sku]["rrf"] += 1.0 / (60 + rank)
            except Exception as e:
                print(f"[retrieval] vector query failed: {e}")

        out: list[ListingHit] = []
        for sku, info in sorted(ranked.items(), key=lambda kv: -kv[1]["rrf"])[:k]:
            m = info["meta"]
            snippet = self._build_snippet(m)
            out.append(ListingHit(
                sku=sku,
                title=m.get("title") or "",
                manufacturer=m.get("manufacturer") or "",
                category=m.get("category") or "",
                price=float(m.get("price") or 0.0),
                stock_qty=int(m.get("stock_qty") or 0),
                score=info["rrf"],
                snippet=snippet,
            ))
        return out

    def _build_snippet(self, m: dict) -> str:
        bits = [
            m.get("title", ""),
            f"{m.get('manufacturer', '')} {m.get('model_name', '')}".strip(),
        ]
        for k in ("internal_memory", "color_category", "screen_size", "carrier"):
            v = m.get(k)
            if v:
                bits.append(f"{k.replace('_', ' ')}: {v}")
        bits.append(f"price: ${float(m.get('price') or 0):.2f}")
        bits.append(f"stock: {int(m.get('stock_qty') or 0)}")
        return " | ".join(b for b in bits if b)

    def search_policies(self, query: str, k: int = 3) -> list[PolicyHit]:
        self._ensure_chroma()
        if self._chroma in (None, "disabled") or self._policies_col is None:
            # fall back to keyword-ish match over filesystem policies
            return self._fallback_policy_keyword(query, k)
        try:
            res = self._policies_col.query(query_texts=[query], n_results=k)
            hits = []
            ids = res["ids"][0]
            docs = res["documents"][0]
            metas = res["metadatas"][0]
            distances = res.get("distances", [[None]*len(ids)])[0]
            for i, (cid, doc, meta) in enumerate(zip(ids, docs, metas)):
                score = 1.0 / (1.0 + (distances[i] or 0))
                hits.append(PolicyHit(
                    policy=meta.get("policy", "policy"),
                    chunk_id=cid,
                    text=doc,
                    score=score,
                ))
            return hits
        except Exception as e:
            print(f"[retrieval] policy query failed: {e}")
            return self._fallback_policy_keyword(query, k)

    def _fallback_policy_keyword(self, query: str, k: int) -> list[PolicyHit]:
        policies_dir = HERE.parent / "data" / "policies"
        if not policies_dir.exists():
            return []
        q_tokens = set(_tokenize(query))
        hits: list[PolicyHit] = []
        for p in policies_dir.glob("*.md"):
            text = p.read_text()
            tokens = set(_tokenize(text))
            score = len(q_tokens & tokens) / (len(q_tokens) or 1)
            if score > 0:
                hits.append(PolicyHit(policy=p.stem, chunk_id=p.stem, text=text[:1500], score=score))
        return sorted(hits, key=lambda h: -h.score)[:k]


# module-level singleton
_RETRIEVER: Retriever | None = None

def get_retriever() -> Retriever:
    global _RETRIEVER
    if _RETRIEVER is None:
        _RETRIEVER = Retriever()
    return _RETRIEVER
