# AptiQuiz

Live multiplayer aptitude quiz. FastAPI + WebSockets backend, plain HTML/JS frontend, SQLite storage.
The server owns everything that matters: option order, correct answers, timing and scores.

## Run locally (Python 3.11+)

```
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # optional
uvicorn main:app --app-dir backend --host 0.0.0.0 --port 8000
```

Open http://localhost:8000. Hosts go to **Host a game**, create an account, build or load a question set, and create a room. Students open the same address (use your computer's LAN IP on shared Wi-Fi) and join with the 5-character code.

## Test with 50 players

With the server running:

```
python tests/simulate_50_players.py
```

It plays a full game with 50 bots and checks the cap, per-player shuffling, answer hiding, reveals, ranks and league fields. It exits with code 1 if any check fails.

## Layout

| File | Job |
| --- | --- |
| backend/main.py | App, REST (auth, saved sets), static frontend, `/ws` route |
| backend/websocket.py | Game engine: rooms, lobby, rounds, reveal, reconnects |
| backend/scoring.py | Points (500 + up to 500 for speed), ranks, player stats |
| backend/models.py | Validation of question sets |
| backend/database.py | SQLite: accounts, saved sets, league |
| frontend/*.html | index (join), host, lobby, quiz, leaderboard |

Each page opens its own socket and rejoins from `sessionStorage`; the server replays the right screen, so refreshes and locked phones recover mid-game.

## Deploy from a Git repo

Push the project to GitHub, then create a Web Service on Render or Railway:

- Build command: `pip install -r requirements.txt`
- Start command: `uvicorn main:app --app-dir backend --host 0.0.0.0 --port $PORT`
- Health check path: `/health`

Rules: run **one** instance (rooms live in memory), and attach a persistent disk with `DATABASE_PATH` pointing at it, otherwise accounts and league scores reset on every deploy. Free tiers sleep when idle, so open the site a few minutes before class. A restart ends any running game.

## WebSocket messages

Client to server: `host_create`, `join`, `rejoin`, `start`, `answer`, `next`, `kick`.
Server to client: `created`, `joined`, `rejoined`, `lobby`, `question`, `locked`, `progress`, `reveal`, `final`, `kicked`, `closed`, `error`.
