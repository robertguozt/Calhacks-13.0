"""Triage: Claude decides hold/allow with cited evidence (PLAYBOOK.md §5).

Any failure (API error, refusal, bad output) falls back to HOLD: a hold costs one
phone call, a released scam costs her savings. The gate re-checks everything after.
"""

import json
import pathlib
from collections import defaultdict
from typing import Literal

import anthropic
from loguru import logger
from pydantic import BaseModel

MODEL = "claude-opus-5-5"
POLICY = pathlib.Path(__file__).with_name("policy.md")  # edited by the morning review, approved by a human

SYSTEM = """You are a credit union fraud analyst. Decide whether to HOLD a pending purchase for a member aged 60+.
Input: member profile, 90-day purchase history summary, the new purchase, signals (CRS identity/credit file and Elastic
history search; keep each fact's source), and current FTC scam alerts (cite one with source "apify" if it matches).
Rules: cite only facts present in the input; each risk signal's fact must be specific and checkable, weight 0-1.
A category the member has bought before at similar amounts is weak evidence. Gift cards, wire/crypto, and urgent
large purchases late at night that break pattern are strong. When in doubt, hold. A hold costs one phone call.
plan: one sentence. rationale: two sentences a human analyst would sign."""


class Signal(BaseModel):
    source: Literal["nessie", "crs", "apify", "elastic", "shield"]
    fact: str
    weight: float


class Triage(BaseModel):
    decision: Literal["hold", "allow"]
    risk_signals: list[Signal]
    plan: str
    rationale: str


_client = anthropic.AsyncAnthropic()


def summarize(history: list[dict]) -> list[dict]:
    """90-day history grouped by merchant: what she buys, how often, how much."""
    g = defaultdict(list)
    for t in history:
        g[(t["merchant"], t["category"])].append(t)
    return [
        {"merchant": m, "category": c, "count": len(ts),
         "min": min(t["amount"] for t in ts), "max": max(t["amount"] for t in ts),
         "last": max((t["date"] or "") for t in ts)}
        for (m, c), ts in sorted(g.items(), key=lambda kv: -len(kv[1]))
    ]


async def triage(member: dict, history: list[dict], txn: dict, signals: list[dict], alerts: list[str], local_time: str) -> Triage:
    payload = {
        "member": {k: member[k] for k in ("name", "age", "notes")},
        "history_90d": summarize(history),
        "new_purchase": {k: txn[k] for k in ("merchant", "category", "amount", "description")} | {"local_time": local_time},
        "signals": signals,
        "scam_alerts": alerts or ["none loaded"],
    }
    try:
        r = await _client.beta.messages.parse(
            model=MODEL,
            max_tokens=4000,
            system=f"{SYSTEM}\n\nCredit union policy (human-approved):\n{POLICY.read_text()}",
            messages=[{"role": "user", "content": json.dumps(payload)}],
            output_format=Triage,
            output_config={"effort": "medium"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",  # a safety-classifier decline re-runs on Anthropic's recommended fallback model
        )
        if r.stop_reason == "refusal" or r.parsed_output is None:
            raise RuntimeError(f"no triage output (stop_reason={r.stop_reason})")
        t = r.parsed_output
        for s in t.risk_signals:
            s.weight = max(0.0, min(1.0, s.weight))
        return t
    except Exception as e:
        logger.error(f"triage failed, holding by default: {e}")
        return Triage(
            decision="hold",
            risk_signals=[Signal(source="nessie", fact=f"${txn['amount']} at {txn['merchant']} ({txn['category']})", weight=0.5)],
            plan="Hold and call the member; automated triage was unavailable.",
            rationale="Triage could not run, so the purchase is held by default. A hold costs one phone call.",
        )
