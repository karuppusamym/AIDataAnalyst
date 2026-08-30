"""Indirect prompt injection screening for retrieved metadata.

Screens metadata text (column descriptions, table comments, etc.) before
it enters the model context window.  All detection is pattern-based and
deterministic -- no model calls required.

Detection categories:
- Instruction override attempts
- Credential/key extraction directives
- Policy/security bypass commands
- Privilege escalation
- Data exfiltration commands
- Encoded/obfuscated payloads (base64, hex, URL encoding)
- Unicode normalization evasion (homoglyphs, zero-width chars)
- Multi-language injection attempts
"""

from __future__ import annotations

import base64
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Literal

INJECTION_DEFENSE_VERSION = "injection-defense-v1"


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ScreeningResult:
    """Outcome of screening a piece of retrieved metadata."""

    flagged: bool
    threat_type: str
    confidence: float
    evidence: list[str]
    content_origin: str
    classifier_version: str = INJECTION_DEFENSE_VERSION


# ---------------------------------------------------------------------------
# Pattern definitions
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class _InjectionPattern:
    threat_type: str
    pattern: re.Pattern[str]
    confidence: float


def _pat(threat_type: str, pattern: str, confidence: float) -> _InjectionPattern:
    return _InjectionPattern(threat_type, re.compile(pattern, re.IGNORECASE | re.DOTALL), confidence)


# Instruction override patterns
INSTRUCTION_OVERRIDE_PATTERNS: list[_InjectionPattern] = [
    _pat(
        "INSTRUCTION_OVERRIDE",
        r"\b(?:ignore|disregard|forget|override)\b.{0,60}\b(?:previous|prior|system|developer|all|above)\b.{0,40}\b(?:instructions?|rules?|messages?|controls?|prompts?)\b",
        0.95,
    ),
    _pat(
        "INSTRUCTION_OVERRIDE",
        r"\b(?:new instructions?|updated instructions?|real instructions?)\b.{0,30}\b(?:follow|obey|execute|comply)\b",
        0.90,
    ),
    _pat(
        "INSTRUCTION_OVERRIDE",
        r"\bdo not follow\b.{0,30}\b(?:previous|original|system)\b",
        0.90,
    ),
    _pat(
        "INSTRUCTION_OVERRIDE",
        r"\b(?:you are now|from now on|starting now)\b.{0,50}\b(?:different|new|my)\b",
        0.85,
    ),
]

# Credential extraction patterns
CREDENTIAL_EXTRACTION_PATTERNS: list[_InjectionPattern] = [
    _pat(
        "CREDENTIAL_EXTRACTION",
        r"\b(?:reveal|print|dump|return|expose|output|show|give)\b.{0,50}\b"
        r"(?:api[_ ]?keys?|passwords?|credentials?|access[_ ]?tokens?|secrets?|"
        r"private[_ ]?keys?|connection[_ ]?strings?)\b",
        0.95,
    ),
    _pat(
        "CREDENTIAL_EXTRACTION",
        r"\b(?:extract|leak|exfiltrate)\b.{0,40}\b(?:credentials?|tokens?|keys?|secrets?)\b",
        0.95,
    ),
]

# Policy bypass patterns
POLICY_BYPASS_PATTERNS: list[_InjectionPattern] = [
    _pat(
        "POLICY_BYPASS",
        r"\b(?:bypass|disable|evade|circumvent|turn off|skip|ignore)\b.{0,50}\b"
        r"(?:authorization|policy|policies|security|guardrails?|controls?|"
        r"audit|masking|filtering|validation|sandbox)\b",
        0.95,
    ),
    _pat(
        "POLICY_BYPASS",
        r"\b(?:jailbreak|DAN|developer mode|unrestricted mode|god mode)\b",
        0.95,
    ),
]

# Privilege escalation patterns
PRIVILEGE_ESCALATION_PATTERNS: list[_InjectionPattern] = [
    _pat(
        "PRIVILEGE_ESCALATION",
        r"\b(?:act as|become|you are now|impersonate|switch to|pretend to be|roleplay as)\b.{0,40}\b"
        r"(?:root|administrator|admin|platform[_ ]?admin|superuser|security[_ ]?officer|sudo)\b",
        0.90,
    ),
    _pat(
        "PRIVILEGE_ESCALATION",
        r"\b(?:grant|escalate|elevate)\b.{0,30}\b(?:privileges?|permissions?|access|role)\b",
        0.85,
    ),
]

