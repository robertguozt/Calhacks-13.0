"""Capital One Nessie client + polling watcher (no webhooks; diff ids every 2s).

Purchases list/create isn't in the published spec (research/nessie.md §0). If
`/accounts/{id}/purchases` 404s on the first test, set NESSIE_TXN_ROUTE=withdrawals and
the merchant rides in `description` instead. Hold state lives in our DB; the Nessie
status update is decoration only.
"""

import asyncio
import datetime
import os

import httpx
from loguru import logger

BASE = "https://api.nessieisreal.com"
ROUTE = os.getenv("NESSIE_TXN_ROUTE", "purchases")
_one = {"purchases": "purchase", "withdrawals": "withdrawal"}[ROUTE]
_http = httpx.AsyncClient(base_url=BASE, timeout=10)


async def _req(method: str, path: str, **kw):
    for attempt in range(3):
        try:
            r = await _http.request(method, path, params={"key": os.environ["NESSIE_KEY"]}, **kw)
            if r.status_code < 500:
                r.raise_for_status()
                return r.json() if r.content else None
        except (httpx.TimeoutException, httpx.TransportError) as e:
            logger.warning(f"nessie {method} {path}: {e}")
        await asyncio.sleep(0.5 * 2**attempt)
    raise RuntimeError(f"nessie {method} {path} failed after retries")


async def txns(account_id: str) -> list[dict]:
    return await _req("GET", f"/accounts/{account_id}/{ROUTE}") or []


async def merchants() -> dict[str, dict]:
    return {m["_id"]: m for m in await _req("GET", "/merchants") or []}


async def merchant_id(name: str, category: str) -> str:
    """Find a merchant by name or create it. Create responses may not return `_id`, so re-list."""
    find = lambda ms: next((i for i, m in ms.items() if m.get("name") == name), None)
    if mid := find(await merchants()):
        return mid
    await _req("POST", "/merchants", json={"name": name, "category": category})
    if mid := find(await merchants()):
        return mid
    raise RuntimeError(f"merchant {name!r} not found after create")


async def create_txn(account_id: str, merchant_id: str, merchant: str, amount: int, description: str,
                     status: str = "pending", date: str | None = None):
    date = date or datetime.date.today().isoformat()
    body = {"medium": "balance", "transaction_date": date, "status": status, "amount": int(amount)}
    if ROUTE == "purchases":
        body |= {"merchant_id": merchant_id, "purchase_date": date, "description": description}
    else:
        body |= {"description": f"{description} at {merchant}"}
    return await _req("POST", f"/accounts/{account_id}/{ROUTE}", json=body)


async def set_status(txn_id: str, status: str):
    try:
        await _req("PUT", f"/{_one}/{txn_id}", json={"status": status})
    except Exception as e:  # decoration only; the hold lives in our DB
        logger.warning(f"nessie status {status} not accepted: {e}")


def normalize(t: dict, ms: dict[str, dict]) -> dict:
    m = ms.get(t.get("merchant_id") or "", {})
    cat = m.get("category")
    return {
        "txn_id": t["_id"],
        "merchant": m.get("name") or t.get("description", "unknown"),
        "category": (cat[0] if isinstance(cat, list) and cat else cat) or "unknown",
        "amount": t.get("amount", 0),
        "date": t.get("transaction_date") or t.get("purchase_date"),
        "description": t.get("description", ""),
        "status": t.get("status"),
    }


async def watch(accounts: dict[str, str], on_new, every: float = 2.0):
    """accounts: {account_id: member_id}. Calls `on_new(member_id, txn, history)` once per new id."""
    seen: set[str] = set()
    for acct in accounts:  # existing history must never re-trigger
        seen |= {t["_id"] for t in await txns(acct)}
    logger.info(f"watcher: {len(seen)} existing txns across {len(accounts)} accounts")
    if not seen:
        logger.warning("watcher saw 0 txns: a bad Nessie key also returns 200 [] — run seed.py and check NESSIE_KEY")
    while True:
        for acct, member_id in accounts.items():
            try:
                rows = await txns(acct)
                new = [t for t in rows if t["_id"] not in seen]
                if new:
                    ms = await merchants()
                    for t in new:
                        seen.add(t["_id"])
                        history = [normalize(h, ms) for h in rows if h["_id"] != t["_id"]]
                        asyncio.create_task(on_new(member_id, normalize(t, ms), history))
            except Exception as e:
                logger.error(f"watcher {acct}: {e}")
        await asyncio.sleep(every)
