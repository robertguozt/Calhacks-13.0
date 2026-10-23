"""One server: Nessie watcher, Twilio webhooks + media-stream websocket, dashboard API.

Run: `uv run server.py`, then `ngrok http 7860` and set LOCAL_SERVER_URL. Point the
Twilio number's inbound Voice webhook at {LOCAL_SERVER_URL}/twiml (POST).
"""

import asyncio
import datetime
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv

load_dotenv(override=True)

import uvicorn  # noqa: E402
from fastapi import FastAPI, HTTPException, Request, WebSocket  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import Response  # noqa: E402
from loguru import logger  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402
from twilio.request_validator import RequestValidator  # noqa: E402
from twilio.twiml.voice_response import Connect, Dial, Stream, VoiceResponse  # noqa: E402

import agent  # noqa: E402
import intel  # noqa: E402
import nessie  # noqa: E402
import review  # noqa: E402
import shield  # noqa: E402
import store  # noqa: E402
from members import MEMBERS, by_phone, seed  # noqa: E402


@asynccontextmanager
async def lifespan(app: FastAPI):
    await intel.load_alerts()
    if os.getenv("SHIELD_NUMBER"):
        await shield.prerender()
    accounts = {ids["account"]: mid for mid, ids in seed()["members"].items()}
    tasks = [asyncio.create_task(agent.expire_holds())]
    if os.getenv("FACTORY_API_KEY"):
        tasks.append(asyncio.create_task(morning_reviews()))
    if accounts:
        tasks.append(asyncio.create_task(nessie.watch(accounts, agent.on_new_purchase)))
    else:
        logger.warning("no seed_ids.json: run `uv run seed.py` first; watcher not started")
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(lifespan=lifespan)
# ponytail: open CORS + unauthenticated analyst routes; demo-only, put the dashboard behind auth for real use
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


async def twilio_form(request: Request) -> dict:
    """Twilio webhook form, rejected unless signed by our Twilio account (stops spoofed call-status/twiml)."""
    form = dict(await request.form())
    url = f"{os.environ['LOCAL_SERVER_URL']}{request.url.path}" + (f"?{request.url.query}" if request.url.query else "")
    if not RequestValidator(os.environ["TWILIO_AUTH_TOKEN"]).validate(url, form, request.headers.get("X-Twilio-Signature", "")):
        raise HTTPException(403, "bad Twilio signature")
    return form


@app.post("/twiml")
async def twiml(request: Request):
    form, q = await twilio_form(request), request.query_params
    if shield.shield_member(form.get("To")):  # a call to her protected line
        return Response(await shield.caller_twiml(form), media_type="application/xml")
    inbound = form.get("Direction") == "inbound"
    mode = q.get("mode", "member")
    case_id = q.get("case_id", "")
    if inbound and not case_id:  # callback to the card number: find her held case by caller id
        case = agent.open_case_for(by_phone(form.get("From")))
        case_id = case.id if case else ""
    stream = Stream(url=os.environ["LOCAL_SERVER_URL"].replace("https://", "wss://") + "/ws")
    for name, value in {
        "case_id": case_id,
        "mode": mode,
        "note": q.get("note", ""),
        "direction": "inbound" if inbound else "outbound",
        "from_number": form.get("From", ""),
        "to_number": form.get("To", ""),
    }.items():
        stream.parameter(name=name, value=value)
    connect = Connect()
    connect.append(stream)
    response = VoiceResponse()
    response.append(connect)
    if case_id and mode in ("member", "bridge"):
        # Only reached if the bot ends its stream WITHOUT hanging up, i.e. when bridging to family (bot.py)
        dial = Dial()
        # member waits with hold music until family joins and starts the conference
        dial.conference(f"case-{case_id}", start_conference_on_enter=mode == "bridge", end_conference_on_exit=True, beep=False)
        response.append(dial)
    return Response(str(response), media_type="application/xml")


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    from pipecat.runner.types import WebSocketRunnerArguments

    from bot import bot

    await websocket.accept()
    try:
        await bot(WebSocketRunnerArguments(websocket=websocket))
    except Exception as e:
        logger.exception(f"bot crashed: {e}")
        await websocket.close()


@app.websocket("/shield-ws")
async def shield_ws(websocket: WebSocket):
    await websocket.accept()
    try:
        await shield.listen(websocket)
    except Exception as e:
        logger.exception(f"shield listener crashed: {e}")  # the call itself carries on; only detection stops


@app.get("/shield/audio/{clip}.wav")
async def shield_audio(clip: str):
    if clip not in shield.AUDIO:
        raise HTTPException(404)
    return Response(shield.AUDIO[clip], media_type="audio/wav")


@app.post("/shield/twiml/{key}")
async def shield_twiml(key: str):
    if key not in shield.TWIML:
        raise HTTPException(404)
    return Response(shield.TWIML[key], media_type="application/xml")


@app.post("/call-status")
async def call_status(request: Request):
    form, q = await twilio_form(request), request.query_params
    asyncio.create_task(agent.on_call_status(q.get("case_id", ""), q.get("mode", ""), form.get("CallStatus", "")))
    return {"ok": True}


class Scenario(BaseModel):
    member_id: str
    merchant: str = Field(min_length=1, max_length=60)
    category: str = Field(min_length=1, max_length=30)
    amount: int = Field(gt=0, le=100_000)
    note: str = Field("", max_length=200)


@app.post("/scenario")
async def scenario(s: Scenario):
    """Judge Scenario form: posts a real purchase to Nessie; the watcher takes it from there."""
    ids = seed()["members"].get(s.member_id)
    if s.member_id not in MEMBERS or not ids:
        raise HTTPException(404, f"unknown or unseeded member {s.member_id!r}")
    mid = await nessie.merchant_id(s.merchant, s.category)
    await nessie.create_txn(ids["account"], mid, s.merchant, s.amount, s.note or f"{s.merchant} purchase")
    return {"ok": True}


@app.post("/cases/{case_id}/{action}")
async def analyst(case_id: str, action: str):
    if action not in ("release", "confirm_fraud"):
        raise HTTPException(404)
    case = agent.CASES.get(case_id)
    if not case:
        raise HTTPException(404, "case not loaded in this server process")
    if why := await agent.do(case, action, "human", f"Analyst decision from the dashboard: {action.replace('_', ' ')}"):
        raise HTTPException(409, why)
    return {"ok": True}


async def morning_reviews(hour: int = 7):
    """Schedule trigger for the Droid review: every day at 7am local, after the night shift."""
    while True:
        now = datetime.datetime.now()
        nxt = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        nxt += datetime.timedelta(days=1) if nxt <= now else datetime.timedelta()
        await asyncio.sleep((nxt - now).total_seconds())
        try:
            await review.run_review(review.case_files(agent.CASES.values()), "schedule")
        except Exception as e:
            logger.error(f"morning review: {e}")


@app.post("/review")
async def run_review():
    """Dashboard trigger. Runs in the background; steps stream into Convex."""
    if review._lock.locked():
        raise HTTPException(409, "a review is already running")
    asyncio.create_task(review.run_review(review.case_files(agent.CASES.values()), "analyst"))
    return {"ok": True}


@app.post("/review/{decision}")
async def decide_review(decision: str):
    if decision not in ("approve", "reject"):
        raise HTTPException(404)
    if why := await review.decide(decision == "approve"):
        raise HTTPException(409, why)
    return {"ok": True}


@app.post("/reset")
async def reset():
    agent.CASES.clear()
    await store.reset()
    return {"ok": True}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "7860")))
