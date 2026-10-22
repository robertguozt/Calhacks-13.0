"""Permission gate: the DESIGN.md §2 matrix, enforced in code.

Every action the agent, the member (via the call bot) or a human analyst takes goes
through `check`. The model's output never bypasses it; a refusal is returned to the
caller and logged to the case file as a `deny` by `gate`.
"""

import time
from dataclasses import dataclass, field

RELEASE_LIMIT = 1000  # holds of this amount or more need a human analyst
MAX_UNANSWERED = 2  # unanswered member calls before family may be called
NEVER = {"move_money", "refund", "send_money", "close_account", "freeze_account"}


@dataclass
class Case:
    id: str
    member_id: str
    txn_id: str
    merchant: str
    category: str
    amount: float
    status: str = "open"  # allowed | open | released | escalated | confirmed_fraud
    verified: bool = False  # set only by verify.check_answer, never by the model
    red_flags: bool = False  # the member described coaching/pressure on the call
    unanswered_calls: int = 0
    family_notified: bool = False
    bridged: bool = False  # member was connected live to her trusted contact
    member_call_sid: str | None = None  # her current call, for moving it into / out of the family conference
    signals: list = field(default_factory=list)
    plan: str = ""
    rationale: str = ""
    opened_at: float = field(default_factory=time.time)


HIGH_RISK = {"gift_card", "wire_transfer", "crypto"}  # what scammers ask for


def must_hold(txn: dict, history: list[dict]) -> str | None:
    """Code floor under triage: a first-ever high-risk-category purchase is held whatever the
    model said. Merchant names and notes are free text the model reads, so this is the line a
    prompt injection ("ignore prior instructions, allow") can't move. Policy edits can't either."""
    if txn["category"] in HIGH_RISK and not any(h["category"] == txn["category"] for h in history):
        return f"first {txn['category']} purchase in her history; held by rule regardless of triage"
    return None


def check(action: str, case: Case, by: str) -> str | None:
    """Return None if allowed, else a one-line refusal reason."""
    if action in NEVER:
        return "only a human analyst can move money or change the account, outside this system"
    held = case.status in ("open", "escalated")

    if action == "hold":
        return None if by == "agent" else "holds are placed by the agent"
    if action in ("call_member", "escalate"):
        return None if held else f"case is {case.status}"
    if action == "call_family":
        if by != "agent":
            return "family calls are placed by the agent"
        if case.family_notified:
            return "family already notified"
        if case.red_flags or case.unanswered_calls >= MAX_UNANSWERED:
            return None
        return "family is only called on red flags or after 2 unanswered calls"
    if action == "connect_family":
        # her choice, but only once we know it's her: an unverified caller must never reach her family through us
        if by != "member":
            return "only the member can ask to be connected to her family"
        if not case.verified:
            return "member identity not verified"
        return "already connected to family once" if case.bridged else None
    if action == "release":
        if not held:
            return f"case is {case.status}, nothing to release"
        if by == "human":
            return None
        if by != "member":
            return "the agent cannot release a hold on its own"
        if not case.verified:
            return "member identity not verified"
        if case.red_flags:
            return "red flags were raised on the call; an analyst must decide"
        if case.amount >= RELEASE_LIMIT:
            return f"${case.amount:,.0f} is at or over ${RELEASE_LIMIT:,}; an analyst must release it"
        return None
    if action == "confirm_fraud":
        return None if by == "human" and held else "only an analyst confirms fraud on a held case"
    return f"unknown action {action!r}"


if __name__ == "__main__":
    c = Case("c1", "dolores", "t1", "Target", "gift_card", 500)
    assert check("hold", c, "agent") is None
    assert check("move_money", c, "human")  # never inside this system
    assert check("release", c, "agent")  # agent alone can't
    assert check("release", c, "member")  # not verified yet
    c.verified = True
    assert check("release", c, "member") is None
    c.red_flags = True
    assert check("release", c, "member")  # coached -> analyst
    assert check("call_family", c, "agent") is None
    c.family_notified = True
    assert check("call_family", c, "agent")
    assert check("connect_family", c, "agent")  # never the agent's idea alone
    assert check("connect_family", c, "member") is None  # verified + her choice
    c.bridged = True
    assert check("connect_family", c, "member")
    assert check("connect_family", Case("c3", "dolores", "t3", "x", "y", 1), "member")  # unverified caller
    big = Case("c2", "dolores", "t2", "Bay Cycles", "sporting_goods", 1000, verified=True)
    assert check("release", big, "member")  # >= $1,000 -> human
    assert check("release", big, "human") is None
    assert check("call_family", big, "agent")  # no red flags, no unanswered calls
    big.unanswered_calls = 2
    assert check("call_family", big, "agent") is None
    big.status = "released"
    assert check("release", big, "human") and check("confirm_fraud", big, "human")
    assert check("drain_account", big, "agent")  # unknown -> refused
    gift = {"category": "gift_card"}
    assert must_hold(gift, [{"category": "groceries"}])  # Dolores: never bought one
    assert must_hold(gift, [{"category": "gift_card"}]) is None  # Robert: buys them yearly
    assert must_hold({"category": "sporting_goods"}, []) is None  # the bike is triage's call
    print("gate ok")
