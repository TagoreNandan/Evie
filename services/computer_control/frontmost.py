"""
Frontmost-application cross-check (docs/07_Computer_Control.md Section 6, rule 10; CC-1a Step 4).

Pure. The worker collects independent, non-mutating readings (NSWorkspace after pumping its run
loop, the system-wide AX focused application, the candidate app's own AXFrontmost); this module
decides. Agreement needs at least two sources that name the same app and no source naming a
different one. A failed reading is recorded but is not a vote. Anything else fails closed - the
frontmost app is then unknown, never guessed, and nothing is activated to resolve it.

(CC-1a Step 3 substituted the observed app when the AX focused-application read failed; that
fabricated "frontmost" value is what this check replaces.)
"""

from typing import Optional, Sequence, Tuple

from pydantic import Field

from services.computer_control.models import AppIdentity, _Strict

MIN_AGREEING_SOURCES = 2


class FrontmostReading(_Strict):
    source: str = Field(..., min_length=1, max_length=64)
    app: Optional[AppIdentity] = None          # None = the source could not produce a reading
    error: Optional[str] = Field(None, max_length=120)


class FrontmostCheck(_Strict):
    agreed: bool
    app: Optional[AppIdentity] = None          # set only when agreed
    readings: Tuple[FrontmostReading, ...] = Field(default=(), max_length=8)
    diagnostic: str = Field(..., max_length=200)

    @property
    def sources(self) -> Tuple[str, ...]:
        return tuple(r.source for r in self.readings if r.app is not None)


def cross_check(readings: Sequence[FrontmostReading], min_sources: int = MIN_AGREEING_SOURCES) -> FrontmostCheck:
    votes = [r for r in readings if r.app is not None]
    bundles = {r.app.bundle_id for r in votes}
    readings = tuple(readings)
    if len(bundles) > 1:
        detail = ", ".join(f"{r.source}={r.app.bundle_id}" for r in votes)
        return FrontmostCheck(agreed=False, readings=readings, diagnostic=f"DISAGREE: {detail}"[:200])
    if len(votes) < min_sources:
        return FrontmostCheck(agreed=False, readings=readings,
                              diagnostic=f"INSUFFICIENT_SOURCES: {len(votes)} of {min_sources} required")
    pids = {r.app.pid for r in votes}
    if len(pids) > 1:
        return FrontmostCheck(agreed=False, readings=readings, diagnostic="DISAGREE: same bundle, different pids")
    return FrontmostCheck(agreed=True, app=votes[0].app, readings=readings,
                          diagnostic=f"AGREED: {len(votes)} sources")
