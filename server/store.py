"""Case file writes to Convex (dashboard reads them live with useQuery).

The Convex client is synchronous, so calls run in a thread to keep the audio loop free.
Without CONVEX_URL every write is just logged, so the server still runs locally.
"""

import asyncio
import os

from loguru import logger

_client = None
if os.getenv("CONVEX_URL"):
    from convex import ConvexClient

    _client = ConvexClient(os.environ["CONVEX_URL"])


async def _m(fn: str, **args):
    if not _client:
        logger.info(f"[convex off] {fn} {args}")
        return args.get("txn_id")
    try:
        return await asyncio.to_thread(_client.mutation, fn, {"secret": os.environ["CONVEX_WRITE_SECRET"], **args})
    except Exception as e:  # a dashboard hiccup must never drop a hold or a call
        logger.error(f"convex {fn}: {e}")


async def open_case(**case) -> str:
    return await _m("cases:open", **case) or case["txn_id"]


async def status(case_id: str, status: str):
    await _m("cases:setStatus", caseId=case_id, status=status)


async def action(case_id: str, type: str, by: str, detail: str):
    logger.info(f"[{case_id}] {by} {type}: {detail}")
    await _m("cases:addAction", caseId=case_id, type=type, by=by, detail=detail)


async def line(case_id: str, speaker: str, text: str):
    if text:
        await _m("cases:addLine", caseId=case_id, speaker=speaker, text=text)


async def reset():
    await _m("cases:reset")


async def review_open(trigger: str) -> str:
    return await _m("cases:reviewOpen", trigger=trigger) or "local-review"


async def review_step(review_id: str, text: str):
    logger.info(f"[review] {text}")
    await _m("cases:reviewStep", reviewId=review_id, text=text)


async def review_set(review_id: str, status: str, **fields):
    await _m("cases:reviewSet", reviewId=review_id, status=status, **fields)
