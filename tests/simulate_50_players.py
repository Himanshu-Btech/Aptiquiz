"""Plays a full game with 50 simulated players against a running server and checks the rules.

Start the server, then:
    python tests/simulate_50_players.py
    python tests/simulate_50_players.py --http http://localhost:8000 --ws ws://localhost:8000/ws
"""
import argparse
import asyncio
import json
import random
import sys
import time
import urllib.error
import urllib.request

import websockets

QUESTIONS = [
    {"text": "15% of 240?", "options": ["24", "30", "36", "48"], "correct": 2, "topic": "Percentages", "difficulty": "Easy", "time": 10},
    {"text": "Next: 2, 6, 12, 20, ?", "options": ["28", "30", "32", "36"], "correct": 1, "topic": "Series", "difficulty": "Medium", "time": 10},
    {"text": "SI on 5000 at 8% for 3 years?", "options": ["1000", "1200", "1500", "2400"], "correct": 1, "topic": "Interest", "difficulty": "Easy", "time": 10},
]
N = 50
failures: list[str] = []


def check(ok: bool, msg: str):
    print(("PASS  " if ok else "FAIL  ") + msg)
    if not ok:
        failures.append(msg)


def post_json(url: str, body: dict) -> dict:
    req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


async def player(i: int, ws_url: str, code: str, rec: dict, joined: asyncio.Event):
    name = f"P{i:02d}"
    rec["name"] = name
    rec.update(orders=[], errors=[], leaks=0, bad_reveals=0, final=None, joined=False)
    skill = random.uniform(0.3, 0.95)
    async with websockets.connect(ws_url) as ws:
        await ws.send(json.dumps({"type": "join", "code": code, "name": name, "college": f"College {i % 5}"}))
        opts, right, picked_right = [], "", False
        async for raw in ws:
            m = json.loads(raw)
            t = m["type"]
            if t == "joined":
                rec["joined"] = True
                joined.set() if all_joined(rec) else None
            elif t == "error":
                rec["errors"].append(m["message"])
            elif t == "question":
                if "correct" in m:
                    rec["leaks"] += 1
                opts = m["options"]
                rec["orders"].append(tuple(opts))
                q = QUESTIONS[m["index"]]
                right = q["options"][q["correct"]]
                if random.random() < skill:
                    choice = opts.index(right)
                else:
                    choice = random.choice([k for k, o in enumerate(opts) if o != right])
                picked_right = opts[choice] == right
                await asyncio.sleep(random.uniform(0.05, 1.5))
                await ws.send(json.dumps({"type": "answer", "choice": choice}))
            elif t == "reveal":
                if opts[m["correct"]] != right or (m["gained"] > 0) != picked_right:
                    rec["bad_reveals"] += 1
            elif t == "final":
                rec["final"] = m
                return


_recs: list[dict] = []


def all_joined(_rec) -> bool:
    return sum(1 for r in _recs if r.get("joined")) >= N


async def main(a):
    t0 = time.time()
    username = f"sim{random.randint(10**6, 10**7)}"
    auth = await asyncio.to_thread(post_json, a.http + "/api/auth", {"mode": "register", "username": username, "password": "simulate123"})

    host = await websockets.connect(a.ws)
    await host.send(json.dumps({"type": "host_create", "token": auth["token"], "questions": QUESTIONS}))
    created = json.loads(await host.recv())
    check(created["type"] == "created", f"room created ({created.get('code')})")
    code = created["code"]

    joined = asyncio.Event()
    tasks = []
    for i in range(N):
        rec: dict = {}
        _recs.append(rec)
        tasks.append(asyncio.create_task(player(i, a.ws, code, rec, joined)))
    try:
        await asyncio.wait_for(joined.wait(), 20)
    except asyncio.TimeoutError:
        pass
    check(all_joined(None), f"{N} players joined")

    extra = await websockets.connect(a.ws)
    await extra.send(json.dumps({"type": "join", "code": code, "name": "Extra", "college": ""}))
    resp = json.loads(await extra.recv())
    check(resp["type"] == "error" and "full" in resp["message"].lower(), "51st player is rejected")
    await extra.close()

    await host.send(json.dumps({"type": "start"}))
    host_final = None
    reveals = 0
    async with asyncio.timeout(120):
        async for raw in host:
            m = json.loads(raw)
            if m["type"] == "reveal":
                reveals += 1
                await asyncio.sleep(0.2)
                await host.send(json.dumps({"type": "next"}))
            elif m["type"] == "final":
                host_final = m
                break
        await asyncio.gather(*tasks)

    check(reveals == len(QUESTIONS), f"{reveals} rounds revealed")
    check(host_final is not None and len(host_final["board"]) == 20, "host received top-20 board")
    scores = [b["score"] for b in host_final["board"]]
    check(scores == sorted(scores, reverse=True), "host board sorted by score")

    check(all(r["final"] for r in _recs), "all 50 players received final results")
    check(sum(r["leaks"] for r in _recs) == 0, "answer never sent with a question")
    check(sum(r["bad_reveals"] for r in _recs) == 0, "reveals match each player's own option order")
    for qi in range(len(QUESTIONS)):
        distinct = len({r["orders"][qi] for r in _recs})
        check(distinct > 1, f"question {qi + 1}: options shuffled per player ({distinct} distinct orders)")

    finals = [r["final"] for r in _recs]
    ok_rank = all(f["rank"] == 1 + sum(1 for g in finals if g["score"] > f["score"]) for f in finals)
    check(ok_rank, "ranks consistent with scores")
    check(all(0 <= f["accuracy"] <= 100 and f["league"]["player_rank"] >= 1 for f in finals), "stats and league fields present")

    print(f"\nDone in {time.time() - t0:.1f}s. Failures: {len(failures)}")
    await host.close()
    return 1 if failures else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--http", default="http://localhost:8000")
    ap.add_argument("--ws", default="ws://localhost:8000/ws")
    sys.exit(asyncio.run(main(ap.parse_args())))
