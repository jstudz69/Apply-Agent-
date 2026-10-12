import json

from apply_agent.answer_bank import AnswerBank
from apply_agent.resolver import Question, Resolver, match_option


def make(profile, tmp_path, llm=None, company="Acme"):
    return Resolver(profile, AnswerBank(tmp_path / "bank.json"), llm, company=company,
                    job_description="Data analyst role")


def test_match_option_yes_no_is_strict():
    opts = ["No, I am not authorized", "Yes, I am authorized"]
    assert match_option("Yes", opts) == "Yes, I am authorized"
    assert match_option("No", ["Yes", "Maybe"]) is None


def test_profile_tier_answers_screening_and_eeo(profile, tmp_path, fake_llm):
    r = make(profile, tmp_path, fake_llm)
    q = Question("Will you now or in the future require sponsorship?", "choice", ["Yes", "No"])
    assert (r.resolve(q).answer, r.log[-1].tier) == ("No", "profile")
    q = Question("Veteran Status", "choice", ["I am a protected veteran", "I am not a protected veteran"])
    assert r.resolve(q).answer == "I am not a protected veteran"
    assert fake_llm.calls == []


def test_eeo_never_goes_to_model(profile, tmp_path, fake_llm):
    r = make(profile, tmp_path, fake_llm)
    res = r.resolve(Question("Gender", "choice", ["Male", "Female"]))
    assert res.answer is None and res.tier == "human"
    assert fake_llm.calls == []


def test_bank_then_small_then_large(profile, tmp_path, fake_llm):
    r = make(profile, tmp_path, fake_llm)
    r.bank.add("Are you comfortable working in a hybrid environment?", "Yes", approved=True)
    assert r.resolve(Question("Are you comfortable working in a hybrid environment?*", "choice",
                              ["Yes", "No"])).tier == "exact"

    small = r.resolve(Question("Years of SQL experience?", "choice", ["0-1", "2-4", "5+"]))
    assert (small.tier, small.answer, small.needs_review) == ("small_model", "5+", True)
    assert not r.bank.entries["years of sql experience"].approved  # pending until you approve

    large = r.resolve(Question("Why are you interested in this role?", "textarea"))
    assert large.tier == "large_model" and "Acme" in large.answer
    assert [c[0] for c in fake_llm.calls] == ["small", "large"]


def test_second_application_is_free(profile, tmp_path, fake_llm):
    r = make(profile, tmp_path, fake_llm)
    q = Question("Years of SQL experience?", "choice", ["0-1", "2-4", "5+"])
    r.resolve(q)
    r.bank.approve("years of sql experience")
    fake_llm.calls.clear()
    assert r.resolve(Question("Years of SQL experience? *", "choice", ["0-1", "2-4", "5+"])).tier == "exact"
    assert fake_llm.calls == []


def test_multichoice_from_bank(profile, tmp_path):
    r = make(profile, tmp_path)
    r.bank.add("Which shifts can you work?", json.dumps(["Day", "Evening"]), kind="multichoice", approved=True)
    res = r.resolve(Question("Which shifts can you work?", "multichoice", ["Day", "Evening", "Night"]))
    assert json.loads(res.answer) == ["Day", "Evening"]


def test_no_llm_means_human(profile, tmp_path):
    r = make(profile, tmp_path, llm=None)
    assert r.resolve(Question("Years of SQL?", "choice", ["1", "2"])).tier == "human"
