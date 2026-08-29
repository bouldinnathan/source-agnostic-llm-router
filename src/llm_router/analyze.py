"""Cheap local task analysis used to create soft routing preferences."""

from __future__ import annotations

import re
from collections.abc import Iterable


_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "coding": (
        re.compile(r"```|\b(code|coding|program|function|class|api|sql|regex)\b", re.I),
        re.compile(r"\b(debug|refactor|compile|test suite|stack trace|repository)\b", re.I),
    ),
    "reasoning": (
        re.compile(r"\b(analy[sz]e|reason|prove|derive|architecture|trade-?offs?|root cause)\b", re.I),
        re.compile(r"\b(step by step|multi-step|strategy|investigate|evaluate)\b", re.I),
    ),
    "math": (
        re.compile(r"\b(equation|integral|derivative|probability|theorem|calculate)\b", re.I),
        re.compile(r"(?:\d\s*[+*/^=]\s*\d)|(?:\\(?:frac|sum|int)\b)"),
    ),
    "vision": (
        re.compile(r"\b(image|photo|screenshot|diagram|visual|picture|scan)\b", re.I),
    ),
    "tool_use": (
        re.compile(r"\b(call|invoke|use) (?:an? )?(?:api|tool|function)\b", re.I),
        re.compile(r"\b(browse|search the web|look up|database|execute)\b", re.I),
    ),
    "structured_output": (
        re.compile(r"\b(json|jsonl|xml|yaml|csv|schema|structured output)\b", re.I),
    ),
    "writing": (
        re.compile(r"\b(write|rewrite|draft|edit|polish|copyedit|blog|memo|email)\b", re.I),
    ),
    "multilingual": (
        re.compile(r"\b(translate|translation|multilingual|locali[sz]e)\b", re.I),
    ),
}


def infer_capabilities(text: str, *, tools_present: bool = False) -> tuple[str, ...]:
    """Infer soft capability preferences without sending the prompt anywhere."""

    inferred: list[str] = []
    for capability, patterns in _PATTERNS.items():
        if any(pattern.search(text) for pattern in patterns):
            inferred.append(capability)

    if tools_present and "tool_use" not in inferred:
        inferred.append("tool_use")
    if len(text) > 24_000 and "long_context" not in inferred:
        inferred.append("long_context")
    non_ascii = sum(1 for character in text if ord(character) > 127)
    if text and non_ascii / len(text) > 0.12 and "multilingual" not in inferred:
        inferred.append("multilingual")
    if not inferred:
        inferred.append("general")
    return tuple(inferred)


def normalize_capabilities(values: Iterable[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = value.strip().lower().replace("-", "_").replace(" ", "_")
        if item and item not in seen:
            normalized.append(item)
            seen.add(item)
    return tuple(normalized)
