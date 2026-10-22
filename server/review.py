"""Morning review: a Factory Droid reads last night's case files and proposes an edit to
policy.md (the guidance triage reads). Nothing takes effect until an analyst approves the diff.

What Droid can touch, enforced here rather than in its prompt:
- It runs in a scratch directory holding only a copy of policy.md. Cases and policy are
  attached as documents, so it needs no Read tool. Droid reads aren't permission-gated,
  so a Read tool could reach server/.env.
- Tools are restricted to Edit, autonomy is OFF (every action asks), and `_guard` approves
  only edits to that one scratch file and rejects everything else.
- The approved file is the only thing copied back, and only if policy.md hasn't changed
  since the review started. Hard hold rules live in gate.py; policy can't change them.

Triggers: 7am daily (server.py) or the dashboard's "Run review now".
"""

import asyncio
import difflib
import json
import pathlib
import shutil
import tempfile

from droid_sdk import (
    ApplyPatchAction, AssistantMessage, Autonomy, CreateFile, Document, EditAction,
    InteractionHandlers, PermissionRequest, Session, SessionConfig, ToolConfirmationOutcome,
)
from loguru import logger

import store

POLICY = pathlib.Path(__file__).with_name("policy.md")
PROMPT = """You are the morning reviewer for a small credit union's overnight fraud desk.
Attached: cases.json (last night's cases: purchase, triage decision, signals, rationale, final outcome) and policy.md
(the human-approved guidance triage reads). policy.md is also in your working directory; edit that file only.

1. Find false positives (holds the member or an analyst released) and misses (purchases later confirmed as fraud).
2. If the evidence supports it, make small edits to policy.md: refine member notes or guidance, citing the case outcome
   in a short parenthetical. Never weaken guidance on gift cards, wire transfers or crypto. Don't create other files.
   If nothing is warranted, change nothing.
3. End with 2-3 plain sentences: what you changed and why, or why you changed nothing."""

_pending: dict = {}  # the proposal waiting for an analyst: {"review_id", "base", "new"}
_lock = asyncio.Lock()


def _guard(work: pathlib.Path, review_id: str):
    target = (work / "policy.md").resolve()

    def paths(a) -> list[pathlib.Path]:
        if isinstance(a, ApplyPatchAction):
            return [work / a.file_path] + [work / f.file_path for f in a.files or []] + \
                   [work / f.move_to for f in a.files or [] if f.move_to]
        return [work / a.file_path]

    async def on_permission(req: PermissionRequest):
        ok = bool(req.actions) and all(
            isinstance(a, (EditAction, CreateFile, ApplyPatchAction)) and all(p.resolve() == target for p in paths(a))
            for a in req.actions
        )
        what = ", ".join(type(a).__name__ for a in req.actions)
        offered = {o.value for o in req.options}
        choice = ToolConfirmationOutcome.PROCEED_ONCE if ok and ToolConfirmationOutcome.PROCEED_ONCE in offered \
            else ToolConfirmationOutcome.CANCEL
        await store.review_step(review_id, f"{'allowed' if choice == ToolConfirmationOutcome.PROCEED_ONCE else 'DENIED'}: {what}")
        return req.respond(choice)

    return on_permission


async def run_review(cases: list[dict], trigger: str) -> str:
    if _lock.locked():
        raise RuntimeError("a review is already running")
    async with _lock:
        rid = await store.review_open(trigger)
        base = POLICY.read_text()
        work = pathlib.Path(tempfile.mkdtemp(prefix="ss-review-"))
        (work / "policy.md").write_text(base)
        summary = ""
        try:
            session = Session(
                cwd=work,
                config=SessionConfig(restrict_tools=["Edit"], autonomy=Autonomy.OFF),
                interactions=InteractionHandlers(on_permission=_guard(work, rid)),
            )
            await store.review_step(rid, f"Droid started: {len(cases)} cases, policy.md ({len(base.splitlines())} lines)")
            async with session:
                try:  # make Droid's actual permissions part of the visible record
                    tools = await session.list_tools()
                    await store.review_step(rid, "tools: " + ", ".join(f"{t.id}{'' if t.allowed else ' (off)'}" for t in tools))
                except Exception as e:
                    await store.review_step(rid, f"tools: could not list ({e})")
                async with session.stream(PROMPT, files=[
                    Document.from_text(json.dumps(cases, indent=1), name="cases.json"),
                    Document.from_text(base, name="policy.md"),
                ]) as stream:
                    async for ev in stream:
                        if tool := getattr(ev, "tool_name", None):
                            await store.review_step(rid, f"tool: {tool}")
                        elif isinstance(ev, AssistantMessage) and ev.text:
                            summary = ev.text
                            await store.review_step(rid, ev.text[:300])
            new = (work / "policy.md").read_text()
            diff = "".join(difflib.unified_diff(base.splitlines(True), new.splitlines(True), "policy.md", "policy.md (proposed)"))
            _pending.clear()
            if diff:
                _pending.update(review_id=rid, base=base, new=new)
            await store.review_set(rid, "proposed", summary=summary.strip(), diff=diff)
        except Exception as e:
            logger.exception("review failed")
            await store.review_set(rid, "failed", summary=f"Review failed: {e}")
        finally:
            shutil.rmtree(work, ignore_errors=True)
        return rid


async def decide(approve: bool) -> str | None:
    """Analyst approves or rejects the waiting proposal. Returns a refusal reason, or None."""
    if not _pending:
        return "no proposal is waiting"
    if approve:
        if POLICY.read_text() != _pending["base"]:
            return "policy.md changed since this review ran; run the review again"
        POLICY.write_text(_pending["new"])
    await store.review_set(_pending["review_id"], "approved" if approve else "rejected")
    _pending.clear()
    return None


def case_files(cases) -> list[dict]:
    """What Droid sees: outcome-labelled cases, no phone numbers or credit data."""
    return [{"merchant": c.merchant, "category": c.category, "amount": c.amount, "member": c.member_id,
             "triage_signals": c.signals, "triage_rationale": c.rationale, "outcome": c.status,
             "verified_on_call": c.verified, "red_flags_on_call": c.red_flags} for c in cases]


if __name__ == "__main__":
    import asyncio as _a
    from types import SimpleNamespace as NS

    w = pathlib.Path(tempfile.mkdtemp())
    (w / "policy.md").write_text("x")
    guard = _guard(w, "r1")
    opts = [NS(value=ToolConfirmationOutcome.PROCEED_ONCE), NS(value=ToolConfirmationOutcome.CANCEL)]

    def req(*actions):
        return PermissionRequest(actions=actions, options=opts)

    async def store_noop(*a, **k):
        pass

    store.review_step = store_noop
    tu = None
    edit = lambda p: EditAction(tool_use=tu, file_path=p, file_name=pathlib.Path(p).name)
    assert _a.run(guard(req(edit("policy.md")))).selected_option == ToolConfirmationOutcome.PROCEED_ONCE
    assert _a.run(guard(req(edit(str(w / "policy.md"))))).selected_option == ToolConfirmationOutcome.PROCEED_ONCE
    for bad in ("../policy.md", "/Users/x/second-signature/server/.env", "policy.md/../../gate.py", "other.md"):
        assert _a.run(guard(req(edit(bad)))).selected_option == ToolConfirmationOutcome.CANCEL, bad
    assert _a.run(guard(req(edit("policy.md"), edit("gate.py")))).selected_option == ToolConfirmationOutcome.CANCEL
    assert _a.run(guard(req())).selected_option == ToolConfirmationOutcome.CANCEL
    print("review guard ok")
