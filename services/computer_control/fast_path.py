"""
Deterministic fast path (docs/07_Computer_Control.md Section 13.2; CC-1a Step 7).

Pure and provider-independent: a small, explicit grammar over the user's utterance plus the bounded
ChooserInput summary. No OS access, no LLM, no fuzzy matching, no randomness, no hidden state. It
returns STOP (session-level cancel), a raw chooser-format DECISION (validated by the chooser boundary
exactly like a provider's output), or NO_MATCH. It never judges safety - the session's gate does.

Grammar (case-insensitive; evaluated in this order):
  stop       "stop" | "stop it" | "cancel" | "cancel that" | "abort" | "never mind"      -> STOP
  done       "done" | "all done" | "that's all" | "that is all" | "i'm done" | "finished" -> DONE (a claim)
  set_text   ("type" | "write" | "enter") <text>   -> text = utterance[start:end], EXACT
             (the command word and the whitespace after it are the prefix; nothing else is trimmed)
  press      [press|click|tap [on] [the]] ("align left" | "align center" | "align right" | "fully justify")
             [button]   -> only the validated TextEdit alignment segments, unique in the observation
  click      ("click"|"tap"|"press"|"select") [on] [the] <label> [button|link|tab|...] -> click_element on the ONE
             observed control whose visible label is exactly <label> (validated control classes, never secure or
             disabled); pronoun -> ASK; several -> ASK; none -> BLOCKED; double-click -> BLOCKED
  select all "select all|everything|all text" -> select_text of the whole focused text element
  copy       "copy" [it|this|that|the selection|...]            -> copy_selection (the worker checks the selection)
  paste      "paste" [it|here|the clipboard|...]                -> paste_clipboard into the focused text element
  scroll     "scroll" [a little|a bit|slightly|a lot] (up|down) [a little|...]   -> bounded amount
  activate   "switch to X" | "go to X" | "open X" | "activate X" | "bring X forward|to the front"
             -> activate_app of an ALREADY-RUNNING app (never a launch)
  blocked    close, quit, delete, send, run, launch, search, go back, ... -> BLOCKED (not enabled)
  otherwise  NO_MATCH (the boundary turns this into ASK when no provider is configured)
"""

import re
from typing import Any, Dict, Literal, Optional, Sequence, Tuple

from pydantic import Field

from services.computer_control.chooser import AppRef, ChooserInput
from services.computer_control.models import APP_ALIASES, Op, _Strict, normalize

FastPathKind = Literal["STOP", "DECISION", "NO_MATCH"]

STOP_PHRASES = frozenset({"stop", "stop it", "stop now", "cancel", "cancel that", "abort", "never mind", "nevermind"})
DONE_PHRASES = frozenset({"done", "all done", "that's all", "that is all", "i'm done", "im done", "finished"})
PRONOUNS = frozenset({"it", "that", "this", "them", "there", "here", "these", "those", "one"})
ALIGNMENT_LABELS = ("align left", "align center", "align right", "fully justify")
# Explicit, closed aliases only: the shared table in models (also used by the allowed-app scope). No fuzzy matching.
SMALL_SCROLL, DEFAULT_SCROLL = 0.1, 0.25             # 0.25 = the validated bound (ScrollArgs max)

BLOCKED_VERBS = {
    "delete": "DESTRUCTIVE_NOT_ENABLED", "remove": "DESTRUCTIVE_NOT_ENABLED", "erase": "DESTRUCTIVE_NOT_ENABLED",
    "trash": "DESTRUCTIVE_NOT_ENABLED", "send": "SENDING_NOT_ENABLED", "email": "SENDING_NOT_ENABLED",
    "message": "SENDING_NOT_ENABLED", "reply": "SENDING_NOT_ENABLED", "run": "COMMANDS_NOT_ENABLED",
    "execute": "COMMANDS_NOT_ENABLED", "launch": "LAUNCH_NOT_ENABLED", "install": "INSTALL_NOT_ENABLED",
    "uninstall": "INSTALL_NOT_ENABLED", "buy": "PURCHASE_NOT_ENABLED", "pay": "PURCHASE_NOT_ENABLED",
    "purchase": "PURCHASE_NOT_ENABLED", "download": "DOWNLOAD_NOT_ENABLED", "save": "SAVE_NOT_ENABLED",
    "rename": "FILES_NOT_ENABLED", "move": "FILES_NOT_ENABLED", "copy": "UNSUPPORTED_COPY_FORM",
    "paste": "UNSUPPORTED_PASTE_FORM", "drag": "DRAG_NOT_ENABLED", "search": "BROWSER_NOT_ENABLED",
    "navigate": "BROWSER_NOT_ENABLED", "reload": "BROWSER_NOT_ENABLED", "refresh": "BROWSER_NOT_ENABLED",
    "shutdown": "SYSTEM_NOT_ENABLED", "restart": "SYSTEM_NOT_ENABLED", "reboot": "SYSTEM_NOT_ENABLED",
    "print": "PRINT_NOT_ENABLED",
}

