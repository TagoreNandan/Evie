"""
Pure target resolver (docs/07_Computer_Control.md Section 8).

Priority: (1) exact stable node_id / unique AXIdentifier, (2) exact role + subrole + label (+ context/ancestor constraints),
(3) bounded ordinal/index against the SAME observation, (4) otherwise ASK or BLOCKED.
Matching is exact after normalize() (case-fold, collapse whitespace). No fuzzy matching, and
never a silent choice between several matches. No AppKit or AX access.
"""

from typing import List, Literal, Optional, Tuple

from pydantic import Field, model_validator

from services.computer_control.models import (
    Observation,
    ObservedTarget,
    ResolutionMethod,
    ResolvedTarget,
    SemanticNode,
    SystemObservation,
    _Strict,
    normalize,
)


class TargetSpec(_Strict):
    """What a decision asks for. obs_id is the observation the request was made against."""
    obs_id: str = Field(..., min_length=1, max_length=64)
    identifier: Optional[str] = Field(None, max_length=200)
    role: Optional[str] = Field(None, max_length=64)
    subrole: Optional[str] = Field(None, max_length=64)
    label: Optional[str] = Field(None, max_length=200)
    context: Optional[Tuple[str, ...]] = None
    index: Optional[int] = Field(None, ge=0)

    @model_validator(mode="after")
    def _has_a_key(self):
        if self.identifier is None and self.index is None and (self.role is None or self.label is None):
            raise ValueError("a target spec needs an identifier, role + label, or an index")
        return self


class SemanticTargetSpec(_Strict):
    """
    Generalized semantic target specification supporting:
    - exact stable node_id within obs_id
    - unique AXIdentifier
    - semantic role + subrole + label + safe value summary
    - ancestor role / label / structural path constraints
    - ordinal index constraint
    """
    obs_id: str = Field(..., min_length=1, max_length=64)
    node_id: Optional[str] = Field(None, max_length=64)
    identifier: Optional[str] = Field(None, max_length=200)
    role: Optional[str] = Field(None, max_length=64)
    subrole: Optional[str] = Field(None, max_length=64)
    label: Optional[str] = Field(None, max_length=200)
    value_summary: Optional[str] = Field(None, max_length=120)
    ancestor_role: Optional[str] = Field(None, max_length=64)
    ancestor_label: Optional[str] = Field(None, max_length=200)
    ancestor_path: Optional[Tuple[str, ...]] = None
    index: Optional[int] = Field(None, ge=0)

    @model_validator(mode="after")
    def _has_a_key(self):
        if (self.node_id is None and self.identifier is None and self.index is None
                and self.role is None and self.label is None):
            raise ValueError("a semantic target spec needs a node_id, identifier, role, label, or index")
        return self


ResolutionKind = Literal["RESOLVED", "ASK", "BLOCKED", "STALE"]


class ResolutionOutcome(_Strict):
    kind: ResolutionKind
    target: Optional[ResolvedTarget] = None
    reason: str = Field(..., min_length=1, max_length=64)
    candidates: Tuple[int, ...] = ()


def _node_to_target(obs: SystemObservation, node: SemanticNode, idx: Optional[int] = None) -> ObservedTarget:
    if obs.targets:
        matches = [t for t in obs.targets if t.role == node.role and t.subrole == node.subrole
                   and t.label == node.label and t.identifier == node.identifier]
        if idx is not None and 0 <= idx < len(matches):
            return matches[idx]
        elif len(matches) == 1:
            return matches[0]
        elif matches:
            return matches[0]
    return ObservedTarget(
        index=idx if idx is not None else 0,
        obs_id=obs.obs_id,
        role=node.role,
        subrole=node.subrole,
        label=node.label,
        identifier=node.identifier,
        context=node.path,
        enabled=node.enabled,
        focused=node.focused,
        value_summary=node.safe_value_summary,
        actions=node.actions,
        settable=node.settable,
        frame=node.frame,
        sensitivity=node.sensitivity,
        window_index=node.window_index,
    )


