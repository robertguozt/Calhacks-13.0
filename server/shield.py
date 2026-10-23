"""Scam Shield: catch the scam while the scammer is still on the phone with her.

Calls to the member's protected line (SHIELD_NUMBER; in real life she forwards her landline
to it) run like this:
1. The caller hears, in the Cartesia voice, that the line is protected. California needs every
   party's consent to listen in, and the notice itself deters scammers.
2. <Start><Stream track="inbound_track"> forks only the CALLER's audio to /shield-ws. The call
   continues to <Dial><Conference>, and we ring her phone into the same conference.
3. Cartesia Ink transcribes. A keyword check decides when to ask Claude Haiku "is this a scam
   script?", which keeps it cheap. The transcript lives in memory only, and only the evidence
   quote is kept if the call is flagged.
4. On a confident yes, Twilio plays a Cartesia-voiced warning to HER participant only. The
   scammer hears nothing.
5. The flag feeds the money side: any purchase in the next 24h is held by rule
   (gate.must_hold), and only an analyst can release it.
"""

import asyncio
import os
import re
import time
import uuid

import anthropic
import httpx
from loguru import logger
from pipecat.frames.frames import TranscriptionFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.runner.utils import parse_telephony_websocket
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.services.cartesia.stt import CartesiaSTTService
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams, FastAPIWebsocketTransport
from pipecat.workers.runner import WorkerRunner
from pydantic import BaseModel
from twilio.rest import Client as Twilio
from twilio.twiml.voice_response import Dial, Start, VoiceResponse

import store
from calltools import CU
from members import MEMBERS, member_phone

MODEL = "claude-haiku-5-5"  # it runs during a live call, so latency matters
THRESHOLD = 0.75
WINDOW_HOURS = 24  # a flagged call holds her high-risk purchases for this long
ANNOUNCE = f"This call is protected by {CU}'s scam shield, and an automated assistant is listening for scams."
WHISPER = ("{name}, this is {cu}. Only you can hear me. This call sounds like a scam: {why}. "
           "Real companies never ask for gift cards or secrecy. It's okay to hang up.")

# Cheap gate before any model call: phrases from real scripts (FTC alerts, PLAYBOOK.md section 2).
PREFILTER = re.compile(
    r"gift ?cards?|read (me )?the (numbers|codes)|don'?t tell|keep (this|it) (a )?secret|microsoft|apple support|"
    r"tech(nical)? support|hacked|virus|remote access|irs|social security|warrant|arrest|police|bail|jail|"
    r"wire (the )?money|bitcoin|crypto|western union|zelle|grand(son|daughter|ma)|your bank (may be|is) involved", re.I)

SESSIONS: dict[str, dict] = {}  # conference name -> live call state (in memory only)
EVENTS: dict[str, list[dict]] = {}  # member_id -> flagged calls, feeds triage + gate
AUDIO: dict[str, bytes] = {}  # rendered Cartesia clips served at /shield/audio/{id}.wav
TWIML: dict[str, str] = {}  # whisper TwiML Twilio fetches as the participant announce_url
_llm = None


class Verdict(BaseModel):
    scam: bool
    confidence: float
    scam_type: str  # tech_support | government_impersonation | family_emergency | romance | other | none
    evidence: str  # the caller's exact words that show it
    why_short: str  # under 12 words, spoken to her, e.g. "he's asking for gift cards and secrecy"


def shield_member(to_number: str | None) -> str | None:
    """Which member owns this protected line."""
    digits = lambda s: "".join(c for c in s or "" if c.isdigit())[-10:]
    for mid, m in MEMBERS.items():
        num = os.getenv(m.get("shield_env") or "")
        if num and to_number and digits(num) == digits(to_number):
            return mid
    return None


def recent(member_id: str, hours: float = WINDOW_HOURS) -> dict | None:
    cutoff = time.time() - hours * 3600
    hits = [e for e in EVENTS.get(member_id, []) if e["t"] >= cutoff]
    return hits[-1] if hits else None


def mask(number: str) -> str:
    d = "".join(c for c in number or "" if c.isdigit())
    return f"***-{d[-4:]}" if len(d) >= 4 else "unknown number"


