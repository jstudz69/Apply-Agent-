from apply_agent.answer_bank import AnswerBank, intent_of, normalize


def test_normalize_strips_noise():
    assert normalize("Are you 18+ years old? *(Required)") == "are you 18 years old"


def test_intent_rules_cover_sponsorship_phrasings():
    for q in [
        "Will you now or in the future require sponsorship for employment visa status?",
        "Do you require H-1B sponsorship, now or in the future?",
        "Will you, in the future, need the company to sponsor a visa?",
    ]:
        assert intent_of(q) == "sponsorship_future", q
    assert intent_of("Do you currently require visa sponsorship?") == "sponsorship"
    assert intent_of("Are you legally authorized to work in the United States?") == "work_authorization"


def test_lookup_tiers(tmp_path):
    bank = AnswerBank(tmp_path / "bank.json")
    bank.add("Are you comfortable working in a hybrid environment?", "Yes", approved=True)
    bank.add("Do you hold an active security clearance?", "No", approved=False)

    assert bank.lookup("Are you comfortable working in a hybrid environment? *").tier == "exact"
    fuzzy = bank.lookup("Are you comfortable with working in a hybrid work environment")
    assert fuzzy and fuzzy.tier == "fuzzy"
    # unapproved entries are invisible until approved
    assert bank.lookup("Do you hold an active security clearance?") is None
    assert bank.lookup("Do you hold an active security clearance?", approved_only=False)
    assert bank.lookup("What is your favourite programming language?") is None


def test_kind_separates_essays_from_reusable_text(tmp_path):
    bank = AnswerBank(tmp_path / "bank.json")
    bank.add("Why do you want to work here? [Acme]", "Because Acme...", kind="essay", approved=True)
    assert bank.lookup("Why do you want to work here?", kind="text") is None


def test_roundtrip_and_approve(tmp_path):
    path = tmp_path / "bank.json"
    bank = AnswerBank(path)
    bank.add("Preferred pronouns?", "they/them", kind="text")
    bank.approve("preferred pronouns", "she/her")
    bank.save()
    again = AnswerBank(path)
    e = again.entries["preferred pronouns"]
    assert e.approved and e.answer == "she/her" and e.approved_at
