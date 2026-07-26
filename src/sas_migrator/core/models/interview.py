"""Interview models — questions, answers, blocks for structured interviews."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class QuestionType(str, Enum):
    FREE_TEXT = "free_text"
    SINGLE_CHOICE = "single_choice"
    MULTI_CHOICE = "multi_choice"
    YES_NO = "yes_no"
    APPROVAL = "approval"  # for improvement cards: approve / reject / postpone


class Question(BaseModel):
    """A single interview question."""

    id: str  # Q-001, Q-002, ...
    text: str
    question_type: QuestionType = QuestionType.FREE_TEXT
    required: bool = True
    options: list[str] = Field(default_factory=list)
    source_finding: str = ""  # reference to the finding that triggered this question
    context: str = ""  # additional context shown to the user


class Answer(BaseModel):
    """User's answer to a question."""

    question_id: str
    value: str
    answered_at: datetime = Field(default_factory=datetime.utcnow)


class QuestionBlock(BaseModel):
    """A thematic group of questions within an interview."""

    block_id: str
    title: str
    description: str = ""
    questions: list[Question] = Field(default_factory=list)
    answers: list[Answer] = Field(default_factory=list)

    @property
    def required_answered(self) -> bool:
        required_ids = {q.id for q in self.questions if q.required}
        answered_ids = {a.question_id for a in self.answers}
        return required_ids.issubset(answered_ids)


class InterviewQA(BaseModel):
    """Complete interview (initial or post-analysis)."""

    interview_type: str  # "initial" | "post_analysis"
    phase: int  # 1 or 4
    blocks: list[QuestionBlock] = Field(default_factory=list)
    started_at: datetime | None = None
    completed_at: datetime | None = None

    @property
    def is_complete(self) -> bool:
        return all(b.required_answered for b in self.blocks)
