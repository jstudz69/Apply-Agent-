"""Question resolver, tiered by cost. Only tiers 3 and 4 ever call a model.

  0. profile  - questions the canonical profile answers directly (sponsorship, EEO, ...)
  1. exact    - normalized text is already in the answer bank
  2. fuzzy    - intent rule or fuzzy match against the bank
  3. small    - unmatched closed question -> cheap model constrained to the option list
  4. large    - open-ended text -> stronger model with the job description; always reviewed

Every model answer is written to the bank as *pending*; approve it once and it becomes
tier 1 forever.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from rapidfuzz import fuzz, process

from .answer_bank import AnswerBank, intent_of, normalize
from .llm import DECLINE, Llm
from .profile import Profile

Kind = Literal["text", "textarea", "choice", "multichoice", "checkbox", "date"]
Tier = Literal["profile", "exact", "intent", "fuzzy", "small_model", "large_model", "human"]


@dataclass
class Question:
    label: str
    kind: Kind
    options: list[str] = field(default_factory=list)
    required: bool = False
    max_chars: int | None = None
    ref: str = ""  # driver-specific handle (e.g. data-automation-id or element id)

    @property
    def closed(self) -> bool:
        return self.kind in ("choice", "multichoice", "checkbox")


@dataclass
class Resolution:
    question: Question
    answer: str | None  # None => needs a human. multichoice answers are JSON lists.
    tier: Tier
    model: str = ""
    rationale: str = ""
    score: float = 100

    @property
    def needs_review(self) -> bool:
        return self.tier in ("small_model", "large_model", "human") or (
            self.tier == "fuzzy" and self.score < 97)


def match_option(answer: str, options: list[str], threshold: int = 85) -> str | None:
    """Map a stored answer onto this tenant's exact option label."""
    if not options:
        return answer
    norm = {o: normalize(o) for o in options}
    a = normalize(answer)
    for o, n in norm.items():
        if n == a:
            return o
    # "Yes" should match "Yes, I am authorized" but never "No, I am not"
    for o, n in norm.items():
        if a in ("yes", "no") and n.split()[:1] == [a]:
            return o
    if a in ("yes", "no"):
        return None
    best = process.extractOne(a, norm, scorer=fuzz.WRatio)
    return best[2] if best and best[1] >= threshold else None


def _yes_no(flag: bool) -> str:
    return "Yes" if flag else "No"


_EEO_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"hispanic|latin[oa]"), "hispanic_or_latino"),
    (re.compile(r"\bgender\b|\bsex\b"), "gender"),
    (re.compile(r"ethnic|\brace\b"), "ethnicity"),
    (re.compile(r"veteran"), "veteran_status"),
    (re.compile(r"disabilit"), "disability_status"),
    (re.compile(r"pronoun"), "pronouns"),
]