async def render(text: str) -> str | None:
    """Cartesia Sonic -> 8 kHz WAV served to Twilio <Play>. Returns the clip id, or None to fall back to <Say>."""
    try:
        async with httpx.AsyncClient(timeout=15) as http:
            r = await http.post("https://api.cartesia.ai/tts/bytes", headers={
                "X-API-Key": os.environ["CARTESIA_API_KEY"], "Cartesia-Version": "2026-03-01"}, json={
                "model_id": "sonic-3.6", "transcript": text,
                "voice": {"mode": "id", "id": os.getenv("CARTESIA_VOICE_ID", "71a7ad14-091c-4e8e-a314-022ece01c121")},
                "output_format": {"container": "wav", "encoding": "pcm_s16le", "sample_rate": 8000},
                "generation_config": {"speed": 0.9, "emotion": "content"}})
            r.raise_for_status()
        clip = uuid.uuid4().hex
        AUDIO[clip] = r.content
        return clip
    except Exception as e:
        logger.error(f"shield: Cartesia render failed, using <Say>: {e}")
        return None


def play_twiml(clip: str | None, fallback_text: str) -> str:
    r = VoiceResponse()
    if clip:
        r.play(f"{os.environ['LOCAL_SERVER_URL']}/shield/audio/{clip}.wav")
    else:
        r.say(fallback_text)
    return str(r)


async def prerender():
    """Render the consent notice once (server startup), so the first call isn't waiting on Cartesia."""
    if "announce" not in AUDIO and (clip := await render(ANNOUNCE)):
        AUDIO["announce"] = AUDIO.pop(clip)


async def caller_twiml(form: dict) -> str:
    """TwiML for an incoming call to a protected line: announce, fork caller audio, conference, ring her."""
    member_id = shield_member(form.get("To"))
    conf = f"shield-{form.get('CallSid', uuid.uuid4().hex)}"
    base = os.environ["LOCAL_SERVER_URL"]
    SESSIONS[conf] = {"member_id": member_id, "caller": form.get("From", ""), "member_sid": None,
                      "flagged": False, "text": [], "checked_at": 0.0}
    await prerender()
    r = VoiceResponse()
    r.play(f"{base}/shield/audio/announce.wav") if "announce" in AUDIO else r.say(ANNOUNCE)
    start = Start()
    stream = start.stream(url=base.replace("https://", "wss://") + "/shield-ws", track="inbound_track")
    stream.parameter(name="conf", value=conf)
    r.append(start)
    dial = Dial()
    dial.conference(conf, start_conference_on_enter=False, end_conference_on_exit=True, beep=False, wait_url="")
    r.append(dial)
    asyncio.create_task(_ring_member(conf, member_id))
    return str(r)


