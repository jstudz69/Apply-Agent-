from apply_agent.accounts.gmail import extract_verification_link
from apply_agent.accounts.vault import MemoryVault, Credential, generate_password
from apply_agent.targeting import targeting_problems


def test_password_policy():
    for _ in range(50):
        pw = generate_password()
        assert len(pw) == 20 and any(c.isupper() for c in pw) and any(c.isdigit() for c in pw)


def test_vault_roundtrip():
    v = MemoryVault()
    v.put("workday:acme", Credential("a+acme@x.com", "pw"))
    assert v.get("workday:acme").email == "a+acme@x.com"


def test_extract_verification_link():
    body = ('<a href="https://acme.wd5.myworkdayjobs.com/External/login">login</a> '
            '<a href="https://acme.wd5.myworkdayjobs.com/External/activate/abc123?x=1&amp;y=2">Verify</a>')
    assert extract_verification_link(body) == "https://acme.wd5.myworkdayjobs.com/External/activate/abc123?x=1&y=2"


def test_targeting(profile):
    assert targeting_problems(profile, company="Acme", title="Data Analyst", description="SQL") == []
    probs = targeting_problems(profile, company="Acme", title="Sales Rep", description="commission only")
    assert len(probs) == 2