class Resolver:
    def __init__(self, profile: Profile, bank: AnswerBank, llm: Llm | None = None,
                 *, company: str = "", job_description: str = ""):
        self.profile = profile
        self.bank = bank
        self.llm = llm
        self.company = company
        self.job_description = job_description
        self.log: list[Resolution] = []

    def resolve(self, q: Question) -> Resolution:
        res = (self._from_profile(q) or self._from_bank(q) or self._from_model(q)
               or Resolution(q, None, "human", rationale="no tier could answer"))
        self.log.append(res)
        return res

    # ---- tier 0 ---------------------------------------------------------------------

    def _from_profile(self, q: Question) -> Resolution | None:
        p = self.profile
        label = q.label.lower()
        # Self-ID forms often have generic labels ("Please check one of the boxes below"),
        # so the option texts count too.
        haystack = " ".join([label, *q.options]).lower() if q.closed else label
        for pattern, attr in _EEO_RULES:
            if pattern.search(haystack) and q.closed:
                wanted = getattr(p.self_id, attr)
                opt = match_option(wanted, q.options, threshold=80) if wanted else None
                if opt:
                    return Resolution(q, opt, "profile")
                # Never let a model pick a demographic answer.
                return Resolution(q, None, "human", rationale=f"no option matches self_id.{attr}")

        if q.kind == "date" and re.search(r"today|signed|signature|^date$", label):
            return Resolution(q, date.today().strftime("%m/%d/%Y"), "profile")
        if q.kind == "text":
            c = p.contact
            for pattern, value in (
                (r"^(full |legal )?name$|your name|signature", f"{c.first_name} {c.last_name}"),
                (r"^first name", c.first_name), (r"^last name|surname|family name", c.last_name),
                (r"e-?mail", c.email), (r"phone", c.phone), (r"linkedin", c.linkedin),
                (r"website|portfolio|github", c.website), (r"^city", c.address.city),
                (r"postal|zip", c.address.postal_code),
            ):
                if re.search(pattern, label) and value:
                    return Resolution(q, value, "profile")

        intent = intent_of(q.label)
        wa, pref = p.work_authorization, p.preferences
        raw: str | None = {
            "sponsorship_future": _yes_no(wa.requires_sponsorship_future or wa.requires_sponsorship_now),
            "sponsorship": _yes_no(wa.requires_sponsorship_now),
            "work_authorization": _yes_no(p.contact.address.country in wa.authorized_countries),
            "relocation": _yes_no(pref.willing_to_relocate) if q.closed else (
                pref.relocation_notes or _yes_no(pref.willing_to_relocate)),
            "salary": None if q.closed else (pref.salary_expectation or None),
            "notice_period": None if q.closed else (pref.earliest_start_date or pref.notice_period),
            "previous_employee": _yes_no(any(
                normalize(c) in normalize(self.company) for c in p.previously_worked_for)
            ) if self.company else None,
            "age_18": "Yes",
        }.get(intent or "")
        if raw is None:
            return None
        ans = match_option(raw, q.options) if q.closed else raw
        return Resolution(q, ans, "profile") if ans else None

    # ---- tiers 1-2 ------------------------------------------------------------------

    def _from_bank(self, q: Question) -> Resolution | None:
        bank_kind = "text" if q.kind in ("text", "textarea") else q.kind
        m = self.bank.lookup(q.label, kind=bank_kind)
        if not m:
            return None
        if q.kind == "multichoice":
            wanted = json.loads(m.entry.answer) if m.entry.answer.startswith("[") else [m.entry.answer]
            mapped = [match_option(w, q.options) for w in wanted]
            if None in mapped:
                return None
            ans: str | None = json.dumps(mapped)
        else:
            ans = match_option(m.entry.answer, q.options) if q.closed else m.entry.answer
        if ans is None:
            return None
        self.bank.record_use(m.key)
        return Resolution(q, ans, m.tier, score=m.score)

    # ---- tiers 3-4 ------------------------------------------------------------------

    def _from_model(self, q: Question) -> Resolution | None:
        if self.llm is None:
            return None
        if q.closed and q.options:
            r = self.llm.choose_option(q.label, q.options, self.profile.slice_for_question(),
                                       multi=q.kind == "multichoice")
            if r.answer == DECLINE:
                return Resolution(q, None, "human", model=r.model, rationale=r.rationale)
            self.bank.add(q.label, r.answer, kind=q.kind, options=q.options, source="small_model")
            return Resolution(q, r.answer, "small_model", model=r.model, rationale=r.rationale)
        if q.kind in ("text", "textarea"):
            examples = [e.answer for e in self.bank.entries.values()
                        if e.approved and e.kind == "essay"][-3:]
            r = self.llm.write_answer(q.label, self.job_description, self.profile.slice_for_question(),
                                      self.company, max_chars=q.max_chars, style_examples=examples)
            if r.answer == DECLINE:
                return Resolution(q, None, "human", model=r.model, rationale=r.rationale)
            # Stored as an essay: a style example for later drafts, never reused verbatim.
            self.bank.add(f"{q.label} [{self.company}]", r.answer, kind="essay", source="large_model")
            return Resolution(q, r.answer, "large_model", model=r.model)
        return None
