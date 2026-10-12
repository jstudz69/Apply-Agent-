# apply-agent

A job-application copilot for **Workday**, run under your supervision. A deterministic
Playwright driver fills the wizard from your profile, models answer only the questions it
can't, and the driver **stops on the Review page**. You read the page and click Submit
yourself.

```
URL ─► ATS detect ─► account layer ─► Workday state machine ─► Review (stop) ─► you submit ─► proof log
                      (vault+Gmail)    │ fill → read back → diff → Next
                                       ├─ question resolver: profile → bank → fuzzy → small model → large model
                                       └─ unknown page → fallback agent (pruned DOM) → you
```

## Layout

| Piece | File | What it does |
|---|---|---|
| Canonical profile | `apply_agent/profile.py`, `examples/profile.example.json` | Contact details, work history with exact dates, education, work authorization and sponsorship, relocation, salary, notice period, EEO/self-ID choices, targeting rules |
| Answer bank | `apply_agent/answer_bank.py` | Maps normalized question text to your approved answer. Lookups go exact match, then intent rules (sponsorship has dozens of phrasings), then fuzzy match. Model answers stay *pending* until you approve them. |
| ATS detection | `apply_agent/ats.py` | `*.wdN.myworkdayjobs.com`, `myworkdaysite.com`, plus Greenhouse and Lever detection (no drivers for those yet) |
| Account layer | `apply_agent/accounts/` | One account per tenant, a plus-address (`you+tenant@…`), a generated password stored in the OS keychain before it's used, and a Gmail API poll that opens the verification link for you |
| Workday driver | `apply_agent/drivers/workday/` | State machine: My Information → My Experience → Application Questions → Voluntary Disclosures → Self Identify → Review. Always chooses **Apply Manually** and attaches the resume as a file only. Selectors use `data-automation-id` and all live in `selectors.py`. |
| Generic DOM layer | `apply_agent/drivers/dom.py` | One in-page scan returns label, kind, options and current value for every field. The same scan drives filling, read-back and the fallback agent. |
| Resolver | `apply_agent/resolver.py`, `apply_agent/llm.py` | Tiered by cost; see below |
| Human gate and proof | `apply_agent/review.py` | Saves `review.png` and `review.html` listing every answer a model or fuzzy match produced. After you submit, saves the confirmation screenshot and text, finds the confirmation email, and appends to `data/applications.jsonl`. |
| Fallback | `apply_agent/fallback.py` | Sends one unrecognised page to a tool-using model that sees the pruned field list, not screenshots. It can set fields and press buttons; it can't see or press Submit. If it gives up, the page goes to you. |
| Targeting | `apply_agent/targeting.py` | Filters before filling: required and excluded keywords, excluded companies |

### Resolver tiers

| Tier | Source | Model call? | Flagged for review? |
|---|---|---|---|
| 0 | Profile: sponsorship, work authorization, relocation, salary, notice period, previous employer, name/email/phone, self-ID | no | no |
| 1 | Exact match in the answer bank | no | no |
| 2 | Intent rule or fuzzy match against the bank | no | if the fuzzy score is below 97 |
| 3 | Closed question with no match → `claude-haiku-5-5`, output limited to the page's option list by a JSON-schema `enum` | cheap | yes |
| 4 | Open-ended text → `claude-opus-5-5` with the job description | yes | always |

Rules that always apply:

- **Demographic and self-ID questions never go to a model.** If no option matches your profile choice, the question goes to you.
- **Essays are saved as `kind: "essay"`.** They're reused only as style examples for later drafts, never pasted verbatim into another company's application.
- **Unanswered required questions come to you.** You answer them in the browser and the driver offers to save your answers to the bank.

To change the models, set `APPLY_AGENT_SMALL_MODEL` and `APPLY_AGENT_LARGE_MODEL`. Opus calls use the server-side refusal fallback (`fallbacks: "default"`).

### Read-back diff

After filling a page, the driver re-scans it and compares each field's value with what it meant to enter. It retries a mismatch once. If the field is still wrong, it shows you the diff and waits. It clicks Next only after this check, and it refuses to click a button labelled "Submit".

## Setup

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[gmail,dev]"
cp examples/profile.example.json profile.json        # then edit it: this is the core asset
cp examples/answer_bank.seed.json answer_bank.json   # optional starter entries
apply-agent check-profile
export ANTHROPIC_API_KEY=...                          # or `ant auth login`
```

**Gmail verification (optional).** Enable the Gmail API in a Google Cloud project, create a
*Desktop app* OAuth client, and save it as `data/credentials.json`. The first run opens a
consent screen. The scope is `gmail.readonly`. If you skip this step, use `--no-gmail` and
click verification links yourself.

**Your real browser.** Start Chrome with a dedicated profile you also browse with normally:

```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-port=9222 --user-data-dir="$HOME/.apply-agent-chrome"
```

## Use

```bash
apply-agent detect  https://acme.wd5.myworkdayjobs.com/en-US/External/job/...
apply-agent apply   https://acme.wd5.myworkdayjobs.com/en-US/External/job/... --company "Acme" --cdp http://localhost:9222
apply-agent bank pending            # model answers waiting for approval
apply-agent bank approve all        # or: approve "<key>" --answer "..."
apply-agent bank test "Will you require sponsorship in the future?"
apply-agent log                     # every run, with status and proof folder
```

Useful flags: `--no-llm` (no model calls; every unknown question comes to you), `--force`
(skip the targeting filter), `--speed` (stays at human pace unless you raise it).

Each run writes `data/runs/<timestamp>-<tenant>/`, containing `job_description.txt`, `review.png`,
`review.html`/`review.json`, `confirmation.png` and `record.json`.

## Tests

```bash
pytest
```

`tests/fixtures/workday_mock.html` is a local imitation of a Workday tenant. It uses the same
`data-automation-id` conventions and widget behaviour: listbox buttons, searchable
multiselect prompts, hidden radio inputs, date segments that auto-advance, the "currently
work here" checkbox that hides the end date, and validation when you click Next.

The end-to-end tests drive the full state machine in Chromium. They cover account creation,
email verification, the sign-in loop, overwriting a pre-filled work-history row, model and
human answers, the stop at Review, and the proof log. They also check that a second
application for the same tenant makes no repeat model calls and creates no second account,
that an unknown step goes to the fallback, and that the targeting filter skips mismatched jobs.

## Caveats

- **The selectors haven't been run against a live tenant yet.** They're tested only against
  the mock. Workday has two DOM generations and tenants customise their steps. Run your first
  few applications with `--speed 1` and watch them. When something breaks, it should be a
  one-line fix in `drivers/workday/selectors.py`.
- **Check each company's terms.** Some tenants prohibit automated access. This tool is built
  as a supervised copilot: you're present, it runs at human pace in your own browser, and you
  click Submit. That's a much safer position than an unattended bot, but it's still your call.
- **Targeting matters more than speed.** Mass-applying to weak matches is how auto-apply tools
  fail. Keep the targeting rules strict.
- **Greenhouse and Lever** are detected but don't have drivers yet. Their forms are single-page
  and simpler, and `dom.scan` plus the resolver already cover most of what they need.
