"""
Closed, code-owned computer-control action registry (docs/07_Computer_Control.md Section 9).

Metadata only: there are no handlers here. The future executor looks an operation up and may
run it only when is_enabled(op) is true AND the resolved target's control class is one this
registry lists as validated for that operation. A schema entry alone never enables anything.

Evidence for every "validated" entry: .planning/research/computer-control/EVIDENCE.md.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Dict, FrozenSet, Optional, Tuple

from services.computer_control.models import (
    ActivateAppArgs,
    ClickElementArgs,
    CopySelectionArgs,
    MenuItemSelectArgs,
    NoArgs,
    ObservedTarget,
    Op,
    PasteClipboardArgs,
    PressKeyArgs,
    QuitAppArgs,
    CloseWindowArgs,
    RiskLevel,
    ScrollArgs,
    SelectArgs,
    SelectTabArgs,
    SelectTextArgs,
    SetTextArgs,
    StateLayer,
    WaitForStateArgs,
)


class Phase(str, Enum):
    CC_1A = "CC-1a"
    CC_1B = "CC-1b"
    CC_2 = "CC-2"


class Validation(str, Enum):
    VALIDATED = "VALIDATED"
    PARTIALLY_VALIDATED = "PARTIALLY_VALIDATED"
    UNVALIDATED = "UNVALIDATED"


CURRENT_PHASE = Phase.CC_2

# (role, subrole) control classes. subrole None means "no subrole".
ControlClass = Tuple[str, Optional[str]]


@dataclass(frozen=True)
class ActionDefinition:
    op: Op
    params_model: Optional[type]          # None = parameters not yet defined (operation cannot be parsed)
    enabled_phase: Phase
    validation: Validation
    mechanism: Optional[str]              # identifier of the (future) worker mechanism
    verifier: Optional[str]               # identifier of the operation-specific verifier
    risk: RiskLevel                       # base risk before context rules
    requires_target: bool
    validated_targets: FrozenSet[ControlClass] = frozenset()   # control classes the mechanism is validated on
    pending_targets: FrozenSet[ControlClass] = frozenset()     # named in docs/07 but still need a live check
    state_layers: FrozenSet[StateLayer] = frozenset({StateLayer.UI})
    evidence: str = ""


def _deferred(op: Op, risk: RiskLevel, requires_target: bool, note: str) -> ActionDefinition:
    return ActionDefinition(op, None, Phase.CC_1B, Validation.UNVALIDATED, None, None, risk, requires_target,
                            evidence=note)


TEXT_AREA: ControlClass = ("AXTextArea", None)

ACTION_DEFINITIONS: Dict[Op, ActionDefinition] = {d.op: d for d in (
    # --- Universal Action Substrate Primitives ---
    ActionDefinition(
        Op.ACTIVATE_APP, ActivateAppArgs, Phase.CC_1A, Validation.VALIDATED,
        "ns_running_application.activate", "activation", RiskLevel.LOW, requires_target=False,
        evidence="Running apps activation via LaunchServices / NSRunningApplication"),
    ActionDefinition(
        Op.FOCUS, NoArgs, Phase.CC_2, Validation.VALIDATED,
        "ax.raise_main_focus", "focus", RiskLevel.LOW, requires_target=True,
        validated_targets=frozenset({TEXT_AREA, ("AXTextField", None), ("AXComboBox", None), ("AXButton", None), ("AXWindow", "AXStandardWindow")}),
        evidence="AXRaise + AXMain + AXFocused element activation"),
    ActionDefinition(
        Op.CLICK_ELEMENT, ClickElementArgs, Phase.CC_2, Validation.VALIDATED,
        "ax.press", "control_state", RiskLevel.LOW, requires_target=True,
        validated_targets=frozenset({
            ("AXButton", None), ("AXCheckBox", None), ("AXCheckBox", "AXSegment"),
            ("AXRadioButton", None), ("AXRadioMenuItem", None), ("AXTab", None),
            ("AXLink", None), ("AXMenuItem", None), ("AXPopUpButton", None)
        }),
        state_layers=frozenset({StateLayer.UI, StateLayer.DOCUMENT}),
        evidence="Generalized accessibility click/press on accessible UI controls"),
    ActionDefinition(
        Op.SET_TEXT, SetTextArgs, Phase.CC_1A, Validation.VALIDATED,
        "ax.set_selected_text", "text", RiskLevel.LOW, requires_target=True,
        validated_targets=frozenset({TEXT_AREA, ("AXTextField", None), ("AXComboBox", None)}),
        state_layers=frozenset({StateLayer.UI, StateLayer.DOCUMENT}),
        evidence="AXSelectedText / AXValue text setting"),
    ActionDefinition(
        Op.SELECT_TEXT, SelectTextArgs, Phase.CC_1A, Validation.VALIDATED,
        "ax.set_selected_text_range", "selection", RiskLevel.LOW, requires_target=True,
        validated_targets=frozenset({TEXT_AREA, ("AXTextField", None)}),
        evidence="AXSelectedTextRange selection"),
    ActionDefinition(
        Op.PRESS, NoArgs, Phase.CC_1A, Validation.PARTIALLY_VALIDATED,
        "ax.press", "control_state", RiskLevel.LOW, requires_target=True,
        validated_targets=frozenset({("AXCheckBox", "AXSegment")}),
        pending_targets=frozenset({("AXCheckBox", None)}),
        state_layers=frozenset({StateLayer.UI, StateLayer.DOCUMENT}),
        evidence="Alignment segment press"),
    ActionDefinition(
        Op.SELECT, SelectArgs, Phase.CC_2, Validation.VALIDATED,
        "ax.select", "selection", RiskLevel.LOW, requires_target=True,
        validated_targets=frozenset({("AXRadioButton", None), ("AXCheckBox", None), ("AXRow", None), ("AXPopUpButton", None)}),
        state_layers=frozenset({StateLayer.UI}),
        evidence="Generalized semantic selection"),
    ActionDefinition(
        Op.SELECT_TAB, SelectTabArgs, Phase.CC_2, Validation.VALIDATED,
        "ax.select_tab", "selection", RiskLevel.LOW, requires_target=True,
        validated_targets=frozenset({("AXTab", None)}),
        state_layers=frozenset({StateLayer.UI}),
        evidence="Semantic tab selection"),
    ActionDefinition(
        Op.MENU_ITEM_SELECT, MenuItemSelectArgs, Phase.CC_2, Validation.VALIDATED,
        "ax.menu_item_select", "menu_item", RiskLevel.LOW, requires_target=True,
        validated_targets=frozenset({("AXMenuItem", None), ("AXRadioMenuItem", None)}),
        state_layers=frozenset({StateLayer.UI}),
        evidence="Semantic menu item selection"),
    ActionDefinition(
        Op.COPY_SELECTION, CopySelectionArgs, Phase.CC_2, Validation.VALIDATED,
        "cg.post_to_pid_copy", "clipboard", RiskLevel.LOW, requires_target=False,
        evidence="Bounded Command+C copy to frontmost app"),
    ActionDefinition(
        Op.PASTE_CLIPBOARD, PasteClipboardArgs, Phase.CC_2, Validation.VALIDATED,
        "cg.post_to_pid_paste", "clipboard", RiskLevel.LOW, requires_target=True,
        validated_targets=frozenset({TEXT_AREA, ("AXTextField", None), ("AXComboBox", None)}),
        state_layers=frozenset({StateLayer.UI, StateLayer.DOCUMENT}),
        evidence="Bounded Command+V paste into target field"),
    ActionDefinition(
        Op.WAIT_FOR_STATE, WaitForStateArgs, Phase.CC_2, Validation.VALIDATED,
        "observer.wait_for_state", "state_condition", RiskLevel.LOW, requires_target=False,
        evidence="Bounded deterministic state waiting"),
    ActionDefinition(
        Op.SCROLL, ScrollArgs, Phase.CC_1A, Validation.VALIDATED,
        "ax.scrollbar_value", "scroll", RiskLevel.LOW, requires_target=True,
        validated_targets=frozenset({("AXScrollArea", None)}),
        evidence="Native scroll area scroll bar value change"),
    ActionDefinition(
        Op.PRESS_KEY, PressKeyArgs, Phase.CC_1A, Validation.VALIDATED,
        "cg.post_to_pid_arrow", "caret", RiskLevel.LOW, requires_target=True,
        validated_targets=frozenset({TEXT_AREA}),
        evidence="Arrow keys caret navigation"),
    ActionDefinition(
        Op.CLOSE_WINDOW, CloseWindowArgs, Phase.CC_2, Validation.VALIDATED,
        "ax.close_window", "window_closed", RiskLevel.LOW, requires_target=False,
        validated_targets=frozenset({("AXWindow", "AXStandardWindow"), ("AXWindow", None)}),
        state_layers=frozenset({StateLayer.UI}),
        evidence="Native window close via AXCloseButton or AXCancel action"),
    ActionDefinition(
        Op.QUIT_APP, QuitAppArgs, Phase.CC_2, Validation.VALIDATED,
        "ns_running_application.terminate", "app_exited", RiskLevel.LOW, requires_target=False,
        state_layers=frozenset({StateLayer.UI}),
        evidence="Application termination via NSRunningApplication.terminate"),
    ActionDefinition(
        Op.SWITCH_APP, ActivateAppArgs, Phase.CC_2, Validation.VALIDATED,
        "ns_running_application.activate", "activation", RiskLevel.LOW, requires_target=False,
        evidence="Switch active running application via NSRunningApplication"),
    ActionDefinition(
        Op.SWITCH_WINDOW, NoArgs, Phase.CC_2, Validation.VALIDATED,
        "ax.raise_main_focus", "focus", RiskLevel.LOW, requires_target=True,
        validated_targets=frozenset({("AXWindow", "AXStandardWindow")}),
        evidence="Switch window focus via AXRaise + AXMain"),
    # --- CC-1b candidates ---
    _deferred(Op.LAUNCH_APP, RiskLevel.LOW, False, "gate: launch via LaunchServices with activation"),
    _deferred(Op.OPEN_URL, RiskLevel.MEDIUM, False, "gate: web accessibility + local URL"),
    _deferred(Op.SCROLL_INTO_VIEW, RiskLevel.LOW, True, "AXScrollToVisible untested"),
    _deferred(Op.NAVIGATE_BACK, RiskLevel.LOW, True, "gate: web accessibility"),
    _deferred(Op.NAVIGATE_FORWARD, RiskLevel.LOW, True, "gate: web accessibility"),
    _deferred(Op.CLOSE_TAB, RiskLevel.MEDIUM, True, "untested"),
    _deferred(Op.NEW_WINDOW, RiskLevel.LOW, False, "needs menus or shortcuts, both untested"),
    _deferred(Op.NEW_TAB, RiskLevel.LOW, False, "needs menus or shortcuts, both untested"),
)}

assert set(ACTION_DEFINITIONS) == set(Op), "every Op must have exactly one definition"


def get_definition(op: Op) -> ActionDefinition:
    return ACTION_DEFINITIONS[Op(op)]


_PHASE_ORDER = {Phase.CC_1A: 0, Phase.CC_1B: 1, Phase.CC_2: 2}


def is_enabled(op: Op, phase: Phase = CURRENT_PHASE) -> bool:
    """Enabled = parameters defined, not a deferred placeholder, and its phase has been reached."""
    d = get_definition(op)
    return (d.params_model is not None and d.validation is not Validation.UNVALIDATED
            and _PHASE_ORDER[d.enabled_phase] <= _PHASE_ORDER[phase])


def enabled_ops(phase: Phase = CURRENT_PHASE) -> Tuple[Op, ...]:
    return tuple(op for op in Op if is_enabled(op, phase))


def control_class(target: ObservedTarget) -> ControlClass:
    return (target.role, target.subrole)


def target_class_validated(op: Op, target: ObservedTarget) -> bool:
    """True only for control classes this operation's mechanism has been validated on."""
    return control_class(target) in get_definition(op).validated_targets
