"""Generic form scanning, filling and read-back for Workday-style widgets.

`scan` walks the live DOM once in the page, tags every field container with a stable
`data-aa-ref`, and returns compact descriptors (label, kind, options, current value). The
same pruned view feeds the deterministic filler, the read-back diff, and the fallback agent,
so we never need screenshots to understand a page.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from playwright.sync_api import Locator, Page
from playwright.sync_api import TimeoutError as PwTimeout

from ..pacing import Pacer
from ..resolver import Question
from .workday import selectors as S

SCAN_JS = r"""
(rootSel) => {
  const root = rootSel ? document.querySelector(rootSel) : document;
  if (!root) return [];
  const vis = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const text = el => clean(el ? (el.innerText || el.textContent) : '');
  window.__aaRef = window.__aaRef || 0;
  const ref = el => { if (!el.dataset.aaRef) el.dataset.aaRef = String(++window.__aaRef); return el.dataset.aaRef; };
  const labelOf = el => {
    const by = (el.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean)
      .map(id => text(document.getElementById(id))).filter(Boolean);
    if (by.length) return by.join(' ');
    if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) return text(l); }
    const wrap = el.closest('label'); if (wrap) return text(wrap);
    return clean(el.getAttribute('aria-label') || el.getAttribute('placeholder') || '');
  };
  const CONTROL = 'input:not([type=hidden]), textarea, select, button[aria-haspopup="listbox"]';
  let boxes = [...root.querySelectorAll('[data-automation-id^="formField-"], fieldset')]
    .filter(vis).filter(c => c.querySelector(CONTROL));
  boxes = boxes.filter(c => !boxes.some(o => o !== c && c.contains(o)));
  // Controls outside any container get their parent element as a pseudo-container.
  for (const el of root.querySelectorAll(CONTROL)) {
    if (!vis(el) && el.type !== 'radio' && el.type !== 'checkbox' && el.type !== 'file') continue;
    if (el.type === 'file' || boxes.some(b => b.contains(el))) continue;
    if (el.matches('button') && !el.matches('[aria-haspopup="listbox"]')) continue;
    boxes.push(el.parentElement);
  }
  const out = [];
  for (const box of boxes) {
    const radios = [...box.querySelectorAll('input[type=radio]')];
    const checks = [...box.querySelectorAll('input[type=checkbox]')];
    const listbtn = box.querySelector('button[aria-haspopup="listbox"]');
    const prompt = box.querySelector('[data-automation-id="multiselectInputContainer"], [data-uxi-widget-type="selectinput"]');
    const area = box.querySelector('textarea');
    const dates = box.querySelectorAll('input[data-automation-id^="dateSection"], input[id*="dateSection"]');
    const sel = box.querySelector('select');
    const inp = box.querySelector('input[type=text], input[type=email], input[type=tel], input[type=number], input:not([type])');
    const legend = box.querySelector('legend, label, [data-automation-id="richText"]');
    let label = clean(text(box.querySelector('legend')) || text(box.querySelector('label')));
    const opt = el => ({ label: labelOf(el) || clean(el.value), ref: ref(el), checked: el.checked });
    let d = null;
    if (radios.length) {
      const o = radios.map(opt);
      d = { kind: 'choice', widget: 'radio', options: o.map(x => x.label), optionRefs: o.map(x => x.ref),
            value: (o.find(x => x.checked) || {}).label || '' };
    } else if (checks.length > 1) {
      const o = checks.map(opt);
      d = { kind: 'multichoice', widget: 'checkboxes', options: o.map(x => x.label), optionRefs: o.map(x => x.ref),
            value: JSON.stringify(o.filter(x => x.checked).map(x => x.label)) };
    } else if (checks.length === 1 && !inp && !area && !listbtn) {
      if (!label || label === labelOf(checks[0])) label = labelOf(checks[0]) || label;
      d = { kind: 'checkbox', widget: 'checkbox', options: [], value: checks[0].checked ? 'true' : 'false' };
    } else if (listbtn) {
      const v = text(listbtn);
      d = { kind: 'choice', widget: 'listbox', options: null, value: /^select one$/i.test(v) ? '' : v };
      if (!label) label = labelOf(listbtn);
    } else if (prompt) {
      const pills = [...box.querySelectorAll('[data-automation-id="selectedItem"], [data-automation-id="selectedItemList"] [role="listitem"]')].map(text);
      d = { kind: 'multichoice', widget: 'prompt', options: null, value: JSON.stringify(pills) };
    } else if (dates.length) {
      d = { kind: 'date', widget: 'date', options: [], value: [...dates].map(x => x.value).join('/') };
    } else if (area) {
      d = { kind: 'textarea', widget: 'textarea', options: [], value: area.value,
            maxChars: area.maxLength > 0 ? area.maxLength : null };
      if (!label) label = labelOf(area);
    } else if (sel) {
      d = { kind: 'choice', widget: 'select', options: [...sel.options].map(o => clean(o.text)).filter(Boolean),
            value: sel.selectedIndex >= 0 ? clean(sel.options[sel.selectedIndex].text) : '' };
    } else if (inp) {
      d = { kind: 'text', widget: 'text', options: [], value: inp.value,
            maxChars: inp.maxLength > 0 ? inp.maxLength : null };
      if (!label) label = labelOf(inp);
    }
    if (!d) continue;
    const required = /\*/.test(label) || !!box.querySelector('[aria-required="true"], [required]');
    d.label = clean(label.replace(/\*/g, ''));
    d.required = required;
    d.ref = ref(box);
    d.automationId = box.getAttribute('data-automation-id') || '';
    out.push(d);
  }
  return out;
}
"""


@dataclass
class Field:
    ref: str
    label: str
    kind: str
    widget: str
    value: str
    required: bool = False
    options: list[str] | None = None
    option_refs: list[str] = field(default_factory=list)
    max_chars: int | None = None
    automation_id: str = ""

    @property
    def empty(self) -> bool:
        return self.value in ("", "false", "[]")

    def to_question(self) -> Question:
        return Question(label=self.label, kind=self.kind, options=self.options or [],  # type: ignore[arg-type]
                        required=self.required, max_chars=self.max_chars, ref=self.ref)


def scan(page: Page, root_selector: str | None = None) -> list[Field]:
    raw = page.evaluate(SCAN_JS, root_selector)
    return [Field(ref=d["ref"], label=d["label"], kind=d["kind"], widget=d["widget"],
                  value=d.get("value") or "", required=d.get("required", False),
                  options=d.get("options"), option_refs=d.get("optionRefs") or [],
                  max_chars=d.get("maxChars"), automation_id=d.get("automationId", ""))
            for d in raw]


def by_ref(page: Page, ref: str) -> Locator:
    return page.locator(f'[data-aa-ref="{ref}"]')


def first_visible(scope: Page | Locator, selector: str) -> Locator | None:
    loc = scope.locator(selector)
    for i in range(loc.count()):
        if loc.nth(i).is_visible():
            return loc.nth(i)
    return None


# ---- widget primitives ---------------------------------------------------------------

def type_text(loc: Locator, value: str, pacer: Pacer) -> None:
    loc.click()
    loc.fill("")
    if len(value) > 200:  # long answers: paste-like fill, still with a human pause after
        loc.fill(value)
    else:
        loc.press_sequentially(value, delay=pacer.key_delay_ms())
    loc.blur()  # Workday commits the value on blur
    pacer.pause(0.2, 0.6)


def open_listbox(page: Page, button: Locator) -> list[str]:
    button.click()
    page.locator(S.LISTBOX).first.wait_for(state="visible", timeout=5000)
    return [t.strip() for t in page.locator(S.LISTBOX_OPTION).all_inner_texts() if t.strip()]


def choose_listbox(page: Page, button: Locator, option: str, pacer: Pacer) -> None:
    options = open_listbox(page, button)
    pacer.pause(0.2, 0.5)
    if option not in options:
        page.keyboard.press("Escape")
        raise FillError(f"option {option!r} not in {options}")
    page.locator(S.LISTBOX_OPTION).filter(has_text=option).first.click()
    pacer.pause()


def choose_prompt(page: Page, container: Locator, option: str, pacer: Pacer, *, exact_only: bool = True) -> None:
    """Workday's searchable multiselect: type, Enter, then pick the matching suggestion."""
    inp = container.locator("input").first
    inp.click()
    inp.fill("")
    inp.press_sequentially(option, delay=pacer.key_delay_ms())
    inp.press("Enter")
    opts = page.locator(S.PROMPT_OPTION)
    try:
        opts.first.wait_for(state="visible", timeout=5000)
    except PwTimeout:
        page.keyboard.press("Escape")
        raise FillError(f"no suggestions for {option!r}") from None
    exact = opts.filter(has_text=option)
    if exact.count():
        exact.first.click()
    elif exact_only:
        page.keyboard.press("Escape")
        raise FillError(f"no suggestion matching {option!r}")
    else:
        opts.first.click()
    pacer.pause()


