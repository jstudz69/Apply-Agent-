"""Fallback for a single page the state machine doesn't recognise (a custom tenant step,
an assessment, a new widget). A model works from the pruned field list produced by
`dom.scan` (plus visible buttons), which is far cheaper than screenshots. It can set
fields and press non-submit buttons, nothing else. If it gives up, the page goes to you.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

import anthropic
from playwright.sync_api import Page

from .drivers import dom
from .llm import LARGE_MODEL
from .pacing import Pacer
from .profile import Profile

BUTTONS_JS = r"""
() => {
  const vis = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  window.__aaRef = window.__aaRef || 0;
  return [...document.querySelectorAll('button, [role="button"], a[data-automation-id]')]
    .filter(vis).filter(b => !b.matches('[aria-haspopup="listbox"]'))
    .map(b => {
      if (!b.dataset.aaRef) b.dataset.aaRef = String(++window.__aaRef);
      return { ref: b.dataset.aaRef, text: (b.innerText || b.getAttribute('aria-label') || '').trim().slice(0, 80) };
    }).filter(b => b.text);
}
"""
_FORBIDDEN = re.compile(r"\bsubmit\b|withdraw|delete account|sign out", re.I)

TOOLS = [
    {
        "name": "set_field",
        "description": "Set a form field by its ref. For choice fields pass one option label exactly; "
                       "for multichoice pass a JSON list of labels; for checkboxes pass true/false; "
                       "dates as MM/DD/YYYY or MM/YYYY.",
        "strict": True,
        "input_schema": {"type": "object", "additionalProperties": False, "required": ["ref", "value"],
                         "properties": {"ref": {"type": "string"}, "value": {"type": "string"}}},
    },
    {
        "name": "click_button",
        "description": "Click a visible button by ref (e.g. 'Add', 'Next', 'Continue'). Submit buttons are refused.",
        "strict": True,
        "input_schema": {"type": "object", "additionalProperties": False, "required": ["ref"],
                         "properties": {"ref": {"type": "string"}}},
    },
    {
        "name": "finish",
        "description": "Call when the page is fully and truthfully filled (do not click Next yourself unless asked).",
        "strict": True,
        "input_schema": {"type": "object", "additionalProperties": False, "required": ["summary"],
                         "properties": {"summary": {"type": "string"}}},
    },
    {
        "name": "need_human",
        "description": "Call when the page needs information you don't have, asks for a legal "
                       "attestation, is a skills assessment or test, or you are unsure.",
        "strict": True,
        "input_schema": {"type": "object", "additionalProperties": False, "required": ["reason"],
                         "properties": {"reason": {"type": "string"}}},
    },
]


@dataclass
class FallbackResult:
    ok: bool
    summary: str
    actions: list[dict] = field(default_factory=list)


def page_snapshot(page: Page) -> dict:
    fields = dom.scan(page)
    dom.resolve_listbox_options(page, fields)
    title = page.locator("h1, h2").first.inner_text(timeout=2000) if page.locator("h1, h2").count() else ""
    return {
        "url": page.url,
        "heading": title.strip(),
        "fields": [{"ref": f.ref, "label": f.label, "kind": f.kind, "options": f.options,
                    "value": f.value, "required": f.required} for f in fields],
        "buttons": [b for b in page.evaluate(BUTTONS_JS) if not _FORBIDDEN.search(b["text"])],
    }


def run_fallback(page: Page, profile: Profile, pacer: Pacer, *, job_description: str = "",
                 client: anthropic.Anthropic | None = None, max_turns: int = 12) -> FallbackResult:
    client = client or anthropic.Anthropic()
    system = (
        "You complete one page of a job application in a browser, on behalf of the candidate. "
        "You see a pruned list of form fields and buttons, never pixels. Fill only what the "
        "candidate's facts truthfully answer. Never answer demographic/self-identification "
        "questions, legal attestations, or assessments - call need_human. Never submit.\n\n"
        f"<candidate>\n{json.dumps(profile.slice_for_question(), indent=2)}\n</candidate>\n"
        f"<job_description>\n{job_description[:6000]}\n</job_description>"
    )
    messages: list[dict] = [{"role": "user", "content": "Current page:\n" + json.dumps(page_snapshot(page))}]
    actions: list[dict] = []
    for _ in range(max_turns):
        resp = client.beta.messages.create(
            model=LARGE_MODEL, max_tokens=8000, system=system, tools=TOOLS,
            output_config={"effort": "low"},
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",
            messages=messages,
        )
        if resp.stop_reason == "refusal":
            return FallbackResult(False, "model declined", actions)
        messages.append({"role": "assistant", "content": resp.content})
        uses = [b for b in resp.content if b.type == "tool_use"]
        if not uses:
            return FallbackResult(False, "model stopped without finishing", actions)
        results = []
        for u in uses:
            if u.name == "finish":
                return FallbackResult(True, u.input["summary"], actions)
            if u.name == "need_human":
                return FallbackResult(False, u.input["reason"], actions)
            try:
                out = _apply(page, u.name, u.input, pacer)
                actions.append({"tool": u.name, **u.input})
                results.append({"type": "tool_result", "tool_use_id": u.id, "content": out})
            except Exception as e:  # report the failure to the model, let it adapt
                results.append({"type": "tool_result", "tool_use_id": u.id, "is_error": True,
                                "content": f"{type(e).__name__}: {e}"})
        results.append({"type": "text", "text": "Page now:\n" + json.dumps(page_snapshot(page))})
        messages.append({"role": "user", "content": results})
    return FallbackResult(False, "turn limit reached", actions)


def _apply(page: Page, tool: str, args: dict, pacer: Pacer) -> str:
    if tool == "click_button":
        loc = dom.by_ref(page, args["ref"])
        if _FORBIDDEN.search(loc.inner_text()):
            raise PermissionError("submit-like buttons are reserved for the human")
        loc.click()
        pacer.think()
        return "clicked"
    if tool == "set_field":
        f = next((f for f in dom.scan(page) if f.ref == args["ref"]), None)
        if f is None:
            raise KeyError(f"no field with ref {args['ref']}")
        dom.fill_field(page, f, args["value"], pacer)
        return "set"
    raise ValueError(tool)
