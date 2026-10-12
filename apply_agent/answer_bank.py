"""Answer bank: normalized question text -> approved answer.

Every question you approve once is answered for free on every later application.
Stored as a human-readable JSON file so you can review and edit it by hand.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel
from rapidfuzz import fuzz, process

Source = Literal["manual", "profile", "fuzzy", "small_model", "large_model", "fallback_agent"]

_STOPWORDS = {
    "a", "an", "the", "please", "you", "your", "are", "is", "do", "does", "will", "would",
    "of", "to", "for", "in", "on", "at", "this", "that", "with", "or", "and", "be", "by",
    "currently", "now", "company", "position", "role", "job", "employer",
}


def normalize(question: str) -> str:
    """Lowercase, drop punctuation, required-markers and filler so trivial rewordings collide."""
    q = question.lower().replace("*", " ")
    q = re.sub(r"\(required\)|\(optional\)", " ", q)
    q = re.sub(r"[^a-z0-9 ]+", " ", q)
    return " ".join(q.split())


def _content_tokens(question: str) -> str:
    return " ".join(t for t in normalize(question).split() if t not in _STOPWORDS)


# Intent rules collapse the dozens of phrasings of the most common screening questions
# onto one canonical key. Order matters: first match wins.
INTENT_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("sponsorship_future", re.compile(r"(sponsor|h-?1b|visa).*future|future.*(sponsor|h-?1b|visa)")),
    ("sponsorship", re.compile(r"sponsor|h-?1b|visa status|immigration (support|petition)")),
    ("work_authorization", re.compile(r"(legally )?(authori[sz]ed|eligible|permitted) to work|right to work|work permit")),
    ("relocation", re.compile(r"relocat")),
    ("salary", re.compile(r"salary|compensation expectation|desired pay|expected pay|pay expectation")),
    ("notice_period", re.compile(r"notice period|how soon can you start|earliest start|available to start|start date")),
    ("previous_employee", re.compile(r"(previously|ever) (been )?(employed|worked) (by|for|at)|former employee|worked here before")),
    ("age_18", re.compile(r"(at least|over) (the age of )?18|18 years")),
    ("non_compete", re.compile(r"non-?compete|restrictive covenant")),
    ("background_check", re.compile(r"background (check|screen)")),
    ("drug_test", re.compile(r"drug (test|screen)")),
    ("related_to_employee", re.compile(r"(related to|relative|family member).*(employ|work)")),
    ("government_official", re.compile(r"government official|public official")),
]


def intent_of(question: str) -> str | None:
    q = question.lower()
    for key, pattern in INTENT_RULES:
        if pattern.search(q):
            return key
    return None


class Entry(BaseModel):
    question: str  # original wording when first approved
    answer: str  # free text answer, or the option label chosen for closed questions
    options_seen: list[str] = []  # option list at approval time, for audit
    # "essay" = company-specific model draft: kept as a style example, never reused verbatim.
    # Change it to "text" by hand if an answer is generic enough to reuse everywhere.
    kind: Literal["text", "essay", "choice", "multichoice", "checkbox", "date"] = "choice"
    intent: str | None = None
    source: Source = "manual"
    approved: bool = True
    approved_at: str | None = None
    uses: int = 0


class Match(BaseModel):
    entry: Entry
    key: str
    tier: Literal["exact", "intent", "fuzzy"]
    score: float


class AnswerBank:
    FUZZY_THRESHOLD = 90  # token_set_ratio on stopword-stripped text

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.entries: dict[str, Entry] = {}
        if self.path.exists():
            raw = json.loads(self.path.read_text())
            self.entries = {k: Entry.model_validate(v) for k, v in raw.items()}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {k: v.model_dump() for k, v in sorted(self.entries.items())}
        self.path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")

    # ---- lookup -------------------------------------------------------------------

    def lookup(self, question: str, *, kind: str | None = None, approved_only: bool = True) -> Match | None:
        """Exact -> intent -> fuzzy. `kind` keeps a free-text answer from being reused for a
        dropdown with the same intent (and vice versa)."""
        pool = {
            k: e for k, e in self.entries.items()
            if (e.approved or not approved_only) and (kind is None or e.kind == kind)
        }
        key = normalize(question)
        if key in pool:
            return Match(entry=pool[key], key=key, tier="exact", score=100)

        intent = intent_of(question)
        if intent:
            for k, e in pool.items():
                if e.intent == intent:
                    return Match(entry=e, key=k, tier="intent", score=100)

        if not pool:
            return None
        choices = {k: _content_tokens(e.question) for k, e in pool.items()}
        best = process.extractOne(_content_tokens(question), choices, scorer=fuzz.token_set_ratio)
        if best and best[1] >= self.FUZZY_THRESHOLD:
            _, score, k = best
            return Match(entry=pool[k], key=k, tier="fuzzy", score=score)
        return None

    # ---- mutation -----------------------------------------------------------------

    def add(self, question: str, answer: str, *, kind: str = "choice", options: list[str] | None = None,
            source: Source = "manual", approved: bool = False) -> Entry:
        entry = Entry(
            question=question.strip(),
            answer=answer,
            options_seen=options or [],
            kind=kind,  # type: ignore[arg-type]
            intent=intent_of(question),
            source=source,
            approved=approved,
            approved_at=_now() if approved else None,
        )
        self.entries[normalize(question)] = entry
        return entry

    def approve(self, key: str, answer: str | None = None) -> Entry:
        entry = self.entries[key]
        if answer is not None:
            entry.answer = answer
        entry.approved = True
        entry.approved_at = _now()
        return entry

    def pending(self) -> dict[str, Entry]:
        return {k: e for k, e in self.entries.items() if not e.approved}

    def record_use(self, key: str) -> None:
        if key in self.entries:
            self.entries[key].uses += 1


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
