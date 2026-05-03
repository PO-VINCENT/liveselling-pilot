"""
Data pipeline: ebay-sample-data.csv → SQLite catalog + Chroma vector index.

Run from backend/:
    python -m app.data.ingest

Reads CSV_PATH from env (defaults to ../ebay-sample-data.csv).
Synthesizes stock_qty, cost, margin_floor_pct, category since the source
CSV has Stock as a boolean and no cost/margin info.
"""
from __future__ import annotations

import json
import os
import random
import re
import sqlite3
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

load_dotenv()

HERE = Path(__file__).resolve().parent
BACKEND_ROOT = HERE.parent.parent
PROJECT_ROOT = BACKEND_ROOT.parent

CSV_PATH = Path(os.getenv("CSV_PATH", PROJECT_ROOT / "ebay-sample-data.csv"))
DB_PATH = Path(os.getenv("DB_PATH", HERE / "catalog.db"))
CHROMA_PATH = Path(os.getenv("CHROMA_PATH", HERE / "chroma"))
SAMPLE_SIZE = int(os.getenv("SAMPLE_SIZE", "800"))
SEED = int(os.getenv("SEED", "42"))

POLICY_DIR = HERE / "policies"
POLICY_DIR.mkdir(parents=True, exist_ok=True)

random.seed(SEED)

# ---------- helpers ----------

PRICE_RE = re.compile(r"[\d.]+")

def parse_price(s) -> float | None:
    if s is None or (isinstance(s, float) and pd.isna(s)):
        return None
    m = PRICE_RE.search(str(s))
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None

CATEGORY_RULES = [
    ("Audio",        ["airpods", "earbuds", "earphone", "headphones", "headphone", "headset", "beats by", "bose ", "soundbar", "speaker"]),
    ("Wearables",    ["apple watch", "fitbit", "garmin", "smartwatch", "watch ", "tracker", "wristband"]),
    ("Gaming",       ["nintendo", "playstation", "ps4", "ps5", "xbox", "switch console", "joy-con", "gamepad", "controller"]),
    ("Tablets",      ["ipad", "tablet", "galaxy tab", "surface pro", "surface go"]),
    ("Laptops",      ["macbook", "laptop", "notebook computer", "thinkpad", "chromebook"]),
    ("Cameras",      ["camera", "gopro", "dji ", "lens", "drone", "camcorder"]),
    ("TVs/Displays", [" tv ", " tv ", "monitor", "display", "soundbar tv"]),
    ("Smartphones",  ["iphone", "galaxy s", "galaxy note", "galaxy a", "pixel ", "smartphone", "moto g", "moto e", "huawei p", "oneplus"]),
]
def derive_category(title: str, manufacturer: str) -> str:
    t = (str(title) + " " + str(manufacturer)).lower()
    for cat, keys in CATEGORY_RULES:
        if any(k in t for k in keys):
            return cat
    return "Accessories"

def synth_stock_qty(in_stock: bool) -> int:
    if not in_stock:
        return 0
    # heavy-tailed: most have 1-15, some have 30-50
    if random.random() < 0.75:
        return random.randint(1, 15)
    return random.randint(16, 50)

def synth_cost_and_floor(price: float, category: str) -> tuple[float, float]:
    """cost between 55% and 75% of price; margin floor 8-18% depending on category."""
    cost_ratio = random.uniform(0.55, 0.75)
    cost = round(price * cost_ratio, 2)
    floor_pct_by_cat = {
        "Smartphones": (0.10, 0.15),
        "Tablets":     (0.10, 0.15),
        "Laptops":     (0.08, 0.12),
        "Audio":       (0.12, 0.18),
        "Wearables":   (0.12, 0.18),
        "Gaming":      (0.08, 0.12),
        "Cameras":     (0.10, 0.16),
        "TVs/Displays":(0.08, 0.12),
        "Accessories": (0.15, 0.25),
    }
    lo, hi = floor_pct_by_cat.get(category, (0.10, 0.18))
    floor_pct = round(random.uniform(lo, hi), 3)
    return cost, floor_pct

def listing_text_for_embedding(row: dict) -> str:
    """One blob used for vector + BM25 indexing."""
    parts = [
        row["title"],
        f"Manufacturer: {row['manufacturer']}" if row.get("manufacturer") else "",
        f"Model: {row['model_name']}" if row.get("model_name") else "",
        f"Model #: {row['model_num']}" if row.get("model_num") else "",
        f"Memory: {row['internal_memory']}" if row.get("internal_memory") else "",
        f"Color: {row['color_category']}" if row.get("color_category") else "",
        f"Carrier: {row['carrier']}" if row.get("carrier") else "",
        f"Screen: {row['screen_size']}" if row.get("screen_size") else "",
        f"Category: {row['category']}",
        f"Price: ${row['price']:.2f}",
    ]
    return " | ".join(p for p in parts if p)

