# AICA system architecture audit — 2026-09-02

This backend and `astrotmo/AiCommerceAssistant` were audited as one system. Canonical documentation lives in the plugin repository; these pinned links avoid two independently edited copies. This branch contains documentation only. The target is proposed and no implementation phase has started.

Audited source baselines:

- Backend main: `fe72dd4af104cef5eaf61104fa73779f0bf7870b`; stage: `604bb5a9c8b5d1e1b3b40d96e108aae2af061a30`.
- Plugin main: `78581facc8470eef3e85ef667339693616c63cc7`; stage: `0555666efe2b94799c7b59dd9a950f6cda292c78`.
- Full main trees and stage differences were inspected; local isolated probes do not establish live Shopware/model/deployment behavior.

## Documents

- [ARCHITECTURE_AUDIT.md](https://github.com/astrotmo/AiCommerceAssistant/blob/72ef88a008b5f9f87479939820382d145aa97c8a/docs/ARCHITECTURE_AUDIT.md)
- [ARCHITECTURE_DECISIONS.md](https://github.com/astrotmo/AiCommerceAssistant/blob/72ef88a008b5f9f87479939820382d145aa97c8a/docs/ARCHITECTURE_DECISIONS.md)
- [ARCHITECTURE_RESEARCH.md](https://github.com/astrotmo/AiCommerceAssistant/blob/72ef88a008b5f9f87479939820382d145aa97c8a/docs/ARCHITECTURE_RESEARCH.md)
- [AUDIT_EVIDENCE.md](https://github.com/astrotmo/AiCommerceAssistant/blob/72ef88a008b5f9f87479939820382d145aa97c8a/docs/AUDIT_EVIDENCE.md)
- [CURRENT_ARCHITECTURE.md](https://github.com/astrotmo/AiCommerceAssistant/blob/72ef88a008b5f9f87479939820382d145aa97c8a/docs/CURRENT_ARCHITECTURE.md)
- [REWORK_PLAN.md](https://github.com/astrotmo/AiCommerceAssistant/blob/72ef88a008b5f9f87479939820382d145aa97c8a/docs/REWORK_PLAN.md)
- [TARGET_ARCHITECTURE.md](https://github.com/astrotmo/AiCommerceAssistant/blob/72ef88a008b5f9f87479939820382d145aa97c8a/docs/TARGET_ARCHITECTURE.md)

## Recommended direction

Keep Shopware and a modular FastAPI backend with a separately deployable local or explicitly configured remote model. Shopware owns contextual commerce and canonical conversation/contact persistence. The backend owns bounded async model orchestration through a thin provider adapter. Replace mandatory MCP/Admin API catalog access with a typed, authenticated Shopware gateway; keep MCP as an optional interoperability adapter.

The first implementation phase should close service-authentication, diagnostic exposure, model-price, output-validation and request-budget gaps. Contextual discovery must be in place, or unsafe catalog recommendations explicitly disabled, before rollout. Later phases establish canonical state, reliable UI and contact delivery, retention/ACL and installable deployment profiles. See the rework plan for breaking changes, migration risks and acceptance gates.

Historical runtime statements in `docs/backend-code-walkthrough.md` on stage are useful context, not tests rerun by this audit. Production code, dependencies and database schemas were not changed.
