"""Workday driver: a deterministic state machine over the application wizard.

  JOB -> START_CHOICE ("Apply Manually") -> AUTH -> My Information -> My Experience
  -> Application Questions (1..n) -> Voluntary Disclosures -> Self Identify -> REVIEW

Each wizard page: compute intended values -> fill -> read every value back and diff ->
only then click Next. The driver stops on Review and never clicks Submit.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Protocol

from playwright.sync_api import Locator, Page
from playwright.sync_api import TimeoutError as PwTimeout

from ...accounts.vault import Credential, Vault, generate_password
from ...answer_bank import AnswerBank
from ...ats import AtsTarget
from ...pacing import Pacer
from ...profile import Job, Profile
from ...resolver import Resolution, Resolver
from ...review import RunRecord, append_log, print_review, review_items, write_review_report
from ...targeting import targeting_problems
from .. import dom
from . import selectors as S


class Step(str, Enum):
    JOB = "job"
    START_CHOICE = "start_choice"
    AUTH = "auth"
    MY_INFORMATION = "my information"
    MY_EXPERIENCE = "my experience"
    APPLICATION_QUESTIONS = "application questions"
    VOLUNTARY_DISCLOSURES = "voluntary disclosures"
    SELF_IDENTIFY = "self identify"
    REVIEW = "review"
    SUBMITTED = "submitted"
    UNKNOWN = "unknown"


WIZARD_STEPS = [Step.MY_INFORMATION, Step.MY_EXPERIENCE, Step.APPLICATION_QUESTIONS,
                Step.VOLUNTARY_DISCLOSURES, Step.SELF_IDENTIFY, Step.REVIEW]


class SkipJob(Exception):
    pass


class NeedsHuman(Exception):
    pass


class HumanIO(Protocol):
    def wait(self, message: str) -> None: ...
    def ask(self, message: str) -> str: ...


class ConsoleIO:
    def wait(self, message: str) -> None:
        input(f"\n>>> {message}\n    Press Enter to continue... ")

    def ask(self, message: str) -> str:
        return input(f"\n>>> {message} ").strip()


class AccountVerifier(Protocol):
    def wait_for_verification_link(self, to_address: str, *, since_epoch: float,
                                   timeout_s: float = 300, interval_s: float = 10) -> str | None: ...


@dataclass
class Intended:
    field: dom.Field
    value: str
    source: str  # "profile" or a resolver tier


@dataclass
class DriverOptions:
    data_dir: Path = Path("data")
    company: str = ""
    force: bool = False  # ignore targeting filter
    use_fallback_agent: bool = True
    confirmation_timeout_s: float = 30 * 60
    max_page_visits: int = 3


@dataclass
class WorkdayDriver:
    page: Page
    target: AtsTarget
    profile: Profile
    bank: AnswerBank
    resolver: Resolver
    vault: Vault
    pacer: Pacer
    io: HumanIO = field(default_factory=ConsoleIO)
    gmail: AccountVerifier | None = None
    fallback: Callable[[Page], "object"] | None = None  # returns FallbackResult
    options: DriverOptions = field(default_factory=DriverOptions)
    record: RunRecord | None = None
    run_dir: Path | None = None

    # ==== entry ===========================================================================

    def run(self) -> RunRecord:
        from ...review import run_dir_for

        self.run_dir = run_dir_for(self.options.data_dir, self.target.tenant or "workday")
        company = self.options.company or self.target.tenant
        self.record = RunRecord(url=self.target.url, ats="workday", tenant=self.target.tenant,
                                company=company, job_title="", account_email="",
                                run_dir=str(self.run_dir))
        self.resolver.company = company
        self.page.goto(self.target.url, wait_until="domcontentloaded")
        self.pacer.think()
        visits: dict[Step, int] = {}
        try:
            for _ in range(60):
                step = self.detect_step()
                visits[step] = visits.get(step, 0) + 1
                print(f"[workday] step: {step.value}")
                if step == Step.REVIEW:
                    return self.at_review()
                if step == Step.SUBMITTED:
                    return self._finish_submitted()
                if visits[step] > self.options.max_page_visits and step in WIZARD_STEPS:
                    self._hand_to_human(f"stuck on '{step.value}' after {visits[step] - 1} attempts")
                    continue
                self._handle(step)
            raise NeedsHuman("step limit reached")
        except SkipJob as e:
            self.record.status = "skipped"
            self.record.notes.append(str(e))
            print(f"[workday] skipped: {e}")
            return self.record
        except Exception as e:
            self.record.status = "failed"
            self.record.notes.append(f"{type(e).__name__}: {e}")
            self._screenshot("failure.png")
            raise
        finally:
            self.bank.save()
            append_log(self.options.data_dir, self.record)

    def _handle(self, step: Step) -> None:
        handlers: dict[Step, Callable[[], None]] = {
            Step.JOB: self.on_job_page,
            Step.START_CHOICE: self.on_start_choice,
            Step.AUTH: self.on_auth,
            Step.MY_INFORMATION: self.on_my_information,
            Step.MY_EXPERIENCE: self.on_my_experience,
            Step.APPLICATION_QUESTIONS: self.on_generic_questions,
            Step.VOLUNTARY_DISCLOSURES: self.on_generic_questions,
            Step.SELF_IDENTIFY: self.on_generic_questions,
        }
        handler = handlers.get(step)
        if handler is None:
            self.on_unknown()
            return
        handler()
        if step in WIZARD_STEPS:
            self.click_next()

    # ==== state detection =================================================================

    def detect_step(self) -> Step:
        p = self.page
        p.wait_for_load_state("domcontentloaded")
        if _visible(p, S.CONFIRMATION):
            return Step.SUBMITTED
        for sel in (S.PROGRESS_ACTIVE, S.PAGE_HEADER):
            loc = dom.first_visible(p, sel)
            if loc:
                txt = " ".join(loc.inner_text().lower().replace("-", " ").split())
                for st in WIZARD_STEPS:
                    if st.value in txt:
                        return st
        if _visible(p, S.AUTH_PASSWORD) or (_visible(p, S.SIGN_IN_LINK) and _visible(p, S.CREATE_ACCOUNT_LINK)):
            return Step.AUTH
        if _visible(p, S.APPLY_MANUALLY):
            return Step.START_CHOICE
        if _visible(p, S.APPLY_BUTTON):
            return Step.JOB
        return Step.UNKNOWN

    # ==== pre-wizard ======================================================================

    def on_job_page(self) -> None:
        title = _text(self.page, S.JOB_TITLE)
        desc = _text(self.page, S.JOB_DESCRIPTION)
        self.record.job_title = title
        self.resolver.job_description = desc
        (self.run_dir / "job_description.txt").write_text(f"{title}\n\n{desc}")
        problems = targeting_problems(self.profile, company=self.record.company, title=title, description=desc)
        if problems and not self.options.force:
            raise SkipJob("; ".join(problems))
        dom.first_visible(self.page, S.APPLY_BUTTON).click()
        self.pacer.think()

    def on_start_choice(self) -> None:
        # Never "Autofill with Resume": its parser mangles titles and dates.
        dom.first_visible(self.page, S.APPLY_MANUALLY).click()
        self.pacer.think()

    def on_auth(self) -> None:
        cred = self.vault.get(self.target.account_key)
        if cred is None:
            cred = self._create_account()
        self.record.account_email = cred.email
        self._sign_in(cred)

    def _create_account(self) -> Credential:
        p = self.page
        email = self.profile.plus_address(self.target.tenant)
        cred = Credential(email=email, password=generate_password())
        self.vault.put(self.target.account_key, cred)  # store before submitting so it is never lost
        if not _visible(p, S.AUTH_VERIFY_PASSWORD):
            dom.first_visible(p, S.CREATE_ACCOUNT_LINK).click()
            self.pacer.pause()
        dom.type_text(dom.first_visible(p, S.AUTH_EMAIL), cred.email, self.pacer)
        dom.type_text(dom.first_visible(p, S.AUTH_PASSWORD), cred.password, self.pacer)
        dom.type_text(dom.first_visible(p, S.AUTH_VERIFY_PASSWORD), cred.password, self.pacer)
        terms = dom.first_visible(p, S.AUTH_TERMS_CHECKBOX)
        if terms is not None and not terms.is_checked():
            # Accepting a tenant's terms is your decision, once per company.
            self.io.wait(f"Read {self.target.tenant}'s account terms in the browser and tick the box if you agree.")
        since = time.time()
        dom.first_visible(p, S.CREATE_ACCOUNT_SUBMIT).click()
        self.pacer.think()
        if _visible(p, S.AUTH_ERROR):
            raise NeedsHuman(f"account creation error: {_text(p, S.AUTH_ERROR)}")
        self._verify_email(cred, since)
        return cred

    def _verify_email(self, cred: Credential, since: float) -> None:
        link = None
        if self.gmail is not None:
            print(f"[workday] waiting for verification email to {cred.email} ...")
            link = self.gmail.wait_for_verification_link(cred.email, since_epoch=since)
        if link:
            tab = self.page.context.new_page()
            tab.goto(link, wait_until="domcontentloaded")
            self.pacer.think()
            tab.close()
        else:
            self.io.wait(f"Open the verification email sent to {cred.email} and click its link.")
        cred.verified = True
        self.vault.put(self.target.account_key, cred)
        self.page.reload(wait_until="domcontentloaded")
        self.pacer.think()

    def _sign_in(self, cred: Credential) -> None:
        p = self.page
        if _visible(p, S.AUTH_VERIFY_PASSWORD) or not _visible(p, S.AUTH_PASSWORD):
            link = dom.first_visible(p, S.SIGN_IN_LINK)
            if link:
                link.click()
                self.pacer.pause()
        if not _visible(p, S.AUTH_PASSWORD):
            return  # already signed in (e.g. after verification)
        dom.type_text(dom.first_visible(p, S.AUTH_EMAIL), cred.email, self.pacer)
        dom.type_text(dom.first_visible(p, S.AUTH_PASSWORD), cred.password, self.pacer)
        dom.first_visible(p, S.SIGN_IN_SUBMIT).click()
        self.pacer.think()
        if _visible(p, S.AUTH_PASSWORD) and _visible(p, S.AUTH_ERROR):
            raise NeedsHuman(f"sign-in failed for {cred.email}: {_text(p, S.AUTH_ERROR)}")

    # ==== wizard pages ====================================================================

    def on_my_information(self) -> None:
        p, c = self.profile, self.profile.contact
        prev = any(w.lower() in self.record.company.lower() for w in p.previously_worked_for)
        wanted = {
            "source": p.how_did_you_hear,
            "previous_worker": "Yes" if prev else "No",
            "country": c.address.country,
            "first_name": c.first_name,
            "last_name": c.last_name,
            "address_line1": c.address.line1,
            "city": c.address.city,
            "state": c.address.state,
            "postal_code": c.address.postal_code,
            "phone_device_type": c.phone_device_type,
            "phone_number": c.phone,
        }
        # Country first: it re-renders the address block for that country's format.
        self.fill_and_verify(self._intend(self.page, {"country": S.MY_INFO["country"]}, wanted))
        intents = self._intend(self.page, {k: v for k, v in S.MY_INFO.items() if k != "country"}, wanted)
        self.fill_and_verify(intents)
        self.fill_remaining_required(exclude={i.field.ref for i in intents})

    def on_my_experience(self) -> None:
        self._upload_resume()
        self._fill_rows(S.WORK_SECTION, S.work_panel, self.profile.work_history, self._work_values, S.WORK_FIELDS)
        self._fill_rows(S.EDU_SECTION, S.edu_panel, self.profile.education, self._edu_values, S.EDU_FIELDS)
        self._fill_skills()
        self._fill_websites()
        self.fill_remaining_required()

    def on_generic_questions(self) -> None:
        fields = dom.scan(self.page, "main") or dom.scan(self.page)
        dom.resolve_listbox_options(self.page, fields)
        intents: list[Intended] = []
        unresolved: list[dom.Field] = []
        for f in fields:
            r = self.resolver.resolve(f.to_question())
            if r.answer is None:
                if f.required and f.empty:
                    unresolved.append(f)
                continue
            intents.append(Intended(f, r.answer, r.tier))
        self.fill_and_verify(intents)
        if unresolved:
            self._ask_human_for(unresolved)

    def on_unknown(self) -> None:
        heading = _text(self.page, S.PAGE_HEADER) or self.page.url
        self.record.fallback_pages.append(heading)
        if self.fallback is not None and self.options.use_fallback_agent:
            print(f"[workday] unrecognised page '{heading}', handing it to the fallback agent")
            result = self.fallback(self.page)
            self.record.model_answers.extend(
                {"question": a.get("ref"), "answer": a.get("value"), "tier": "fallback_agent"}
                for a in getattr(result, "actions", []))
            if getattr(result, "ok", False):
                self.click_next()
                return
            print(f"[workday] fallback agent stopped: {getattr(result, 'summary', '')}")
        self._hand_to_human(f"unrecognised page '{heading}'")

    # ==== Review / submit =================================================================

    def at_review(self) -> RunRecord:
        rec = self.record
        rec.status = "at_review"
        rec.review_screenshot = self._screenshot("review.png")
        items = review_items(self.resolver.log)
        rec.model_answers.extend(items)
        report = write_review_report(self.run_dir, rec, items)
        print("\n================ REVIEW ================")
        print(f"{rec.job_title} @ {rec.company}")
        print_review(items)
        print(f"Report: {report}")
        print("Check every page in the browser, then click Submit yourself.")
        print(f"Waiting up to {int(self.options.confirmation_timeout_s // 60)} min for the confirmation page "
              "(Ctrl+C to abandon) ...")
        try:
            self.page.locator(S.CONFIRMATION).first.wait_for(
                state="visible", timeout=self.options.confirmation_timeout_s * 1000)
        except PwTimeout:
            rec.status = "abandoned"
            rec.notes.append("no confirmation seen before timeout")
            return rec
        except KeyboardInterrupt:
            rec.status = "abandoned"
            return rec
        return self._finish_submitted()

    def _finish_submitted(self) -> RunRecord:
        rec = self.record
        if rec.status != "at_review":  # submitted before we reached Review: still record answers
            rec.model_answers.extend(review_items(self.resolver.log))
            write_review_report(self.run_dir, rec, rec.model_answers)
        rec.status = "submitted"
        rec.submitted_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        rec.confirmation_screenshot = self._screenshot("confirmation.png")
        rec.confirmation_text = _text(self.page, S.CONFIRMATION)[:2000]
        finder = getattr(self.gmail, "find_confirmation", None)
        if finder and rec.account_email:
            time.sleep(20)  # give the confirmation email a moment
            rec.confirmation_email = finder(rec.account_email, since_epoch=time.time() - 3600)
        print(f"[workday] submitted. Proof saved in {self.run_dir}")
        self._approve_new_answers()
        return rec

    def _approve_new_answers(self) -> None:
        for key, entry in self.bank.pending().items():
            if entry.kind == "essay":
                self.bank.approve(key)  # you submitted it; keep as a style example
                continue
            ans = self.io.ask(f"Save to answer bank?  Q: {entry.question}\n    A: {entry.answer}\n    "
                              "[y]es / [n]o / or type a corrected answer:")
            if ans.lower() in ("y", "yes", ""):
                self.bank.approve(key)
            elif ans.lower() not in ("n", "no"):
                self.bank.approve(key, ans)

    # ==== fill / verify machinery =========================================================

    def _intend(self, scope: Page | Locator, selectors: dict[str, str], wanted: dict[str, str]) -> list[Intended]:
        fields = {f.ref: f for f in dom.scan(self.page)}
        out = []
        for key, sel in selectors.items():
            value = wanted.get(key)
            if not value:
                continue
            f = self._field_at(scope, sel, fields)
            if f is not None:
                out.append(Intended(f, value, "profile"))
        return out

    def _field_at(self, scope: Page | Locator, selector: str, fields: dict[str, dom.Field]) -> dom.Field | None:
        el = dom.first_visible(scope, selector)
        if el is None:
            return None
        ref = el.evaluate("e => (e.closest('[data-aa-ref]:not(input)') || {dataset: {}}).dataset.aaRef")
        return fields.get(ref)

    def fill_and_verify(self, intents: list[Intended]) -> None:
        """Fill, then read every value back and diff against intent. One retry, then a human."""
        if not intents:
            return
        dom.resolve_listbox_options(self.page, [i.field for i in intents])
        for attempt in range(2):
            pending = self._mismatches(intents)
            if not pending:
                return
            for i in pending:
                value = i.value
                if i.field.options and i.field.kind == "choice":
                    from ...resolver import match_option
                    value = match_option(i.value, i.field.options, threshold=80) or i.value
                try:
                    dom.fill_field(self.page, i.field, value, self.pacer)
                except Exception as e:  # recorded; the read-back below catches the miss
                    print(f"[workday] could not fill '{i.field.label}': {e}")
        bad = self._mismatches(intents)
        if bad:
            diff = "\n".join(f"  - {i.field.label}: wanted {i.value!r}, page shows {self._current(i.field)!r}"
                             for i in bad)
            print(f"[workday] read-back mismatch:\n{diff}")
            self.io.wait("Fix these fields in the browser.")

    def _current(self, f: dom.Field) -> str:
        now = {x.ref: x for x in dom.scan(self.page)}
        return now[f.ref].value if f.ref in now else ""

    def _mismatches(self, intents: list[Intended]) -> list[Intended]:
        now = {x.ref: x for x in dom.scan(self.page)}
        out = []
        for i in intents:
            cur = now.get(i.field.ref)
            if cur is None:
                continue  # field disappeared (conditional section); nothing to diff
            i.field.value = cur.value
            wanted = i.value
            if i.field.kind == "choice" and i.field.options:
                from ...resolver import match_option
                wanted = match_option(i.value, i.field.options, threshold=80) or i.value
            if not dom.values_match(i.field.kind, wanted, cur.value):
                out.append(i)
        return out

    def fill_remaining_required(self, exclude: set[str] | None = None) -> None:
        """Required fields the explicit mapping didn't cover go through the resolver."""
        exclude = exclude or set()
        fields = [f for f in dom.scan(self.page) if f.required and f.empty and f.ref not in exclude
                  and f.widget != "date"]
        if not fields:
            return
        dom.resolve_listbox_options(self.page, fields)
        intents, unresolved = [], []
        for f in fields:
            r = self.resolver.resolve(f.to_question())
            (intents.append(Intended(f, r.answer, r.tier)) if r.answer else unresolved.append(f))
        self.fill_and_verify(intents)
        if unresolved:
            self._ask_human_for(unresolved)

    def _ask_human_for(self, fields: list[dom.Field]) -> None:
        names = "\n".join(f"  - {f.label}" + (f"  options: {f.options}" if f.options else "") for f in fields)
        self.io.wait(f"No approved answer for these required questions; answer them in the browser:\n{names}")
        now = {x.ref: x for x in dom.scan(self.page)}
        for f in fields:
            cur = now.get(f.ref)
            if cur and not cur.empty:
                kind = "text" if f.kind in ("text", "textarea") else f.kind
                save = self.io.ask(f"Remember '{cur.value[:80]}' for \"{f.label}\" in the answer bank? [y/n]")
                if save.lower().startswith("y"):
                    self.bank.add(f.label, cur.value, kind=kind, options=f.options or [],
                                  source="manual", approved=True)
                self.record.human_answers.append({"question": f.label, "answer": cur.value})
                for r in self.resolver.log:
                    if r.question.ref == f.ref and r.answer is None:
                        r.answer = cur.value  # show what you chose in the review report

    def click_next(self) -> None:
        before = _text(self.page, S.PROGRESS_ACTIVE)
        btn = dom.first_visible(self.page, S.NEXT_BUTTON)
        if btn is None:
            raise NeedsHuman("no Next button visible")
        if "submit" in btn.inner_text().lower():
            raise NeedsHuman("Next button reads 'Submit': refusing to click it")
        self.pacer.think()
        btn.click()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            self.page.wait_for_timeout(500)
            if _text(self.page, S.PROGRESS_ACTIVE) != before or _visible(self.page, S.CONFIRMATION):
                return
            if _visible(self.page, S.FIELD_ERRORS) or _visible(self.page, S.ERROR_BANNER):
                errs = self.page.locator(S.FIELD_ERRORS).all_inner_texts()
                print(f"[workday] page has validation errors: {errs[:8]}")
                return  # the main loop revisits this step and escalates if it repeats

    def _hand_to_human(self, reason: str) -> None:
        self._screenshot(f"human-{int(time.time())}.png")
        self.io.wait(f"{reason}. Complete this page in the browser and move to the next one yourself.")

    # ==== My Experience details ===========================================================

    def _upload_resume(self) -> None:
        resume = Path(self.profile.resume_path).expanduser()
        inp = self.page.locator(S.FILE_INPUT).first
        if not inp.count():
            return
        if resume.name in self.page.content():
            return  # already attached on a previous visit
        inp.set_input_files(str(resume))  # attachment only: no "Autofill with Resume"
        self.pacer.think()

    def _fill_rows(self, section_sel: str, panel_sel: Callable[[int], str], items: list,
                   values: Callable[[object], dict[str, str]], field_sels: dict[str, str]) -> None:
        section = dom.first_visible(self.page, section_sel)
        if section is None or not items:
            return
        existing = 0
        while dom.first_visible(self.page, panel_sel(existing + 1)):
            existing += 1
        while existing > len(items):
            panel = dom.first_visible(self.page, panel_sel(existing))
            panel.locator(S.DELETE_ROW).first.click()
            self.pacer.pause()
            existing -= 1
        while existing < len(items):
            section.locator(S.ADD_BUTTON).last.click()
            self.pacer.pause()
            existing += 1
        for i, item in enumerate(items, start=1):
            panel = dom.first_visible(self.page, panel_sel(i))
            if panel is None:
                raise NeedsHuman(f"row {i} did not appear in {section_sel}")
            self.fill_and_verify(self._intend(panel, field_sels, values(item)))

    @staticmethod
    def _work_values(job: Job) -> dict[str, str]:
        v = {"title": job.title, "company": job.company, "location": job.location,
             "current": "true" if job.current else "false", "start": job.start.as_mmyyyy(),
             "description": job.description}
        if job.end:
            v["end"] = job.end.as_mmyyyy()
        return v

    @staticmethod
    def _edu_values(edu) -> dict[str, str]:
        v = {"school": edu.school, "degree": edu.degree, "field_of_study": json.dumps([edu.field_of_study]),
             "gpa": edu.gpa}
        if edu.start:
            v["start"] = str(edu.start.year)
        if edu.end:
            v["end"] = str(edu.end.year)
        return v

    def _fill_skills(self) -> None:
        container = dom.first_visible(self.page, S.SKILLS_CONTAINER)
        if container is None or not self.profile.skills:
            return
        have = {t.strip().lower() for t in container.locator(S.SELECTED_PILL).all_inner_texts()}
        for skill in self.profile.skills:
            if skill.lower() in have:
                continue
            try:
                dom.choose_prompt(self.page, container, skill, self.pacer, exact_only=True)
            except dom.FillError:
                self.record.notes.append(f"skill not in tenant list: {skill}")
                self.page.keyboard.press("Escape")

    def _fill_websites(self) -> None:
        urls = [u for u in (self.profile.contact.linkedin, self.profile.contact.website) if u]
        section = dom.first_visible(self.page, S.WEBSITE_SECTION)
        if section is None or not urls:
            return
        inputs = section.locator(S.WEBSITE_INPUT)
        present = {inputs.nth(i).input_value() for i in range(inputs.count())}
        for url in urls:
            if url in present:
                continue
            section.locator(S.ADD_BUTTON).last.click()
            self.pacer.pause()
            dom.type_text(section.locator(S.WEBSITE_INPUT).last, url, self.pacer)

    # ==== misc ============================================================================

    def _screenshot(self, name: str) -> str:
        path = self.run_dir / name
        try:
            self.page.screenshot(path=str(path), full_page=True)
        except Exception:
            return ""
        return str(path)


def _visible(page: Page, selector: str) -> bool:
    return dom.first_visible(page, selector) is not None


def _text(page: Page, selector: str) -> str:
    loc = dom.first_visible(page, selector)
    return " ".join(loc.inner_text().split()) if loc else ""