# ---------- main ----------

def load_and_clean() -> pd.DataFrame:
    print(f"[ingest] reading {CSV_PATH}")
    df = pd.read_csv(CSV_PATH, low_memory=False, on_bad_lines="skip")
    print(f"[ingest] raw rows: {len(df):,}")

    # parse price, drop bad rows
    df["price_num"] = df["Price"].apply(parse_price)
    df = df.dropna(subset=["price_num", "Title", "Manufacturer"])
    df = df[df["price_num"] > 1.0]
    df = df[df["Broken Link"] == False]  # noqa: E712
    df = df[df["Discontinued"] == False]  # noqa: E712
    print(f"[ingest] after filter: {len(df):,}")

    # Always-include "anchor" listings so demo queries land — airpods, iphone, switch, etc.
    anchor_patterns = [
        ("AirPods", r"airpod", 6),
        ("iPhone",  r"iphone", 8),
        ("Galaxy",  r"galaxy s|galaxy note", 6),
        ("Switch",  r"nintendo switch", 6),
        ("Watch",   r"apple watch", 4),
        ("Beats",   r"beats", 4),
        ("Bose",    r"bose ", 4),
        ("MacBook", r"macbook", 4),
        ("PS",      r"playstation|ps4|ps5", 4),
        ("Pixel",   r"pixel ", 3),
    ]
    anchors = []
    used_idx: set[int] = set()
    for label, pat, n in anchor_patterns:
        m = df[df["Title"].str.contains(pat, case=False, regex=True, na=False)]
        m = m[~m.index.isin(used_idx)]
        if len(m) == 0:
            continue
        pick = m.sample(n=min(n, len(m)), random_state=SEED)
        anchors.append(pick)
        used_idx.update(pick.index.tolist())
        print(f"[ingest]   anchor {label}: +{len(pick)}")
    anchor_df = pd.concat(anchors) if anchors else df.iloc[:0]

    rest = df[~df.index.isin(used_idx)]
    in_stock = rest[rest["Stock"] == True]  # noqa: E712
    out_stock = rest[rest["Stock"] == False]  # noqa: E712
    remaining = max(0, SAMPLE_SIZE - len(anchor_df))
    n_out = min(len(out_stock), int(remaining * 0.05))
    n_in = remaining - n_out
    sampled = pd.concat([
        anchor_df,
        in_stock.sample(n=min(n_in, len(in_stock)), random_state=SEED),
        out_stock.sample(n=n_out, random_state=SEED) if n_out > 0 else out_stock.iloc[:0],
    ]).sample(frac=1.0, random_state=SEED).reset_index(drop=True)
    print(f"[ingest] sampled: {len(sampled):,} (incl. {len(anchor_df)} anchors)")
    return sampled

def to_listing_rows(df: pd.DataFrame) -> list[dict]:
    rows: list[dict] = []
    for _, r in df.iterrows():
        title = str(r["Title"])[:300]
        manufacturer = str(r["Manufacturer"])[:80] if not pd.isna(r["Manufacturer"]) else ""
        model_name = "" if pd.isna(r["Model Name"]) else str(r["Model Name"])[:120]
        model_num = "" if pd.isna(r["Model Num"]) else str(r["Model Num"])[:60]
        category = derive_category(title, manufacturer)
        price = float(r["price_num"])
        in_stock = bool(r["Stock"])
        stock_qty = synth_stock_qty(in_stock)
        cost, floor_pct = synth_cost_and_floor(price, category)
        row = {
            "sku": str(r["Uniq Id"]),
            "title": title,
            "manufacturer": manufacturer,
            "model_name": model_name,
            "model_num": model_num,
            "category": category,
            "price": price,
            "cost": cost,
            "margin_floor_pct": floor_pct,
            "stock_qty": stock_qty,
            "color_category": "" if pd.isna(r["Color Category"]) else str(r["Color Category"])[:60],
            "internal_memory": "" if pd.isna(r["Internal Memory"]) else str(r["Internal Memory"])[:40],
            "screen_size": "" if pd.isna(r["Screen Size"]) else str(r["Screen Size"])[:40],
            "carrier": "" if pd.isna(r["Carrier"]) else str(r["Carrier"])[:40],
            "average_rating": None if pd.isna(r["Average Rating"]) else float(r["Average Rating"]),
            "num_of_reviews": None if pd.isna(r["Num Of Reviews"]) else _to_int(r["Num Of Reviews"]),
            "seller_rating": "" if pd.isna(r["Seller Rating"]) else str(r["Seller Rating"])[:20],
            "seller_num_of_reviews": None if pd.isna(r["Seller Num Of Reviews"]) else _to_int(r["Seller Num Of Reviews"]),
            "pageurl": "" if pd.isna(r["Pageurl"]) else str(r["Pageurl"])[:500],
        }
        rows.append(row)
    return rows

