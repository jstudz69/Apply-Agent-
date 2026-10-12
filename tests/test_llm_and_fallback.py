import json
from types import SimpleNamespace as NS

from apply_agent.fallback import run_fallback
from apply_agent.llm import DECLINE, Llm
from apply_agent.pacing import Pacer


class FakeMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def create(self, **kw):
        self.requests.append(kw)
        return self.responses.pop(0)


def usage():
    return NS(input_tokens=10, output_tokens=5)


def test_choose_option_constrains_output_to_options():
    msgs = FakeMessages([NS(stop_reason="end_turn", model="claude-haiku-5-5", usage=usage(),
                            content=[NS(type="text", text=json.dumps({"answer": "2-4", "rationale": "r"}))])])
    llm = Llm(client=NS(messages=msgs))
    r = llm.choose_option("Years of SQL?", ["0-1", "2-4"], {"skills": ["SQL"]})
    assert r.answer == "2-4"
    req = msgs.requests[0]
    schema = req["output_config"]["format"]["schema"]
    assert schema["properties"]["answer"]["enum"] == ["0-1", "2-4", DECLINE]
    assert req["model"] == "claude-haiku-5-5"


def test_choose_option_refusal_goes_to_human():
    msgs = FakeMessages([NS(stop_reason="refusal", model="m", usage=usage(), content=[])])
    assert Llm(client=NS(messages=msgs)).choose_option("q", ["a"], {}).answer == DECLINE


PAGE = """
<main><h2>Assessment</h2>
<fieldset data-automation-id="formField-shift"><legend>Preferred shift*</legend>
  <input type="radio" id="d" name="s"><label for="d">Day</label>
  <input type="radio" id="n" name="s"><label for="n">Night</label></fieldset>
<button id="sub" onclick="window.__submitted=true">Submit</button>
<button id="nx">Next</button></main>
"""


def tool_use(i, name, **inp):
    return NS(type="tool_use", id=f"t{i}", name=name, input=inp)


def test_fallback_agent_fills_page_and_cannot_submit(page, profile):
    page.set_content(PAGE)
    from apply_agent.fallback import page_snapshot
    snap = page_snapshot(page)
    shift = next(f for f in snap["fields"] if f["label"] == "Preferred shift")
    assert shift["options"] == ["Day", "Night"]
    assert all("Submit" not in b["text"] for b in snap["buttons"])  # submit is hidden from the model
    submit_ref = page.evaluate("() => { const b = document.getElementById('sub'); b.dataset.aaRef = 'S'; return 'S'; }")

    msgs = FakeMessages([
        NS(stop_reason="tool_use", content=[tool_use(1, "set_field", ref=shift["ref"], value="Day"),
                                            tool_use(2, "click_button", ref=submit_ref)]),
        NS(stop_reason="tool_use", content=[tool_use(3, "finish", summary="done")]),
    ])
    client = NS(beta=NS(messages=msgs))
    result = run_fallback(page, profile, Pacer(speed=1000), client=client)
    assert result.ok and result.summary == "done"
    assert page.locator("#d").is_checked()
    assert not page.evaluate("window.__submitted === true")
    second_turn = msgs.requests[1]["messages"][2]["content"]  # tool results for turn 1
    assert any(c.get("is_error") for c in second_turn if isinstance(c, dict))  # submit refused
    assert msgs.requests[0]["fallbacks"] == "default"
