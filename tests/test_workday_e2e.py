"""Drive the whole state machine against a local mock Workday tenant in a real browser."""

from pathlib import Path

from apply_agent.accounts.vault import MemoryVault
from apply_agent.answer_bank import AnswerBank
from apply_agent.ats import AtsTarget
from apply_agent.drivers.workday.driver import DriverOptions, WorkdayDriver
from apply_agent.pacing import Pacer
from apply_agent.resolver import Resolver

# Simulates you on the Review page: reads it, then clicks Submit yourself.
HUMAN_SUBMITS = """
setInterval(() => {
  const step = document.querySelector('[data-automation-id="progressBarActiveStep"]');
  if (step && step.textContent.includes('Review') && !window.__humanClicked) {
    window.__humanClicked = true;
    setTimeout(() => document.querySelector('[data-automation-id="bottom-navigation-next-button"]').click(), 2500);
  }
}, 200);
"""


class ScriptedHuman:
    def __init__(self, page):
        self.page = page
        self.waits: list[str] = []

    def wait(self, message):
        self.waits.append(message)
        if "consent to the terms" in message:
            self.page.get_by_text("I have read and consent").click()
        elif "account terms" in message:
            self.page.locator('label[for="terms"]').click()

    def ask(self, message):
        return "y"


def build(page, url, profile, tmp_path, llm, **opts):
    bank = AnswerBank(tmp_path / "bank.json")
    bank.add("Are you comfortable working in a hybrid environment?", "Yes", approved=True)
    vault = MemoryVault()
    human = ScriptedHuman(page)
    driver = WorkdayDriver(
        page=page, target=AtsTarget("workday", url, tenant="mockco"), profile=profile, bank=bank,
        resolver=Resolver(profile, bank, llm), vault=vault, pacer=Pacer(speed=1000, seed=1), io=human,
        options=DriverOptions(data_dir=tmp_path / "data", company="MockCo", **opts),
    )
    return driver, bank, vault, human


def submitted(page):
    return page.evaluate("window.__submitted")


def test_full_application_stops_at_review_and_logs_proof(page, mock_server, profile, tmp_path, fake_llm):
    page.add_init_script(HUMAN_SUBMITS)
    driver, bank, vault, human = build(page, mock_server, profile, tmp_path, fake_llm,
                                       confirmation_timeout_s=20)
    record = driver.run()

    assert record.status == "submitted"
    assert page.evaluate("window.__humanClicked") is True  # the human clicked Submit, not the driver
    run = Path(record.run_dir)
    assert (run / "review.png").exists() and (run / "confirmation.png").exists()
    assert "Application Submitted" in record.confirmation_text
    assert (tmp_path / "data" / "applications.jsonl").exists()

    # account layer: plus-addressed per-tenant account, stored before use
    cred = vault.get("workday:mockco")
    assert cred.email == "alex.rivera+mockco@gmail.com" and cred.verified

    sub = submitted(page)
    info = sub["My Information"]
    assert info["formField-legalNameSection_firstName"] == "Alex"
    assert info["formField-addressSection_countryRegion"] == "Texas"
    assert info["formField-addressSection_addressLine2"] == "Apt 4B"
    assert info["formField-candidateIsPreviousWorker"] == "No"
    assert info["formField-source"] == "LinkedIn"

    exp = sub["My Experience"]
    # the pre-existing row was overwritten, a second row added, dates typed segment by segment
    assert (exp["formField-jobTitle"], exp["formField-jobTitle#2"]) == ("Senior Data Analyst", "Data Analyst")
    assert exp["formField-currentlyWorkHere"] == "I currently work here"
    assert exp["formField-startDate"] == "03/2021"
    assert (exp["formField-startDate#2"], exp["formField-endDate"]) == ("06/2018", "02/2021")
    assert exp["formField-fieldOfStudy"] == "Economics"
    assert exp["formField-firstYearAttended"] == "2014"
    assert exp["formField-degree"] == "Bachelor's Degree"
    assert exp["formField-skills"] == "Python|SQL|Tableau"  # dbt isn't in the tenant's list

    qa = sub["Application Questions"]
    assert qa["formField-q1"] == "No"            # profile tier (sponsorship)
    assert qa["formField-q2"] == "Yes"           # profile tier (work authorization)
    assert qa["formField-q3"] == "5+"            # small model
    assert qa["formField-q4"] == "Yes"           # answer bank
    assert "MockCo" in qa["formField-q5"]        # large model essay

    vd = sub["Voluntary Disclosures"]
    assert vd["formField-veteranStatus"] == "I am not a protected veteran"
    assert vd["formField-gender"] == "I do not wish to self-identify"
    assert sub["Self Identify"]["formField-disabilityStatus"] == "I do not want to answer"
    assert sub["Self Identify"]["formField-name"] == "Alex Rivera"

    # model calls: exactly one cheap closed question and one essay
    assert sorted(c[0] for c in fake_llm.calls) == ["large", "small"]
    # the consent checkbox has no approved answer -> went to the human, then learned
    assert any("consent" in w for w in human.waits)
    assert bank.lookup("I have read and consent to the terms and conditions", kind="checkbox")
    # review report lists the model-written answers
    review = (run / "review.json").read_text()
    assert "years of SQL" in review and "large_model" in review


