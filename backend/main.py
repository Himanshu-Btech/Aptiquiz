"""AptiQuiz API.

Run from the project root:

    uvicorn main:app --app-dir backend --host 0.0.0.0 --port 8000

Run a single worker: rooms live in this process's memory.
"""

import json
import logging
import os
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
from google import genai

import database as db
import models
import websocket as game


logging.basicConfig(level=logging.INFO)


FRONTEND = Path(__file__).resolve().parent.parent / "frontend"


# ---------------------------------------------------------
# AI Configuration
# ---------------------------------------------------------

GEMINI_API_KEY = os.getenv(
    "GEMINI_API_KEY",
    ""
).strip()


# ---------------------------------------------------------
# Application Startup
# ---------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(
    title="AptiQuiz",
    lifespan=lifespan
)


# ---------------------------------------------------------
# Rate Limiting
# ---------------------------------------------------------

_hits: dict[str, list[float]] = {}


def throttle(
    request: Request,
    limit: int = 15,
    window: int = 60
):
    ip = (
        request.client.host
        if request.client
        else "?"
    )

    now = time.time()

    hits = [
        t
        for t in _hits.get(ip, [])
        if now - t < window
    ]

    if len(hits) >= limit:
        raise HTTPException(
            429,
            "Too many attempts. Wait a minute and try again."
        )

    hits.append(now)

    _hits[ip] = hits


# ---------------------------------------------------------
# Authentication Helper
# ---------------------------------------------------------

def current_user(
    authorization: str
) -> int:

    token = authorization.removeprefix(
        "Bearer "
    ).strip()

    uid = db.user_for_token(
        token
    )

    if not uid:
        raise HTTPException(
            401,
            "Please sign in again."
        )

    return uid


# ---------------------------------------------------------
# Health Check
# ---------------------------------------------------------

@app.get("/health")
def health():

    return {
        "ok": True,
        "rooms": len(game.ROOMS)
    }


# ---------------------------------------------------------
# Authentication
# ---------------------------------------------------------

@app.post("/api/auth")
def auth(
    body: dict,
    request: Request
):

    throttle(request)

    mode = body.get("mode")

    username = str(
        body.get("username", "")
    ).strip().lower()

    password = str(
        body.get("password", "")
    )

    if (
        not re.fullmatch(
            r"[a-z0-9_]{3,24}",
            username
        )
        or len(password) < 6
        or len(password) > 128
    ):
        raise HTTPException(
            400,
            "Username: 3 to 24 letters, numbers or underscores. "
            "Password: 6 or more characters."
        )

    # -------------------------
    # Register
    # -------------------------

    if mode == "register":

        uid = db.create_user(
            username,
            password
        )

        if not uid:
            raise HTTPException(
                409,
                "That username is taken."
            )

    # -------------------------
    # Login
    # -------------------------

    else:

        uid = db.verify_user(
            username,
            password
        )

        if not uid:
            raise HTTPException(
                401,
                "Wrong username or password."
            )

    return {
        "token": db.create_token(uid),
        "username": username
    }


# ---------------------------------------------------------
# Quiz Sets
# ---------------------------------------------------------

@app.get("/api/sets")
def get_sets(
    authorization: str = Header(
        default=""
    )
):

    return db.list_sets(
        current_user(
            authorization
        )
    )


# ---------------------------------------------------------
# Save Quiz Set
# ---------------------------------------------------------

@app.post("/api/sets")
def save_set(
    body: dict,
    authorization: str = Header(
        default=""
    )
):

    uid = current_user(
        authorization
    )

    title = " ".join(
        str(
            body.get(
                "title",
                ""
            )
        ).split()
    )[:60]

    if not title:

        raise HTTPException(
            400,
            "Give the set a title."
        )

    try:

        questions = models.clean_questions(
            body.get("questions")
        )

    except ValueError as e:

        raise HTTPException(
            400,
            str(e)
        )

    set_id = db.add_set(
        uid,
        title,
        questions
    )

    if not set_id:

        raise HTTPException(
            400,
            "You can keep up to 50 sets. Delete one first."
        )

    return {
        "id": set_id
    }


# ---------------------------------------------------------
# Delete Quiz Set
# ---------------------------------------------------------

@app.delete("/api/sets/{set_id}")
def remove_set(
    set_id: int,
    authorization: str = Header(
        default=""
    )
):

    db.delete_set(
        current_user(
            authorization
        ),
        set_id
    )

    return {
        "ok": True
    }


# ---------------------------------------------------------
# AI Question Generation - Gemini
# ---------------------------------------------------------

