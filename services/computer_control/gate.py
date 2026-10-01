"""
Code-owned computer-control safety gate (docs/07_Computer_Control.md Section 12).

The model may propose; this module decides. In CC-1a only LOW-risk, enabled, validated
actions inside the session's allowed-app scope are ALLOWed. MEDIUM and HIGH are blocked,
secure fields / Evie itself / no-observation apps are FORBIDDEN, and activating an app outside
the allowed scope needs ASK. The allowed scope is derived only from the user's utterance and
the frontmost app at session start (derive_allowed_apps) - never from screen text.
"""

import re
from dataclasses import dataclass, field
from typing import Any, FrozenSet, Iterable, Optional

from pydantic import Field

from services.computer_control.actions import CURRENT_PHASE, Phase, get_definition, is_enabled, target_class_validated
from services.computer_control.models import (
    APP_ALIASES,
    ActivateAppArgs,
    AppIdentity,
    GateDecision,
    GateOutcome,
    Observation,
    Op,
    QuitAppArgs,
    OpArgs,
    ResolvedTarget,
    RiskLevel,
    Sensitivity,
    WindowKey,
    _Strict,
    normalize,
    title_hash,
)

EVIE_UI_TITLE = "Evie — Dashboard & Security Assistant"   # ui/index.html <title>

# No observation at all: password managers and Keychain Access.
NO_OBSERVE_BUNDLES = frozenset({
    "com.apple.keychainaccess", "com.apple.Passwords", "com.1password.1password",
    "com.agilebits.onepassword7", "com.bitwarden.desktop", "com.lastpass.LastPass",
})

# Observation allowed, every action HIGH: Terminal-class apps, Mail, Messages, WhatsApp, System Settings.
RESTRICTED_BUNDLES = frozenset({
    "com.apple.Terminal", "com.googlecode.iterm2", "dev.warp.Warp-Stable", "com.mitchellh.ghostty",
    "com.apple.mail", "com.apple.MobileSMS", "net.whatsapp.WhatsApp", "desktop.WhatsApp",
    "com.apple.systempreferences",
})

DESTRUCTIVE_LEXICON = (
    "send", "delete", "remove", "trash", "buy", "pay", "purchase", "submit", "sign in", "log in",
    "allow", "install", "erase", "confirm", "unsubscribe", "share", "post",
)
_DESTRUCTIVE = re.compile(r"\b(" + "|".join(re.escape(w) for w in DESTRUCTIVE_LEXICON) + r")\b")

# Operations whose effect is routed through the app's main document (e.g. format bar), or which
# edit a document, need the target window to be the app's main window (wrong-document guard).
_MAIN_WINDOW_OPS = frozenset({
    Op.PRESS, Op.SET_TEXT, Op.SELECT_TEXT, Op.SCROLL, Op.PRESS_KEY,
    Op.CLICK_ELEMENT, Op.SELECT, Op.SELECT_TAB, Op.MENU_ITEM_SELECT, Op.PASTE_CLIPBOARD
})


@dataclass(frozen=True)
class GatePolicy:
    no_observe_bundles: FrozenSet[str] = NO_OBSERVE_BUNDLES
    restricted_bundles: FrozenSet[str] = RESTRICTED_BUNDLES
    evie_bundle_ids: FrozenSet[str] = frozenset()
    evie_pids: FrozenSet[int] = frozenset()
    evie_title_hashes: FrozenSet[str] = field(default_factory=lambda: frozenset({title_hash(EVIE_UI_TITLE)}))
    phase: Phase = CURRENT_PHASE


DEFAULT_POLICY = GatePolicy()


class GateContext(_Strict):
    op: Op
    args: OpArgs
    target: Optional[ResolvedTarget] = None
    app: AppIdentity                       # app the action lands in (for activate_app: the app to activate)
    window: Optional[WindowKey] = None     # window the target lives in
    allowed_apps: FrozenSet[str] = Field(..., min_length=1)


def _decision(outcome: GateOutcome, risk: RiskLevel, reason: str) -> GateDecision:
    return GateDecision(outcome=outcome, risk=risk, reason=reason)


def _block(risk: RiskLevel, reason: str) -> GateDecision:
    return _decision(GateOutcome.BLOCK, risk, reason)


def is_evie_self(app: AppIdentity, window: Optional[WindowKey], target: Optional[ResolvedTarget],
                 policy: GatePolicy) -> bool:
    return (app.bundle_id in policy.evie_bundle_ids or app.pid in policy.evie_pids
            or (window is not None and window.title_hash in policy.evie_title_hashes)
            or (target is not None and Sensitivity.EVIE_SELF in target.target.sensitivity))


def _identifier_words(target) -> str:
    """An unlabelled control is named by its identifier ("DeleteButton" -> "delete button"), so it is checked too."""
    ident = target.identifier or ""
    return normalize(re.sub(r"(?<=[a-z0-9])(?=[A-Z])|[_\-.]", " ", ident)) or ""


