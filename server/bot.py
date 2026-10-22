"""Pipecat voice bot on Twilio Media Streams: Deepgram STT -> Claude -> Cartesia TTS.

Modes, chosen from the stream parameters set in server.py /twiml:
- member (outbound call, or inbound callback to the card number): verify, then talk it through
- family: one-way Cartesia message to the trusted contact, then hang up
- bridge: short Cartesia intro to the trusted contact, then she joins the member in a live call
- no open case on an inbound call: short message, then hang up

Bridging: member and bridge TwiML put <Dial><Conference> after <Connect><Stream>. Twilio runs
verbs after <Connect> only once our server closes the WebSocket (Twilio <Stream> docs). Normally
the serializer hangs up on End/CancelFrame first, so the conference is never reached. To bridge,
the bot calls `hand_off()` and ends its stream; Twilio then moves the call into the conference,
and the agent is no longer on the line.
"""

import asyncio
import os

from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import CancelFrame, EndFrame, EndWorkerFrame, LLMRunFrame, TTSSpeakFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair, LLMUserAggregatorParams
from pipecat.runner.types import RunnerArguments
from pipecat.runner.utils import parse_telephony_websocket
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.services.anthropic.llm import AnthropicLLMService
from pipecat.services.cartesia.tts import CartesiaTTSService, GenerationConfig
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.llm_service import FunctionCallParams
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams, FastAPIWebsocketTransport
from pipecat.workers.runner import WorkerRunner
from twilio.rest import Client as Twilio

import agent
import store
import verify
from members import MEMBERS, by_phone

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


def services(system: str | None = None):
    tts = CartesiaTTSService(
        api_key=os.environ["CARTESIA_API_KEY"],
        settings=CartesiaTTSService.Settings(
            voice=os.getenv("CARTESIA_VOICE_ID", "71a7ad14-091c-4e8e-a314-022ece01c121"),
            generation_config=GenerationConfig(speed=0.9, emotion="content"),  # slower helps older listeners
        ),
    )
    if system is None:
        return tts
    stt = DeepgramSTTService(api_key=os.environ["DEEPGRAM_API_KEY"])
    llm = AnthropicLLMService(
        api_key=os.environ["ANTHROPIC_API_KEY"],
        settings=AnthropicLLMService.Settings(model=CALL_MODEL, system_instruction=system, enable_prompt_caching=True),
    )
    return stt, llm, tts


class HandoffSerializer(TwilioFrameSerializer):
    """Twilio serializer with a public switch: after hand_off(), ending the stream doesn't hang up."""

    handing_off = False

    def hand_off(self):
        self.handing_off = True

    async def serialize(self, frame):
        if self.handing_off and isinstance(frame, (EndFrame, CancelFrame)):
            return None  # what the base class returns after hanging up, minus the hang-up
        return await super().serialize(frame)


async def twilio_transport(runner_args: RunnerArguments):
    """create_transport() for Twilio, with our serializer (same steps as pipecat.runner.utils)."""
    _, call_data = await parse_telephony_websocket(runner_args.websocket)
    runner_args.call_data = call_data
    serializer = HandoffSerializer(
        stream_sid=call_data["stream_id"], call_sid=call_data["call_id"],
        account_sid=os.environ["TWILIO_ACCOUNT_SID"], auth_token=os.environ["TWILIO_AUTH_TOKEN"],
    )
    params = FastAPIWebsocketParams(audio_in_enabled=True, audio_out_enabled=True, add_wav_header=False, serializer=serializer)
    return FastAPIWebsocketTransport(websocket=runner_args.websocket, params=params), serializer


async def run(worker: PipelineWorker, runner_args: RunnerArguments, transport):
    runner = WorkerRunner(handle_sigint=runner_args.handle_sigint)
    await runner.add_workers(worker)

    @transport.event_handler("on_client_disconnected")
    async def _(transport, client):  # caller hung up: stop now, don't wait for the idle timeout
        await runner.cancel()

    await runner.run()