# Data exfiltration patterns
DATA_EXFILTRATION_PATTERNS: list[_InjectionPattern] = [
    _pat(
        "DATA_EXFILTRATION",
        r"\b(?:send|post|transmit|upload|forward|email)\b.{0,50}\b(?:to|at)\b.{0,30}\b(?:https?://|ftp://|[a-z0-9._%+-]+@[a-z0-9.-]+)\b",
        0.90,
    ),
    _pat(
        "DATA_EXFILTRATION",
        r"\b(?:dump|export|extract)\b.{0,30}\b(?:all|every|entire|complete)\b.{0,40}\b(?:data|records?|rows?|table|database)\b",
        0.85,
    ),
    _pat(
        "DATA_EXFILTRATION",
        r"\b(?:curl|wget|fetch|requests?\.(?:get|post))\b.{0,60}\bhttps?://",
        0.90,
    ),
]

# System command injection patterns
SYSTEM_COMMAND_PATTERNS: list[_InjectionPattern] = [
    _pat(
        "SYSTEM_COMMAND",
        r"\b(?:execute|run|eval|exec)\b.{0,30}\b(?:command|shell|bash|system|os\.|subprocess)\b",
        0.90,
    ),
    _pat(
        "SYSTEM_COMMAND",
        r"(?:;\s*(?:rm|cat|ls|chmod|chown|wget|curl)\s)",
        0.90,
    ),
]

# Multi-language injection patterns
MULTILINGUAL_PATTERNS: list[_InjectionPattern] = [
    # Chinese: "ignore previous instructions"
    _pat("MULTILINGUAL_INJECTION", r"(?:忽略|无视|不理).{0,20}(?:指令|规则|指示|说明)", 0.85),
    # Arabic: "ignore instructions"
    _pat("MULTILINGUAL_INJECTION", r"(?:تجاهل|أهمل).{0,20}(?:التعليمات|الأوامر)", 0.85),
    # Russian/Cyrillic: "ignore instructions"
    _pat("MULTILINGUAL_INJECTION", r"(?:игнорируй|проигнорируй).{0,20}(?:инструкции|правила|указания)", 0.85),
    # Spanish
    _pat("MULTILINGUAL_INJECTION", r"\b(?:ignora|olvida|descarta)\b.{0,30}\b(?:instrucciones|reglas|indicaciones)\b.{0,20}\b(?:anteriores|previas|del sistema)\b", 0.85),
    # French
    _pat("MULTILINGUAL_INJECTION", r"\b(?:ignore|oublie)\b.{0,30}\b(?:instructions|r[eè]gles|consignes)\b.{0,20}\b(?:pr[eé]c[eé]dentes|du syst[eè]me)\b", 0.85),
]

ALL_PATTERNS: list[_InjectionPattern] = (
    INSTRUCTION_OVERRIDE_PATTERNS
    + CREDENTIAL_EXTRACTION_PATTERNS
    + POLICY_BYPASS_PATTERNS
    + PRIVILEGE_ESCALATION_PATTERNS
    + DATA_EXFILTRATION_PATTERNS
    + SYSTEM_COMMAND_PATTERNS
    + MULTILINGUAL_PATTERNS
)


# ---------------------------------------------------------------------------
# Encoding / obfuscation detection
# ---------------------------------------------------------------------------

_BASE64_RE = re.compile(r"[A-Za-z0-9+/]{20,}={0,2}")
_HEX_RE = re.compile(r"(?:(?:0x|\\x)[0-9a-fA-F]{2}){4,}")
_URL_ENCODED_RE = re.compile(r"(?:%[0-9a-fA-F]{2}){4,}")

# Zero-width characters that can be used to evade detection
_ZERO_WIDTH_CHARS = frozenset(
    "​‌‍‎‏‪‫‬‭‮"
    "⁠⁡⁢⁣⁤⁦⁧⁨⁩﻿"
)

# Common homoglyph mappings (Cyrillic/Greek -> Latin)
_HOMOGLYPH_MAP: dict[str, str] = {
    "А": "A", "В": "B", "С": "C", "Е": "E",
    "Н": "H", "К": "K", "М": "M", "О": "O",
    "Р": "P", "Т": "T", "Х": "X",
    "а": "a", "е": "e", "о": "o", "р": "p",
    "с": "c", "у": "y", "х": "x",
    "Α": "A", "Β": "B", "Ε": "E", "Η": "H",
    "Ι": "I", "Κ": "K", "Μ": "M", "Ν": "N",
    "Ο": "O", "Ρ": "P", "Τ": "T", "Χ": "X",
    "α": "a", "ε": "e", "ο": "o", "ρ": "p",
}


def _strip_zero_width(text: str) -> str:
    """Remove zero-width characters used for steganographic evasion."""
    return "".join(ch for ch in text if ch not in _ZERO_WIDTH_CHARS)


def _normalize_homoglyphs(text: str) -> str:
    """Replace known homoglyphs with their Latin equivalents."""
    return "".join(_HOMOGLYPH_MAP.get(ch, ch) for ch in text)


