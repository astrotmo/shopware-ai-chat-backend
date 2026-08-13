"""Provider boundary for backend-managed domain term catalogues."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from .domain_knowledge_models import DomainTermEntry


class DomainTermsProvider(Protocol):
    """Minimal source contract consumed by :class:`DomainKnowledgeResolver`.

    ``source_version`` is an inexpensive change token.  Returning ``None``
    means no stable version is available, so an auto-reloading resolver cannot
    skip a provider load on that basis.
    """

    def load_terms(self) -> list[DomainTermEntry]:
        ...

    def source_version(self) -> str | None:
        ...


class JsonDomainTermsProvider:
    """Load a UTF-8 JSON array of domain term objects from local storage."""

    def __init__(self, file_path: str | Path):
        """Store the path without reading it; resolver startup controls loading."""
        self.file_path = Path(file_path)

    def source_version(self) -> str | None:
        """Return an mtime/size token, or ``None`` while the file is absent."""
        try:
            stat = self.file_path.stat()
        except FileNotFoundError:
            return None
        return f"{stat.st_mtime_ns}:{stat.st_size}"

    def load_terms(self) -> list[DomainTermEntry]:
        """Validate the top-level/list item shapes and sanitize every entry.

        A malformed item fails the complete load rather than producing a
        partially indexed knowledge catalogue.
        """
        if not self.file_path.exists():
            raise FileNotFoundError(f"Domain knowledge file not found: {self.file_path}")

        with self.file_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)

        if not isinstance(payload, list):
            raise ValueError("Domain knowledge JSON must be a list of objects.")

        entries: list[DomainTermEntry] = []
        for idx, item in enumerate(payload):
            if not isinstance(item, dict):
                raise ValueError(f"Domain knowledge entry at index {idx} must be an object.")
            entries.append(DomainTermEntry.from_dict(item))
        return entries
