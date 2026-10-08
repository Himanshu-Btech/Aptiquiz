"""Scoring, ranking and per-player stats. Pure functions, no I/O."""


def points(correct: bool, elapsed_ms: int, limit_s: int) -> int:
    """500 for a correct answer plus up to 500 for speed. Wrong or late answers score 0."""
    if not correct:
        return 0
    frac = min(max(elapsed_ms / (limit_s * 1000), 0.0), 1.0)
    return 500 + round(500 * (1 - frac))


def build_board(players) -> list[dict]:
    """Sorted leaderboard. Equal scores share a rank."""
    rows = sorted(players, key=lambda p: (-p.score, p.name.lower()))
    scores = [p.score for p in rows]
    return [
        {
            "pid": p.pid,
            "name": p.name,
            "college": p.college,
            "score": p.score,
            "rank": 1 + sum(1 for s in scores if s > p.score),
        }
        for p in rows
    ]


def public_rows(board: list[dict], limit: int) -> list[dict]:
    return [{k: v for k, v in row.items() if k != "pid"} for row in board[:limit]]


def player_stats(log: list[dict]) -> dict:
    """log items: {topic, correct, ms}. Returns accuracy, average seconds on correct answers, topic split."""
    total = len(log)
    right = [x for x in log if x["correct"]]
    topics: dict[str, dict] = {}
    for x in log:
        t = topics.setdefault(x["topic"], {"right": 0, "total": 0})
        t["total"] += 1
        t["right"] += 1 if x["correct"] else 0
    return {
        "accuracy": round(100 * len(right) / total) if total else 0,
        "avg_seconds": round(sum(x["ms"] for x in right) / len(right) / 1000, 1) if right else None,
        "topics": topics,
    }
