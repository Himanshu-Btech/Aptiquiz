"""AptiQuiz API. Run from the project root:
    uvicorn main:app --app-dir backend --host 0.0.0.0 --port 8000
Run a single worker: rooms live in this process's memory.
"""
import logging
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from fastapi import FastAPI, Header, HTTPException, Request, WebSocket
from fastapi.staticfiles import StaticFiles

import database as db
import models
import websocket as game

logging.basicConfig(level=logging.INFO)
FRONTEND = Path(__file__).resolve().parent.parent / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="AptiQuiz", lifespan=lifespan)

_hits: dict[str, list[float]] = {}


def throttle(request: Request, limit: int = 15, window: int = 60):
    ip = request.client.host if request.client else "?"
    now = time.time()
    hits = [t for t in _hits.get(ip, []) if now - t < window]
    if len(hits) >= limit:
        raise HTTPException(429, "Too many attempts. Wait a minute and try again.")
    hits.append(now)
    _hits[ip] = hits


def current_user(authorization: str) -> int:
    token = authorization.removeprefix("Bearer ").strip()
    uid = db.user_for_token(token)
    if not uid:
        raise HTTPException(401, "Please sign in again.")
    return uid


@app.get("/health")
def health():
    return {"ok": True, "rooms": len(game.ROOMS)}


@app.post("/api/auth")
def auth(body: dict, request: Request):
    throttle(request)
    mode = body.get("mode")
    username = str(body.get("username", "")).strip().lower()
    password = str(body.get("password", ""))
    if not re.fullmatch(r"[a-z0-9_]{3,24}", username) or len(password) < 6 or len(password) > 128:
        raise HTTPException(400, "Username: 3 to 24 letters, numbers or underscores. Password: 6 or more characters.")
    if mode == "register":
        uid = db.create_user(username, password)
        if not uid:
            raise HTTPException(409, "That username is taken.")
    else:
        uid = db.verify_user(username, password)
        if not uid:
            raise HTTPException(401, "Wrong username or password.")
    return {"token": db.create_token(uid), "username": username}


@app.get("/api/sets")
def get_sets(authorization: str = Header(default="")):
    return db.list_sets(current_user(authorization))


@app.post("/api/sets")
def save_set(body: dict, authorization: str = Header(default="")):
    uid = current_user(authorization)
    title = " ".join(str(body.get("title", "")).split())[:60]
    if not title:
        raise HTTPException(400, "Give the set a title.")
    try:
        questions = models.clean_questions(body.get("questions"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    set_id = db.add_set(uid, title, questions)
    if not set_id:
        raise HTTPException(400, "You can keep up to 50 sets. Delete one first.")
    return {"id": set_id}


@app.delete("/api/sets/{set_id}")
def remove_set(set_id: int, authorization: str = Header(default="")):
    db.delete_set(current_user(authorization), set_id)
    return {"ok": True}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await game.handle(ws)


# Mounted last so the API routes above win.
app.mount("/", StaticFiles(directory=FRONTEND, html=True), name="frontend")
