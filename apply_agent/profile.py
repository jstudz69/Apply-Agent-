"""Canonical candidate profile: the single source of truth every driver fills from."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class Address(BaseModel):
    line1: str
    line2: str = ""
    city: str
    state: str  # full name as Workday shows it in dropdowns, e.g. "California"
    postal_code: str
    country: str = "United States of America"


class Contact(BaseModel):
    first_name: str
    last_name: str
    preferred_name: str = ""
    email: str  # base address; per-tenant plus-addresses are derived from it
    phone: str  # digits only, no country code, e.g. "5551234567"
    phone_country_code: str = "+1"
    phone_device_type: str = "Mobile"
    address: Address
    linkedin: str = ""
    website: str = ""


class YearMonth(BaseModel):
    year: int
    month: int = Field(ge=1, le=12)

    def as_mmyyyy(self) -> str:
        return f"{self.month:02d}/{self.year}"

    def as_date(self) -> date:
        return date(self.year, self.month, 1)


class Job(BaseModel):
    title: str
    company: str
    location: str = ""
    start: YearMonth
    end: YearMonth | None = None  # None means current role
    description: str = ""

    @property
    def current(self) -> bool:
        return self.end is None

    @model_validator(mode="after")
    def _dates_ordered(self) -> "Job":
        if self.end and self.end.as_date() < self.start.as_date():
            raise ValueError(f"{self.company}: end date before start date")
        return self


class Education(BaseModel):
    school: str
    degree: str  # as Workday lists it, e.g. "Bachelor's Degree"
    field_of_study: str
    start: YearMonth | None = None
    end: YearMonth | None = None
    gpa: str = ""


class WorkAuthorization(BaseModel):
    authorized_countries: list[str] = ["United States of America"]
    requires_sponsorship_now: bool = False
    requires_sponsorship_future: bool = False


class Preferences(BaseModel):
    willing_to_relocate: bool = False
    relocation_notes: str = ""
    salary_expectation: str = ""  # free text, e.g. "150,000 USD base"
    salary_min: int | None = None  # used by targeting filter
    notice_period: str = "2 weeks"
    earliest_start_date: str = ""
    remote_preference: Literal["remote", "hybrid", "onsite", "any"] = "any"


class SelfIdentification(BaseModel):
    """Your chosen EEO / self-ID answers. Use the exact option wording you want picked;
    the resolver fuzzy-matches against each tenant's option list."""

    gender: str = "I do not wish to self-identify"
    ethnicity: str = "I do not wish to self-identify"
    hispanic_or_latino: str = "I do not wish to self-identify"
    veteran_status: str = "I do not wish to self-identify"
    disability_status: str = "I do not want to answer"
    pronouns: str = ""


class Targeting(BaseModel):
    """Filter-before-fill rules. A job that fails these is skipped unless --force."""

    required_keywords_any: list[str] = []
    excluded_keywords: list[str] = []
    excluded_companies: list[str] = []


class Profile(BaseModel):
    contact: Contact
    work_history: list[Job]
    education: list[Education] = []
    skills: list[str] = []
    work_authorization: WorkAuthorization = WorkAuthorization()
    preferences: Preferences = Preferences()
    self_id: SelfIdentification = SelfIdentification()
    targeting: Targeting = Targeting()
    resume_path: str
    cover_letter_path: str = ""
    how_did_you_hear: str = "LinkedIn"
    previously_worked_for: list[str] = []  # companies you have been an employee of

    @classmethod
    def load(cls, path: str | Path) -> "Profile":
        return cls.model_validate(json.loads(Path(path).read_text()))

    def plus_address(self, tag: str) -> str:
        local, domain = self.contact.email.split("@", 1)
        local = local.split("+", 1)[0]
        safe = "".join(c for c in tag.lower() if c.isalnum() or c in "-_")
        return f"{local}+{safe}@{domain}"

    def slice_for_question(self) -> dict:
        """The profile facts a model may see when resolving a closed question.
        Deliberately excludes self-ID answers: those are only ever filled from the profile."""
        return {
            "name": f"{self.contact.first_name} {self.contact.last_name}",
            "location": f"{self.contact.address.city}, {self.contact.address.state}, "
            f"{self.contact.address.country}",
            "work_authorization": self.work_authorization.model_dump(),
            "preferences": self.preferences.model_dump(),
            "current_title": next((j.title for j in self.work_history if j.current), None),
            "years_experience": self.years_experience(),
            "education": [f"{e.degree}, {e.field_of_study}, {e.school}" for e in self.education],
            "skills": self.skills,
            "previously_worked_for": self.previously_worked_for,
        }

    def years_experience(self) -> float:
        today = date.today()
        months = 0
        for job in self.work_history:
            end = job.end.as_date() if job.end else today
            months += (end.year - job.start.year) * 12 + (end.month - job.start.month)
        return round(months / 12, 1)
