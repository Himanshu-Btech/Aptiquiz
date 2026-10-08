"""Real-time game engine. The server owns option order, answers, timing and scores.

Clients can drop and reconnect (every page of the frontend opens its own socket),
so each player and the host keep a snapshot of the last message that defines their screen.
"""
import asyncio
import json
import logging
import math
import os
import random
import secrets
import time
from collections import deque

from fastapi import WebSocket, WebSocketDisconnect

import database as db
import models
import scoring

log = logging.getLogger("aptiquiz")
MAX_PLAYERS = int(os.environ.get("MAX_PLAYERS", 50))
HOST_GRACE = int(os.environ.get("HOST_GRACE_SECONDS", 600))
LOBBY_GRACE = 20
CODE_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
ROOMS: dict[str, "Room"] = {}


async def send(ws, data):
    if ws is None:
        return
    try:
        await ws.send_json(data)
    except Exception:
        pass  # a dead socket is handled by its disconnect path


def new_code() -> str:
    while True:
        code = "".join(random.choice(CODE_CHARS) for _ in range(5))
        if code not in ROOMS:
            return code


class Player:
    def __init__(self, name: str, college: str):
        self.pid = secrets.token_urlsafe(12)
        self.name = name
        self.college = college
        self.score = 0
        self.log: list[dict] = []
        self.perm: list[int] = []
        self.current = None
        self.ws = None
        self.snap = None


