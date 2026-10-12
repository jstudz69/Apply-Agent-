"""Filter before you fill. Mass-applying to weak matches is the failure mode of auto-apply
tools; the bottleneck is targeting, not typing speed."""

from __future__ import annotations

import re

from .profile import Profile


def targeting_problems(profile: Profile, *, company: str, title: str, description: str) -> list[str]:
    t = profile.targeting
    text = f"{title}\n{description}".lower()
    problems = []
    if any(c.lower() in company.lower() for c in t.excluded_companies if c):
        problems.append(f"company {company!r} is excluded")
    hits = [k for k in t.excluded_keywords if re.search(rf"\b{re.escape(k.lower())}\b", text)]
    if hits:
        problems.append(f"excluded keywords present: {', '.join(hits)}")
    if t.required_keywords_any and not any(
            re.search(rf"\b{re.escape(k.lower())}\b", text) for k in t.required_keywords_any):
        problems.append(f"none of the required keywords: {', '.join(t.required_keywords_any)}")
    return problems
