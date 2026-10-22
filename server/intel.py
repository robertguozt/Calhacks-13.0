"""Scam intel (our Apify actor's FTC alerts) and member history search (Elastic).

Both are optional, and they're first on the cut list. Without APIFY_TOKEN, alerts come from
`alerts.json` if present. Without ELASTIC_URL, alerts are matched in memory and there
are no `elastic` history signals (triage still summarizes history itself).
"""

import asyncio
import json
import os
import pathlib

from loguru import logger

ALERTS: list[dict] = []
ALERTS_FILE = pathlib.Path(__file__).with_name("alerts.json")
# purchase category -> payment method the FTC alerts talk about
PAYMENT_FOR = {"gift_card": "gift_card", "wire_transfer": "wire_transfer", "crypto": "crypto"}

_es = None
if os.getenv("ELASTIC_URL"):
    from elasticsearch import Elasticsearch, helpers

    _es = Elasticsearch(os.environ["ELASTIC_URL"], api_key=os.environ["ELASTIC_API_KEY"])

TXNS, PATTERNS = "member_txns", "scam_patterns"


def _ensure_indices():
    for index, props in (
        (TXNS, {"member_id": {"type": "keyword"}, "date": {"type": "date"}, "amount": {"type": "float"},
                "category": {"type": "keyword"}, "merchant": {"type": "keyword"}, "description": {"type": "text"}}),
        (PATTERNS, {"title": {"type": "text"}, "date": {"type": "date"}, "scam_types": {"type": "keyword"},
                    "payment_methods": {"type": "keyword"}, "red_flags": {"type": "text"}, "content": {"type": "text"},
                    "url": {"type": "keyword"}}),
    ):
        if not _es.indices.exists(index=index):
            _es.indices.create(index=index, mappings={"properties": props})


def _load_alerts_sync():
    items = []
    if os.getenv("APIFY_TOKEN") and os.getenv("APIFY_ACTOR"):
        from apify_client import ApifyClient

        run = ApifyClient(os.environ["APIFY_TOKEN"]).actor(os.environ["APIFY_ACTOR"]).last_run(status="SUCCEEDED")
        items = run.dataset().list_items().items
        ALERTS_FILE.write_text(json.dumps(items))  # offline copy for the next restart
    elif ALERTS_FILE.exists():
        items = json.loads(ALERTS_FILE.read_text())
    ALERTS[:] = items
    if _es:
        _ensure_indices()
        helpers.bulk(_es, ({"_index": PATTERNS, "_id": a["url"], **{k: a.get(k) for k in
                      ("title", "date", "scam_types", "payment_methods", "red_flags", "content", "url")}} for a in items))
    logger.info(f"intel: {len(items)} FTC alerts loaded; elastic={'on' if _es else 'off'}")


async def load_alerts():
    try:
        await asyncio.to_thread(_load_alerts_sync)
    except Exception as e:
        logger.error(f"intel: alerts not loaded: {e}")


WORDS = {"gift_card": "gift card", "wire_transfer": "wire", "crypto": "crypto"}


def _fmt(a: dict, word: str = "") -> str:
    flags = a.get("red_flags") or []
    flag = next((f for f in flags if word and word in f.lower()), flags[0] if flags else "")  # quote the sentence that names it
    return f'FTC alert {a.get("date")}: "{a.get("title")}"' + (f" (FTC: {flag})" if flag else "")


def _rank(a: dict, word: str) -> tuple:
    """Alerts whose own warnings name the payment method first, then ones about older people / imposters, then newest."""
    flags = " ".join(a.get("red_flags") or []).lower()
    text = f"{a.get('title', '')} {a.get('content', '')}".lower()
    return (word in flags, any(w in text for w in ("older", "elder", "imposter", "impersonat")), a.get("date") or "")


def _alerts_sync(txn: dict) -> list[str]:
    pay = PAYMENT_FOR.get(txn["category"])
    word = WORDS.get(txn["category"], "")
    if _es:
        should = [{"match": {"red_flags": {"query": word or txn["category"], "boost": 3}}},
                  {"multi_match": {"query": f"{txn['merchant']} {txn['category'].replace('_', ' ')} {txn['description']}",
                                   "fields": ["title^2", "content", "red_flags"]}}]
        if pay:
            should.append({"term": {"payment_methods": {"value": pay, "boost": 5}}})
        hits = _es.search(index=PATTERNS, size=3, query={"bool": {"should": should, "minimum_should_match": 1}})["hits"]["hits"]
        return [_fmt(h["_source"], word) for h in hits]
    matches = [a for a in ALERTS if pay and pay in (a.get("payment_methods") or [])]
    return [_fmt(a, word) for a in sorted(matches, key=lambda a: _rank(a, word), reverse=True)[:3]]


async def alerts_for(txn: dict) -> list[str]:
    try:
        return await asyncio.to_thread(_alerts_sync, txn)
    except Exception as e:
        logger.error(f"intel: alert lookup failed: {e}")
        return []


def _history_sync(member_id: str, txn: dict, history: list[dict]) -> list[dict]:
    _ensure_indices()
    helpers.bulk(_es, ({"_index": TXNS, "_id": t["txn_id"], "member_id": member_id,
                        **{k: t[k] for k in ("date", "amount", "category", "merchant", "description")}} for t in history),
                 refresh="wait_for")
    q = {"bool": {"filter": [{"term": {"member_id": member_id}}, {"range": {"date": {"gte": "now-90d/d"}}}]}}
    r = _es.search(index=TXNS, size=0, query=q, aggs={
        "same": {"filter": {"term": {"category": txn["category"]}}, "aggs": {"max": {"max": {"field": "amount"}}}},
        "max_any": {"max": {"field": "amount"}},
    })
    n = r["aggregations"]["same"]["doc_count"]
    biggest = r["aggregations"]["max_any"]["value"] or 0
    out = [{"source": "elastic", "weight": 0.1 if n else 0.6,
            "fact": f"{n} {txn['category']} purchases in her last 90 days"
                    + (f" (largest ${r['aggregations']['same']['max']['value']:,.0f})" if n else ": first time in this category")}]
    if txn["amount"] > biggest:
        out.append({"source": "elastic", "weight": 0.4,
                    "fact": f"${txn['amount']:,.0f} is larger than any purchase in her last 90 days (previous max ${biggest:,.0f})"})
    return out


async def history_signals(member_id: str, txn: dict, history: list[dict]) -> list[dict]:
    """Index her history (idempotent by txn id), then ask Elastic the analyst's questions."""
    if not _es:
        return []
    try:
        return await asyncio.to_thread(_history_sync, member_id, txn, history)
    except Exception as e:
        logger.error(f"intel: elastic history failed: {e}")
        return []


if __name__ == "__main__":
    ALERTS[:] = [
        {"date": "2026-10-01", "title": "Gift card scams", "payment_methods": ["gift_card"],
         "red_flags": ["Only scammers will tell you to pay with a gift card."]},
        {"date": "2026-09-01", "title": "Jury duty calls", "payment_methods": [], "red_flags": []},
    ]
    got = _alerts_sync({"merchant": "Target", "category": "gift_card", "description": "gift cards"})
    assert got == ['FTC alert 2026-10-01: "Gift card scams" (FTC: Only scammers will tell you to pay with a gift card.)'], got
    assert _alerts_sync({"merchant": "Safeway", "category": "groceries", "description": ""}) == []
    print("intel ok")