def _to_int(v) -> int | None:
    try:
        return int(float(str(v).replace(",", "")))
    except Exception:
        return None

# ---------- SQLite ----------

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
  sku TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  manufacturer TEXT,
  model_name TEXT,
  model_num TEXT,
  category TEXT,
  price REAL NOT NULL,
  cost REAL NOT NULL,
  margin_floor_pct REAL NOT NULL,
  stock_qty INTEGER NOT NULL,
  color_category TEXT,
  internal_memory TEXT,
  screen_size TEXT,
  carrier TEXT,
  average_rating REAL,
  num_of_reviews INTEGER,
  seller_rating TEXT,
  seller_num_of_reviews INTEGER,
  pageurl TEXT
);
CREATE INDEX IF NOT EXISTS idx_listings_manufacturer ON listings(manufacturer);
CREATE INDEX IF NOT EXISTS idx_listings_category ON listings(category);

CREATE TABLE IF NOT EXISTS audit_log (
  audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  session_id TEXT,
  tool_name TEXT NOT NULL,
  input_json TEXT NOT NULL,
  output_json TEXT NOT NULL,
  guardrail_verdict TEXT,
  reversed INTEGER DEFAULT 0,
  reverse_of INTEGER
);
CREATE INDEX IF NOT EXISTS idx_audit_session ON audit_log(session_id);

