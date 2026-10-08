"""Validation for question sets. Everything a host sends is cleaned here before use."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator, model_validator


class QuestionIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    text: str = Field(min_length=1, max_length=300)
    options: list[str] = Field(min_length=2, max_length=4)
    correct: int
    topic: str = Field(default="General", max_length=40)
    difficulty: Literal["Easy", "Medium", "Hard"] = "Medium"
    time: int = Field(default=20, ge=5, le=120)

    @field_validator("options")
    @classmethod
    def options_ok(cls, v):
        v = [o.strip() for o in v]
        if any(not o or len(o) > 120 for o in v):
            raise ValueError("each option must be 1 to 120 characters")
        return v

    @field_validator("topic")
    @classmethod
    def topic_default(cls, v):
        return v or "General"

    @model_validator(mode="after")
    def correct_in_range(self):
        if not 0 <= self.correct < len(self.options):
            raise ValueError("correct answer must point at one of the options")
        return self


_adapter = TypeAdapter(list[QuestionIn])


def clean_questions(raw) -> list[dict]:
    """Return validated questions as plain dicts, or raise ValueError with a readable message."""
    if not isinstance(raw, list) or not raw:
        raise ValueError("Add at least one question.")
    if len(raw) > 100:
        raise ValueError("A set can hold at most 100 questions.")
    try:
        return [q.model_dump() for q in _adapter.validate_python(raw)]
    except ValidationError as e:
        err = e.errors()[0]
        loc = err.get("loc", ())
        where = f"Question {loc[0] + 1}: " if loc and isinstance(loc[0], int) else ""
        msg = str(err.get("msg", "invalid")).replace("Value error, ", "")
        raise ValueError(f"{where}{msg}")