def _resolved(obs: Observation, t: ObservedTarget, method: ResolutionMethod,
              spec: Optional["TargetSpec"] = None) -> ResolutionOutcome:
    if spec is not None and spec.index is not None and spec.index != t.index:
        # Keys disagree (e.g. label says one control, index another): never pick one silently.
        return ResolutionOutcome(kind="BLOCKED", reason="INDEX_INCONSISTENT", candidates=(t.index, spec.index))
    if t.enabled is False:
        return ResolutionOutcome(kind="BLOCKED", reason="TARGET_DISABLED", candidates=(t.index,))
    return ResolutionOutcome(kind="RESOLVED", reason=method.value.upper(),
                             target=ResolvedTarget(target=t, method=method, obs_id=obs.obs_id))


def _exact(spec: TargetSpec, t: ObservedTarget) -> bool:
    return (t.role == spec.role and t.subrole == spec.subrole
            and normalize(t.label) == normalize(spec.label)
            and (spec.context is None or tuple(t.context) == tuple(spec.context)))


def _consistent(spec: TargetSpec, t: ObservedTarget) -> bool:
    """For ordinal specs that also name a role/label, the indexed target must agree with them."""
    if spec.role is not None and t.role != spec.role:
        return False
    if spec.role is not None and spec.subrole != t.subrole:
        return False
    if spec.label is not None and normalize(t.label) != normalize(spec.label):
        return False
    return True


def resolve(observation: Observation, spec: TargetSpec) -> ResolutionOutcome:
    if spec.obs_id != observation.obs_id:
        return ResolutionOutcome(kind="STALE", reason="OBSERVATION_MISMATCH")
    targets = observation.targets

    # 1. unique AXIdentifier
    if spec.identifier is not None:
        hits: List[ObservedTarget] = [t for t in targets if t.identifier is not None
                                      and normalize(t.identifier) == normalize(spec.identifier)]
        if len(hits) == 1:
            return _resolved(observation, hits[0], ResolutionMethod.IDENTIFIER, spec)
        if len(hits) > 1 and spec.role is None and spec.index is None:
            return ResolutionOutcome(kind="ASK", reason="AMBIGUOUS_IDENTIFIER", candidates=tuple(t.index for t in hits))

    # 2. exact role + subrole + label (+ context)
    if spec.role is not None and spec.label is not None:
        hits = [t for t in targets if _exact(spec, t)]
        if len(hits) == 1:
            return _resolved(observation, hits[0], ResolutionMethod.EXACT, spec)
        if len(hits) > 1:
            if spec.index is None:
                return ResolutionOutcome(kind="ASK", reason="AMBIGUOUS_TARGET", candidates=tuple(t.index for t in hits))

    # 3. bounded ordinal against the same observation
    if spec.index is not None:
        if spec.index >= len(targets):
            return ResolutionOutcome(kind="BLOCKED", reason="INDEX_OUT_OF_RANGE")
        t = targets[spec.index]
        if not _consistent(spec, t):
            return ResolutionOutcome(kind="BLOCKED", reason="INDEX_INCONSISTENT", candidates=(t.index,))
        return _resolved(observation, t, ResolutionMethod.ORDINAL)

    return ResolutionOutcome(kind="BLOCKED", reason="NO_MATCH")


