"""Verification questions built from the member's CRS credit file.

The call bot only ever sees questions and options. Answers stay in this module and
are checked by `check_answer`, so a prompt-injected caller can't talk the model into
revealing them. Each question can be answered once. 2 correct verifies (3 when CRS shows a
fraud alert); a CRS identity FAIL means no questions at all, so the call escalates.
"""

import random
import re

NEEDED = 2
DECOY_STREETS = ["Maple Ave", "Harbor Blvd", "Cedar Ln", "Mission St", "Lincoln Way", "Elm St", "Pacific Ave"]
DECOY_LENDERS = ["Wells Fargo", "Discover", "Ally Financial", "Citibank", "US Bank", "Synchrony", "Toyota Financial"]

# case_id -> {"questions": [...], "answers": [...], "asked": set(), "right": int, "wrong": int}
_state: dict[str, dict] = {}  # ponytail: in-process; one server holds every call


def _street(line: str) -> str:
    return re.sub(r"^\d+\s+", "", line or "").strip().title()


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def build(case_id: str, report: dict | None, needed: int = NEEDED) -> list[dict]:
    """Make up to 3 multiple-choice questions from a CRS standard-format credit report."""
    report = report or {}
    rng = random.Random(case_id)
    qs: list[tuple[str, str, list[str]]] = []

    streets = {_street(a.get("addressLine1", "")) for f in report.get("creditFiles", []) for a in f.get("addresses", [])}
    streets.discard("")
    if streets:
        real = sorted(streets)[0]
        qs.append(("Which of these streets have you lived on?", real, [d for d in DECOY_STREETS if d not in streets]))

    lines = report.get("tradelines", [])
    lenders = {(t.get("creditorName") or "").strip().title() for t in lines} - {""}
    if lenders:
        real = sorted(lenders)[0]
        qs.append(("Which of these lenders have you had an account with?", real, [d for d in DECOY_LENDERS if d.title() not in lenders]))

    years = [int(t["accountOpenedDate"][:4]) for t in lines if (t.get("accountOpenedDate") or "")[:4].isdigit()]
    if years:
        y = min(years)
        qs.append(("About what year did you open your oldest credit account?", str(y), [str(y + d) for d in (-9, 6, 14)]))

    questions, answers = [], []
    for text, real, decoys in (qs if needed else []):
        options = rng.sample(decoys, 3) + [real]
        rng.shuffle(options)
        questions.append({"number": len(questions) + 1, "question": text, "options": options})
        answers.append(real)
    _state[case_id] = {"questions": questions, "answers": answers, "asked": set(), "right": 0, "wrong": 0, "needed": needed}
    return questions


def questions(case_id: str) -> list[dict]:
    return _state.get(case_id, {}).get("questions", [])


def check_answer(case_id: str, number: int, answer: str) -> dict:
    """Returns {"correct", "verified", "failed"}; never the right answer."""
    s = _state.get(case_id)
    if not s or not 1 <= number <= len(s["answers"]):
        return {"error": "no such question"}
    if number in s["asked"]:
        return {"error": "that question was already answered; ask a different one"}
    if _norm(answer) not in {_norm(o) for o in s["questions"][number - 1]["options"]}:
        return {"error": "pass one of the listed options exactly; ask her to pick one"}
    s["asked"].add(number)
    ok = _norm(answer) == _norm(s["answers"][number - 1])
    s["right" if ok else "wrong"] += 1
    remaining = len(s["answers"]) - len(s["asked"])
    return {
        "correct": ok,
        "verified": s["right"] >= s["needed"],
        "failed": s["right"] + remaining < s["needed"],
    }


if __name__ == "__main__":
    report = {
        "creditFiles": [{"addresses": [{"addressLine1": "612 OCEAN VIEW DR"}, {"addressLine1": "40 ELM ST"}]}],
        "tradelines": [
            {"creditorName": "BANK OF AMERICA", "accountOpenedDate": "1978-03-01"},
            {"creditorName": "SEARS", "accountOpenedDate": "1985-06-01"},
        ],
    }
    qs = build("case1", report)
    assert len(qs) == 3 and all(len(q["options"]) == 4 for q in qs)
    assert "Elm St" in qs[0]["options"] and "Ocean View Dr" not in qs[0]["options"]  # no 2nd real street as decoy
    assert "1978" in qs[2]["options"]
    assert check_answer("case1", 2, "bank").get("error")  # not an option: re-ask, attempt not used
    r = check_answer("case1", 2, "bank of america")
    assert r["correct"] and not r["verified"]
    assert check_answer("case1", 2, "bank of america").get("error")  # one try per question
    r = check_answer("case1", 3, "1978")
    assert r["verified"] and not r["failed"]

    q2 = build("case2", report)
    wrong = lambda n: next(o for o in q2[n - 1]["options"] if o not in ("Elm St", "Bank Of America", "1978"))
    assert not check_answer("case2", 1, wrong(1))["failed"]
    assert check_answer("case2", 2, wrong(2))["failed"]

    assert build("empty", None) == [] and check_answer("empty", 1, "x").get("error")
    assert build("fail", report, needed=0) == []  # CRS identity FAIL: nothing to ask
    q3 = build("stepup", report, needed=3)  # fraud alert: all three
    assert not check_answer("stepup", 1, "Elm St")["verified"] and not check_answer("stepup", 2, "Bank Of America")["verified"]
    assert check_answer("stepup", 3, "1978")["verified"]
    q4 = build("stepup2", report, needed=3)
    assert check_answer("stepup2", 1, next(o for o in q4[0]["options"] if o != "Elm St"))["failed"]  # one miss = can't reach 3
    print("verify ok")
