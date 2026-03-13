from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from .domain_knowledge_models import DomainTermEntry


class DomainTermsProvider(Protocol):
    """Provider abstraction so resolver logic is decoupled from file format."""

    def load_terms(self) -> list[DomainTermEntry]:
        ...

    def source_version(self) -> str | None:
        ...


class JsonDomainTermsProvider:
    """Load domain terms from a local JSON file (list-of-objects)."""

    def __init__(self, file_path: str | Path):
        self.file_path = Path(file_path)

    def source_version(self) -> str | None:
        try:
            stat = self.file_path.stat()
        except FileNotFoundError:
            return None
        return f"{stat.st_mtime_ns}:{stat.st_size}"

    def load_terms(self) -> list[DomainTermEntry]:
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
