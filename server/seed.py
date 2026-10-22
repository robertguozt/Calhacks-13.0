"""Seed Nessie with Dolores and Robert plus ~90 days of history (PLAYBOOK.md §1).

Run once: `uv run seed.py`. Writes seed_ids.json. Re-running makes new customers
(Nessie has no reset), which is also how you get a clean demo.
"""

import asyncio
import datetime
import json

from dotenv import load_dotenv

load_dotenv(override=True)

import nessie  # noqa: E402
from members import SEED_FILE  # noqa: E402

ADDR = {"street_number": "612", "street_name": "Ocean View Dr", "city": "Santa Barbara", "state": "CA", "zip": "93109"}

# (merchant, category, [(days_ago, amount), ...])
DOLORES = [
    ("Safeway", "groceries", [(d, 60 + (d * 7) % 50) for d in range(88, 0, -7)]),
    ("CVS Pharmacy", "pharmacy", [(d, 20 + (d * 3) % 25) for d in range(85, 0, -14)]),
    ("PG&E", "utilities", [(d, 92 + d % 7) for d in (80, 50, 20)]),
    ("Comcast", "utilities", [(d, 79) for d in (75, 45, 15)]),
    ("St. Mark's Church", "donation", [(d, 50) for d in (70, 40, 10)]),
    ("Arnoldi's Cafe", "restaurant", [(d, 25 + d % 15) for d in (62, 33, 9)]),
]
ROBERT = [
    ("Safeway", "groceries", [(d, 70 + (d * 5) % 40) for d in range(86, 0, -7)]),
    ("Target Gift Cards", "gift_card", [(d, a) for d, a in ((80, 50), (60, 100), (31, 75), (12, 50))]),
    ("Walgreens", "pharmacy", [(d, 30) for d in (70, 40, 10)]),
]


async def customer(first: str, last: str) -> dict:
    await nessie._req("POST", "/customers", json={"first_name": first, "last_name": last, "address": ADDR})
    cust = [c for c in await nessie._req("GET", "/customers") if c["first_name"] == first and c["last_name"] == last][-1]["_id"]
    await nessie._req("POST", f"/customers/{cust}/accounts",
                      json={"type": "Checking", "nickname": f"{first} Checking", "rewards": 0, "balance": 12000})
    acct = (await nessie._req("GET", f"/customers/{cust}/accounts"))[-1]["_id"]
    return {"customer": cust, "account": acct}


async def history(acct: str, rows, merchants: dict):
    today = datetime.date.today()
    for name, cat, txns in rows:
        merchants[name] = mid = merchants.get(name) or await nessie.merchant_id(name, cat)
        for days, amount in txns:
            date = (today - datetime.timedelta(days=days)).isoformat()
            await nessie.create_txn(acct, mid, name, amount, f"{name} {cat}", status="completed", date=date)


async def main():
    out = {"members": {}, "merchants": {}}
    for mid, (first, last), rows in (("dolores", ("Dolores", "Whitcomb"), DOLORES), ("robert", ("Robert", "Chen"), ROBERT)):
        out["members"][mid] = ids = await customer(first, last)
        await history(ids["account"], rows, out["merchants"])
        n = len(await nessie.txns(ids["account"]))
        print(f"{mid}: account {ids['account']}, {n} txns")
        assert n > 0, "0 txns read back: wrong key, or set NESSIE_TXN_ROUTE=withdrawals (research/nessie.md §7)"
    SEED_FILE.write_text(json.dumps(out, indent=2))
    print(f"wrote {SEED_FILE}")


if __name__ == "__main__":
    asyncio.run(main())