async def _ring_member(conf: str, member_id: str | None):
    to = member_phone(member_id) if member_id else None
    if not to:
        logger.error(f"shield: no phone for member {member_id}")
        return
    twiml = VoiceResponse()
    d = Dial()
    d.conference(conf, start_conference_on_enter=True, end_conference_on_exit=True, beep=False)
    twiml.append(d)
    try:
        call = await asyncio.to_thread(
            Twilio(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"]).calls.create,
            to=to, from_=os.environ["SHIELD_NUMBER"], twiml=str(twiml))
        SESSIONS[conf]["member_sid"] = call.sid
    except Exception as e:  # background task: log it, or a failed ring disappears silently
        logger.error(f"shield: could not ring {member_id}: {e}")


async def classify(transcript: str) -> Verdict:
    global _llm
    _llm = _llm or anthropic.AsyncAnthropic()
    r = await _llm.messages.parse(
        model=MODEL, max_tokens=600, output_format=Verdict, output_config={"effort": "low"},
        system=("You listen to the CALLER's side of a phone call to an older adult and decide whether it is a scam script "
                "(tech support, government or bank impersonation, family emergency, romance, prize). Asking for gift cards, "
                "wires, crypto or cash, secrecy from the bank or family, urgency, or remote computer access are strong signs. "
                "A relative chatting about a birthday gift card is not a scam. Quote the caller's exact words as evidence."),
        messages=[{"role": "user", "content": f"Caller so far:\n{transcript}"}],
    )
    return r.parsed_output or Verdict(scam=False, confidence=0, scam_type="none", evidence="", why_short="")


async def whisper(conf: str, v: Verdict):
    """Warn her and only her, then record it and arm the money-side hold."""
    s = SESSIONS[conf]
    s["flagged"] = True
    m = MEMBERS[s["member_id"]]
    text = WHISPER.format(name=m["first_name"], cu=CU, why=v.why_short or "it matches a known scam script")
    key = await render(text) or conf
    TWIML[key] = play_twiml(None if key == conf else key, text)
    try:
        tw = Twilio(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])
        sid = (await asyncio.to_thread(tw.conferences.list, friendly_name=conf, status="in-progress", limit=1))[0].sid
        await asyncio.to_thread(tw.conferences(sid).participants(s["member_sid"]).update,
                                announce_url=f"{os.environ['LOCAL_SERVER_URL']}/shield/twiml/{key}", announce_method="POST")
        whispered = "Whispered a warning only she could hear"
    except Exception as e:
        logger.error(f"shield: whisper failed: {e}")
        whispered = f"Whisper failed ({e}); the money-side hold is still armed"
    EVENTS.setdefault(s["member_id"], []).append({"t": time.time(), "scam_type": v.scam_type, "evidence": v.evidence})
    cid = await store.open_case(
        member_id=s["member_id"], member_name=m["name"], txn_id=conf, merchant=f"Phone call from {mask(s['caller'])}",
        category="scam_call", amount=0.0, status="escalated",
        risk_signals=[{"source": "shield", "fact": f'Caller said: "{v.evidence[:200]}"', "weight": round(v.confidence, 2)}],
        plan=f"{whispered}. Any gift card, wire or crypto purchase in the next {WINDOW_HOURS}h is held for an analyst.",
        decision_rationale=f"Scam Shield: {v.scam_type.replace('_', ' ')} script detected live (confidence {v.confidence:.0%}).",
    )
    await store.action(cid, "shield", "agent", whispered)


class Detector(FrameProcessor):
    """Collects the caller's words; asks Claude only after a scam-ish phrase, at most every 6s."""

    def __init__(self, conf: str, classify=classify, whisper=whisper):
        super().__init__()
        self.conf, self._classify, self._whisper, self._busy = conf, classify, whisper, False

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, TranscriptionFrame) and frame.text:
            await self.heard(frame.text)
        await self.push_frame(frame, direction)

    async def heard(self, text: str):
        s = SESSIONS.get(self.conf)
        if not s:
            return
        s["text"] = (s["text"] + [text])[-40:]  # rolling window, memory only
        joined = " ".join(s["text"])
        if s["flagged"] or self._busy or not PREFILTER.search(joined) or time.time() - s["checked_at"] < 6:
            return
        self._busy, s["checked_at"] = True, time.time()
        try:
            v = await self._classify(joined)
            if v.scam and v.confidence >= THRESHOLD:
                await self._whisper(self.conf, v)
        except Exception as e:
            logger.error(f"shield: classify failed: {e}")
        finally:
            self._busy = False


async def listen(websocket):
    """Listen-only pipeline on the forked caller audio. Never speaks, never hangs up."""
    _, call_data = await parse_telephony_websocket(websocket)
    conf = (call_data.body or {}).get("conf", "")
    serializer = TwilioFrameSerializer(stream_sid=call_data["stream_id"], call_sid=call_data["call_id"],
                                       params=TwilioFrameSerializer.InputParams(auto_hang_up=False))
    transport = FastAPIWebsocketTransport(websocket=websocket, params=FastAPIWebsocketParams(
        audio_in_enabled=True, audio_out_enabled=False, add_wav_header=False, serializer=serializer))
    stt = CartesiaSTTService(api_key=os.environ["CARTESIA_API_KEY"], settings=CartesiaSTTService.Settings(
        model="ink-2", keyterm=["gift cards", "Microsoft", "IRS", "Social Security", "warrant", "bail", "Western Union", "bitcoin"]))
    worker = PipelineWorker(Pipeline([transport.input(), stt, Detector(conf)]),
                            params=PipelineParams(audio_in_sample_rate=8000))
    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)

    @transport.event_handler("on_client_disconnected")
    async def _(transport, client):
        await runner.cancel()

    try:
        await runner.run()
    finally:
        s = SESSIONS.pop(conf, None)  # the transcript is dropped here; only a flagged call's evidence was kept
        logger.info(f"shield: {conf} ended, flagged={bool(s and s['flagged'])}")