async def one_way(transport, runner_args, text: str):
    """Speak a fixed message in the Cartesia voice, then end the stream."""
    tts = services()
    worker = PipelineWorker(Pipeline([transport.input(), tts, transport.output()]),
                            params=PipelineParams(audio_in_sample_rate=8000, audio_out_sample_rate=8000))

    @transport.event_handler("on_client_connected")
    async def _(transport, client):
        await worker.queue_frames([TTSSpeakFrame(text), EndFrame()])

    await run(worker, runner_args, transport)


async def member_call(transport, serializer: HandoffSerializer, runner_args, case, inbound: bool, resume: bool):
    m = MEMBERS[case.member_id]
    fam = m["family"]
    fam_name = f"your {fam['relation']} {fam['name']}" if fam else "your family"
    qs = verify.questions(case.id)
    first = RESUME.format(family=fam_name) if resume else (FIRST_IN if inbound else FIRST_OUT).format(cu=CU, first_name=m["first_name"])
    system = PROMPT.format(
        cu=CU, how="answering a call from" if inbound else "calling", name=m["name"], first=first,
        amount=case.amount, merchant=case.merchant, category=case.category, rationale=case.rationale, family=fam_name,
        questions="\n".join(f"  {q['number']}. {q['question']} Options: {', '.join(q['options'])}." for q in qs) or "  (none available)",
    )
    stt, llm, tts = services(system)
    case.member_call_sid = runner_args.call_data.call_id if runner_args.call_data else None

    async def check_answer(params: FunctionCallParams, question_number: int, answer: str):
        """Check the member's answer to a verification question.

        Args:
            question_number: The question's number.
            answer: The option she picked, exactly as written in the options.
        """
        r = verify.check_answer(case.id, question_number, answer)
        if r.get("verified") and not case.verified:
            case.verified = True  # set by code from the checked answers, never by the model
            await store.action(case.id, "verify", "member", "Identity verified from her credit-file answers")
        elif r.get("failed"):
            await store.action(case.id, "verify", "member", "Identity verification failed")
        await params.result_callback(r)

    async def release_hold(params: FunctionCallParams, reason: str):
        """Release the hold so the purchase goes through. Only after verification, when she made it knowingly and nobody told her to.

        Args:
            reason: One sentence for the case file.
        """
        why = await agent.do(case, "release", "member", reason)
        await params.result_callback({"released": why is None, "refused": why})

    async def notify_trusted_contact(params: FunctionCallParams, reason: str):
        """Call her trusted family contact to say a payment is paused (no amounts or details). Use on red flags.

        Args:
            reason: One sentence for the case file, naming the red flag.
        """
        case.red_flags = True  # only ever tightens what the gate allows
        why = await agent.do(case, "call_family", "agent", reason)
        await params.result_callback({"calling": why is None, "refused": why})

    async def connect_family(params: FunctionCallParams, reason: str):
        """Connect her to her trusted family contact on a live call, then leave the line. Only if she said yes.

        Args:
            reason: One sentence for the case file, including that she asked for it.
        """
        case.red_flags = True  # she was pressured; same tightening as notify_trusted_contact
        why = await agent.do(case, "connect_family", "member", reason)
        await params.result_callback({"connecting": why is None, "refused": why})
        if why is None:
            serializer.hand_off()
            await params.llm.push_frame(EndWorkerFrame(reason="bridging to family"))

    async def escalate_to_analyst(params: FunctionCallParams, reason: str):
        """Keep the hold and send the case to a human analyst for the morning.

        Args:
            reason: One sentence for the case file.
        """
        why = await agent.do(case, "escalate", "agent", reason)
        await params.result_callback({"escalated": why is None, "refused": why})

    async def end_call(params: FunctionCallParams, reason: str):
        """Hang up. Say goodbye first.

        Args:
            reason: One sentence for the case file.
        """
        await store.action(case.id, "call", "agent", f"Call ended: {reason}")
        await params.result_callback({"ok": True})
        await params.llm.push_frame(EndWorkerFrame(reason=reason))

    tools = [check_answer, release_hold, notify_trusted_contact, escalate_to_analyst, end_call] + ([connect_family] if fam else [])
    context = LLMContext(tools=tools)
    user_agg, asst_agg = LLMContextAggregatorPair(context, user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer()))
    worker = PipelineWorker(
        Pipeline([transport.input(), stt, user_agg, llm, tts, transport.output(), asst_agg]),
        params=PipelineParams(audio_in_sample_rate=8000, audio_out_sample_rate=8000, enable_metrics=True),
        idle_timeout_secs=runner_args.pipeline_idle_timeout_secs,
    )

    @user_agg.event_handler("on_user_turn_stopped")
    async def _(aggregator, strategy, message):
        asyncio.create_task(store.line(case.id, "member", message.content or ""))

    @asst_agg.event_handler("on_assistant_turn_stopped")
    async def _(aggregator, message):
        asyncio.create_task(store.line(case.id, "agent", message.content or ""))

    @transport.event_handler("on_client_connected")
    async def _(transport, client):
        what = "Back on the line after the family call went unanswered" if resume else \
               "Inbound callback answered" if inbound else "Member answered"
        await store.action(case.id, "call", "agent", what)
        context.add_message({"role": "developer", "content": "The call just connected. Say your first words now."})
        await worker.queue_frames([LLMRunFrame()])

    await run(worker, runner_args, transport)


