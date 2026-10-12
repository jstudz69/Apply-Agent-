"""Human gate + proof.

The driver never clicks Submit. It stops on the Review page, saves a screenshot and a
report of every answer that did not come straight from the profile or an approved bank
entry, and waits for you to submit in the browser. Then it records proof that the
application landed: the confirmation page (screenshot + text) and, if Gmail is set up,
the confirmation email.
"""

from __future__ import annotations

import html
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .resolver import Resolution


@dataclass
class RunRecord:
    url: str
    ats: str
    tenant: str
    company: str
    job_title: str
    account_email: str
    started_at: str = field(default_factory=lambda: _now())
    status: str = "started"  # started | at_review | submitted | abandoned | failed
    run_dir: str = ""
    review_screenshot: str = ""
    confirmation_screenshot: str = ""
    confirmation_text: str = ""
    confirmation_email: dict | None = None
    submitted_at: str = ""
    model_answers: list[dict] = field(default_factory=list)
    human_answers: list[dict] = field(default_factory=list)
    fallback_pages: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def run_dir_for(data_dir: Path, tenant: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    d = data_dir / "runs" / f"{stamp}-{tenant}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def review_items(log: list[Resolution]) -> list[dict]:
    return [{
        "question": r.question.label,
        "answer": r.answer,
        "tier": r.tier,
        "model": r.model,
        "score": r.score,
        "rationale": r.rationale,
        "options": r.question.options,
    } for r in log if r.needs_review]


def write_review_report(run_dir: Path, record: RunRecord, items: list[dict]) -> Path:
    rows = "\n".join(
        f"<tr class='{html.escape(i['tier'])}'><td>{html.escape(i['question'])}</td>"
        f"<td><pre>{html.escape(str(i['answer']))}</pre></td><td>{html.escape(i['tier'])}"
        f"{' ' + html.escape(i['model']) if i['model'] else ''}"
        f"{' (%d%%)' % i['score'] if i['tier'] == 'fuzzy' else ''}</td>"
        f"<td>{html.escape(i['rationale'])}</td></tr>"
        for i in items) or "<tr><td colspan=4>Every answer came from your profile or approved bank.</td></tr>"
    page = f"""<!doctype html><meta charset="utf-8"><title>Review {html.escape(record.company)}</title>
<style>body{{font:14px system-ui;margin:24px;max-width:1100px}}td,th{{border:1px solid #ccc;padding:6px;vertical-align:top}}
table{{border-collapse:collapse;width:100%}}pre{{white-space:pre-wrap;margin:0}}.large_model{{background:#fff3cd}}
.small_model{{background:#e7f1ff}}.fuzzy{{background:#f3f3f3}}.human{{background:#f8d7da}}img{{max-width:100%;border:1px solid #ccc}}</style>
<h1>{html.escape(record.job_title)} - {html.escape(record.company)}</h1>
<p><a href="{html.escape(record.url)}">{html.escape(record.url)}</a> &middot; account {html.escape(record.account_email)}</p>
<h2>Answers that need your eyes ({len(items)})</h2>
<table><tr><th>Question</th><th>Answer</th><th>Source</th><th>Why</th></tr>{rows}</table>
<h2>Review page</h2><img src="review.png">"""
    out = run_dir / "review.html"
    out.write_text(page)
    (run_dir / "review.json").write_text(json.dumps(items, indent=2))
    return out


def print_review(items: list[dict]) -> None:
    if not items:
        print("  All answers came from your profile or approved answer bank.")
        return
    for i in items:
        tag = i["tier"] + (f" {i['model']}" if i["model"] else "")
        ans = str(i["answer"]).replace("\n", " ")
        print(f"  [{tag}] {i['question']}\n      -> {ans[:300]}{'...' if len(ans) > 300 else ''}")


def append_log(data_dir: Path, record: RunRecord) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    with (data_dir / "applications.jsonl").open("a") as fh:
        fh.write(json.dumps(asdict(record)) + "\n")
    Path(record.run_dir, "record.json").write_text(json.dumps(asdict(record), indent=2))
