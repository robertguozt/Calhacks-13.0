"""CRS (StitchCredit) sandbox client: identity, fraud and credit-file signals.

Login happens once per process. Repeated FAILED logins lock the account (CRS113/114,
only CRS can unlock), so a failed login is never retried automatically.
"""

import datetime
import os

import httpx
from loguru import logger

BASE = "https://api-sandbox.stitchcredit.com/api"
_http = httpx.AsyncClient(base_url=BASE, timeout=20, headers={"Accept": "application/json"})
_token: str | None = None
_login_failed = False
_cache: dict[str, dict] = {}  # member_id -> pull; identical requests return cached data anyway


async def _login() -> str:
    global _token, _login_failed
    if _login_failed:
        raise RuntimeError("CRS login failed earlier; fix CRS_USERNAME/CRS_PASSWORD and restart")
    r = await _http.post("/users/login", json={"username": os.environ["CRS_USERNAME"], "password": os.environ["CRS_PASSWORD"]})
    if r.status_code != 200:
        _login_failed = True
        raise RuntimeError(f"CRS login {r.status_code}: {r.text[:200]}")
    _token = tok = r.json()["token"]
    return tok


async def _post(path: str, body: dict) -> dict:
    global _token
    for _ in range(2):  # one re-login on an expired token (401 from a GOOD login is not a lockout risk)
        r = await _http.post(path, json=body, headers={"Authorization": f"Bearer {_token or await _login()}"})
        if r.status_code == 401 and _token:
            _token = None
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"CRS {path}: 401 after re-login (product not enabled on this account?)")


async def pull(member_id: str, persona: dict | None) -> dict:
    """{"identity", "fraud", "report"}; a product that fails is None, not fatal."""
    if not persona:
        return {}
    if member_id in _cache:
        return _cache[member_id]
    out = {}
    for key, path in (
        ("identity", "/transunion/identity-manager-verification/basic"),
        ("fraud", "/fraud-finder/fraud-finder"),
        ("report", "/transunion/credit-report/standard/tu-prequal-fico9"),
    ):
        try:
            out[key] = await _post(path, persona[key])
        except Exception as e:
            logger.error(f"CRS {key}: {e}")
            out[key] = None
    _cache[member_id] = out
    return out


FAIL_CODES = {"2201", "2202", "2301"}  # SSN issued to another name / before DOB, deceased: identity can't be trusted
STEP_UP_CODES = {"2401"}  # fraud alert on file


def _messages(data: dict) -> list[dict]:
    ident = (data or {}).get("identity") or {}
    return ((ident.get("addOnProducts") or [{}])[0].get("idManagerVerification") or {}).get("messages") or []


def answers_needed(data: dict) -> int:
    """CRS guide: mismatched/deceased/synthetic = FAIL (no phone verification at all),
    fraud alert = step up (ask for more proof), otherwise 2 of 3."""
    codes = {str(m.get("code")) for m in _messages(data)}
    return 0 if codes & FAIL_CODES else 3 if codes & STEP_UP_CODES else 2


def signals(data: dict) -> list[dict]:
    """Facts for triage, each checkable against the raw CRS response."""
    out = []
    add = lambda fact, w: out.append({"source": "crs", "fact": fact, "weight": w})
    if not data:
        return out

    ident = data.get("identity") or {}
    msgs = _messages(data)
    for m in msgs:
        add(f"TransUnion identity message {m.get('code')}: {m.get('text')}", 0.8 if str(m.get("code")) in {"2301", "2401"} else 0.5)
    if ident and not msgs:
        add("TransUnion identity check passed (name, SSN, DOB, address verify)", 0.0)

    risk = (data.get("fraud") or {}).get("risk") or {}
    if "score" in risk:
        add(f"AtData Fraud Finder risk score {risk['score']}/100 for her email/phone/address", round(min(risk["score"], 100) / 100, 2))

    report = data.get("report") or {}
    for e in report.get("errorMessages") or []:
        add(f"Credit report error: {e.get('creditErrorMessageText')}", 0.6)
    cutoff = (datetime.date.today() - datetime.timedelta(days=30)).isoformat()
    recent = [i for i in report.get("inquiries") or [] if (i.get("inquiryDate") or "") >= cutoff]
    if recent:
        add(f"{len(recent)} credit inquiries in the last 30 days ({', '.join(i.get('creditorName', '?') for i in recent[:3])}): possible account takeover", 0.7)
    elif report:
        add("No new credit inquiries in the last 30 days", 0.0)
    for f in report.get("creditFiles") or []:
        status = (f.get("creditFileDetail") or {}).get("creditFileResultStatusType")
        if status and "Freeze" in status:
            add(f"Credit file status {status}", 0.3)
    return out
