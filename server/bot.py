"""Pipecat voice bot on Twilio Media Streams: Deepgram STT -> Claude -> Cartesia TTS.

The prompt and tool effects live in calltools.py (shared with arena.py). Modes, chosen
from the stream parameters set in server.py /twiml:
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
from pipecat.adapters.schemas.function_schema import FunctionSchema
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
from pipecat.services.cartesia.stt import CartesiaSTTService
from pipecat.services.cartesia.tts import CartesiaTTSService, GenerationConfig
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.llm_service import FunctionCallParams
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams, FastAPIWebsocketTransport
from pipecat.workers.runner import WorkerRunner
from twilio.rest import Client as Twilio

import agent
import calltools
import store
from calltools import CALL_MODEL, CU
from members import MEMBERS, by_phone


def services(system: str | None = None, keyterms: list[str] | None = None):
    tts = CartesiaTTSService(
        api_key=os.environ["CARTESIA_API_KEY"],
        settings=CartesiaTTSService.Settings(
            voice=os.getenv("CARTESIA_VOICE_ID", "71a7ad14-091c-4e8e-a314-022ece01c121"),
            generation_config=GenerationConfig(speed=0.9, emotion="content"),  # slower helps older listeners
        ),
    )
    if system is None:
        return tts
    if os.getenv("STT") == "deepgram":  # fallback knob for demo day
        stt = DeepgramSTTService(api_key=os.environ["DEEPGRAM_API_KEY"])
    else:  # Cartesia Ink, biased toward this call's answer options so her answers aren't misheard
        stt = CartesiaSTTService(api_key=os.environ["CARTESIA_API_KEY"],
                                 settings=CartesiaSTTService.Settings(model="ink-2", keyterm=keyterms or None))
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
    stt, llm, tts = services(calltools.build_prompt(case, inbound, resume), calltools.keyterms(case))
    case.member_call_sid = runner_args.call_data.call_id if runner_args.call_data else None

    async def handler(params: FunctionCallParams):
        r = await calltools.run_tool(case, params.function_name, dict(params.arguments))
        await params.result_callback({k: v for k, v in r.items() if not k.startswith("_")})
        if r.get("_handoff"):
            serializer.hand_off()
        if r.get("_end") or r.get("_handoff"):
            await params.llm.push_frame(EndWorkerFrame(reason=params.function_name))

    tools = [FunctionSchema(t["name"], t["description"], t["properties"], t["required"], handler=handler)
             for t in calltools.tool_specs(case)]
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