async def bot(runner_args: RunnerArguments):
    transport, serializer = await twilio_transport(runner_args)
    p = runner_args.call_data.body if runner_args.call_data else {}
    inbound = p.get("direction") == "inbound"
    case = agent.CASES.get(p.get("case_id", "")) or (agent.open_case_for(by_phone(p.get("from_number"))) if inbound else None)
    mode = p.get("mode", "member")
    logger.info(f"call: mode={mode} inbound={inbound} case={case.id if case else None}")
    m = MEMBERS[case.member_id] if case else None
    fam = m["family"] if m else None
    kin = "mother" if fam and fam["relation"] == "daughter" else "family member"

    if mode == "family" and case and fam:
        await store.action(case.id, "family_call", "agent", f"Calling {fam['name']}: payment paused, please call {m['first_name']} (no details shared)")
        await one_way(transport, runner_args,
                      f"Hi, this is the after-hours assistant at {CU}, calling for {fam['name']}. "
                      f"We paused a payment on your {kin} {m['first_name']}'s account tonight and spoke with her. Please give her a call. "
                      "We will never ask you for codes or passwords. Goodbye.")
    elif mode == "bridge" and case and fam:
        await store.action(case.id, "family_bridge", "agent", f"{fam['name']} answered; joining her to {m['first_name']}'s call. The agent leaves the line.")
        serializer.hand_off()
        await one_way(transport, runner_args,
                      f"Hi {fam['name']}, this is the after-hours assistant at {CU}. Your {kin} {m['first_name']} is on the line "
                      "and asked to talk with you about a payment we paused tonight. We will never ask you for codes or passwords. "
                      "Connecting you now.")
    elif case:
        try:
            await member_call(transport, serializer, runner_args, case, inbound, resume=p.get("note") == "family_unavailable")
        except Exception:
            # her TwiML continues to the family conference after the stream; never strand her there alone
            sid = runner_args.call_data.call_id if runner_args.call_data else None
            if sid:
                Twilio(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"]).calls(sid).update(status="completed")
            raise
    else:
        await one_way(transport, runner_args,
                      f"Thank you for calling the {CU} after-hours line. There's nothing waiting on your account tonight. "
                      "If you have a question, please call back during business hours. Goodbye.")