class Room:
    def __init__(self, questions: list[dict]):
        self.code = new_code()
        self.host_key = secrets.token_urlsafe(16)
        self.questions = questions
        self.players: dict[str, Player] = {}
        self.state = "lobby"  # lobby | question | reveal | done
        self.index = -1
        self.started = 0.0
        self.answered = 0
        self.host_ws = None
        self.host_snap = None
        self.lock = asyncio.Lock()
        self.round_task = None
        self.close_task = None

    # ---- helpers
    def roster(self):
        return [p.name for p in self.players.values()]

    def remaining(self) -> int:
        q = self.questions[self.index]
        return max(1, math.ceil(q["time"] - (time.monotonic() - self.started)))

    async def to_player(self, p: Player, data: dict):
        p.snap = data
        await send(p.ws, data)

    async def to_host(self, data: dict):
        self.host_snap = data
        await send(self.host_ws, data)

    async def broadcast_lobby(self):
        names = self.roster()
        await self.to_host({"type": "lobby", "players": names, "max": MAX_PLAYERS})
        for p in list(self.players.values()):
            await self.to_player(p, {"type": "lobby", "players": names, "max": MAX_PLAYERS})

    # ---- lifecycle
    async def start(self):
        async with self.lock:
            if self.state != "lobby" or not self.players:
                return
            self.index = 0
            await self._send_question()

    async def next(self):
        async with self.lock:
            if self.state != "reveal":
                return
            self.index += 1
            if self.index >= len(self.questions):
                await self._finish()
            else:
                await self._send_question()

    async def _send_question(self):
        q = self.questions[self.index]
        self.state = "question"
        self.started = time.monotonic()
        self.answered = 0
        base = {
            "type": "question", "index": self.index, "total": len(self.questions),
            "text": q["text"], "topic": q["topic"], "difficulty": q["difficulty"], "time": q["time"],
        }
        for p in list(self.players.values()):
            p.perm = random.sample(range(len(q["options"])), len(q["options"]))  # unique order per player
            p.current = None
            await self.to_player(p, {**base, "options": [q["options"][i] for i in p.perm]})
        await self.to_host({**base, "options": q["options"], "players": len(self.players), "answered": 0})
        self.round_task = asyncio.create_task(self._timer(self.index, q["time"] + 0.4))

    async def _timer(self, idx: int, seconds: float):
        await asyncio.sleep(seconds)
        async with self.lock:
            if self.index == idx:
                await self._end_round()

    async def answer(self, p: Player, choice):
        async with self.lock:
            if self.state != "question" or p.current:
                return
            q = self.questions[self.index]
            ms = int((time.monotonic() - self.started) * 1000)
            if ms > q["time"] * 1000 + 300:
                return  # late
            if isinstance(choice, bool) or not isinstance(choice, int) or not 0 <= choice < len(p.perm):
                return
            orig = p.perm[choice]
            correct = orig == q["correct"]
            pts = scoring.points(correct, ms, q["time"])
            p.current = {"choice": choice, "orig": orig, "correct": correct, "points": pts, "ms": ms}
            p.score += pts
            self.answered += 1
            await send(p.ws, {"type": "locked"})
            await send(self.host_ws, {"type": "progress", "answered": self.answered, "total": len(self.players)})
            if all(x.current for x in self.players.values() if x.ws):
                await self._end_round()

    async def _end_round(self):
        if self.state != "question":
            return
        self.state = "reveal"
        if self.round_task and self.round_task is not asyncio.current_task():
            self.round_task.cancel()
        q = self.questions[self.index]
        counts = [0] * len(q["options"])
        for p in self.players.values():
            a = p.current
            if a:
                counts[a["orig"]] += 1
            p.log.append({"topic": q["topic"], "correct": bool(a and a["correct"]),
                          "ms": a["ms"] if a else q["time"] * 1000})
        board = scoring.build_board(self.players.values())
        by_pid = {b["pid"]: b for b in board}
        for p in list(self.players.values()):
            a = p.current
            await self.to_player(p, {
                "type": "reveal",
                "correct": p.perm.index(q["correct"]),  # index within THIS player's order
                "picked": a["choice"] if a else None,
                "gained": a["points"] if a else 0,
                "score": p.score, "rank": by_pid[p.pid]["rank"], "of": len(board),
            })
        await self.to_host({
            "type": "reveal", "answer": q["correct"], "options": q["options"], "counts": counts,
            "board": scoring.public_rows(board, 10), "last": self.index == len(self.questions) - 1,
        })

    async def _finish(self):
        self.state = "done"
        board = scoring.build_board(self.players.values())
        by_pid = {b["pid"]: b for b in board}
        db.add_league([(p.name, p.college, p.score) for p in self.players.values()])
        for p in list(self.players.values()):
            await self.to_player(p, {
                "type": "final", "score": p.score, "rank": by_pid[p.pid]["rank"], "of": len(board),
                **scoring.player_stats(p.log), "league": db.league_ranks(p.name, p.college),
            })
        await self.to_host({"type": "final", "board": scoring.public_rows(board, 20)})
        asyncio.create_task(self._expire())

    async def _expire(self):
        await asyncio.sleep(1800)
        await self.close()

    async def _host_gone(self):
        await asyncio.sleep(HOST_GRACE)
        if self.host_ws is None:
            await self.close()

    async def _drop_if_gone(self, p: Player):
        await asyncio.sleep(LOBBY_GRACE)
        if p.ws is None and self.state == "lobby" and self.players.get(p.pid) is p:
            del self.players[p.pid]
            await self.broadcast_lobby()

    async def close(self):
        me = asyncio.current_task()
        for t in (self.round_task, self.close_task):
            if t and t is not me:
                t.cancel()
        for p in list(self.players.values()):
            await send(p.ws, {"type": "closed"})
        ROOMS.pop(self.code, None)

    async def kick(self, name: str):
        if self.state != "lobby":
            return
        for pid, p in list(self.players.items()):
            if p.name == name:
                del self.players[pid]
                await send(p.ws, {"type": "kicked"})
        await self.broadcast_lobby()


# ---- connection handling
class Ctx:
    def __init__(self):
        self.room = None
        self.player = None
        self.host = False


async def replay(room: Room, ws, host: bool, p):
    """Send the snapshot that defines this client's current screen."""
    snap = room.host_snap if host else (p.snap if p else None)
    if snap is None or snap["type"] == "lobby":
        snap = {"type": "lobby", "players": room.roster(), "max": MAX_PLAYERS}
    elif snap["type"] == "question":
        snap = {**snap, "time": room.remaining()}
        if host:
            snap["answered"] = room.answered
    await send(ws, snap)
    if not host and snap["type"] == "question" and p.current:
        await send(ws, {"type": "locked"})


