"""The only module that talks to a model. Two calls, tiered by cost:

- `choose_option`: unmatched closed question -> small model, output constrained to the
  exact option list via a JSON-schema enum.
- `write_answer`: open-ended text (e.g. "why this company") -> stronger model, given the
  job description. Always flagged for human review by the caller.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

import anthropic

SMALL_MODEL = os.environ.get("APPLY_AGENT_SMALL_MODEL", "claude-haiku-5-5")
LARGE_MODEL = os.environ.get("APPLY_AGENT_LARGE_MODEL", "claude-opus-5-5")
DECLINE = "__NEEDS_HUMAN__"


@dataclass
class LlmResult:
    answer: str
    model: str
    rationale: str = ""
    input_tokens: int = 0
    output_tokens: int = 0


class Llm:
    def __init__(self, client: anthropic.Anthropic | None = None):
        self.client = client or anthropic.Anthropic()

    def choose_option(self, question: str, options: list[str], profile_slice: dict,
                      *, multi: bool = False) -> LlmResult:
        enum = options + [DECLINE]
        answer_schema = ({"type": "array", "items": {"type": "string", "enum": options}}
                         if multi else {"type": "string", "enum": enum})
        schema = {
            "type": "object",
            "properties": {"answer": answer_schema, "rationale": {"type": "string"}},
            "required": ["answer", "rationale"],
            "additionalProperties": False,
        }
        prompt = (
            "You are filling a job application form on behalf of the candidate below. "
            "Pick the option that truthfully answers the question using only the candidate's "
            "facts. If the facts do not determine the answer, answer "
            f"{DECLINE!r} (single-choice) or an empty list (multi-choice) so a human decides. "
            "Never guess about legal attestations, criminal history, or demographic questions.\n\n"
            f"<candidate>\n{json.dumps(profile_slice, indent=2)}\n</candidate>\n\n"
            f"<question>{question}</question>\n"
            f"<options>{json.dumps(options)}</options>"
        )
        resp = self.client.messages.create(
            model=SMALL_MODEL,
            max_tokens=1024,
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": prompt}],
        )
        if resp.stop_reason == "refusal":
            return LlmResult(DECLINE, resp.model, "model declined")
        data = json.loads(next(b.text for b in resp.content if b.type == "text"))
        answer = data["answer"]
        if multi:
            answer = DECLINE if not answer else json.dumps(answer)
        return LlmResult(answer, resp.model, data.get("rationale", ""),
                         resp.usage.input_tokens, resp.usage.output_tokens)

    def write_answer(self, question: str, job_description: str, profile_slice: dict,
                     company: str, *, max_chars: int | None = None,
                     style_examples: list[str] | None = None) -> LlmResult:
        limit = f" Hard limit: {max_chars} characters." if max_chars else ""
        examples = ""
        if style_examples:
            examples = "\n\n<my_previous_answers>\n" + "\n---\n".join(style_examples) + \
                "\n</my_previous_answers>\nMatch the voice of these previous answers."
        system = (
            "You draft short, specific job-application answers in the candidate's own voice. "
            "Plain prose, first person, no headings or bullet points, no flattery, no claims "
            "that are not supported by the candidate facts. Return only the answer text."
        )
        prompt = (
            f"<company>{company}</company>\n"
            f"<job_description>\n{job_description}\n</job_description>\n"
            f"<candidate>\n{json.dumps(profile_slice, indent=2)}\n</candidate>{examples}\n\n"
            f"Question: {question}\nAim for 80-150 words.{limit}"
        )
        with self.client.beta.messages.stream(
            model=LARGE_MODEL,
            max_tokens=16000,
            system=system,
            output_config={"effort": "medium"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            resp = stream.get_final_message()
        if resp.stop_reason == "refusal":
            return LlmResult(DECLINE, resp.model, "model declined")
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        if max_chars and len(text) > max_chars:
            text = text[:max_chars].rsplit(" ", 1)[0]
        return LlmResult(text, resp.model, "", resp.usage.input_tokens, resp.usage.output_tokens)