def resolve_semantic(observation: SystemObservation, spec: SemanticTargetSpec) -> ResolutionOutcome:
    """
    Generalized semantic target resolver against a SystemObservation tree.
    """
    if spec.obs_id != observation.obs_id:
        return ResolutionOutcome(kind="STALE", reason="OBSERVATION_MISMATCH")

    # 1. Exact node_id
    if spec.node_id is not None:
        if spec.node_id in observation.nodes:
            node = observation.nodes[spec.node_id]
            if node.enabled is False:
                return ResolutionOutcome(kind="BLOCKED", reason="TARGET_DISABLED")
            t = _node_to_target(observation, node)
            return ResolutionOutcome(kind="RESOLVED", reason="EXACT", target=ResolvedTarget(target=t, method=ResolutionMethod.EXACT, obs_id=observation.obs_id))
        return ResolutionOutcome(kind="ASK", reason="TARGET_NOT_FOUND")

    nodes_pool = [n for n in observation.nodes.values() if n.role != "AXApplication"] if observation.nodes else []

    # 2. Unique AXIdentifier
    if spec.identifier is not None:
        hits = [n for n in nodes_pool if n.identifier is not None and normalize(n.identifier) == normalize(spec.identifier)]
        if len(hits) == 1:
            n = hits[0]
            if n.enabled is False:
                return ResolutionOutcome(kind="BLOCKED", reason="TARGET_DISABLED")
            t = _node_to_target(observation, n)
            return ResolutionOutcome(kind="RESOLVED", reason="IDENTIFIER", target=ResolvedTarget(target=t, method=ResolutionMethod.IDENTIFIER, obs_id=observation.obs_id))
        if len(hits) > 1 and spec.role is None and spec.index is None:
            return ResolutionOutcome(kind="ASK", reason="AMBIGUOUS_IDENTIFIER")

    # 3. Semantic filtering
    candidates: List[SemanticNode] = []
    for n in nodes_pool:
        if spec.role is not None and n.role != spec.role:
            continue
        if spec.subrole is not None and n.subrole != spec.subrole:
            continue
        if spec.label is not None and normalize(n.label) != normalize(spec.label):
            continue
        if spec.value_summary is not None and normalize(n.safe_value_summary) != normalize(spec.value_summary):
            continue
        if spec.ancestor_role is not None and spec.ancestor_role not in n.path:
            continue
        if spec.ancestor_label is not None:
            ancestor_matched = False
            curr = n.parent_id
            while curr and curr in observation.nodes:
                parent_n = observation.nodes[curr]
                if normalize(parent_n.label) == normalize(spec.ancestor_label):
                    ancestor_matched = True
                    break
                curr = parent_n.parent_id
            if not ancestor_matched:
                continue
        if spec.ancestor_path is not None:
            if tuple(n.path[:len(spec.ancestor_path)]) != spec.ancestor_path:
                continue
        candidates.append(n)

    # 4. Ordinal index resolution / ambiguity evaluation
    if spec.index is not None:
        if 0 <= spec.index < len(candidates):
            n = candidates[spec.index]
            if n.enabled is False:
                return ResolutionOutcome(kind="BLOCKED", reason="TARGET_DISABLED")
            t = _node_to_target(observation, n, spec.index)
            return ResolutionOutcome(kind="RESOLVED", reason="ORDINAL", target=ResolvedTarget(target=t, method=ResolutionMethod.ORDINAL, obs_id=observation.obs_id))
        return ResolutionOutcome(kind="ASK", reason="TARGET_NOT_FOUND")

    if len(candidates) == 1:
        n = candidates[0]
        if n.enabled is False:
            return ResolutionOutcome(kind="BLOCKED", reason="TARGET_DISABLED")
        t = _node_to_target(observation, n)
        return ResolutionOutcome(kind="RESOLVED", reason="EXACT", target=ResolvedTarget(target=t, method=ResolutionMethod.EXACT, obs_id=observation.obs_id))
    elif len(candidates) > 1:
        return ResolutionOutcome(kind="ASK", reason="AMBIGUOUS_TARGET")

    # If no candidate was found in semantic tree, check if legacy targetspec matches
    if observation.targets:
        legacy_spec = TargetSpec(
            obs_id=spec.obs_id,
            identifier=spec.identifier,
            role=spec.role,
            subrole=spec.subrole,
            label=spec.label,
            index=spec.index
        ) if (spec.identifier or spec.index or (spec.role and spec.label)) else None
        if legacy_spec:
            legacy_res = resolve(observation, legacy_spec)
            if legacy_res.reason != "NO_MATCH":
                return legacy_res

    return ResolutionOutcome(kind="ASK", reason="TARGET_NOT_FOUND")

