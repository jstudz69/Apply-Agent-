"""ATS detection: route a job URL to the driver that knows its wizard."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlparse

AtsName = Literal["workday", "greenhouse", "lever", "unknown"]

_WORKDAY_HOST = re.compile(r"^(?P<tenant>[a-z0-9-]+)\.(?P<pod>wd\d+)\.myworkdayjobs\.com$")
_WORKDAY_SITE = re.compile(r"^(?P<tenant>[a-z0-9-]+)\.myworkdayjobs\.com$")


@dataclass(frozen=True)
class AtsTarget:
    ats: AtsName
    url: str
    tenant: str = ""  # Workday tenant / Greenhouse board / Lever company slug
    pod: str = ""  # Workday data-center pod, e.g. wd1, wd3, wd5
    site: str = ""  # Workday career site path segment, e.g. "External"

    @property
    def account_key(self) -> str:
        """One account per tenant: this is the key credentials are stored under."""
        return f"{self.ats}:{self.tenant}"


def detect(url: str) -> AtsTarget:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = [p for p in parsed.path.split("/") if p]

    m = _WORKDAY_HOST.match(host) or _WORKDAY_SITE.match(host)
    if m or host.endswith(".myworkdaysite.com"):
        tenant = m.group("tenant") if m else host.split(".")[0]
        pod = m.groupdict().get("pod", "") if m else ""
        # Paths look like /en-US/External/job/... or /External/job/...
        segs = [p for p in path if not re.fullmatch(r"[a-z]{2}-[A-Z]{2}", p)]
        site = segs[0] if segs and segs[0] not in ("job", "recruiting") else ""
        if host.endswith(".myworkdaysite.com") and len(path) >= 3 and path[0] == "recruiting":
            tenant, site = path[1], path[2]
        return AtsTarget("workday", url, tenant=tenant, pod=pod, site=site)

    if host in ("boards.greenhouse.io", "job-boards.greenhouse.io") and path:
        return AtsTarget("greenhouse", url, tenant=path[0])
    if host == "jobs.lever.co" and path:
        return AtsTarget("lever", url, tenant=path[0])
    return AtsTarget("unknown", url)