_END = r"\s*[.!?]?\s*$"
_TEXT = re.compile(r"^\s*(?P<verb>type|write|enter)(?:(?P<sep>\s+)(?P<text>\S.*))?\s*$", re.I | re.S)
_ALIGN = re.compile(r"^\s*(?:(?:press|click|tap)\s+(?:on\s+)?(?:the\s+)?)?"
                    r"(?P<label>align\s+left|align\s+center|align\s+right|fully\s+justify)(?:\s+button)?" + _END, re.I)
_CLICK = re.compile(r"^\s*(?P<verb>click|tap|press|select|double[- ]click)\b(?P<rest>.*)$", re.I | re.S)
_CONTROL_NOUNS = re.compile(r"\s+(?:button|link|tab|checkbox|check\s+box|radio\s+button|menu\s+item|option|item)$")
_COPY = re.compile(r"^\s*copy(?:\s+(?:it|this|that|selection|selected\s+text|the\s+selection|the\s+selected\s+text|"
                   r"the\s+text))?" + _END, re.I)
_SELECT_ALL = re.compile(r"^\s*select\s+(?:all|everything|all\s+(?:the\s+)?text)" + _END, re.I)
_PASTE = re.compile(r"^\s*paste(?:\s+(?:it|this|that|the\s+clipboard))?(?:\s+(?:here|in\s+here|there))?" + _END, re.I)
_SCROLL = re.compile(r"^\s*scroll\b(?P<rest>.*)$", re.I | re.S)
_SCROLL_FORM = re.compile(r"^\s*(?P<a1>a\s+little|a\s+bit|slightly|a\s+lot)?\s*(?P<dir>up|down|left|right)?\s*"
                          r"(?P<a2>a\s+little|a\s+bit|slightly|a\s+lot)?" + _END, re.I)
_ALIGN_PHRASE = re.compile(r"^\s*(?P<verb>center|left[- ]align|right[- ]align|justify)\s+(?:this\s+paragraph|it)"
                           + _END, re.I)
_ALIGN_PHRASE_LABEL = {"center": "align center", "left": "align left", "right": "align right", "justify": "fully justify"}
_ACTIVATE = (
    re.compile(r"^\s*(?:switch|change)\s+to\s+(?P<name>.*?)" + _END, re.I),
    re.compile(r"^\s*switch\s+back\s+to\s+(?P<name>.*?)" + _END, re.I),
    re.compile(r"^\s*go\s+to\s+(?P<name>.*?)" + _END, re.I),
    re.compile(r"^\s*open\s+(?P<name>.*?)" + _END, re.I),
    re.compile(r"^\s*activate\s+(?P<name>.*?)" + _END, re.I),
    re.compile(r"^\s*bring\s+(?P<name>.*?)\s+(?:forward|to\s+the\s+front|to\s+front)" + _END, re.I),
)
_QUIT_ALL = re.compile(r"^\s*(?:quit|close|exit|terminate)\s+(?:all|everything|all\s+apps|all\s+applications|all\s+windows)" + _END, re.I)
_CLOSE_WINDOW_PATTERN = re.compile(r"^\s*close\s+(?:this\s+window|that\s+window|current\s+window|the\s+window|window)" + _END, re.I)
_QUIT_THIS_APP = re.compile(r"^\s*(?:quit|close)\s+(?:this\s+app|current\s+app|the\s+current\s+application|current\s+application|this\s+application)" + _END, re.I)
_QUIT_APP_PATTERN = re.compile(r"^\s*(?:quit|exit|terminate)\s+(?P<name>.*?)" + _END, re.I)
_CLOSE_APP_PATTERN = re.compile(r"^\s*close\s+(?P<name>.*?)" + _END, re.I)
_OPEN_BARE = re.compile(r"^\s*(?:open|switch\s+to|go\s+to|activate)" + _END, re.I)
_GO_NAV = re.compile(r"^\s*go\s+(?:back|forward)\b", re.I)


class FastPathResult(_Strict):
    kind: FastPathKind
    rule: Optional[str] = Field(None, max_length=32)
    decision: Optional[Dict[str, Any]] = None           # raw chooser-format decision (validated by the boundary)


