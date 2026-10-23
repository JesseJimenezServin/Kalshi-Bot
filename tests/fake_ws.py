"""A minimal fake of Kalshi's WebSocket server, on a real localhost socket."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from websockets.asyncio.server import Server, ServerConnection, serve


class FakeKalshiWS:
    def __init__(self, market_limit: int | None = None) -> None:
        self.market_limit = market_limit
        self.commands: list[dict[str, Any]] = []
        self.handshake_headers: list[dict[str, str]] = []
        self.subs: dict[int, dict[str, Any]] = {}  # sid -> {"channel", "tickers"}
        self.seqs: dict[int, int] = {}  # sid -> last seq sent
        self._next_sid = 1
        self._conn: ServerConnection | None = None
        self._server: Server | None = None
        self.connected = asyncio.Event()
        self.url = ""

    async def __aenter__(self) -> FakeKalshiWS:
        self._server = await serve(self._handle, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        self.url = f"ws://127.0.0.1:{port}/trade-api/ws/v2"
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    async def _handle(self, conn: ServerConnection) -> None:
        self._conn = conn
        self.subs.clear()
        self.handshake_headers.append(dict(conn.request.headers))  # type: ignore[union-attr]
        self.connected.set()
        try:
            async for raw in conn:
                cmd = json.loads(raw)
                self.commands.append(cmd)
                await self._reply(conn, cmd)
        except Exception:  # noqa: BLE001
            pass
        finally:
            self.connected.clear()

    async def _reply(self, conn: ServerConnection, cmd: dict[str, Any]) -> None:
        cid, name, p = cmd["id"], cmd["cmd"], cmd.get("params", {})
        if name == "subscribe":
            channel = p["channels"][0]
            tickers = p.get("market_tickers", [])
            if self.market_limit is not None and len(tickers) > self.market_limit:
                await conn.send(json.dumps({"id": cid, "type": "error", "msg": {"code": 26, "msg": "limit"}}))
                return
            sid = self._next_sid
            self._next_sid += 1
            self.subs[sid] = {"channel": channel, "tickers": set(tickers)}
            await conn.send(json.dumps({"id": cid, "type": "subscribed", "msg": {"channel": channel, "sid": sid}}))
        elif name == "update_subscription":
            sid = p["sids"][0]
            sub = self.subs[sid]
            if p["action"] == "add_markets":
                if self.market_limit is not None and len(sub["tickers"]) + len(p["market_tickers"]) > self.market_limit:
                    await conn.send(json.dumps({"id": cid, "type": "error", "msg": {"code": 26, "msg": "limit"}}))
                    return
                sub["tickers"].update(p["market_tickers"])
            elif p["action"] == "delete_markets":
                sub["tickers"].difference_update(p["market_tickers"])
            # Like the real server: an ok reply carries the NEXT seq of that
            # subscription (docs example: {"id":123,"sid":456,"seq":222,"type":"ok"}).
            await conn.send(json.dumps({"id": cid, "sid": sid, "seq": self.next_seq(sid), "type": "ok",
                                        "msg": {"market_tickers": sorted(sub["tickers"])}}))
        elif name == "unsubscribe":
            for sid in p["sids"]:
                self.subs.pop(sid, None)
            await conn.send(json.dumps({"id": cid, "type": "unsubscribed", "sid": p["sids"][0]}))

    def next_seq(self, sid: int) -> int:
        """Consume and return the next sequence number of a subscription."""
        n = self.seqs.get(sid, 0) + 1
        self.seqs[sid] = n
        return n

    def sid_for(self, channel: str) -> int:
        return next(sid for sid, s in self.subs.items() if s["channel"] == channel)

    async def push(self, msg: dict[str, Any]) -> None:
        assert self._conn is not None
        await self._conn.send(json.dumps(msg))

    async def drop(self) -> None:
        assert self._conn is not None
        await self._conn.close()

    def cmds(self, name: str) -> list[dict[str, Any]]:
        return [c for c in self.commands if c["cmd"] == name]
