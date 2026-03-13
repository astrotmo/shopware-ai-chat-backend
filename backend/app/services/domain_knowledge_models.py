from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def _as_clean_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _as_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = _as_clean_str(item)
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


@dataclass(slots=True)
class DomainTermEntry:
    """Single domain concept from a backend-managed knowledge source."""

    id: str
    canonical_name: str
    synonyms: list[str] = field(default_factory=list)
    related_terms: list[str] = field(default_factory=list)
    abbreviations: list[str] = field(default_factory=list)
    category_hint: str = ""
    notes: str = ""
    mcp_search_terms: list[str] = field(default_factory=list)
    shop_examples: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "DomainTermEntry":
        """Parse and sanitize one domain entry from JSON/CSV-like records."""
        canonical_name = _as_clean_str(raw.get("canonical_name"))
        if not canonical_name:
            raise ValueError("domain entry is missing canonical_name")

        entry_id = _as_clean_str(raw.get("id")) or canonical_name.lower().replace(" ", "-")

        return cls(
            id=entry_id,
            canonical_name=canonical_name,
            synonyms=_as_string_list(raw.get("synonyms")),
            related_terms=_as_string_list(raw.get("related_terms")),
            abbreviations=_as_string_list(raw.get("abbreviations")),
            category_hint=_as_clean_str(raw.get("category_hint")),
            notes=_as_clean_str(raw.get("notes")),
            mcp_search_terms=_as_string_list(raw.get("mcp_search_terms")),
            shop_examples=_as_string_list(raw.get("shop_examples")),
        )


@dataclass(slots=True)
class DomainKnowledgeMatch:
    """Structured resolver output used for prompt injection and downstream logic."""

    matched_text: str
    matched_via: str
    canonical_name: str
    synonyms: list[str]
    related_terms: list[str]
    category_hint: str
    notes: str
    mcp_search_terms: list[str]
    confidence: float
    entry_id: str

    def to_dict(self) -> dict[str, Any]:
        """Convert to a JSON-serializable dictionary."""
        return {
            "matched_text": self.matched_text,
            "matched_via": self.matched_via,
            "canonical_name": self.canonical_name,
            "synonyms": self.synonyms,
            "related_terms": self.related_terms,
            "category_hint": self.category_hint,
            "notes": self.notes,
            "mcp_search_terms": self.mcp_search_terms,
            "confidence": round(self.confidence, 4),
            "entry_id": self.entry_id,
        }