def text_payload_span(utterance: str) -> Optional[Tuple[int, int]]:
    """
    The EXACT text a type/write/enter command carries, as [start, end) offsets into the utterance: everything
    after the command word and the whitespace after it - nothing else is trimmed or normalised. None when the
    utterance is not a text command with a payload. Single source of truth for the fast path and hardening.
    """
    m = _TEXT.match(utterance)
    if not m or m.group("text") is None:
        return None
    return (m.start("text"), len(utterance))


def _decision(rule: str, raw: Dict[str, Any]) -> FastPathResult:
    return FastPathResult(kind="DECISION", rule=rule, decision=raw)


def _ask(rule: str, reason: str, question: str, candidates: Sequence[int] = ()) -> FastPathResult:
    return _decision(rule, {"status": "ASK", "reason": reason, "question": question,
                            "candidates": list(candidates)[:10]})


def _blocked(rule: str, reason: str) -> FastPathResult:
    return _decision(rule, {"status": "BLOCKED", "reason": reason})


def _action(rule: str, ci: ChooserInput, op: Op, target: Optional[int] = None,
            args: Optional[Dict[str, Any]] = None) -> FastPathResult:
    raw: Dict[str, Any] = {"status": "ACTION", "op": op.value, "args": args or {}, "obs_id": ci.obs_id}
    if target is not None:
        raw["target_index"] = target
    return _decision(rule, raw)


def in_actionable_window(ci: ChooserInput, t) -> bool:
    """The gate runs target operations only in the app's main window (wrong-document guard), so a control in
    another window is never a candidate. When the observation reports no main window, nothing is filtered."""
    main = {w.index for w in ci.windows if w.is_main}
    return not main or t.window_index in main


def _targets_for(ci: ChooserInput, op: Op):
    """Targets whose validated control class fits op; never secure, never explicitly disabled."""
    return [t for t in ci.targets if op.value in t.ops and not t.secure and t.enabled is not False
            and in_actionable_window(ci, t)]


_DIGIT_WORDS = {"0": "zero", "1": "one", "2": "two", "3": "three", "4": "four", "5": "five", "6": "six",
                "7": "seven", "8": "eight", "9": "nine"}


def control_name(t) -> Optional[str]:
    """The name a person would use for a control: its visible label; for an UNLABELLED control only, its
    accessibility identifier split into words ("AllClear" -> "all clear"). Digits are spelled out so "7" and
    "seven" compare equal. Exact comparison only - no fuzzy matching."""
    name = t.label
    if not name and t.identifier and re.fullmatch(r"[A-Za-z][A-Za-z0-9 _-]{0,40}", t.identifier):
        name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|[_-]", " ", t.identifier)
    return spoken(name)


def spoken(text: Optional[str]) -> Optional[str]:
    said = normalize(text)
    return " ".join(_DIGIT_WORDS.get(w, w) for w in said.split()) if said else None


def _unique_target(rule: str, ci: ChooserInput, op: Op, candidates, none_reason: str, question: str,
                   args: Optional[Dict[str, Any]] = None) -> FastPathResult:
    if len(candidates) == 1:
        return _action(rule, ci, op, candidates[0].index, args)
    if not candidates:
        return _blocked(rule, none_reason)
    return _ask(rule, "AMBIGUOUS_TARGET", question, [t.index for t in candidates])


def _app_name(raw: str) -> str:
    name = normalize(raw) or ""
    name = re.sub(r"^the\s+", "", name)
    return re.sub(r"\s+(?:app|application)$", "", name)


def _clean_slug(text: Optional[str]) -> str:
    if not text:
        return ""
    cleaned = re.sub(r"\s+", " ", text).strip().casefold()
    return re.sub(r"[^a-z0-9]", "", cleaned)


def _matches_app(req_norm: str, req_slug: str, app: AppRef) -> bool:
    app_norm = normalize(app.name) or ""
    app_slug = _clean_slug(app.name)
    last_seg = app.bundle_id.split(".")[-1].casefold()
    last_slug = _clean_slug(last_seg)
    alias = APP_ALIASES.get(req_norm)
    if alias and app.bundle_id == alias:
        return True
    if req_norm and (req_norm == app_norm or req_norm == last_seg):
        return True
    if req_slug and (req_slug == app_slug or req_slug == last_slug):
        return True
    app_words = set(re.findall(r"[a-z0-9]+", app_norm))
    req_words = set(re.findall(r"[a-z0-9]+", req_norm))
    if req_words and req_words.issubset(app_words):
        return True
    return False


