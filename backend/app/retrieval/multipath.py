"""
Multi-path retrieval pipeline.

Steps (per the product brief):
  1. Extract structured intent from the buyer message.
  2. Run one retrieval path per aspect:
       - product_name → keyword (TF-IDF / BM25)
       - product_name → vector embedding
       - manufacturer → SQL filter
       - category     → SQL filter
       - price        → SQL price-window filter
       - memory       → SQL filter
       - color        → SQL filter
     Each path keeps the top 10 unique listings.
  3. Rerank across paths via Reciprocal Rank Fusion → top 3.
  4. Judge: does top 1 satisfy the buyer's hard constraints? If not, return
     top 1 as "closest match" and surface top 2/3 as alternatives with reasons.

Returns a serializable dict; the LLM tool layer presents the result to the model.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Iterable

from app.db import get_conn
from app.retrieval.hybrid import ListingHit, Retriever, get_retriever, _tokenize
from app.retrieval.intent import Intent, get_intent_extractor


# ---------- result types ----------

@dataclass
class PathResult:
    path: str           # e.g. "product_name_keyword"
    method: str         # tfidf | vector | filter | hybrid
    n_hits: int
    top_skus: list[str] = field(default_factory=list)


@dataclass
class RankedHit:
    sku: str
    title: str
    manufacturer: str
    category: str
    price: float
    stock_qty: int
    snippet: str
    rrf_score: float
    matched_paths: list[str]
    meets_hard: bool
    miss_reasons: list[str]
    pageurl: str = ""

    def to_dict(self) -> dict:
        return {
            "sku": self.sku,
            "title": self.title,
            "manufacturer": self.manufacturer,
            "category": self.category,
            "price": self.price,
            "stock_qty": self.stock_qty,
            "snippet": self.snippet,
            "rrf_score": round(self.rrf_score, 5),
            "matched_paths": self.matched_paths,
            "meets_hard": self.meets_hard,
            "miss_reasons": self.miss_reasons,
            "pageurl": self.pageurl,
        }


# ---------- multipath retriever ----------

class MultipathRetriever:
    """Owns the 4-step pipeline. Stateless per query."""

    def __init__(self, base: Retriever | None = None) -> None:
        self.base = base or get_retriever()
        self.intent_x = get_intent_extractor()

    def search(self, query: str, top_k: int = 3, per_path_k: int = 10) -> dict:
        intent = self.intent_x.extract(query)
        paths_meta: list[PathResult] = []
        per_path_hits: list[tuple[str, list[ListingHit]]] = []

        # ---- Step 2: per-aspect paths ----

        if intent.product_name:
            kh = self._keyword_only(intent.product_name, k=per_path_k)
            paths_meta.append(PathResult("product_name_keyword", "tfidf", len(kh), [h.sku for h in kh]))
            per_path_hits.append(("product_name_keyword", kh))

            vh = self._vector_only(intent.product_name, k=per_path_k)
            paths_meta.append(PathResult("product_name_vector", "vector", len(vh), [h.sku for h in vh]))
            per_path_hits.append(("product_name_vector", vh))

        if intent.manufacturer:
            fh = self._sql_filter(
                manufacturer=intent.manufacturer,
                product_name=intent.product_name,
                k=per_path_k,
            )
            paths_meta.append(PathResult("manufacturer_filter", "filter", len(fh), [h.sku for h in fh]))
            per_path_hits.append(("manufacturer_filter", fh))

        if intent.category:
            fh = self._sql_filter(
                category=intent.category,
                product_name=intent.product_name,
                k=per_path_k,
            )
            paths_meta.append(PathResult("category_filter", "filter", len(fh), [h.sku for h in fh]))
            per_path_hits.append(("category_filter", fh))

        if intent.price_target is not None or intent.price_max is not None:
            fh = self._price_window(
                price_target=intent.price_target,
                price_max=intent.price_max,
                product_name=intent.product_name,
                manufacturer=intent.manufacturer,
                category=intent.category,
                k=per_path_k,
            )
            paths_meta.append(PathResult("price_window", "filter", len(fh), [h.sku for h in fh]))
            per_path_hits.append(("price_window", fh))

        if intent.memory:
            fh = self._sql_filter(memory=intent.memory, product_name=intent.product_name, k=per_path_k)
            paths_meta.append(PathResult("memory_filter", "filter", len(fh), [h.sku for h in fh]))
            per_path_hits.append(("memory_filter", fh))

        if intent.color:
            fh = self._sql_filter(color=intent.color, product_name=intent.product_name, k=per_path_k)
            paths_meta.append(PathResult("color_filter", "filter", len(fh), [h.sku for h in fh]))
            per_path_hits.append(("color_filter", fh))

        # No structured signal → just hybrid the raw query so we never return empty
        if not per_path_hits:
            hh = self.base.search_listings(query, k=per_path_k)
            paths_meta.append(PathResult("raw_hybrid", "hybrid", len(hh), [h.sku for h in hh]))
            per_path_hits.append(("raw_hybrid", hh))

        # ---- Step 3: cross-path rerank (RRF) ----

        ranked = self._rrf_rerank(per_path_hits, top_k=top_k)

        # ---- Step 4: judge ----

        ranked = self._mark_hard_constraints(ranked, intent)
        top1_meets = bool(ranked) and ranked[0].meets_hard
        judge_note, alternatives_offered = self._judge_note(ranked, intent)

        return {
            "ok": True,
            "query": query,
            "intent": intent.to_dict(),
            "paths": [
                {"path": p.path, "method": p.method, "n_hits": p.n_hits, "top_skus": p.top_skus[:3]}
                for p in paths_meta
            ],
            "hits": [h.to_dict() for h in ranked],
            "top1_meets_requirements": top1_meets,
            "alternatives_offered": alternatives_offered,
            "judge_note": judge_note,
        }

    # ---------- per-path implementations ----------

    def _keyword_only(self, query: str, k: int) -> list[ListingHit]:
        """BM25 leg only."""
        self.base._ensure_bm25()
        if self.base._bm25 in (None, "disabled"):
            return []
        try:
            from rank_bm25 import BM25Okapi  # noqa: F401
        except ImportError:
            return []
        tokens = _tokenize(query)
        if not tokens:
            return []
        scores = self.base._bm25.get_scores(tokens)
        order = sorted(range(len(scores)), key=lambda i: -scores[i])[: k * 2]
        out: list[ListingHit] = []
        for i in order:
            if scores[i] <= 0:
                break
            m = self.base._bm25_corpus_meta[i]
            out.append(self._row_to_hit(m, score=float(scores[i])))
            if len(out) >= k:
                break
        return out

    def _vector_only(self, query: str, k: int) -> list[ListingHit]:
        """Chroma leg only. Empty list if Chroma not available."""
        self.base._ensure_chroma()
        if self.base._chroma in (None, "disabled") or self.base._listings_col is None:
            return []
        try:
            res = self.base._listings_col.query(query_texts=[query], n_results=k)
            ids = res["ids"][0]
            distances = res.get("distances", [[None] * len(ids)])[0]
        except Exception as e:
            print(f"[multipath] vector path failed: {e}")
            return []
        out: list[ListingHit] = []
        con = get_conn()
        for sku, dist in zip(ids, distances):
            row = con.execute("SELECT * FROM listings WHERE sku = ?", (sku,)).fetchone()
            if row:
                score = 1.0 / (1.0 + (dist or 0))
                out.append(self._row_to_hit(dict(row), score=score))
        con.close()
        return out

    def _sql_filter(
        self,
        *,
        manufacturer: str | None = None,
        category: str | None = None,
        memory: str | None = None,
        color: str | None = None,
        product_name: str | None = None,
        k: int = 10,
    ) -> list[ListingHit]:
        """Hard-filter + relevance order. In-stock first, then by price desc as a stable tiebreak."""
        clauses: list[str] = []
        params: list = []
        if manufacturer:
            clauses.append("LOWER(manufacturer) LIKE ?")
            params.append(f"%{manufacturer.lower()}%")
        if category:
            clauses.append("category = ?")
            params.append(category)
        if memory:
            clauses.append("UPPER(REPLACE(internal_memory, ' ', '')) LIKE ?")
            params.append(f"%{memory.upper().replace(' ', '')}%")
        if color:
            clauses.append("LOWER(color_category) LIKE ?")
            params.append(f"%{color.lower()}%")
        if product_name:
            # weak free-text match across title + model
            clauses.append("(LOWER(title) LIKE ? OR LOWER(model_name) LIKE ?)")
            tok = product_name.lower()
            params.extend([f"%{tok}%", f"%{tok}%"])

        if not clauses:
            return []

        sql = (
            "SELECT * FROM listings WHERE " + " AND ".join(clauses)
            + " ORDER BY (stock_qty > 0) DESC, stock_qty DESC, price DESC LIMIT ?"
        )
        params.append(k)
        con = get_conn()
        rows = con.execute(sql, params).fetchall()
        con.close()
        # if free-text product_name made it too restrictive, retry without it
        if not rows and product_name and (manufacturer or category or memory or color):
            return self._sql_filter(
                manufacturer=manufacturer, category=category,
                memory=memory, color=color, product_name=None, k=k,
            )
        return [self._row_to_hit(dict(r), score=1.0) for r in rows]

    def _price_window(
        self,
        *,
        price_target: float | None,
        price_max: float | None,
        product_name: str | None,
        manufacturer: str | None,
        category: str | None,
        k: int,
    ) -> list[ListingHit]:
        # window: prefer items at-or-below max; if a target was offered, prefer items
        # within ±20% of target (so we surface negotiable ones).
        upper = price_max if price_max is not None else (price_target * 1.20 if price_target else None)
        lower = price_target * 0.80 if price_target is not None else 0
        if upper is None:
            return []

        clauses = ["price BETWEEN ? AND ?"]
        params: list = [lower, upper]
        if manufacturer:
            clauses.append("LOWER(manufacturer) LIKE ?")
            params.append(f"%{manufacturer.lower()}%")
        if category:
            clauses.append("category = ?")
            params.append(category)
        if product_name:
            clauses.append("LOWER(title) LIKE ?")
            params.append(f"%{product_name.lower()}%")

        sql = (
            "SELECT * FROM listings WHERE " + " AND ".join(clauses)
            + " ORDER BY (stock_qty > 0) DESC, ABS(price - ?) ASC LIMIT ?"
        )
        params.append(price_target if price_target is not None else (price_max or 0))
        params.append(k)
        con = get_conn()
        rows = con.execute(sql, params).fetchall()
        con.close()
        return [self._row_to_hit(dict(r), score=1.0) for r in rows]

    # ---------- ranking + judging ----------

    def _rrf_rerank(self, paths: list[tuple[str, list[ListingHit]]], top_k: int) -> list[RankedHit]:
        agg: dict[str, dict] = {}
        for path_name, hits in paths:
            for rank, h in enumerate(hits):
                rec = agg.setdefault(h.sku, {"hit": h, "rrf": 0.0, "paths": set()})
                rec["rrf"] += 1.0 / (60 + rank)
                rec["paths"].add(path_name)
        ordered = sorted(agg.values(), key=lambda r: -r["rrf"])[:top_k]
        out: list[RankedHit] = []
        for rec in ordered:
            h = rec["hit"]
            out.append(RankedHit(
                sku=h.sku, title=h.title, manufacturer=h.manufacturer,
                category=h.category, price=h.price, stock_qty=h.stock_qty,
                snippet=h.snippet,
                rrf_score=rec["rrf"],
                matched_paths=sorted(rec["paths"]),
                meets_hard=True, miss_reasons=[],
            ))
        return out

    def _mark_hard_constraints(self, hits: list[RankedHit], intent: Intent) -> list[RankedHit]:
        cons = intent.hard_constraints()
        is_negotiation = intent.intention == "price_negotiation"
        # Pull content tokens from product_name to check title relevance.
        # We only enforce this when there's a meaningful product noun — short
        # tokens like "got", "any" don't qualify.
        pn_tokens: list[str] = []
        if intent.product_name:
            for t in _tokenize(intent.product_name):
                if len(t) >= 4 and t not in {"have", "want", "stock", "with", "this", "that", "from"}:
                    pn_tokens.append(t)
        for h in hits:
            misses: list[str] = []
            if "min_stock_qty" in cons and h.stock_qty < cons["min_stock_qty"]:
                misses.append(f"stock={h.stock_qty}<{cons['min_stock_qty']}")
            if "max_price" in cons and h.price > cons["max_price"]:
                misses.append(f"price=${h.price:.2f}>${cons['max_price']:.2f}")
            if "manufacturer" in cons and cons["manufacturer"].lower() not in (h.manufacturer or "").lower():
                misses.append(f"manufacturer={h.manufacturer}!={cons['manufacturer']}")
            if "category" in cons and cons["category"] != h.category:
                misses.append(f"category={h.category}!={cons['category']}")
            # Soft price_target check — only when NOT negotiation (in negotiation, buyer
            # expects seller to discount toward target, so target ≠ filter).
            if not is_negotiation and "price_target" in cons and "max_price" not in cons:
                if h.price > cons["price_target"] * 1.10:
                    misses.append(f"price=${h.price:.2f}>10%_above_target_${cons['price_target']:.2f}")
            # Title-token relevance: if buyer named a product, the top hit's
            # title (or model name) should contain at least one of those tokens.
            # Otherwise category-filter alone could promote unrelated items.
            if pn_tokens:
                title_blob = (h.title or "").lower()
                if not any(t in title_blob for t in pn_tokens):
                    misses.append(f"title_lacks:{','.join(pn_tokens[:3])}")
            # stock 0 is always a hard miss for availability/negotiation/recommend
            if intent.intention in ("availability", "price_negotiation", "recommendation") and h.stock_qty == 0:
                if "stock=0" not in " ".join(misses):
                    misses.append("stock=0")
            h.miss_reasons = misses
            h.meets_hard = not misses
        return hits

    def _judge_note(self, hits: list[RankedHit], intent: Intent) -> tuple[str, bool]:
        if not hits:
            return "no_matches", False
        h1 = hits[0]
        if h1.meets_hard:
            return f"top_1_meets_requirements (paths: {', '.join(h1.matched_paths)})", False

        # find first hit that DOES meet
        good = next((h for h in hits[1:] if h.meets_hard), None)
        if good:
            return (
                f"top_1 misses ({'; '.join(h1.miss_reasons)}); "
                f"alternative {good.sku[:8]} satisfies all hard constraints",
                True,
            )
        return (
            f"no candidate meets all hard constraints; closest: {h1.sku[:8]} "
            f"(misses: {'; '.join(h1.miss_reasons)})",
            True,
        )

    # ---------- helpers ----------

    @staticmethod
    def _row_to_hit(m: dict, score: float) -> ListingHit:
        snippet_bits = [m.get("title", ""), f"{m.get('manufacturer','')} {m.get('model_name','')}".strip()]
        for k in ("internal_memory", "color_category", "screen_size", "carrier"):
            v = m.get(k)
            if v:
                snippet_bits.append(f"{k.replace('_', ' ')}: {v}")
        snippet_bits.append(f"price: ${float(m.get('price') or 0):.2f}")
        snippet_bits.append(f"stock: {int(m.get('stock_qty') or 0)}")
        return ListingHit(
            sku=m["sku"],
            title=m.get("title") or "",
            manufacturer=m.get("manufacturer") or "",
            category=m.get("category") or "",
            price=float(m.get("price") or 0.0),
            stock_qty=int(m.get("stock_qty") or 0),
            score=score,
            snippet=" | ".join(b for b in snippet_bits if b),
        )


# module-level singleton
_MULTIPATH: MultipathRetriever | None = None
def get_multipath_retriever() -> MultipathRetriever:
    global _MULTIPATH
    if _MULTIPATH is None:
        _MULTIPATH = MultipathRetriever()
    return _MULTIPATH
