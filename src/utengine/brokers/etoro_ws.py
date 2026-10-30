"""eToro real-time quote SNAPSHOTS over the WebSocket feed (wss://ws.etoro.com/ws), READ-ONLY.

Why: the REST rates endpoint returns quotes that are hours old for most leveraged ETFs during the session (live blocker
G14, measured 2026-10-29), while the WebSocket `instrument:{id}` topic delivers eToro's own current Bid/Ask/Date
(documented: core/websocket/topics, market-data-feeds-status/instrument-rates; shape proven by Gold Miner's
ws_prefetch since 2026-09 and by this engine's read-only key on 2026-10-30).

Safety model:
  * Exactly two operations are ever sent, `Authenticate` and `Subscribe`, and only public `instrument:<digits>`
    topics (never `private`): check_ws_read_only() enforces it before anything is sent.
  * One bounded session per call: connect, authenticate, one Subscribe for all ids with snapshot=true, collect the
    Snapshot frames, close. No daemon, no reconnect loop.
  * Timestamps are the broker's own `Date`; freshness is judged downstream by the same max-quote-age gate as REST.
  * Never the sole source: any failure returns what arrived (possibly nothing) and the caller keeps its REST quotes.
"""
from __future__ import annotations

import asyncio
import json
import re
import uuid

from ..core.errors import ReadOnlyViolation

WS_URL = "wss://ws.etoro.com/ws"
_TOPIC = re.compile(r"^instrument:\d+$")


def check_ws_read_only(operation: str, data: dict) -> None:
    if operation == "Authenticate" and set(data) == {"userKey", "apiKey"}:
        return
    if operation == "Subscribe" and set(data) <= {"topics", "snapshot"} and data.get("topics") and all(
            isinstance(t, str) and _TOPIC.match(t) for t in data["topics"]):
        return
    raise ReadOnlyViolation(f"read-only eToro WebSocket refused operation {operation!r}")


def parse_frames(raw) -> list:
    """eToro pushes an outer object whose `messages` items are JSON strings; returns the decoded payload dicts."""
    try:
        outer = json.loads(raw)
    except (TypeError, ValueError):
        return []
    items = outer.get("messages") if isinstance(outer, dict) else None
    out = []
    for item in items if isinstance(items, list) else [outer]:
        if isinstance(item, str):
            try:
                item = json.loads(item)
            except ValueError:
                continue
        if isinstance(item, dict):
            out.append(item)
    return out


def snapshot_rows(payload: dict):
    """(instrument_id, bid, ask, date) from one Snapshot payload, or None (deltas, malformed or non-positive)."""
    if payload.get("type") != "Snapshot" or not _TOPIC.match(str(payload.get("topic", ""))):
        return None
    c = payload.get("content")
    if isinstance(c, str):
        try:
            c = json.loads(c)
        except ValueError:
            return None
    if not isinstance(c, dict):
        return None
    try:
        bid, ask = float(c.get("Bid")), float(c.get("Ask"))
    except (TypeError, ValueError):
        return None
    if bid <= 0 or ask <= 0 or not c.get("Date"):
        return None
    return int(str(payload["topic"]).split(":")[1]), bid, ask, str(c["Date"])


def fetch_snapshots(instrument_ids, api_key: str, user_key: str, timeout: float = 15.0) -> dict:
    """{instrument_id: (bid, ask, date)} for every id whose Snapshot arrived within `timeout` (one session)."""
    ids = sorted({int(i) for i in instrument_ids})
    if not ids:
        return {}
    import websockets                                   # imported lazily: only the live read path needs it
    got: dict = {}

    async def send(ws, op, data):
        check_ws_read_only(op, data)
        await ws.send(json.dumps({"id": str(uuid.uuid4()), "operation": op, "data": data}))

    async def run():
        async with websockets.connect(WS_URL, open_timeout=timeout) as ws:
            await ws.recv()                               # undocumented single-byte frame after the handshake
            await send(ws, "Authenticate", {"userKey": user_key, "apiKey": api_key})
            if not json.loads(await ws.recv()).get("success"):
                return
            await send(ws, "Subscribe", {"topics": [f"instrument:{i}" for i in ids], "snapshot": True})
            if not json.loads(await ws.recv()).get("success"):
                return
            while len(got) < len(ids):
                raw = await ws.recv()
                if not isinstance(raw, str):
                    continue
                for p in parse_frames(raw):
                    row = snapshot_rows(p)
                    if row and row[0] in ids:
                        got[row[0]] = row[1:]

    try:
        asyncio.run(asyncio.wait_for(run(), timeout))
    except Exception:  # noqa: BLE001 -- best effort: a timeout or any WS failure keeps what arrived; REST remains
        pass
    return got