def _normalize_text(text: str) -> str:
    """Full normalization pipeline for detection."""
    # Unicode NFKC normalization
    normalized = unicodedata.normalize("NFKC", text)
    # Strip zero-width chars
    normalized = _strip_zero_width(normalized)
    # Replace homoglyphs
    normalized = _normalize_homoglyphs(normalized)
    # Collapse whitespace
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def _detect_encoded_payloads(text: str) -> list[str]:
    """Detect potentially malicious encoded content."""
    evidence: list[str] = []

    # Base64 detection
    for match in _BASE64_RE.finditer(text):
        candidate = match.group()
        try:
            decoded = base64.b64decode(candidate, validate=True).decode("utf-8", errors="ignore")
            # Check if the decoded content contains suspicious keywords
            decoded_lower = decoded.lower()
            suspicious_keywords = [
                "ignore", "instructions", "system prompt", "password",
                "credential", "bypass", "admin", "eval", "exec",
            ]
            if any(kw in decoded_lower for kw in suspicious_keywords):
                evidence.append(f"base64_encoded_injection:{candidate[:30]}...")
        except Exception:
            pass

    # Hex encoding
    if _HEX_RE.search(text):
        evidence.append("hex_encoded_content_detected")

    # URL encoding
    if _URL_ENCODED_RE.search(text):
        evidence.append("url_encoded_content_detected")

    return evidence


def _detect_zero_width_steganography(original_text: str) -> list[str]:
    """Detect zero-width character steganography."""
    evidence: list[str] = []
    zwc_count = sum(1 for ch in original_text if ch in _ZERO_WIDTH_CHARS)
    if zwc_count > 3:
        evidence.append(f"zero_width_chars_detected:count={zwc_count}")
    return evidence


def _detect_homoglyph_evasion(original_text: str) -> list[str]:
    """Detect use of visually-similar Unicode characters to evade filters."""
    evidence: list[str] = []
    homoglyph_count = sum(1 for ch in original_text if ch in _HOMOGLYPH_MAP)
    if homoglyph_count > 2:
        evidence.append(f"homoglyph_evasion_detected:count={homoglyph_count}")
    return evidence


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def screen_metadata(
    text: str,
    content_origin: str = "unknown",
) -> ScreeningResult:
    """Screen a piece of retrieved metadata for indirect prompt injection.

    Parameters
    ----------
    text:
        The metadata text to screen (column description, table comment, etc.).
    content_origin:
        Attribution label for the source (e.g. "column:users.email.description").

    Returns
    -------
    ScreeningResult with flagged=True if injection is detected.
    """
    if not text or not text.strip():
        return ScreeningResult(
            flagged=False,
            threat_type="NONE",
            confidence=0.0,
            evidence=[],
            content_origin=content_origin,
        )

    # Pre-processing: detect evasion techniques on original text
    evasion_evidence: list[str] = []
    evasion_evidence.extend(_detect_zero_width_steganography(text))
    evasion_evidence.extend(_detect_homoglyph_evasion(text))
    evasion_evidence.extend(_detect_encoded_payloads(text))

    # Normalize for pattern matching
    normalized = _normalize_text(text)
    normalized_lower = normalized.lower()

    # Pattern matching on normalized text
    max_confidence = 0.0
    threat_type = "NONE"
    pattern_evidence: list[str] = []

    for pat in ALL_PATTERNS:
        if pat.pattern.search(normalized):
            if pat.confidence > max_confidence:
                max_confidence = pat.confidence
                threat_type = pat.threat_type
            pattern_evidence.append(f"pattern_match:{pat.threat_type}")

    # Evasion techniques themselves increase confidence
    encoded_injection = any("base64_encoded_injection" in e for e in evasion_evidence)
    if evasion_evidence and max_confidence > 0:
        max_confidence = min(1.0, max_confidence + 0.05)
    elif encoded_injection:
        # Base64-encoded injection content is high-confidence on its own
        max_confidence = 0.90
        threat_type = "INSTRUCTION_OVERRIDE"
    elif evasion_evidence and max_confidence == 0:
        # Evasion detected without pattern match -- still suspicious
        # but only flag if multiple evasion techniques are used
        if len(evasion_evidence) >= 2:
            max_confidence = 0.70
            threat_type = "OBFUSCATION_DETECTED"

    all_evidence = pattern_evidence + evasion_evidence
    flagged = max_confidence >= 0.70

    return ScreeningResult(
        flagged=flagged,
        threat_type=threat_type if flagged else "NONE",
        confidence=round(max_confidence, 4),
        evidence=all_evidence,
        content_origin=content_origin,
    )


def screen_metadata_batch(
    items: list[tuple[str, str]],
) -> list[ScreeningResult]:
    """Screen multiple metadata items.

    Parameters
    ----------
    items:
        List of (text, content_origin) pairs.

    Returns
    -------
    List of ScreeningResult, one per input item.
    """
    return [screen_metadata(text, origin) for text, origin in items]