@app.post("/api/ai/generate")
def generate_ai_questions(
    body: dict,
    authorization: str = Header(
        default=""
    )
):

    # Make sure the user is logged in.
    current_user(
        authorization
    )

    # -----------------------------------------------------
    # Check Gemini API key
    # -----------------------------------------------------

    if not GEMINI_API_KEY:

        raise HTTPException(
            503,
            "AI question generation is not configured. "
            "Add GEMINI_API_KEY to your .env file."
        )

    # -----------------------------------------------------
    # Read user input
    # -----------------------------------------------------

    topic = str(
        body.get(
            "topic",
            ""
        )
    ).strip()

    difficulty = str(
        body.get(
            "difficulty",
            "medium"
        )
    ).strip().lower()

    instructions = str(
        body.get(
            "instructions",
            ""
        )
    ).strip()

    # -----------------------------------------------------
    # Question count
    # -----------------------------------------------------

    try:

        count = int(
            body.get(
                "count",
                5
            )
        )

    except (
        TypeError,
        ValueError
    ):

        raise HTTPException(
            400,
            "Question count must be a number."
        )

    # -----------------------------------------------------
    # Validation
    # -----------------------------------------------------

    if not topic:

        raise HTTPException(
            400,
            "Please provide a topic."
        )

    if difficulty not in {
        "easy",
        "medium",
        "hard"
    }:

        raise HTTPException(
            400,
            "Difficulty must be easy, medium or hard."
        )

    if count < 1 or count > 20:

        raise HTTPException(
            400,
            "You can generate between 1 and 20 questions."
        )

    # -----------------------------------------------------
    # Gemini Prompt
    # -----------------------------------------------------

    prompt = f"""
You are an expert aptitude quiz question generator
for a competitive quiz platform called AptiQuiz.

Generate exactly {count} multiple-choice aptitude questions.

Topic:
{topic}

Difficulty:
{difficulty}

Additional instructions:
{instructions if instructions else "None"}

Every question MUST have exactly four answer options.

Return ONLY valid JSON using exactly this structure:

{{
  "questions": [
    {{
      "text": "Question text",
      "options": [
        "Option A",
        "Option B",
        "Option C",
        "Option D"
      ],
      "correct": 0,
      "time": 30,
      "points": 10
    }}
  ]
}}

Rules:

- "text" must contain the question.
- "options" must contain exactly four strings.
- "correct" must be a zero-based index from 0 to 3.
- There must be exactly one correct answer.
- Questions must be clear and unambiguous.
- Do not include explanations.
- Do not include markdown.
- Do not include numbering outside the JSON.
- "time" should normally be 30.
- "points" should normally be 10.
"""

       # -----------------------------------------------------
    # Call Gemini
    # -----------------------------------------------------

    try:

        client = genai.Client(
            api_key=GEMINI_API_KEY
        )

        max_attempts = 3
        response = None

        for attempt in range(1, max_attempts + 1):

            try:

                logging.info(
                    f"Gemini request attempt {attempt}/{max_attempts}"
                )

                response = client.models.generate_content(
                    model="gemini-3.8-flash",
                    contents=prompt,
                    config={
                        "response_mime_type": "application/json",
                    },
                )

                break

            except Exception as e:

                error_text = str(e)

                logging.warning(
                    f"Gemini attempt {attempt} failed: {error_text}"
                )

                # Retry temporary server errors.
                if (
                    "503" in error_text
                    or "UNAVAILABLE" in error_text
                ) and attempt < max_attempts:

                    wait_time = attempt * 3

                    logging.info(
                        f"Gemini temporarily unavailable. "
                        f"Retrying in {wait_time} seconds..."
                    )

                    time.sleep(wait_time)

                    continue

                raise

    except Exception as e:

        logging.exception(
            "Gemini AI question generation failed"
        )

        if (
            "503" in str(e)
            or "UNAVAILABLE" in str(e)
        ):

            raise HTTPException(
                503,
                "Gemini AI is temporarily busy. "
                "Please try again in a few moments."
            )

        raise HTTPException(
            502,
            f"AI generation failed: {str(e)}"
        )

    # -----------------------------------------------------
    # Parse Gemini Response
    # -----------------------------------------------------

    try:

        content = response.text

        data = json.loads(
            content
        )

        questions = data.get(
            "questions"
        )

        if not isinstance(
            questions,
            list
        ):

            raise ValueError(
                "Gemini did not return a question list."
            )

        cleaned_questions = models.clean_questions(
            questions
        )

    except Exception:

        logging.exception(
            "Invalid Gemini response"
        )

        raise HTTPException(
            502,
            "AI returned an invalid question format. "
            "Please try again."
        )

    # -----------------------------------------------------
    # Return Questions
    # -----------------------------------------------------

    return {
        "questions": cleaned_questions,
        "count": len(
            cleaned_questions
        )
    }


# ---------------------------------------------------------
# WebSocket Multiplayer Game
# ---------------------------------------------------------

@app.websocket("/ws")
async def ws_endpoint(
    ws: WebSocket
):

    await game.handle(
        ws
    )


# ---------------------------------------------------------
# Frontend
# ---------------------------------------------------------

# Mounted last so API routes above win.
app.mount(
    "/",
    StaticFiles(
        directory=FRONTEND,
        html=True
    ),
    name="frontend"
)