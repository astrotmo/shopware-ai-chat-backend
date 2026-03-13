from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Iterable

from .domain_knowledge_loader import DomainTermsProvider
from .domain_knowledge_models import DomainKnowledgeMatch, DomainTermEntry

_PUNCT_RE = re.compile(r"[^\w\s]+", re.UNICODE)
_SPACE_RE = re.compile(r"\s+")
_WORD_RE = re.compile(r"\w+", re.UNICODE)
_UMLAUT_MAP = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})

_EXACT_CONFIDENCE = {
    "canonical": 0.99,
    "synonym": 0.97,
    "abbreviation": 0.96,
    "related_term": 0.91,
}
_PHRASE_CONFIDENCE = {
    "canonical": 0.93,
    "synonym": 0.90,
    "abbreviation": 0.88,
    "related_term": 0.84,
}
_STAGE_WEIGHT = {
    "exact": 300,
    "phrase": 200,
    "fuzzy": 100,
}
_VIA_WEIGHT = {
    "canonical": 40,
    "synonym": 30,
    "abbreviation": 20,
    "related_term": 10,
    "fuzzy": 0,
}


def normalize_text(text: str, *, fold_umlauts: bool = False) -> str:
    """Normalize free text for stable matching."""
    value = (text or "").strip().lower()
    if not value:
        return ""

    value = value.replace("-", " ").replace("_", " ").replace("/", " ")
    if fold_umlauts:
        value = value.translate(_UMLAUT_MAP)
    value = _PUNCT_RE.sub(" ", value)
    value = _SPACE_RE.sub(" ", value).strip()
    return value


def _singularize_token(token: str) -> str:
    """Apply conservative singularization to reduce plural mismatch misses."""
    if len(token) <= 4:
        return token
    if token.endswith("en") and len(token) > 6:
        return token[:-2]
    if token.endswith("e") and len(token) > 5:
        return token[:-1]
    if token.endswith("n") and len(token) > 5:
        return token[:-1]
    if token.endswith("s") and len(token) > 4:
        return token[:-1]
    return token


def _singularize_phrase(text: str) -> str:
    tokens = text.split()
    if not tokens:
        return text
    singularized = " ".join(_singularize_token(t) for t in tokens)
    return _SPACE_RE.sub(" ", singularized).strip()


def normalized_variants(text: str) -> set[str]:
    """Return normalization variants (base, umlaut-folded, singularized)."""
    base = normalize_text(text, fold_umlauts=False)
    if not base:
        return set()
    folded = normalize_text(text, fold_umlauts=True)
    variants = {base, folded, _singularize_phrase(base), _singularize_phrase(folded)}
    return {v for v in variants if v}


@dataclass(slots=True, frozen=True)
class _Candidate:
    entry_id: str
    source_text: str
    normalized: str
    via: str
    token_count: int


@dataclass(slots=True)
class _ScoredMatch:
    match: DomainKnowledgeMatch
    rank: int
    confidence: float


