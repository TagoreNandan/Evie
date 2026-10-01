"""
Text provenance (docs/07_Computer_Control.md Section 12.3).

Text typed by set_text is always a slice of the user's verbatim utterance, addressed by
character offsets. Neither a model nor on-screen content can supply the text itself.
"""

from typing import Sequence

from services.computer_control.models import SetTextArgs


class TextSpanError(ValueError):
    pass


def validate_text_span(utterance: str, span: Sequence[int]) -> None:
    """Require 0 <= start <= end <= len(utterance)."""
    if not isinstance(utterance, str):
        raise TextSpanError("utterance must be a string")
    if len(span) != 2 or not all(isinstance(i, int) and not isinstance(i, bool) for i in span):
        raise TextSpanError("text_span must be two integers")
    start, end = span
    if not (0 <= start <= end <= len(utterance)):
        raise TextSpanError(f"text_span {list(span)} is outside the utterance (length {len(utterance)})")


def extract_text(utterance: str, span: Sequence[int]) -> str:
    validate_text_span(utterance, span)
    return utterance[span[0]:span[1]]


def text_for(utterance: str, args: SetTextArgs) -> str:
    """The only way the text for a set_text action is obtained."""
    return extract_text(utterance, args.text_span)