def set_date(container: Locator, value: str, pacer: Pacer) -> None:
    """value is MM/YYYY, MM/DD/YYYY or YYYY. Workday auto-advances between segments."""
    first = container.locator(f"{S.DATE_MONTH}, {S.DATE_DAY}, {S.DATE_YEAR}").first
    first.click()
    first.press_sequentially(value.replace("/", ""), delay=pacer.key_delay_ms())
    pacer.pause()


class FillError(RuntimeError):
    pass


def fill_field(page: Page, f: Field, answer: str, pacer: Pacer) -> None:
    box = by_ref(page, f.ref)
    if f.widget == "text":
        type_text(box.locator("input").first, answer, pacer)
    elif f.widget == "textarea":
        type_text(box.locator("textarea").first, answer, pacer)
    elif f.widget == "listbox":
        choose_listbox(page, box.locator('button[aria-haspopup="listbox"]').first, answer, pacer)
    elif f.widget == "select":
        box.locator("select").first.select_option(label=answer)
    elif f.widget == "prompt":
        wanted = json.loads(answer) if answer.startswith("[") else [answer]
        current = set(json.loads(f.value or "[]"))
        for w in wanted:
            if w not in current:
                choose_prompt(page, box, w, pacer)
    elif f.widget in ("radio", "checkboxes"):
        wanted = json.loads(answer) if answer.startswith("[") else [answer]
        for label, oref in zip(f.options or [], f.option_refs):
            el = by_ref(page, oref)
            if (label in wanted) != el.is_checked():
                _click_choice(page, el)
                pacer.pause(0.2, 0.6)
    elif f.widget == "checkbox":
        el = box.locator('input[type="checkbox"]').first
        if (answer.lower() in ("true", "yes", "checked")) != el.is_checked():
            _click_choice(page, el)
    elif f.widget == "date":
        set_date(box, answer, pacer)
    else:
        raise FillError(f"unsupported widget {f.widget}")