def resolve_app(name: str, running: Sequence[AppRef], installed: Sequence[AppRef] = ()):
    """Dynamic resolution: exact or normalized name / slug / token match; unique; running or installed."""
    if not name:
        return "ASK", "APP_REQUIRED"
    if name in PRONOUNS or all(w in PRONOUNS for w in name.split()):
        return "ASK", "UNRESOLVED_REFERENCE"

    req_name = _app_name(name)
    if not req_name:
        return "ASK", "APP_REQUIRED"
    req_slug = _clean_slug(req_name)

    # 1. Match currently running apps
    running_matches = {a.bundle_id for a in running if _matches_app(req_name, req_slug, a)}
    if len(running_matches) == 1:
        return "OK", running_matches.pop()
    if len(running_matches) > 1:
        return "ASK", "AMBIGUOUS_APP"

    # 2. Match installed apps
    installed_matches = {a.bundle_id for a in installed if _matches_app(req_name, req_slug, a)}
    if len(installed_matches) == 1:
        return "OK", installed_matches.pop()
    if len(installed_matches) > 1:
        return "ASK", "AMBIGUOUS_APP"

    return "ASK", "UNKNOWN_APP"


def fast_path(ci: ChooserInput, installed_apps: Sequence[AppRef] = ()) -> FastPathResult:
    utterance = ci.utterance
    said = normalize(utterance) or ""
    bare = said.rstrip(".!? ")

    if bare in STOP_PHRASES:
        return FastPathResult(kind="STOP", rule="stop")
    if bare in DONE_PHRASES:
        return _decision("done", {"status": "DONE"})

    m = _TEXT.match(utterance)
    if m:
        if m.group("text") is None:
            return _ask("type", "TEXT_REQUIRED", "What should I type?")
        span = list(text_payload_span(utterance))         # exact: nothing after the prefix is rewritten
        candidates = _targets_for(ci, Op.SET_TEXT)
        focused = [t for t in candidates if t.focused is True]           # typing goes where the keyboard focus is
        return _unique_target("type", ci, Op.SET_TEXT, focused if len(focused) == 1 else candidates, "NO_TEXT_TARGET",
                              "There is more than one text field here. Which one should I type into?",
                              {"text_span": span, "mode": "insert_at_cursor"})

    m = _ALIGN.match(utterance)
    phrase = None if m else _ALIGN_PHRASE.match(utterance)
    if m or phrase:
        label = normalize(m.group("label")) if m else \
            _ALIGN_PHRASE_LABEL[re.split(r"[- ]", phrase.group("verb").lower())[0]]
        hits = [t for t in _targets_for(ci, Op.PRESS) if normalize(t.label) == label]
        return _unique_target("align", ci, Op.PRESS, hits, "ALIGNMENT_CONTROL_NOT_AVAILABLE",
                              "There is more than one matching alignment control. Which one?")

    if _SELECT_ALL.match(utterance):
        candidates = _targets_for(ci, Op.SELECT_TEXT)
        focused = [t for t in candidates if t.focused is True]
        return _unique_target("select_all", ci, Op.SELECT_TEXT, focused if len(focused) == 1 else candidates,
                              "NO_TEXT_TARGET", "Which text should I select?", {"location": 0, "length": 100_000})

    m = _CLICK.match(utterance)
    if m:
        rest = _app_name(m.group("rest"))
        if not rest or all(w in PRONOUNS for w in rest.split()):
            return _ask("click", "UNRESOLVED_REFERENCE", "What should I click?")
        if m.group("verb").lower().startswith("double"):
            return _blocked("click", "DOUBLE_CLICK_NOT_ENABLED")
        label = _CONTROL_NOUNS.sub("", re.sub(r"^on\s+", "", rest.rstrip(".!? "))).strip()
        # Spacing-insensitive, still exact: "AllClear", "all clear" and "allclear" name the same control.
        wanted = (spoken(label) or "").replace(" ", "")
        named = [t for t in ci.targets if wanted and (control_name(t) or "").replace(" ", "") == wanted]
        hits = [t for t in _targets_for(ci, Op.CLICK_ELEMENT) if t in named]
        if not hits and any(t.enabled is False for t in named):
            return _blocked("click", "CONTROL_DISABLED")
        return _unique_target("click", ci, Op.CLICK_ELEMENT, hits, "CONTROL_NOT_FOUND",
                              "More than one control has that name. Which one?")

    if _COPY.match(utterance):
        return _action("copy", ci, Op.COPY_SELECTION)
    if _PASTE.match(utterance):
        candidates = _targets_for(ci, Op.PASTE_CLIPBOARD)
        focused = [t for t in candidates if t.focused is True]
        return _unique_target("paste", ci, Op.PASTE_CLIPBOARD, focused if len(focused) == 1 else candidates,
                              "NO_TEXT_TARGET", "Which field should I paste into?")

    m = _SCROLL.match(utterance)
    if m:
        rest = m.group("rest")
        if re.search(r"\d|pixel|\bpx\b|,", rest, re.I):
            return _blocked("scroll", "UNSUPPORTED_SCROLL_AMOUNT")
        form = _SCROLL_FORM.match(rest)
        if not form:
            return _ask("scroll", "SCROLL_FORM_NOT_UNDERSTOOD", "Should I scroll up or down?")
        direction = (form.group("dir") or "").lower()
        if not direction:
            return _ask("scroll", "DIRECTION_REQUIRED", "Should I scroll up or down?")
        if direction in ("left", "right"):
            return _blocked("scroll", "HORIZONTAL_SCROLL_NOT_ENABLED")
        size = normalize(form.group("a1") or form.group("a2") or "")
        amount = SMALL_SCROLL if size in ("a little", "a bit", "slightly") else DEFAULT_SCROLL
        return _unique_target("scroll", ci, Op.SCROLL, _targets_for(ci, Op.SCROLL), "NO_SCROLLABLE_AREA",
                              "There is more than one area I could scroll. Which one?",
                              {"direction": direction, "amount": amount})

    if _GO_NAV.match(utterance):
        return _blocked("navigate", "BROWSER_NOT_ENABLED")
    if _OPEN_BARE.match(utterance):
        return _ask("activate", "APP_REQUIRED", "Which app should I switch to?")
    for pattern in _ACTIVATE:
        m = pattern.match(utterance)
        if m:
            status, value = resolve_app(_app_name(m.group("name")), ci.running_apps, installed_apps)
            if status == "OK":
                return _action("activate", ci, Op.ACTIVATE_APP, args={"bundle_id": value})
            if status == "BLOCKED":
                return _blocked("activate", value)
            question = {"UNRESOLVED_REFERENCE": "Which app do you mean?",
                        "AMBIGUOUS_APP": "More than one running app has that name. Which one do you mean?",
                        "APP_REQUIRED": "Which app should I switch to?"}.get(value, "I don't know that app. Which app?")
            return _ask("activate", value, question)

    if _QUIT_ALL.match(utterance):
        return _ask("quit_all", "CONFIRMATION_REQUIRED", "Are you sure you want to quit all running applications?")

    if _QUIT_THIS_APP.match(utterance):
        if ci.frontmost and ci.frontmost.bundle_id:
            bid = ci.frontmost.bundle_id.lower()
            if "evie" in bid:
                return _blocked("quit_evie", "EVIE_SELF_PROTECTION")
            return _action("quit", ci, Op.QUIT_APP, args={"bundle_id": ci.frontmost.bundle_id})
        return _ask("quit", "APP_REQUIRED", "Which application should I quit?")

    if _CLOSE_WINDOW_PATTERN.match(utterance):
        return _action("close_window", ci, Op.CLOSE_WINDOW)

    m = _QUIT_APP_PATTERN.match(utterance)
    if m:
        raw_name = m.group("name")
        name = _app_name(raw_name)
        if "evie" in name.lower():
            return _blocked("quit_evie", "EVIE_SELF_PROTECTION")
        status, value = resolve_app(name, ci.running_apps, installed_apps)
        if status == "OK":
            return _action("quit", ci, Op.QUIT_APP, args={"bundle_id": value})
        if status == "BLOCKED":
            return _blocked("quit", value)
        return _ask("quit", value, f"Which app do you mean by '{raw_name}'?")

    m = _CLOSE_APP_PATTERN.match(utterance)
    if m:
        raw_name = m.group("name")
        name = _app_name(raw_name)
        if "evie" in name.lower():
            return _blocked("quit_evie", "EVIE_SELF_PROTECTION")
        if name in ("window", "this window", "that window", "current window", "the window"):
            return _action("close_window", ci, Op.CLOSE_WINDOW)
        status, value = resolve_app(name, ci.running_apps, installed_apps)
        if status == "OK":
            return _action("quit", ci, Op.QUIT_APP, args={"bundle_id": value})
        if status == "AMBIGUOUS_APP":
            return _ask("close", "AMBIGUOUS_CLOSE_TARGET", f"More than one running app matches '{raw_name}'. Do you want to close a specific window or quit the application?")
        return _ask("close", value, f"Which application or window should I close for '{raw_name}'?")

    first = bare.split(" ", 1)[0] if bare else ""
    if first in BLOCKED_VERBS:
        return _blocked("blocked_verb", BLOCKED_VERBS[first])
    return FastPathResult(kind="NO_MATCH")