async def dispatch(ws, ctx: Ctx, msg: dict):
    t = msg.get("type")

    if t == "host_create":
        if not db.user_for_token(str(msg.get("token", ""))):
            return await send(ws, {"type": "error", "message": "Please sign in again."})
        try:
            questions = models.clean_questions(msg.get("questions"))
        except ValueError as e:
            return await send(ws, {"type": "error", "message": str(e)})
        room = Room(questions)
        ROOMS[room.code] = room
        room.host_ws = ws
        ctx.room, ctx.host = room, True
        return await send(ws, {"type": "created", "code": room.code, "host_key": room.host_key})

    if t == "join":
        code = str(msg.get("code", "")).strip().upper()
        name = " ".join(str(msg.get("name", "")).split())[:20]
        college = " ".join(str(msg.get("college", "")).split())[:40]
        room = ROOMS.get(code)
        err = None
        if not room:
            err = "No room with that code."
        elif room.state != "lobby":
            err = "That game has already started."
        elif not name:
            err = "Enter a name."
        elif len(room.players) >= MAX_PLAYERS:
            err = f"Room is full ({MAX_PLAYERS} players)."
        elif any(x.name.lower() == name.lower() for x in room.players.values()):
            err = "That name is taken in this room."
        if err:
            return await send(ws, {"type": "error", "message": err})
        p = Player(name, college)
        p.ws = ws
        room.players[p.pid] = p
        ctx.room, ctx.player = room, p
        await send(ws, {"type": "joined", "code": room.code, "pid": p.pid})
        return await room.broadcast_lobby()

    if t == "rejoin":
        room = ROOMS.get(str(msg.get("code", "")).upper())
        key = str(msg.get("key", ""))
        if not room:
            return await send(ws, {"type": "error", "fatal": True, "message": "This game has ended."})
        if msg.get("role") == "host" and secrets.compare_digest(key, room.host_key):
            room.host_ws = ws
            if room.close_task:
                room.close_task.cancel()
                room.close_task = None
            ctx.room, ctx.host = room, True
            await send(ws, {"type": "rejoined", "role": "host", "code": room.code})
            return await replay(room, ws, True, None)
        p = room.players.get(key)
        if msg.get("role") == "player" and p:
            p.ws = ws
            ctx.room, ctx.player = room, p
            await send(ws, {"type": "rejoined", "role": "player", "code": room.code, "name": p.name})
            return await replay(room, ws, False, p)
        return await send(ws, {"type": "error", "fatal": True, "message": "Could not rejoin this game."})

    room = ctx.room
    if not room:
        return
    if ctx.host:
        if t == "start":
            await room.start()
        elif t == "next":
            await room.next()
        elif t == "kick":
            await room.kick(str(msg.get("name", "")))
    elif ctx.player and t == "answer":
        await room.answer(ctx.player, msg.get("choice"))


async def on_disconnect(ctx: Ctx, ws):
    room = ctx.room
    if not room:
        return
    if ctx.host:
        if room.host_ws is ws:
            room.host_ws = None
            room.close_task = asyncio.create_task(room._host_gone())
    elif ctx.player and ctx.player.ws is ws:
        ctx.player.ws = None
        if room.state == "lobby":
            asyncio.create_task(room._drop_if_gone(ctx.player))


async def handle(ws: WebSocket):
    await ws.accept()
    ctx = Ctx()
    recent = deque(maxlen=40)
    try:
        while True:
            raw = await ws.receive_text()
            now = time.monotonic()
            recent.append(now)
            if len(recent) == recent.maxlen and now - recent[0] < 2:
                continue  # flood guard
            if len(raw) > 200_000:
                continue
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if isinstance(msg, dict):
                await dispatch(ws, ctx, msg)
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("websocket error")
    finally:
        await on_disconnect(ctx, ws)