class DomainKnowledgeResolver:
    """Resolve user terms to canonical domain concepts."""

    def __init__(
        self,
        provider: DomainTermsProvider,
        *,
        enable_fuzzy: bool = True,
        fuzzy_threshold: float = 0.93,
        fuzzy_min_term_length: int = 5,
        auto_reload: bool = False,
    ):
        self.provider = provider
        self.enable_fuzzy = enable_fuzzy
        self.fuzzy_threshold = max(0.0, min(1.0, fuzzy_threshold))
        self.fuzzy_min_term_length = max(3, fuzzy_min_term_length)
        self.auto_reload = auto_reload

        self._entries_by_id: dict[str, DomainTermEntry] = {}
        self._exact_index: dict[str, list[_Candidate]] = {}
        self._phrase_candidates: list[_Candidate] = []
        self._fuzzy_candidates: list[_Candidate] = []
        self._max_candidate_tokens = 1
        self._source_version: str | None = None
        self._loaded = False

    def reload(self, *, force: bool = False) -> None:
        """Reload entries from provider and rebuild indexes."""
        version = self.provider.source_version()
        if self._loaded and not force and version is not None and version == self._source_version:
            return

        entries = self.provider.load_terms()
        self._entries_by_id = {entry.id: entry for entry in entries}
        self._exact_index = {}
        self._phrase_candidates = []
        self._fuzzy_candidates = []
        self._max_candidate_tokens = 1

        for entry in entries:
            self._index_entry(entry, entry.canonical_name, "canonical")
            for synonym in entry.synonyms:
                self._index_entry(entry, synonym, "synonym")
            for abbreviation in entry.abbreviations:
                self._index_entry(entry, abbreviation, "abbreviation")
            for related in entry.related_terms:
                self._index_entry(entry, related, "related_term")

        self._source_version = version
        self._loaded = True

    def resolve_message(self, message: str, *, max_matches: int = 5) -> list[DomainKnowledgeMatch]:
        """Resolve a user message to a ranked list of domain knowledge matches."""
        if not message or not message.strip():
            return []

        self._ensure_loaded()
        if not self._entries_by_id:
            return []

        normalized_message = normalize_text(message)
        if not normalized_message:
            return []

        gram_index = self._build_message_gram_index(message, max_tokens=self._max_candidate_tokens)
        if not gram_index:
            gram_index = {normalized_message: message.strip()}

        best_by_entry: dict[str, _ScoredMatch] = {}

        self._apply_exact_matching(gram_index, best_by_entry)
        self._apply_phrase_matching(message, normalized_message, best_by_entry)
        if self.enable_fuzzy:
            self._apply_fuzzy_matching(gram_index, best_by_entry)

        matches = [scored.match for scored in best_by_entry.values()]
        matches.sort(key=lambda item: (-item.confidence, item.canonical_name.lower()))

        if max_matches <= 0:
            return []
        return matches[:max_matches]

    def resolve_message_to_dicts(self, message: str, *, max_matches: int = 5) -> list[dict]:
        """Convenience method for API/trace serialization."""
        return [match.to_dict() for match in self.resolve_message(message, max_matches=max_matches)]

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.reload(force=True)
            return
        if self.auto_reload:
            self.reload(force=False)

    def _index_entry(self, entry: DomainTermEntry, raw_text: str, via: str) -> None:
        normalized = normalize_text(raw_text)
        if not normalized:
            return

        candidate = _Candidate(
            entry_id=entry.id,
            source_text=raw_text,
            normalized=normalized,
            via=via,
            token_count=len(normalized.split()),
        )
        self._max_candidate_tokens = max(self._max_candidate_tokens, candidate.token_count)

        for variant in normalized_variants(raw_text):
            self._exact_index.setdefault(variant, []).append(candidate)

        # Phrase matches are disabled for very short terms to avoid noise.
        if len(normalized) >= 4 or candidate.token_count > 1:
            self._phrase_candidates.append(candidate)

        if via in {"canonical", "synonym"} and len(normalized) >= self.fuzzy_min_term_length:
            self._fuzzy_candidates.append(candidate)

    def _apply_exact_matching(
        self,
        gram_index: dict[str, str],
        best_by_entry: dict[str, _ScoredMatch],
    ) -> None:
        for gram_norm, matched_surface in gram_index.items():
            for candidate in self._exact_index.get(gram_norm, []):
                confidence = _EXACT_CONFIDENCE.get(candidate.via, 0.9)
                self._consider_match(
                    best_by_entry=best_by_entry,
                    candidate=candidate,
                    matched_text=matched_surface,
                    matched_via=candidate.via,
                    confidence=confidence,
                    stage="exact",
                )

    def _apply_phrase_matching(
        self,
        raw_message: str,
        normalized_message: str,
        best_by_entry: dict[str, _ScoredMatch],
    ) -> None:
        message_variants = normalized_variants(raw_message)
        message_variants.add(normalized_message)

        for candidate in self._phrase_candidates:
            if candidate.token_count == 1 and len(candidate.normalized) < 5:
                continue

            if not any(self._contains_phrase(variant, candidate.normalized) for variant in message_variants):
                continue

            confidence = _PHRASE_CONFIDENCE.get(candidate.via, 0.83)
            via = f"{candidate.via}_phrase"
            matched_surface = self._extract_surface(raw_message, candidate.source_text) or candidate.source_text
            self._consider_match(
                best_by_entry=best_by_entry,
                candidate=candidate,
                matched_text=matched_surface,
                matched_via=via,
                confidence=confidence,
                stage="phrase",
            )

    def _apply_fuzzy_matching(
        self,
        gram_index: dict[str, str],
        best_by_entry: dict[str, _ScoredMatch],
    ) -> None:
        grams = [(norm, surface) for norm, surface in gram_index.items() if len(norm) >= self.fuzzy_min_term_length]
        if not grams:
            return

        for candidate in self._fuzzy_candidates:
            best_ratio = 0.0
            best_surface = ""

            for gram_norm, surface in grams:
                if not gram_norm:
                    continue
                if gram_norm[0] != candidate.normalized[0]:
                    continue
                if abs(len(gram_norm) - len(candidate.normalized)) > max(2, int(len(candidate.normalized) * 0.35)):
                    continue

                ratio = SequenceMatcher(None, gram_norm, candidate.normalized).ratio()
                if ratio > best_ratio:
                    best_ratio = ratio
                    best_surface = surface

            if best_ratio < self.fuzzy_threshold:
                continue

            confidence = min(0.89, max(0.7, best_ratio - 0.02))
            self._consider_match(
                best_by_entry=best_by_entry,
                candidate=candidate,
                matched_text=best_surface or candidate.source_text,
                matched_via="fuzzy",
                confidence=confidence,
                stage="fuzzy",
            )

    def _consider_match(
        self,
        *,
        best_by_entry: dict[str, _ScoredMatch],
        candidate: _Candidate,
        matched_text: str,
        matched_via: str,
        confidence: float,
        stage: str,
    ) -> None:
        entry = self._entries_by_id.get(candidate.entry_id)
        if entry is None:
            return

        rank = _STAGE_WEIGHT.get(stage, 0) + _VIA_WEIGHT.get(candidate.via, 0)
        match = self._build_match(
            entry=entry,
            matched_text=matched_text.strip() or candidate.source_text,
            matched_via=matched_via,
            confidence=confidence,
        )

        current = best_by_entry.get(entry.id)
        if current is None:
            best_by_entry[entry.id] = _ScoredMatch(match=match, rank=rank, confidence=confidence)
            return

        if rank > current.rank or (rank == current.rank and confidence > current.confidence):
            best_by_entry[entry.id] = _ScoredMatch(match=match, rank=rank, confidence=confidence)

    def _build_match(
        self,
        *,
        entry: DomainTermEntry,
        matched_text: str,
        matched_via: str,
        confidence: float,
    ) -> DomainKnowledgeMatch:
        mcp_terms = entry.mcp_search_terms or [
            entry.canonical_name,
            *entry.synonyms,
            *entry.abbreviations,
        ]
        return DomainKnowledgeMatch(
            matched_text=matched_text,
            matched_via=matched_via,
            canonical_name=entry.canonical_name,
            synonyms=list(entry.synonyms),
            related_terms=list(entry.related_terms),
            category_hint=entry.category_hint,
            notes=entry.notes,
            mcp_search_terms=mcp_terms,
            confidence=round(max(0.0, min(1.0, confidence)), 4),
            entry_id=entry.id,
        )

    @staticmethod
    def _contains_phrase(message_norm: str, phrase_norm: str) -> bool:
        haystack = f" {message_norm.strip()} "
        needle = f" {phrase_norm.strip()} "
        return needle in haystack

    @staticmethod
    def _extract_surface(raw_message: str, phrase: str) -> str | None:
        pattern = r"\b" + re.escape(phrase).replace(r"\ ", r"\s+") + r"\b"
        match = re.search(pattern, raw_message, flags=re.IGNORECASE)
        if not match:
            return None
        return raw_message[match.start() : match.end()]

    @staticmethod
    def _build_message_gram_index(message: str, *, max_tokens: int) -> dict[str, str]:
        tokens = list(_WORD_RE.finditer(message))
        if not tokens:
            return {}

        out: dict[str, str] = {}
        max_tokens = max(1, max_tokens)
        for start in range(len(tokens)):
            end_limit = min(len(tokens), start + max_tokens)
            for end in range(start, end_limit):
                span_start = tokens[start].start()
                span_end = tokens[end].end()
                surface = message[span_start:span_end].strip()
                if not surface:
                    continue
                for variant in normalized_variants(surface):
                    out.setdefault(variant, surface)
        return out


def iter_match_mcp_terms(matches: Iterable[DomainKnowledgeMatch]) -> list[str]:
    """Flatten unique MCP search terms from resolver matches."""
    out: list[str] = []
    seen: set[str] = set()
    for match in matches:
        for term in match.mcp_search_terms:
            key = term.strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(term)
    return out
