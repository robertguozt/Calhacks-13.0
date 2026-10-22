"""The overnight analyst: new purchase -> triage -> gate -> hold -> call -> case file.

`do()` is the only way anything happens to a case. It asks the gate first, logs a
`deny` by `gate` on refusal, and otherwise logs the action and applies its effect.
"""

import asyncio
import datetime
import os
import time

from loguru import logger
from twilio.rest import Client as Twilio

import crs
import gate
import intel
import nessie
import store
import verify
from gate import Case
from members import MEMBERS, member_phone, phone
from triage import triage

CASES: dict[str, Case] = {}  # ponytail: in-process; one server owns every case while it runs
STATUS = {"release": "released", "escalate": "escalated", "confirm_fraud": "confirmed_fraud"}
REDIAL_SECONDS = 20


async def on_new_purchase(member_id: str, txn: dict, history: list[dict]):
    m = MEMBERS[member_id]
    data, alerts, history_signals = await asyncio.gather(
        crs.pull(member_id, m["crs"]), intel.alerts_for(txn), intel.history_signals(member_id, txn, history)
    )
    local_time = datetime.datetime.now().strftime("%a %-I:%M %p")
    t = await triage(m, history, txn, crs.signals(data) + history_signals, alerts=alerts, local_time=local_time)
    floor = gate.must_hold(txn, history) if t.decision == "allow" else None
    held = t.decision == "hold" or bool(floor)
    signals = [s.model_dump() for s in t.risk_signals]
    cid = await store.open_case(
        member_id=member_id, member_name=m["name"], txn_id=txn["txn_id"], merchant=txn["merchant"],
        category=txn["category"], amount=float(txn["amount"]), status="open" if held else "allowed",
        risk_signals=signals, plan=t.plan, decision_rationale=t.rationale,
    )
    case = Case(cid, member_id, txn["txn_id"], txn["merchant"], txn["category"], float(txn["amount"]),
                status="open" if held else "allowed", signals=signals, plan=t.plan, rationale=t.rationale)
    CASES[cid] = case
    await store.action(cid, "triage", "agent", f"{t.decision.upper()}: {t.rationale}")
    if floor:
        await store.action(cid, "override", "gate", f"Triage said allow, held anyway: {floor}")
    if not held:
        return
    if await do(case, "hold", "agent", f"Paused ${case.amount:,.0f} at {case.merchant}. {t.plan}"):
        return
    await nessie.set_status(case.txn_id, "held")
    needed = crs.answers_needed(data)
    qs = verify.build(cid, data.get("report"), needed)
    why = {0: "CRS identity check failed: no phone verification, an analyst must handle this",
           3: "CRS shows a fraud alert: stepping up to 3 of 3"}.get(needed, "2 of 3 needed")
    await store.action(cid, "verify", "agent", f"{len(qs)} verification questions from her CRS credit file, answers kept server-side ({why})")
    await do(case, "call_member", "agent", f"Calling {m['first_name']} about the paused purchase")


async def do(case: Case, action: str, by: str, reason: str) -> str | None:
    """Gate-checked action. Returns None on success, the refusal reason otherwise."""
    if why := gate.check(action, case, by):
        await store.action(case.id, "deny", "gate", f"{by} tried to {action}: {why}")
        return why
    await store.action(case.id, action, by, reason)
    if action in STATUS:
        case.status = STATUS[action]
        await store.status(case.id, case.status)
        if action == "release":
            await nessie.set_status(case.txn_id, "pending")
    elif action == "call_member":
        await dial(case, member_phone(case.member_id), "member")
    elif action in ("call_family", "connect_family"):
        fam = MEMBERS[case.member_id]["family"]
        if action == "call_family":
            case.family_notified = True
        else:
            case.bridged = True
        await dial(case, phone(fam["phone_env"]) if fam else None, "family" if action == "call_family" else "bridge")
    return None


async def dial(case: Case, to: str | None, mode: str):
    base, frm = os.getenv("LOCAL_SERVER_URL"), os.getenv("TWILIO_NUMBER")
    if not (to and base and frm):
        await store.action(case.id, "error", "agent", f"cannot place {mode} call: phone/LOCAL_SERVER_URL/TWILIO_NUMBER not set")
        return
    q = f"case_id={case.id}&mode={mode}"
    try:
        call = await asyncio.to_thread(
            Twilio(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"]).calls.create,
            to=to, from_=frm, url=f"{base}/twiml?{q}", method="POST",
            status_callback=f"{base}/call-status?{q}", status_callback_event=["completed"],
        )
        logger.info(f"dialed {mode} for {case.id}: {call.sid}")
    except Exception as e:
        await store.action(case.id, "error", "agent", f"{mode} call failed to start: {e}")


async def on_call_status(case_id: str, mode: str, call_status: str):
    """Twilio's final status for a call we placed. Unanswered member calls retry, then go to family."""
    case = CASES.get(case_id)
    if not case or call_status not in ("busy", "no-answer", "failed"):
        return
    if mode == "bridge":  # family didn't pick up: bring her out of the waiting conference, back to the agent
        await store.action(case.id, "family_bridge", "agent", f"Family didn't answer ({call_status}); returning the member to the agent")
        if case.member_call_sid:
            base = os.environ["LOCAL_SERVER_URL"]
            await asyncio.to_thread(
                Twilio(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"]).calls(case.member_call_sid).update,
                url=f"{base}/twiml?case_id={case.id}&mode=member&note=family_unavailable", method="POST",
            )
        return
    if mode != "member":
        return
    case.unanswered_calls += 1
    await store.action(case.id, "call", "agent", f"Call {case.unanswered_calls} not answered ({call_status})")
    if case.unanswered_calls < gate.MAX_UNANSWERED:
        await asyncio.sleep(REDIAL_SECONDS)
        await do(case, "call_member", "agent", "Trying her again")
    else:
        await do(case, "call_family", "agent", f"No answer after {case.unanswered_calls} calls")
        await do(case, "escalate", "agent", "Member unreachable; hold stays until an analyst reviews it")


def hold_expires(opened: float) -> float:
    """One business day after the hold: the agent's hold authority ends then (DESIGN.md §2)."""
    d = datetime.datetime.fromtimestamp(opened) + datetime.timedelta(days=1)
    while d.weekday() >= 5:
        d += datetime.timedelta(days=1)
    return d.timestamp()


async def expire_holds(every: float = 60):
    while True:
        for case in list(CASES.values()):
            if case.status == "open" and time.time() >= hold_expires(case.opened_at):
                await do(case, "escalate", "agent", "Hold reached 1 business day; the agent's authority ends, an analyst decides")
        await asyncio.sleep(every)


def open_case_for(member_id: str | None) -> Case | None:
    """Newest held case for a member (inbound callback lookup)."""
    held = [c for c in CASES.values() if c.member_id == member_id and c.status in ("open", "escalated")]
    return held[-1] if held else None
