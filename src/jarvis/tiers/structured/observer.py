"""See: turn any web page into compact text the planner can read — no per-site code.

One JavaScript pass over the current tab collects the visible interactive elements — semantic ones
(links, buttons, inputs, selects, ARIA widgets, contenteditable) and custom ones that only *look*
clickable (`cursor: pointer` divs: number pickers, "Done" boxes) — gives each a number (stored as
`data-jarvis-id` so the executor can find it again), and adds the URL, title, headings and a short
text excerpt. Elements on screen that something else covers (a popup, dropdown, dialog) are left
out — they can't be clicked — and the snapshot says a popup is in the way.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from playwright.async_api import Page

ID_ATTR = "data-jarvis-id"


class Element(BaseModel):
    id: int
    tag: str
    role: str = ""
    name: str = ""  # accessible name: aria-label / label / text / placeholder / alt / title
    type: str = ""  # input type
    href: str = ""
    value: str = ""  # never filled for password fields
    checked: bool | None = None
    disabled: bool = False
    password: bool = False
    otp: bool = False
    focused: bool = False


class Snapshot(BaseModel):
    url: str
    title: str
    headings: list[str] = Field(default_factory=list)
    text: str = ""
    elements: list[Element] = Field(default_factory=list)
    more_below: bool = False  # the page continues below what was captured
    covered: int = 0  # on-screen elements hidden behind a popup / overlay
    cover: str = ""  # what covers them: the popup's own text
    cover_share: float = 0.0  # how much of the screen that popup takes (0–1)

    def fingerprint(self) -> tuple[str, ...]:
        """What the page shows (address, headings, controls) — to tell progress from a loop."""
        return (self.url, *self.headings, *(f"{e.name}={e.value}" for e in self.elements))

    def by_id(self, element_id: int | None) -> Element | None:
        return next((e for e in self.elements if e.id == element_id), None)

    def render(self) -> str:
        lines = [f"URL: {self.url}", f"Title: {self.title}"]
        if self.headings:
            lines.append("Headings: " + " | ".join(self.headings))
        if self.text:
            lines.append(f"Text: {self.text}")
        lines.append("Elements:")
        for e in self.elements:
            kind = e.role or e.tag
            if e.type and e.type not in ("submit", "button"):
                kind += f"[{e.type}]"
            parts = [f"[{e.id}] {kind}", f'"{e.name}"' if e.name else '""']
            if e.href:
                parts.append(f"-> {e.href}")
            if e.value:
                parts.append(f'value="{e.value}"')
            if e.checked is not None:
                parts.append("checked" if e.checked else "unchecked")
            if e.disabled:
                parts.append("disabled")
            if e.password:
                parts.append("(password field)")
            if e.otp:
                parts.append("(one-time code field)")
            if e.focused:
                parts.append("(focused)")
            lines.append(" ".join(parts))
        if not self.elements:
            lines.append("(none)")
        if self.covered:
            size = "large" if self.cover_share >= 0.25 else "small"
            what = f' "{self.cover}"' if self.cover else ""
            lines.append(
                f"(a {size} popup or overlay{what} is covering {self.covered} other elements. If "
                "you need something behind it, finish or close it first (its Done / Close / "
                "Dismiss, or Escape); otherwise ignore it)"
            )
        if self.more_below:
            lines.append("(more content below — scroll to see it)")
        return "\n".join(lines)


_SNAPSHOT_JS = r"""([attr, maxEls, maxText]) => {
  document.querySelectorAll('[' + attr + ']').forEach(e => e.removeAttribute(attr));
  const sel = 'a[href], button, input:not([type=hidden]), select, textarea, summary, ' +
    '[role=button], [role=link], [role=tab], [role=menuitem], [role=checkbox], [role=radio], ' +
    '[role=switch], [role=option], [role=combobox], [role=searchbox], [role=textbox], ' +
    '[contenteditable=""], [contenteditable=true], [onclick], [tabindex]:not([tabindex="-1"])';
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const cut = (s, n) => s.length > n ? s.slice(0, n - 1) + '…' : s;
  const vh = window.innerHeight;
  const visible = el => {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    if (r.bottom < 0 || r.top > vh * 2) return false;  // the current screen and one more below
    const st = getComputedStyle(el);
    return st.visibility !== 'hidden' && st.display !== 'none' && st.opacity !== '0';
  };
  const labelFor = el => {
    if (el.getAttribute('aria-label')) return el.getAttribute('aria-label');
    const by = el.getAttribute('aria-labelledby');
    if (by) { const t = by.split(/\s+/).map(i => document.getElementById(i)).filter(Boolean)
                .map(n => n.innerText).join(' '); if (clean(t)) return t; }
    if (el.id) { const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
                 if (l && clean(l.innerText)) return l.innerText; }
    const wrap = el.closest('label'); if (wrap && clean(wrap.innerText)) return wrap.innerText;
    const tag = el.tagName.toLowerCase();
    if (!['input', 'textarea', 'select'].includes(tag) && clean(el.innerText)) return el.innerText;
    return el.getAttribute('placeholder') || el.getAttribute('title') || el.getAttribute('alt') ||
      (el.querySelector('img[alt]') || {}).alt || el.getAttribute('name') ||
      (tag === 'input' && ['submit', 'button'].includes(el.type) ? el.value : '') || '';
  };
  // custom controls: the outermost element that shows a pointer cursor and has a short label.
  // A row of same-kind items (<li>1</li><li>2</li>…) is a group, not one control — but a box the
  // page names (aria-label) or a label + value pair ("Departure From / New Delhi") is one control.
  // The cursor is often inherited from a whole popup, so a group/long parent doesn't count.
  const shortLabel = el => { const l = clean(el.getAttribute('aria-label') || el.innerText);
                             return l.length > 0 && l.length <= 60; };
  const isGroup = el => {
    if (el.getAttribute('aria-label')) return false;
    const tags = [...el.children].filter(c => clean(c.innerText)).map(c => c.tagName);
    return tags.some((tag, i) => tags.indexOf(tag) !== i);  // two or more items of one kind
  };
  const control = el => getComputedStyle(el).cursor === 'pointer' && shortLabel(el) && !isGroup(el);
  const pointerish = el => {
    if (!control(el) || el.querySelector(sel)) return false;  // a wrapper: list the real control
    const parent = el.parentElement;
    return !(parent && control(parent));  // use the outermost
  };
  // a listed element that holds real controls (a combobox with its popup inside) is a container:
  // its inner controls are listed too, instead of being swallowed by it
  const container = el => !!el.querySelector(sel);
  // is it the thing actually on top at its centre? (only checkable for on-screen elements)
  const onTop = el => {
    const r = el.getBoundingClientRect();
    const x = Math.min(Math.max(r.left + r.width / 2, 0), innerWidth - 1);
    const y = Math.min(Math.max(r.top + r.height / 2, 0), vh - 1);
    if (r.bottom <= 0 || r.top >= vh || r.right <= 0 || r.left >= innerWidth) return true;
    const top = document.elementFromPoint(x, y);
    if (!top || top === el || el.contains(top) || top.contains(el)) return true;
    const label = top.closest('label');
    if (label && (label.control === el || label.contains(el))) return true;
    // covered: remember by what — the popup itself, i.e. the nearest positioned container
    let box = top;
    for (let i = 0; i < 12 && box.parentElement && box.parentElement !== document.body; i++) {
      const p = getComputedStyle(box).position;
      if (p === 'fixed' || p === 'absolute' || p === 'sticky') break;
      box = box.parentElement;
    }
    covers.set(box, (covers.get(box) || 0) + 1);
    return false;
  };
  const covers = new Map();
  // what a custom box shows now, when its fixed label says something else ("Departure From
  // New Delhi" label, "Departure From Mumbai" on screen after the user changed it)
  const shown = el => {
    const label = clean(el.getAttribute('aria-label')), text = clean(el.innerText);
    return label && text && !label.includes(text) ? cut(text, 60) : '';
  };
  const out = []; let id = 0; let truncated = false; let covered = 0;
  for (const el of document.querySelectorAll('body *')) {
    const semantic = el.matches(sel);
    if (!semantic && !(el.children.length < 8 && pointerish(el))) continue;
    if (!visible(el)) continue;
    const isField = ['input', 'textarea', 'select'].includes(el.tagName.toLowerCase());
    const listed = el.parentElement && el.parentElement.closest('[' + attr + ']');
    if (listed && !isField && !container(listed)) continue;  // part of an already-listed control
    if (!onTop(el)) { covered += 1; continue; }  // behind a popup: can't be clicked now
    if (out.length >= maxEls) { truncated = true; break; }
    id += 1; el.setAttribute(attr, String(id));
    const tag = el.tagName.toLowerCase();
    const type = tag === 'input' ? (el.type || 'text') : '';
    const auto = (el.getAttribute('autocomplete') || '').toLowerCase();
    const ident = ((el.name || '') + ' ' + (el.id || '')).toLowerCase();
    const password = type === 'password';
    const otp = auto === 'one-time-code' || /\b(otp|one.?time|verification.?code)\b/.test(ident);
    let href = tag === 'a' ? (el.getAttribute('href') || '') : '';
    if (href.startsWith('javascript:')) href = '';
    out.push({
      id, tag, role: el.getAttribute('role') || '', type,
      name: cut(clean(labelFor(el)), 80), href: cut(href, 120),
      value: password ? '' : isField ? cut(clean(el.value), 60) : shown(el),
      checked: (type === 'checkbox' || type === 'radio') ? el.checked
        : (el.getAttribute('aria-checked') ? el.getAttribute('aria-checked') === 'true' : null),
      disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true',
      password, otp, focused: document.activeElement === el,
    });
  }
  const headings = [...document.querySelectorAll('h1, h2, h3')].filter(visible)
    .map(h => cut(clean(h.innerText), 80)).filter(Boolean).slice(0, 8);
  const text = cut(clean(document.body ? document.body.innerText : ''), maxText);
  let cover = '', coverShare = 0, most = 0;
  for (const [box, n] of covers) if (n > most) {
    most = n;
    const r = box.getBoundingClientRect();
    cover = cut(clean(box.getAttribute('aria-label') || box.innerText), 70);
    coverShare = Math.round(100 * Math.min(1, (r.width * r.height) / (innerWidth * vh))) / 100;
  }
  const moreBelow = truncated || document.documentElement.scrollHeight > window.scrollY + vh * 2;
  return {url: location.href, title: document.title, headings, text, elements: out,
          more_below: moreBelow, covered, cover, cover_share: coverShare};
}"""


# Wait until the page stops changing: no elements added/removed for `quiet` ms (capped at `limit`).
# Sites keep drawing content after "load" (search results, video lists, feeds); reading too early
# made the agent spend whole planning rounds on "it hasn't loaded yet — wait". Event-based, generic.
_SETTLE_JS = r"""([quiet, limit]) => new Promise(resolve => {
  const start = performance.now();
  let timer = null;
  const finish = () => {
    observer.disconnect(); clearTimeout(timer); resolve(performance.now() - start);
  };
  const observer = new MutationObserver(() => {
    clearTimeout(timer);
    timer = setTimeout(finish, quiet);
  });
  observer.observe(document.documentElement, {childList: true, subtree: true});
  timer = setTimeout(finish, quiet);
  setTimeout(finish, limit);
})"""

SETTLE_QUIET_MS = 500
SETTLE_LIMIT_MS = 3000


async def settle(
    page: Page, quiet_ms: int = SETTLE_QUIET_MS, limit_ms: int = SETTLE_LIMIT_MS
) -> float:
    """Milliseconds waited until the page went quiet (or the cap). Never raises."""
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=limit_ms)
        return float(await page.evaluate(_SETTLE_JS, [quiet_ms, limit_ms]))
    except Exception:
        return 0.0


async def still_there(page: Page, element_id: int) -> bool:
    """Is the numbered element still on the page (not re-rendered away)? Never raises."""
    try:
        return await page.locator(locator_for(element_id)).count() > 0
    except Exception:
        return True  # let the action itself report the problem


async def dom_size(page: Page) -> int:
    """How many elements the page has — a cheap "did something appear?" check. Never raises."""
    try:
        return int(await page.evaluate("document.getElementsByTagName('*').length"))
    except Exception:
        return -1


async def observe(page: Page, max_elements: int = 120, max_text_chars: int = 1500) -> Snapshot:
    await settle(page)
    raw = await page.evaluate(_SNAPSHOT_JS, [ID_ATTR, max_elements, max_text_chars])
    return Snapshot.model_validate(raw)


def locator_for(element_id: int) -> str:
    return f'[{ID_ATTR}="{element_id}"]'