CREATE TABLE IF NOT EXISTS messages (
  message_id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  ts TEXT NOT NULL,
  role TEXT NOT NULL,         -- buyer | seller | system
  text TEXT NOT NULL,
  buyer_handle TEXT,
  citations_json TEXT,
  audit_id INTEGER,
  auto INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
"""

def write_sqlite(rows: list[dict]) -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()
    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)
    con.executemany(
        """INSERT INTO listings (
            sku, title, manufacturer, model_name, model_num, category,
            price, cost, margin_floor_pct, stock_qty,
            color_category, internal_memory, screen_size, carrier,
            average_rating, num_of_reviews, seller_rating, seller_num_of_reviews, pageurl
        ) VALUES (
            :sku, :title, :manufacturer, :model_name, :model_num, :category,
            :price, :cost, :margin_floor_pct, :stock_qty,
            :color_category, :internal_memory, :screen_size, :carrier,
            :average_rating, :num_of_reviews, :seller_rating, :seller_num_of_reviews, :pageurl
        )""",
        rows,
    )
    con.commit()
    con.close()
    print(f"[ingest] sqlite written: {DB_PATH} ({len(rows):,} listings)")

# ---------- Chroma ----------

def write_chroma(rows: list[dict]) -> None:
    try:
        import chromadb
        from chromadb.utils import embedding_functions
    except Exception as e:
        print(f"[ingest] WARN: chromadb not available, skipping vector index: {e}")
        return
    CHROMA_PATH.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(path=str(CHROMA_PATH))
    try:
        client.delete_collection("listings")
    except Exception:
        pass
    try:
        client.delete_collection("policies")
    except Exception:
        pass
    embed_fn = embedding_functions.SentenceTransformerEmbeddingFunction(
        model_name="all-MiniLM-L6-v2"
    )
    coll = client.create_collection("listings", embedding_function=embed_fn)
    BATCH = 200
    for i in range(0, len(rows), BATCH):
        chunk = rows[i:i+BATCH]
        coll.add(
            ids=[r["sku"] for r in chunk],
            documents=[listing_text_for_embedding(r) for r in chunk],
            metadatas=[{
                "manufacturer": r["manufacturer"] or "",
                "category": r["category"] or "",
                "price": r["price"],
                "stock_qty": r["stock_qty"],
            } for r in chunk],
        )
    print(f"[ingest] chroma listings written: {len(rows):,}")

    # policy index
    pol_coll = client.create_collection("policies", embedding_function=embed_fn)
    pol_ids, pol_docs, pol_meta = [], [], []
    for p in POLICY_DIR.glob("*.md"):
        text = p.read_text()
        for j, chunk in enumerate(_chunk_text(text, 500)):
            pol_ids.append(f"{p.stem}::{j}")
            pol_docs.append(chunk)
            pol_meta.append({"policy": p.stem, "source": p.name})
    if pol_ids:
        pol_coll.add(ids=pol_ids, documents=pol_docs, metadatas=pol_meta)
        print(f"[ingest] chroma policies written: {len(pol_ids):,} chunks")

def _chunk_text(text: str, target: int) -> list[str]:
    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    out, buf = [], ""
    for p in paras:
        if len(buf) + len(p) + 2 > target and buf:
            out.append(buf.strip())
            buf = p
        else:
            buf = (buf + "\n\n" + p) if buf else p
    if buf:
        out.append(buf.strip())
    return out

# ---------- policy + pricing seed files ----------

POLICIES = {
    "returns.md": """# Returns Policy

Buyers may return any item within **30 days** of delivery for a full refund, provided the item is in its original condition and packaging.

- Buyer pays return shipping unless the item arrived damaged, defective, or not as described.
- Refunds are issued within 2 business days of receiving the returned item.
- Items marked **Final Sale** in the listing description are not eligible for return.
- Electronics must include all original accessories, cables, and chargers.
- Restocking fee of 10% may apply to opened consumer electronics.
""",
    "shipping.md": """# Shipping Policy

We ship from our US warehouse Monday through Saturday.

- **Standard shipping:** 3–5 business days, free on orders over $50.
- **Expedited shipping:** 1–2 business days, flat $14.99.
- **International:** 7–14 business days, calculated at checkout. Buyer is responsible for any customs or import duties.
- All orders placed before 1 PM ET ship the same day.
- Tracking numbers are emailed within 24 hours of shipment.
- We do not ship to PO boxes for items over $300.
""",
    "authenticity.md": """# Authenticity & Condition

Every item we sell is sourced from authorized distributors or verified secondary-market suppliers.

- New items are sealed in original manufacturer packaging.
- Open-box items have been inspected and tested; they include all original accessories unless otherwise noted.
- Refurbished items carry a 90-day warranty from us in addition to any manufacturer warranty that still applies.
- We do not sell counterfeit, replica, or unauthorized goods. If any item is found to be inauthentic, we will refund 100% plus return shipping.
""",
    "prohibited_claims.md": """# Prohibited Claims (Internal)

The seller assistant must not make any of these claims unless they are explicitly verified in the listing data or policy documents:

- Specific delivery dates ("you will receive it by Tuesday").
- Compatibility claims for products not described in the listing.
- Manufacturer warranty terms beyond what's listed.
- Permanent price guarantees ("this price won't go up").
- Health, medical, or safety claims about any product.
- Comparisons disparaging other eBay sellers.
""",
}

BRAND_TONE = """# Brand Tone Guide

**Voice:** warm, direct, knowledgeable. Talk like a helpful local shop owner who knows the products. Avoid corporate hedging.

**Always:**
- Open with the answer, not a greeting. The chat UI already labels who's speaking.
- State the actual SKU/model when answering availability.
- Quote the listing or policy when making factual claims.
- Offer a clear next step (add to cart, see listing, ask another question).

**Never:**
- Promise specific delivery dates.
- Use ALL CAPS for emphasis.
- Use emoji other than a single 👍 in a confirmation.
- Apologize more than once per message.
- Disparage competitors or other sellers.
- Use "absolutely", "literally", "to be honest".

**Examples:**

Good: "We've got 4 left in 256 GB Deep Purple at $1,049. Tap the listing to grab one — link below."

Bad: "ABSOLUTELY! We totally have that in stock, you'll LOVE it 🔥🔥"
"""

PRICING_RULES = {
    "global": {
        "max_markdown_pct": 0.20,
        "auto_send_max_markdown_pct": 0.10,
        "require_human_above_pct": 0.10,
    },
    "by_category": {
        "Smartphones":  {"max_markdown_pct": 0.12, "auto_send_max_markdown_pct": 0.06},
        "Tablets":      {"max_markdown_pct": 0.12, "auto_send_max_markdown_pct": 0.06},
        "Laptops":      {"max_markdown_pct": 0.10, "auto_send_max_markdown_pct": 0.05},
        "Audio":        {"max_markdown_pct": 0.18, "auto_send_max_markdown_pct": 0.08},
        "Wearables":    {"max_markdown_pct": 0.18, "auto_send_max_markdown_pct": 0.08},
        "Gaming":       {"max_markdown_pct": 0.10, "auto_send_max_markdown_pct": 0.05},
        "Cameras":      {"max_markdown_pct": 0.15, "auto_send_max_markdown_pct": 0.07},
        "TVs/Displays": {"max_markdown_pct": 0.10, "auto_send_max_markdown_pct": 0.05},
        "Accessories":  {"max_markdown_pct": 0.25, "auto_send_max_markdown_pct": 0.10},
    },
    "notes": "All markdowns must respect the per-listing margin_floor_pct. The lower of (price * (1 - max_markdown_pct)) and (cost * (1 + margin_floor_pct)) is the floor.",
}

def write_seed_files() -> None:
    for name, body in POLICIES.items():
        (POLICY_DIR / name).write_text(body)
    (HERE / "brand_tone.md").write_text(BRAND_TONE)
    (HERE / "pricing_rules.json").write_text(json.dumps(PRICING_RULES, indent=2))
    print(f"[ingest] policies + brand_tone + pricing_rules written")

# ---------- chat replay generator ----------

def write_chat_replay(rows: list[dict]) -> None:
    """Build a deterministic 50-message chat stream that exercises the 5 north-star scenarios."""
    in_stock = [r for r in rows if r["stock_qty"] > 0]
    out_stock = [r for r in rows if r["stock_qty"] == 0]
    if not in_stock:
        print("[ingest] WARN: no in-stock listings to script chat against")
        return

    # pick anchor SKUs across categories
    by_cat: dict[str, list[dict]] = {}
    for r in in_stock:
        by_cat.setdefault(r["category"], []).append(r)

    def first(cat: str, fallback_idx: int = 0) -> dict:
        return by_cat.get(cat, [in_stock[fallback_idx]])[0]

    phone = first("Smartphones", 0)
    audio = first("Audio", 1)
    wear  = first("Wearables", 2)
    game  = first("Gaming", 3)
    laptop= first("Laptops", 4)
    oos   = out_stock[0] if out_stock else in_stock[-1]

    msgs = [
        {"buyer": "@mia_k",  "text": f"hey is the {phone['title'][:60]} still available?"},
        {"buyer": "@dan99",  "text": f"do you have {audio['title'][:50]} in stock right now?"},
        {"buyer": "@sara",   "text": f"what's your return policy on opened electronics?"},
        {"buyer": "@mia_k",  "text": f"can you do ${max(1, int(phone['price']*0.92))} on the {phone['manufacturer']}?"},
        {"buyer": "@kev",    "text": f"how fast does shipping take to california?"},
        {"buyer": "@dan99",  "text": "is this authentic? i've been burned before"},
        {"buyer": "@troll",  "text": "this is fake garbage, you're a scammer"},
        {"buyer": "@jess",   "text": f"do u have the {wear['title'][:60]}?"},
        {"buyer": "@mia_k",  "text": "great, will it arrive by friday?"},
        {"buyer": "@sara",   "text": f"what colors does the {phone['manufacturer']} {phone['model_name'][:30]} come in?"},
        {"buyer": "@nick",   "text": f"got any deals on {game['manufacturer']} stuff?"},
        {"buyer": "@dan99",  "text": f"can i return it if my wife doesn't like it"},
        {"buyer": "@kev",    "text": f"will the {laptop['title'][:60]} run starcraft 2"},
        {"buyer": "@mia_k",  "text": f"do u ship to canada"},
        {"buyer": "@oos_buyer", "text": f"is the {oos['title'][:60]} available?"},
        {"buyer": "@jess",   "text": f"i'll take 2 of the {audio['manufacturer']} {audio['model_name'][:25]}"},
        {"buyer": "@sara",   "text": f"price match? i saw it cheaper somewhere"},
        {"buyer": "@dan99",  "text": "what's the warranty"},
        {"buyer": "@nick",   "text": "do you accept paypal"},
        {"buyer": "@kev",    "text": f"any in 256gb"},
    ]

    out_path = HERE / "chat_replay.jsonl"
    with out_path.open("w") as f:
        for i, m in enumerate(msgs):
            f.write(json.dumps({
                "seq": i,
                "delay_ms": random.randint(800, 3500),
                "buyer_handle": m["buyer"],
                "text": m["text"],
            }) + "\n")
    print(f"[ingest] chat replay written: {out_path} ({len(msgs)} messages)")

# ---------- entrypoint ----------

def main():
    write_seed_files()
    df = load_and_clean()
    rows = to_listing_rows(df)
    write_sqlite(rows)
    write_chat_replay(rows)
    write_chroma(rows)
    print("[ingest] done.")

if __name__ == "__main__":
    main()