def test_second_run_reuses_bank_and_account(page, browser, mock_server, profile, tmp_path, fake_llm):
    page.add_init_script(HUMAN_SUBMITS)
    driver, bank, vault, _ = build(page, mock_server, profile, tmp_path, fake_llm, confirmation_timeout_s=20)
    driver.run()
    small_calls = sum(1 for c in fake_llm.calls if c[0] == "small")

    ctx = browser.new_context()
    page2 = ctx.new_page()
    page2.add_init_script(HUMAN_SUBMITS)
    # same tenant, account already exists in the mock (copy localStorage) and in the vault
    page2.goto(mock_server)
    page2.evaluate("acct => localStorage.setItem('acct', acct)", page.evaluate("localStorage.getItem('acct')"))
    human2 = type(driver.io)(page2)
    driver2 = WorkdayDriver(page=page2, target=driver.target, profile=profile, bank=bank,
                            resolver=Resolver(profile, bank, fake_llm), vault=vault,
                            pacer=Pacer(speed=1000), io=human2,
                            options=DriverOptions(data_dir=tmp_path / "data", company="MockCo",
                                                  confirmation_timeout_s=20))
    rec2 = driver2.run()
    ctx.close()
    assert rec2.status == "submitted"
    assert sum(1 for c in fake_llm.calls if c[0] == "small") == small_calls  # approved answers reused
    assert not any("account terms" in w for w in human2.waits)  # no second sign-up


def test_without_human_submit_run_is_abandoned_at_review(page, mock_server, profile, tmp_path, fake_llm):
    driver, *_ = build(page, mock_server, profile, tmp_path, fake_llm, confirmation_timeout_s=1)
    record = driver.run()
    assert record.status == "abandoned"
    assert page.locator('[data-automation-id="congratulationsPopup"]').count() == 0


def test_unknown_step_goes_to_fallback(page, mock_server, profile, tmp_path, fake_llm):
    page.add_init_script(HUMAN_SUBMITS)
    calls = []

    class Result:
        ok = True
        summary = "filled"
        actions = [{"ref": "x", "value": "Day"}]

    def fake_fallback(pg):
        calls.append(pg.url)
        pg.locator('[data-automation-id="formField-aq1"] button').click()
        pg.locator('[role="option"]', has_text="Day").click()
        return Result()

    driver, *_ = build(page, mock_server + "?extra=1", profile, tmp_path, fake_llm, confirmation_timeout_s=20)
    driver.fallback = fake_fallback
    record = driver.run()
    assert calls and record.status == "submitted"
    assert record.fallback_pages == ["Assessment"]


def test_targeting_filter_skips(page, mock_server, profile, tmp_path, fake_llm):
    profile.targeting.required_keywords_any = ["kubernetes"]
    driver, *_ = build(page, mock_server, profile, tmp_path, fake_llm)
    assert driver.run().status == "skipped"
