"""R11-MP21: identifiers typed into a question never reach a model provider.

A person asks "what is the balance of account 004512339871?" and the question --
account number included -- went verbatim to the model route and to the hosted
embedding API. Nothing detected it. Masking and tokenization protect result
rows; they never saw the question.

This module replaces the values that identify a person or an account with
stable tokens (`ATLAS_VALUE_1`, ...) before the question leaves the platform,
and puts them back into the generated SQL afterwards, locally. The model writes
`WHERE account_no = 'ATLAS_VALUE_1'`; the statement that reaches the query
gateway reads `WHERE account_no = '004512339871'`. The provider sees the shape
of the question, never the value.

What is detected, deliberately narrow so that ordinary questions pass through
untouched ("top 10 branches", "loans over 250000 in 2025"):

* e-mail addresses;
* IBANs (two letters, two check digits, 11 to 30 alphanumerics);
* payment card numbers: 13 to 19 digits, spaces or dashes allowed, that pass
  the Luhn check;
* US social security numbers written ``ddd-dd-dddd``;
* any other run of 9 or more digits -- account, customer and reference numbers.

Restoring is plain token replacement. It is safe because every value admitted
above is drawn from ``[A-Za-z0-9@._+-]`` and a space or dash inside a card
number: none can close a string literal or start a statement, and the restored
SQL still goes through the whole gateway pipeline.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Final

PLACEHOLDER_PREFIX: Final = "ATLAS_VALUE_"

_EMAIL = re.compile(r"\b[A-Za-z0-9._+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+\b")
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")
_CARD = re.compile(r"\b\d(?:[ -]?\d){12,18}\b")
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_LONG_NUMBER = re.compile(r"\b\d{9,}\b")


def _luhn_valid(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


@dataclass(frozen=True, slots=True)
class RedactedQuestion:
    """The question as it may leave the platform, and what was taken out."""

    text: str
    values: dict[str, str] = field(default_factory=dict)
    kinds: tuple[str, ...] = ()

    @property
    def redacted(self) -> bool:
        return bool(self.values)

    def evidence(self) -> dict[str, object]:
        """Counts and kinds only -- never a value (INV-6)."""
        return {"redacted_values": len(self.values), "kinds": sorted(set(self.kinds))}


def redact_question(question: str) -> RedactedQuestion:
    """Replace identifying values with tokens. Pure; no I/O."""
    values: dict[str, str] = {}
    kinds: list[str] = []
    by_value: dict[str, str] = {}

    def token_for(value: str, kind: str) -> str:
        if value in by_value:
            return by_value[value]
        token = f"{PLACEHOLDER_PREFIX}{len(values) + 1}"
        values[token] = value
        by_value[value] = token
        kinds.append(kind)
        return token

    text = _EMAIL.sub(lambda m: token_for(m.group(0), "EMAIL"), question)
    text = _IBAN.sub(lambda m: token_for(m.group(0), "IBAN"), text)

    def _card(match: re.Match[str]) -> str:
        digits = re.sub(r"[ -]", "", match.group(0))
        if 13 <= len(digits) <= 19 and _luhn_valid(digits):
            return token_for(match.group(0), "CARD_NUMBER")
        return match.group(0)

    text = _CARD.sub(_card, text)
    text = _SSN.sub(lambda m: token_for(m.group(0), "SSN"), text)
    text = _LONG_NUMBER.sub(lambda m: token_for(m.group(0), "LONG_NUMBER"), text)
    return RedactedQuestion(text=text, values=values, kinds=tuple(kinds))


def restore_values(sql: str, values: dict[str, str]) -> str:
    """Put the redacted values back into a generated statement, locally.

    Longest token first, so `ATLAS_VALUE_12` is never read as `ATLAS_VALUE_1`
    followed by a 2.
    """
    for token in sorted(values, key=len, reverse=True):
        sql = sql.replace(token, values[token])
    return sql


def tokenize_values(sql: str, values: dict[str, str]) -> str:
    """The inverse of `restore_values`: a restored statement back in token form,
    for sending it to a model again (a repair). Longest value first."""
    for token, value in sorted(values.items(), key=lambda item: len(item[1]), reverse=True):
        sql = sql.replace(value, token)
    return sql


#: What the model is told when a question carried redacted values.
MODEL_INSTRUCTION: Final = (
    " Values that identify a person or an account have been replaced in the question by "
    "tokens named ATLAS_VALUE_<n>. Where the SQL needs such a value, write the token "
    "itself exactly as it appears, as a string literal (for example 'ATLAS_VALUE_1') or "
    "as a bare number where the column is numeric; the platform substitutes the real "
    "value afterwards. Never guess or invent the value."
)
