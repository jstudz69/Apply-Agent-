import functools
import http.server
import os
import threading
from pathlib import Path

import pytest

from apply_agent.llm import LlmResult
from apply_agent.profile import Profile

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def profile(tmp_path) -> Profile:
    p = Profile.load(ROOT / "examples" / "profile.example.json")
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 test")
    return p.model_copy(update={"resume_path": str(resume)})


class FakeLlm:
    """Stands in for the model tiers; records every call so tests can assert cost."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.client = None

    def choose_option(self, question, options, profile_slice, *, multi=False):
        self.calls.append(("small", question))
        return LlmResult(options[-1], "fake-small", "picked last option")

    def write_answer(self, question, job_description, profile_slice, company, **kw):
        self.calls.append(("large", question))
        return LlmResult(f"I am excited about {company} because of forecasting work.", "fake-large")


@pytest.fixture
def fake_llm():
    return FakeLlm()


@pytest.fixture(scope="session")
def mock_server():
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    handler = functools.partial(Quiet, directory=str(FIXTURES))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/workday_mock.html"
    srv.shutdown()


@pytest.fixture(scope="session")
def browser():
    from playwright.sync_api import sync_playwright

    exe = os.environ.get("APPLY_AGENT_TEST_CHROMIUM", "/opt/pw-browsers/chromium")
    with sync_playwright() as pw:
        kwargs = {"headless": True}
        if Path(exe).exists():
            kwargs["executable_path"] = exe if Path(exe).is_file() else None
        try:
            b = pw.chromium.launch(**{k: v for k, v in kwargs.items() if v is not None})
        except Exception as e:  # no browser available in this environment
            pytest.skip(f"chromium unavailable: {e}")
        yield b
        b.close()


@pytest.fixture
def page(browser):
    ctx = browser.new_context()
    pg = ctx.new_page()
    yield pg
    ctx.close()
