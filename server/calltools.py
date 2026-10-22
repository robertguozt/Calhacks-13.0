"""The call bot's brain without Pipecat: system prompt, tool specs and tool effects.

bot.py (live phone calls) and arena.py (simulated calls) both use this, so the arena
tests exactly the prompt, tools, gate and verification that run on a real call.
`run_tool` returns the result for the model; keys starting with "_" are instructions
for the caller (end the call / hand off to the family conference), never shown to it.
"""

import store
import verify
from members import MEMBERS

CU = "Bayview Community Credit Union"
CALL_MODEL = "claude-haiku-5-5"  # lowest latency for live speech

PROMPT = """You are the after-hours fraud assistant for {cu}, {how} {name}. This is a phone call: everything you say is spoken aloud.
Speak slowly, warmly, in short sentences. One question at a time. No lists, markdown or emoji. Never rush.
Never say "you were scammed" or "you fell for". Say "this looks like a pattern scammers use" and let her decide.

{first}

THE PAUSED PURCHASE: ${amount:,.0f} at {merchant} ({category}). Why it was paused: {rationale}

HARD RULES:
- Never ask for a PIN, password, one-time code, full card number or Social Security number. Say once, early: "I will never ask for a PIN, password or code."
- Before discussing the purchase, verify identity with these questions, one at a time, reading the four options aloud:
{questions}
  Call check_answer with the option she picked, exactly as written. You are never told the right answer. Stop when it says verified or failed.
  If failed, or there are no questions, do not discuss the purchase: say a staff member will call in the morning, call escalate_to_analyst, say goodbye, then end_call.
- Once verified, ask whether she made this purchase herself, and whether anyone asked her to buy it.
- If someone asked her to buy gift cards, read numbers, keep it secret, or said they were Microsoft, the IRS, the police, or a grandchild in trouble:
  keep the hold. Explain gently that real companies never ask for gift cards. Call escalate_to_analyst.
  Scammers work by cutting people off from family. So offer, as her choice: "Would you like me to connect you with {family} right now, on this call?"
  If she says yes: say "I'm connecting you now. Please stay on the line; you'll hear music for a moment." then call connect_family.
  If she says no, respect it, don't ask again, and call notify_trusted_contact instead (a short message, no details).
- release_hold only if she is verified, made the purchase knowingly, and nobody told her to buy it. It may be refused; if so, say an analyst will review it in the morning.
- You cannot move, refund or send money. If unsure, keep the hold and escalate. Holding is cheap; releasing a scam is not.
- Every tool call needs a one-sentence reason; it goes into the case file.
- When the conversation is done, say goodbye, then call end_call."""

FIRST_OUT = ('Your first words, exactly: "Hi, this is the after-hours assistant at {cu}, calling for {first_name}. '
             "You don't have to trust this call. You can hang up and call the number on the back of your card, and I'll pick up there.\"")
FIRST_IN = ('Your first words, exactly: "{cu} after-hours line. Is this {first_name}? I see we paused a purchase on your account tonight. '
            'Thank you for calling back. Calling the number on your card was exactly the right thing to do."')
RESUME = ("You are back on the line after trying to connect her with {family}, who did not answer. Your first words: tell her gently that "
          "{family} didn't pick up, the payment stays paused, and a staff member will call her in the morning. "
          "She is already verified; do not ask the questions again. Ask if there's anything else, then say goodbye and end_call.")

_reason = {"reason": {"type": "string", "description": "One sentence for the case file."}}
TOOL_SPECS = [
    {"name": "check_answer", "description": "Check the member's answer to a verification question.",
     "properties": {"question_number": {"type": "integer", "description": "The question's number."},
                    "answer": {"type": "string", "description": "The option she picked, exactly as written in the options."}},
     "required": ["question_number", "answer"]},
    {"name": "release_hold", "description": "Release the hold so the purchase goes through. Only after verification, when she made it knowingly and nobody told her to.",
     "properties": _reason, "required": ["reason"]},
    {"name": "notify_trusted_contact", "description": "Call her trusted family contact to say a payment is paused (no amounts or details).",
     "properties": _reason, "required": ["reason"]},
    {"name": "connect_family", "description": "Connect her to her trusted family contact on a live call, then leave the line. Only if she said yes.",
     "properties": _reason, "required": ["reason"]},
    {"name": "escalate_to_analyst", "description": "Keep the hold and send the case to a human analyst for the morning.",
     "properties": _reason, "required": ["reason"]},
    {"name": "end_call", "description": "Hang up. Say goodbye first.", "properties": _reason, "required": ["reason"]},
]


def family_label(case) -> str | None:
    fam = MEMBERS[case.member_id]["family"]
    return f"your {fam['relation']} {fam['name']}" if fam else None


def tool_specs(case) -> list[dict]:
    return [t for t in TOOL_SPECS if t["name"] != "connect_family" or family_label(case)]


def build_prompt(case, inbound: bool, resume: bool = False) -> str:
    m = MEMBERS[case.member_id]
    fam = family_label(case) or "your family"
    first = RESUME.format(family=fam) if resume else (FIRST_IN if inbound else FIRST_OUT).format(cu=CU, first_name=m["first_name"])
    qs = verify.questions(case.id)
    return PROMPT.format(
        cu=CU, how="answering a call from" if inbound else "calling", name=m["name"], first=first,
        amount=case.amount, merchant=case.merchant, category=case.category, rationale=case.rationale, family=fam,
        questions="\n".join(f"  {q['number']}. {q['question']} Options: {', '.join(q['options'])}." for q in qs) or "  (none available)",
    )


async def run_tool(case, name: str, args: dict) -> dict:
    import agent  # late: agent imports Twilio etc.

    reason = str(args.get("reason", ""))
    if name == "check_answer":
        r = verify.check_answer(case.id, int(args.get("question_number", 0)), str(args.get("answer", "")))
        if r.get("verified") and not case.verified:
            case.verified = True  # set by code from the checked answers, never by the model
            await store.action(case.id, "verify", "member", "Identity verified from her credit-file answers")
        elif r.get("failed"):
            await store.action(case.id, "verify", "member", "Identity verification failed")
        return r
    if name == "release_hold":
        why = await agent.do(case, "release", "member", reason)
        return {"released": why is None, "refused": why}
    if name == "notify_trusted_contact":
        case.red_flags = True  # only ever tightens what the gate allows
        why = await agent.do(case, "call_family", "agent", reason)
        return {"calling": why is None, "refused": why}
    if name == "connect_family":
        case.red_flags = True
        why = await agent.do(case, "connect_family", "member", reason)
        return {"connecting": why is None, "refused": why, "_handoff": why is None}
    if name == "escalate_to_analyst":
        why = await agent.do(case, "escalate", "agent", reason)
        return {"escalated": why is None, "refused": why}
    if name == "end_call":
        await store.action(case.id, "call", "agent", f"Call ended: {reason}")
        return {"ok": True, "_end": True}
    return {"error": f"unknown tool {name}"}