def _click_choice(page: Page, el: Locator) -> None:
    """Workday hides the real input under a styled label; clicking the label is what React sees."""
    el_id = el.get_attribute("id")
    label = page.locator(f'label[for="{el_id}"]') if el_id else None
    if label is not None and label.count() and label.first.is_visible():
        label.first.click()
    else:
        el.click(force=True)


def resolve_listbox_options(page: Page, fields: list[Field]) -> None:
    """Listbox options only exist in the DOM while open; read them so the resolver can match."""
    for f in fields:
        if f.widget == "listbox" and f.options is None:
            btn = by_ref(page, f.ref).locator('button[aria-haspopup="listbox"]').first
            f.options = [o for o in open_listbox(page, btn) if o.lower() != "select one"]
            page.keyboard.press("Escape")
        elif f.widget == "prompt" and f.options is None:
            f.options = []  # searchable prompt: options come from typing; resolver treats as text


def values_match(kind: str, intended: str, actual: str) -> bool:
    norm = lambda s: " ".join(str(s).lower().split())
    if kind == "multichoice":
        a = intended if intended.startswith("[") else json.dumps([intended])
        return sorted(map(norm, json.loads(a))) == sorted(map(norm, json.loads(actual or "[]")))
    if kind == "date":
        parts = lambda s: [int(p) for p in str(s).replace("-", "/").split("/") if p.strip().isdigit()]
        return parts(intended) == parts(actual)
    return norm(intended) == norm(actual)
