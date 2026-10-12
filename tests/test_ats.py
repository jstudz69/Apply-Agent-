from apply_agent.ats import detect


def test_workday_pod_host():
    t = detect("https://acme.wd5.myworkdayjobs.com/en-US/External/job/Austin-TX/Data-Analyst_R123")
    assert (t.ats, t.tenant, t.pod, t.site) == ("workday", "acme", "wd5", "External")
    assert t.account_key == "workday:acme"


def test_workday_without_locale():
    t = detect("https://globex.wd1.myworkdayjobs.com/Careers/job/Remote/Engineer_JR1")
    assert (t.tenant, t.pod, t.site) == ("globex", "wd1", "Careers")


def test_workday_myworkdaysite():
    t = detect("https://wd3.myworkdaysite.com/recruiting/initech/Initech_Careers/job/X")
    assert (t.ats, t.tenant, t.site) == ("workday", "initech", "Initech_Careers")


def test_other_ats():
    assert detect("https://boards.greenhouse.io/stripe/jobs/123").ats == "greenhouse"
    assert detect("https://jobs.lever.co/netflix/abc").tenant == "netflix"
    assert detect("https://example.com/careers").ats == "unknown"
