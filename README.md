# Shopware ↔ Ollama Chat Backend (MCP)

This project provides a chat backend for the Shopware storefront chat. It connects a **local Ollama model** (OpenAI-compatible API) with **Shopware product data** via **MCP tools** (Model Context Protocol). The MCP boundary exposes a limited product/category field set, and the prompts instruct the model not to invent catalogue data.

> Note: Prompts and response formatting are **German-first**. The MCP tool
> signatures accept `DEFAULT_LOCALE`, but the current Shopware requests do not
> forward that argument; see the walkthrough before relying on locale changes.

For the exact runtime flow, contracts, container/host path mapping, domain
matching, normalization fallbacks, and safe validation, see
[`docs/backend-code-walkthrough.md`](docs/backend-code-walkthrough.md).

## Overview

**Components:**

- **Chat API (`app.py`)**: FastAPI service for Shopware chat. Builds prompts, calls Ollama, and orchestrates MCP tool calls.
- **MCP Server (`shopware_mcp_server.py`)**: Exposes product/category tools and talks to the Shopware Admin API.
- **Ollama**: Local LLM server with an OpenAI-compatible endpoint.

## Features

- Tool-assisted product search with a field-minimized catalogue view; storefront visibility is not proven
- JSON response schema for storefront frontend
- Optional in-memory tracing of model and tool phases
- Backend-managed domain terminology for search-query guidance

## Requirements

