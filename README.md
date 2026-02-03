# Shopware ↔ Ollama Chat Backend (MCP)

This project provides a chat backend for the Shopware storefront chat. It connects a **local Ollama model** (OpenAI-compatible API) with **Shopware product data** via **MCP tools** (Model Context Protocol). The LLM can fetch products, categories, and details without hallucinating product data.

> Note: Defaults (prompts, locale, and response formatting) are **German-first**. You can customize locale and copy by updating the environment values and prompts in `app.py`.

## Overview

**Components:**

- **Chat API (`app.py`)**: FastAPI service for Shopware chat. Builds prompts, calls Ollama, and orchestrates MCP tool calls.
- **MCP Server (`shopware_mcp_server.py`)**: Exposes product/category tools and talks to the Shopware Admin API.
- **Ollama**: Local LLM server with an OpenAI-compatible endpoint.

## Features

- Tool-assisted product search (public & authenticated)
- Clear separation of **public** (no prices) and **auth** (with prices)
- JSON response schema for storefront frontend
- Optional tracing of tool calls

## Requirements

- Python **>= 3.10**
- [uv](https://github.com/astral-sh/uv) (recommended) or pip
- Local Ollama installation with a model pulled (e.g. `llama3.1:8b`)
- Shopware Admin API access

## Configuration (.env)

Create a `.env` file in the project root (see example below). The same file is used by both services.

```dotenv
# Chat Backend
CHAT_HOST=0.0.0.0
CHAT_PORT=8002
CHAT_LOGGING_LEVEL=info
CHAT_AUTH_SECRET=change-me
CHAT_DRY_RUN=0
CORS_ORIGINS=*

# Ollama (OpenAI-compatible)
OLLAMA_BASE_URL=http://localhost:11434/v1
OLLAMA_API_KEY=ollama
OLLAMA_MODEL=llama3.1:8b

# MCP (Shopware Tools)
MCP_URL=http://localhost:8005/mcp
MCP_LOGGING_LEVEL=info
DEFAULT_LOCALE=de-DE

# Shopware Admin API (OAuth Client Credentials)
SHOPWARE_BASE_URL=https://your-shopware-host
SHOPWARE_CLIENT_ID=your-client-id
SHOPWARE_CLIENT_SECRET=your-client-secret
```

> Note: `CHAT_AUTH_SECRET` is used for optional context-token validation.

## Local Development (uv)

```bash
# 1) install uv (once)
# 1) Install uv (once)
pip install uv

# 2) clone/push this repo, then in the project dir:
# 2) Create venv & install dependencies
uv venv
uv sync  # installs dependencies from pyproject.toml
uv sync

# 3) configure environment
cp .env.example .env
# edit .env: SHOPWARE_BASE_URL, SHOPWARE_ACCESS_TOKEN, OLLAMA_MODEL, etc.
# 3) Start the MCP server (Shopware tools)
uv run python shopware_mcp_server.py

# 4) start the HTTP backend
# 4) Start the chat backend
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

Health check.

**Response:**

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
    "contextToken": "optional"
  }
}
```

**Response:**

The LLM always returns a JSON object with `type` and `blocks`. For the exact schema rules, see `FORMAT_PROMPT_PUBLIC` and `FORMAT_PROMPT_AUTH` in `app.py`.

### `GET /trace/{request_id}`

Optional trace endpoint when `TRACE_ENABLED=1` is set.

## MCP Tools (Shopware)

The following MCP tools are available to the LLM:

- `search_products_public`
- `get_product_by_id_public`
- `get_product_by_number_public`
- `search_products_auth`
- `get_product_by_id_auth`
- `get_product_by_number_auth`
- `list_categories`

These tools use the Shopware Admin API via OAuth Client Credentials.

## Shopware Integration Notes

- **Public mode**: no prices/conditions.
- **Auth mode**: prices available (depending on your Shopware setup).
- The chat logic includes rules for when tools must/must not be called.

## Troubleshooting

- **401/403 from Shopware**: verify client ID/secret and API permissions.
- **LLM returns non-JSON**: switch to `CHAT_LOGGING_LEVEL=debug` and inspect the response.
- **Tools unavailable**: MCP server not running or `MCP_URL` misconfigured.

## License

MIT