def evaluate(ctx: GateContext, policy: GatePolicy = DEFAULT_POLICY) -> GateDecision:
    definition = get_definition(ctx.op)
    target = ctx.target.target if ctx.target is not None else None

    # FORBIDDEN, regardless of operation
    if is_evie_self(ctx.app, ctx.window, ctx.target, policy):
        return _block(RiskLevel.FORBIDDEN, "EVIE_SELF")
    if target is not None and target.is_secure:
        return _block(RiskLevel.FORBIDDEN, "SECURE_FIELD")
    if ctx.app.bundle_id in policy.no_observe_bundles:
        return _block(RiskLevel.FORBIDDEN, "SENSITIVE_APP")

    # Not available in this phase: base risk is reported, but it is never executed
    if not is_enabled(ctx.op, policy.phase):
        return _block(definition.risk, "OP_NOT_ENABLED")

    # HIGH: restricted apps (any action, including activation) and destructive labels
    if ctx.app.bundle_id in policy.restricted_bundles or (
            target is not None and Sensitivity.SENSITIVE_APP in target.sensitivity):
        return _block(RiskLevel.HIGH, "RESTRICTED_APP")
    if target is not None and ctx.op in (Op.PRESS, Op.CLICK_ELEMENT, Op.MENU_ITEM_SELECT) and (
            _DESTRUCTIVE.search(normalize(target.label) or "") or _DESTRUCTIVE.search(_identifier_words(target))):
        return _block(RiskLevel.HIGH, "DESTRUCTIVE_LABEL")

    # MEDIUM and above are blocked in CC-1a
    if definition.risk is not RiskLevel.LOW:
        return _block(definition.risk, f"{definition.risk.value}_BLOCKED")

    # Target shape: only control classes this mechanism has been validated on
    if definition.requires_target:
        if target is None:
            return _block(RiskLevel.LOW, "TARGET_REQUIRED")
        if not target_class_validated(ctx.op, target):
            return _block(RiskLevel.LOW, "TARGET_CLASS_NOT_VALIDATED")
        if ctx.op in _MAIN_WINDOW_OPS and (ctx.window is None or not ctx.window.is_main):
            return _block(RiskLevel.LOW, "NOT_MAIN_WINDOW")
        if ctx.op is Op.PRESS_KEY and target.focused is not True:
            return _block(RiskLevel.LOW, "TEXT_AREA_NOT_FOCUSED")          # keys go to the focused element

    # Allowed-app scope: code-derived, cannot be widened by screen content
    scope_bundle = ctx.args.bundle_id if isinstance(ctx.args, ActivateAppArgs) else ctx.app.bundle_id
    if scope_bundle not in ctx.allowed_apps:
        return _decision(GateOutcome.ASK, RiskLevel.LOW, "OUTSIDE_ALLOWED_APPS")

    return _decision(GateOutcome.ALLOW, RiskLevel.LOW, "LOW_ALLOWED")


def _clean_slug(text: Optional[str]) -> str:
    if not text:
        return ""
    cleaned = re.sub(r"\s+", " ", text).strip().casefold()
    return re.sub(r"[^a-z0-9]", "", cleaned)


def derive_allowed_apps(utterance: str, frontmost: Optional[AppIdentity],
                        known_apps: Iterable[AppIdentity],
                        installed_apps: Iterable[Any] = ()) -> FrozenSet[str]:
    """
    Session scope: the frontmost app at session start plus every running or installed app
    the user NAMED in their utterance (exact, whole-word, normalised, or slug matched).
    Deliberately takes no observation or screen text, so on-screen content cannot add apps.
    """
    allowed = {frontmost.bundle_id} if frontmost is not None else set()
    words = re.findall(r"[a-z0-9]+", utterance.lower())
    clean_words = [_clean_slug(w) for w in words if w]

    seq_slugs = set(clean_words)
    for i in range(len(clean_words)):
        for j in range(i + 2, len(clean_words) + 1):
            seq_slugs.add("".join(clean_words[i:j]))

    said = f" {normalize(utterance) or ''} "
    named = lambda word: re.search(r"(?<![\w])" + re.escape(word) + r"(?![\w])", said)

    for app in list(known_apps) + list(installed_apps):
        name = normalize(app.name)
        slug = _clean_slug(app.name)
        last_seg = app.bundle_id.split(".")[-1].casefold()
        last_slug = _clean_slug(last_seg)
        if (name and named(name)) or (slug and slug in seq_slugs) or (last_slug and last_slug in seq_slugs):
            allowed.add(app.bundle_id)

    known_bundles = {app.bundle_id for app in list(known_apps) + list(installed_apps)}
    for alias, bundle in APP_ALIASES.items():
        if named(alias) and bundle in known_bundles:
            allowed.add(bundle)

    return frozenset(allowed)


def context_for(op: Op, args: OpArgs, target: Optional[ResolvedTarget], observation: Observation,
                allowed_apps: FrozenSet[str]) -> GateContext:
    """Build the gate context from an observation. For activate_app and quit_app with bundle_id, the relevant app is named in args."""
    if isinstance(args, ActivateAppArgs):
        known = {a.bundle_id: a for a in observation.running_apps}
        app = known.get(args.bundle_id) or AppIdentity(bundle_id=args.bundle_id, pid=0)
        return GateContext(op=op, args=args, target=None, app=app, window=None, allowed_apps=allowed_apps)
    if isinstance(args, QuitAppArgs) and args.bundle_id:
        known = {a.bundle_id: a for a in observation.running_apps}
        app = known.get(args.bundle_id) or AppIdentity(bundle_id=args.bundle_id, pid=0)
        return GateContext(op=op, args=args, target=None, app=app, window=None, allowed_apps=allowed_apps)
    window = observation.window_for(target.target) if target is not None else observation.window
    return GateContext(op=op, args=args, target=target, app=observation.app, window=window, allowed_apps=allowed_apps)