- Python **>= 3.10**
- [uv](https://github.com/astral-sh/uv) (recommended) or pip
- Local Ollama installation with a model pulled (e.g. `llama3.1:8b`)
- Shopware Admin API access

## Configuration (.env)

Create an untracked `.env` file in the project root (see the sanitized example
below). `compose.yaml` injects this exact file into both Python services.
Compose CLI `--env-file .env.local` does not replace the service-level
`env_file: .env`.

```dotenv
# Chat Backend
CHAT_HOST=0.0.0.0
CHAT_PORT=8002
CHAT_LOGGING_LEVEL=info
CHAT_DRY_RUN=0
CORS_ORIGINS=*
# Optional additional origin regex
CORS_ORIGIN_REGEX=

# Ollama (OpenAI-compatible)
OLLAMA_BASE_URL=http://ollama:11434/v1
OLLAMA_API_KEY=ollama
OLLAMA_MODEL=llama3.1:8b
# Optional fallback context size for all models
OLLAMA_NUM_CTX=8192
# Optional per-model context overrides
OLLAMA_NUM_CTX_BY_MODEL=llama3.1:8b=8192,gpt-oss:20b=8192,ministral-3:8b=8192,qwen3-vl:8b=8192
# Optional model aliases (recommended for per-model context)
OLLAMA_MODEL_ALIAS_BY_MODEL=llama3.1:8b=llama3.1:8b-8k,gpt-oss:20b=gpt-oss:20b-8k,ministral-3:8b=ministral-3:8b-8k,qwen3-vl:8b=qwen3-vl:8b-8k

# MCP (Shopware Tools)
MCP_URL=http://mcp:8005/mcp
MCP_LOGGING_LEVEL=info
DEFAULT_LOCALE=de-DE

# Backend-managed domain terminology
DOMAIN_KNOWLEDGE_ENABLED=1
DOMAIN_KNOWLEDGE_PATH=backend/data/domain_terms.json
DOMAIN_KNOWLEDGE_MAX_MATCHES=4
DOMAIN_KNOWLEDGE_ENABLE_FUZZY=1
DOMAIN_KNOWLEDGE_FUZZY_THRESHOLD=0.93

# Model-phase diagnostics (development only)
TRACE_ENABLED=0

# Shopware Admin API (OAuth Client Credentials)
SHOPWARE_BASE_URL=https://your-shopware-host
SHOPWARE_CLIENT_ID=your-client-id
SHOPWARE_CLIENT_SECRET=your-client-secret
```

> Note: `OLLAMA_NUM_CTX_BY_MODEL` overrides `OLLAMA_NUM_CTX` for matching model names.
> Note: For Ollama's OpenAI-compatible `/v1` API, per-model context is most reliable via alias models created with `PARAMETER num_ctx` (see below).

### Per-model `num_ctx` with Ollama aliases

Create one alias model per base model and bake in `num_ctx`:

```bash
docker compose exec -T ollama sh -lc "printf 'FROM llama3.1:8b\nPARAMETER num_ctx 8192\n' >/tmp/llama3_8b_8k.Modelfile && ollama create llama3.1:8b-8k -f /tmp/llama3_8b_8k.Modelfile"
docker compose exec -T ollama sh -lc "printf 'FROM gpt-oss:20b\nPARAMETER num_ctx 8192\n' >/tmp/gpt_oss_20b_8k.Modelfile && ollama create gpt-oss:20b-8k -f /tmp/gpt_oss_20b_8k.Modelfile"
docker compose exec -T ollama sh -lc "printf 'FROM ministral-3:8b\nPARAMETER num_ctx 8192\n' >/tmp/ministral3_8b_8k.Modelfile && ollama create ministral-3:8b-8k -f /tmp/ministral3_8b_8k.Modelfile"
docker compose exec -T ollama sh -lc "printf 'FROM qwen3-vl:8b\nPARAMETER num_ctx 8192\n' >/tmp/qwen3_vl_8b_8k.Modelfile && ollama create qwen3-vl:8b-8k -f /tmp/qwen3_vl_8b_8k.Modelfile"
```

Then route normal model names to aliases with `OLLAMA_MODEL_ALIAS_BY_MODEL`.

You can also rebuild all aliases from your `.env` in one step:

```bash
./scripts/rebuild_ollama_aliases.sh
docker compose restart server
```

## Local Development (uv)

```bash
# 1) Install uv (once)
pip install uv

# 2) Create venv & install dependencies
uv venv
uv sync

# 3) configure environment
# create an untracked .env from the sanitized configuration above
# set SHOPWARE_BASE_URL, SHOPWARE_CLIENT_ID, SHOPWARE_CLIENT_SECRET, etc.
# when all processes run directly on the host (not Compose), change the
# Ollama/MCP service hostnames above to localhost

# 4) Start the MCP server (Shopware tools)
uv run python shopware_mcp_server.py

# 5) Start the chat backend
uv run uvicorn app:app --host 0.0.0.0 --port 8002 --reload
```

## Docker Compose

The repo includes a `compose.yaml` that starts **Ollama**, **MCP**, and the **chat backend** together:

```bash
docker compose up --build
```

Ports:

- **8002** → Chat API
- **8005** → MCP Server
- **11434** → Ollama

## API Endpoints

### `GET /healthz`

Process-liveness and effective-configuration check. It does not probe Ollama,
MCP, or Shopware.

**Abbreviated response:**

```json
{ "status": "ok", "model": "llama3.1:8b" }
```

### `POST /chat`

Chat endpoint for the storefront frontend.

**Request Body (example):**

```json
{
  "message": "Do you have HDMI cables?",
  "history": [],
  "model": "llama3.1:8b",
  "client": {
    "contextToken": "optional but currently ignored"
  }
}
```

**Response:**

Successful chat responses are JSON. Raw model output is normalized to the
supported `type`/`blocks` contract, with a text-block fallback for invalid or
legacy output. See `FORMAT_PROMPT_PUBLIC`, `normalize_chat_reply`, and
`normalize_blocks` in `app.py`.

### `GET /trace/{request_id}`

Optional trace endpoint when `TRACE_ENABLED=1` is set. Traces are stored after
both model phases, before response normalization, and become eligible for lazy
removal after ten minutes. The endpoint has no application-level
authentication, so tracing should remain restricted to an appropriately
protected development environment.

## MCP Tools (Shopware)

The following MCP tools are available to the LLM:

- `search_products_public`
- `get_product_by_id_public`
- `get_product_by_number_public`
- `list_categories`

These tools use the Shopware Admin API via OAuth Client Credentials.

## Shopware Integration Notes

- The MCP layer exposes only limited catalogue fields, but Admin API results are
  not proof of storefront visibility or sales-channel availability.
- `contextToken` may still be sent by the storefront contract, but the chat backend ignores it.
- The chat logic includes rules for when tools must/must not be called.

## Troubleshooting

- **401/403 from Shopware**: verify client ID/secret and API permissions.
- **LLM returns non-JSON**: switch to `CHAT_LOGGING_LEVEL=debug` and inspect the response.
- **Tools unavailable**: MCP server not running or `MCP_URL` misconfigured.

## License

MIT
