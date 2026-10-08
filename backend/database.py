"""SQLite storage: host accounts, saved question sets, and the season league."""
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager

_DEFAULT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "aptiquiz.db")
DB_PATH = os.environ.get("DATABASE_PATH", _DEFAULT)
TOKEN_TTL = 30 * 24 * 3600
_lock = threading.Lock()


@contextmanager
def conn():
    with _lock:
        c = sqlite3.connect(DB_PATH)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()


def init_db():
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    with conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS users(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                salt TEXT NOT NULL,
                pw_hash TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tokens(
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                expires REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS question_sets(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS league_players(name TEXT PRIMARY KEY, score INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS league_colleges(college TEXT PRIMARY KEY, score INTEGER NOT NULL);
            """
        )


def _hash(password: str, salt: bytes) -> str:
    return hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, dklen=32).hex()


# ---- accounts
def create_user(username: str, password: str):
    salt = secrets.token_bytes(16)
    try:
        with conn() as c:
            cur = c.execute(
                "INSERT INTO users(username, salt, pw_hash) VALUES(?,?,?)",
                (username, salt.hex(), _hash(password, salt)),
            )
            return cur.lastrowid
    except sqlite3.IntegrityError:
        return None


def verify_user(username: str, password: str):
    with conn() as c:
        row = c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    if not row:
        return None
    ok = hmac.compare_digest(_hash(password, bytes.fromhex(row["salt"])), row["pw_hash"])
    return row["id"] if ok else None


def create_token(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    with conn() as c:
        c.execute("DELETE FROM tokens WHERE expires < ?", (time.time(),))
        c.execute("INSERT INTO tokens VALUES(?,?,?)", (token, user_id, time.time() + TOKEN_TTL))
    return token


def user_for_token(token: str):
    if not token:
        return None
    with conn() as c:
        row = c.execute(
            "SELECT user_id FROM tokens WHERE token=? AND expires > ?", (token, time.time())
        ).fetchone()
    return row["user_id"] if row else None


# ---- question sets
def list_sets(user_id: int):
    with conn() as c:
        rows = c.execute(
            "SELECT id, title, data FROM question_sets WHERE user_id=? ORDER BY id DESC", (user_id,)
        ).fetchall()
    return [{"id": r["id"], "title": r["title"], "questions": json.loads(r["data"])} for r in rows]


def add_set(user_id: int, title: str, questions: list):
    with conn() as c:
        if c.execute("SELECT COUNT(*) FROM question_sets WHERE user_id=?", (user_id,)).fetchone()[0] >= 50:
            return None
        cur = c.execute(
            "INSERT INTO question_sets(user_id, title, data) VALUES(?,?,?)",
            (user_id, title, json.dumps(questions)),
        )
        return cur.lastrowid


def delete_set(user_id: int, set_id: int):
    with conn() as c:
        c.execute("DELETE FROM question_sets WHERE id=? AND user_id=?", (set_id, user_id))


# ---- league
def add_league(results):
    """results: iterable of (name, college, score)."""
    with conn() as c:
        for name, college, score in results:
            c.execute(
                "INSERT INTO league_players VALUES(?,?) ON CONFLICT(name) DO UPDATE SET score=score+excluded.score",
                (name, score),
            )
            if college:
                c.execute(
                    "INSERT INTO league_colleges VALUES(?,?) ON CONFLICT(college) DO UPDATE SET score=score+excluded.score",
                    (college, score),
                )


def league_ranks(name: str, college: str):
    with conn() as c:
        me = c.execute("SELECT score FROM league_players WHERE name=?", (name,)).fetchone()
        season = me["score"] if me else 0
        p_rank = c.execute("SELECT COUNT(*) FROM league_players WHERE score > ?", (season,)).fetchone()[0] + 1
        c_rank = None
        if college:
            row = c.execute("SELECT score FROM league_colleges WHERE college=?", (college,)).fetchone()
            cs = row["score"] if row else 0
            c_rank = c.execute("SELECT COUNT(*) FROM league_colleges WHERE score > ?", (cs,)).fetchone()[0] + 1
    return {"season": season, "player_rank": p_rank, "college": college or None, "college_rank": c_rank}
