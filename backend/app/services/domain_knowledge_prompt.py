from __future__ import annotations

from typing import Sequence

from .domain_knowledge_models import DomainKnowledgeMatch


def build_domain_knowledge_prompt_block(matches: Sequence[DomainKnowledgeMatch]) -> str:
    """Build a compact deterministic system block for resolved domain context."""
    if not matches:
        return ""

    lines: list[str] = ["Resolved domain knowledge (trusted backend context):"]
    for match in matches:
        lines.append(f'- User term: "{match.matched_text}"')
        lines.append(f"- Matched via: {match.matched_via} (confidence: {match.confidence:.2f})")
        lines.append(f'- Canonical shop concept: "{match.canonical_name}"')
        lines.append(f"- Synonyms: {match.synonyms}")
        lines.append(f"- Related terms: {match.related_terms}")
        lines.append(f'- Category hint: "{match.category_hint}"')
        lines.append(f'- Notes: "{match.notes}"')
        lines.append(f"- Preferred MCP search terms: {match.mcp_search_terms}")

    lines.append("")
    lines.append("Instruction:")
    lines.append("Treat this resolved domain knowledge as trusted backend context when generating MCP queries.")
    lines.append("Prefer canonical_name and preferred MCP search terms when choosing query terms.")
    return "\n".join(lines)
