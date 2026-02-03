from inspect import trace
import os, json, logging, asyncio, base64, time, hashlib, hmac
from typing import Any, Callable, Dict, List, Optional, cast
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from openai import OpenAI
from enum import Enum, auto

from mcp.client.streamable_http import streamablehttp_client
from mcp.client.session import ClientSession

from openai.types.chat import ChatCompletionMessageParam
from openai.types.chat import ChatCompletionToolUnionParam

load_dotenv()

CHAT_HOST = os.getenv("CHAT_HOST", "0.0.0.0")
CHAT_PORT = int(os.getenv("CHAT_PORT", "8002"))
CHAT_LOGGING_LEVEL = os.getenv("CHAT_LOGGING_LEVEL", "info").upper()
CHAT_AUTH_SECRET = os.getenv("CHAT_AUTH_SECRET", "")
CHAT_DRY_RUN = os.getenv("CHAT_DRY_RUN", "0") in ("1","true","TRUE","yes","YES")

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1").rstrip("/")
OLLAMA_API_KEY = os.getenv("OLLAMA_API_KEY", "ollama")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1:8b")

MCP_URL = os.getenv("MCP_URL", "http://localhost:8005/mcp").rstrip("/")

TRACE_ENABLED = os.getenv("TRACE_ENABLED", "0") == "1"

TRACE_STORE: dict[str, list[dict]] = {}
TRACE_TTL_SECONDS = 60 * 10
TRACE_CREATED: dict[str, float] = {}

""" Tool prompt for the LLM to behave as Shopware Storefront assistant """
TOOL_PROMPT = """
Du bist ein hilfreicher Assistent im Shopware-Storefront-Chat. Sprich Deutsch.

ENTSCHEIDUNGSREGELN (WICHTIG):
1) Wenn die Nutzerfrage Preise, Konditionen, Rabatte, Staffelpreise, Versandkosten, Angebote,
   oder andere nicht über Tools verfügbare Infos betrifft:
   - Rufe KEINE Tools auf.
   - Antworte kurz, dass dafür ein Kontakt nötig ist.
   - Gib KEINEN Tool-Call zurück (tool_calls leer).
   - (Die finale Ausgabe wird später als JSON inkl. Formular formatiert.)

2) Wenn die Nutzerfrage Produkte, Kategorien oder Produktdetails betrifft:
   - MUSST du Tools aufrufen, um Infos abzurufen.
   - Erfinde niemals Produktdaten.
   - Gib KEINE finale Antwort aus, sondern nur Tool-Calls (content darf leer sein).

VERFÜGBARE TOOLS:
- search_products_public: Suche Produkte per Freitext (ohne Preise).
- get_product_by_id_public: Produkt per UUID (ohne Preise).
- get_product_by_number_public: Produkt(e) per exakter productNumber (ohne Preise).
- list_categories: Kategorien auflisten.

Wenn du Tools aufrufst, setze finish_reason="tool_calls" und gib passende Argumente an.
Falls du kein Ergebnis von den Tools erhältst, melde dies als Fehler.
"""

""" Format prompt for public requests to the LLM to format the final answer as JSON"""
FORMAT_PROMPT_PUBLIC = """
Gib jetzt die finale Antwort als GENAU EIN JSON-OBJEKT aus (kein Text außerhalb).
Nutze dieses Schema:
{
  "type": "answer" | "clarification" | "error",
  "blocks": [
    {
      "kind": "text",
      "text": "Antwort in natürlicher Sprache."
    },
    {
      "kind": "product_list",
      "title": "string",
      "products": [
        {
          "id": "string",
          "name": "string",
          "productNumber": "string",
        }
      ]
    },
    {
      "kind": "info_box",
      "style": "info" | "warning" | "error",
      "title": "string",
      "text": "string"
    },
    {
      "kind": "formular",
      "title": "string",
      "reason": "string",
      "submitLabel": "string",
      "endpoint": "string",
      "method": "POST",
      "fields": [
        {
          "key": "name" | "email" | "phone" | "company" | "message" | "productRef" | "quantity" | "deliveryZip",
          "label": "string",
          "type": "text" | "email" | "tel" | "textarea" | "number",
          "required": true | false,
          "placeholder": "string|null",
          "value": "string|null"
        }
      ]
    }
  ]
}

REGELN ZUM JSON-SCHEMA
- "type" beschreibt den Charakter der Antwort:
  - "answer": normale Antworten
  - "clarification": Rückfragen, wenn Informationen fehlen
  - "error": echte Fehler (z.B. Tool nicht verfügbar)
- "blocks" ist IMMER ein Array.
- Jeder Block hat ein "kind".
- Verwende IMMER mindestens einen "text"-Block.
- Wenn Produkte erwähnt oder empfohlen werden, MUSS zusätzlich ein "product_list"-Block enthalten sein, da im Antwort "text" KEINE Produktdetails ausgegeben werden dürfen.

KONTAKTFORMULAR (PUBLIC):
- Keine Preise/Konditionen nennen oder andeuten.
- Wenn nach Preisen, Konditionen, Angeboten, Rabatten, Staffelpreisen, Versandkosten gefragt wird:
  - Gib einen "formular"-Block aus (zusätzlich zum "text"-Block).
  - Im "text"-Block: kurz erklären, dass Preise/Konditionen über Kontakt/Angebot laufen.
  - "reason": kurze Begründung (z.B. "Preise und Konditionen sind kundenabhängig").
  - Felder minimal: name, email, message, phone, company; optional: productRef, quantity, deliveryZip.
  - endpoint z.B. "/paul-ai-chat/contact" (oder dein Endpoint).

SPRACHE & STIL
- Schreibe klar, knapp und freundlich.
- Wenige Emojis sind erlaubt, aber nicht erforderlich.
- Nutze die Blocks sinnvoll:
  - "text" für Erklärung
  - "product_list" für Produkte
  - "info_box" für Hinweise oder leere Ergebnisse
  - "formular" für Kontaktanfragen

FEHLERFALL
- Wenn du unsicher bist, liefere trotzdem syntaktisch gültiges JSON
  und erkläre die Unsicherheit im "text"-Block.

WICHTIG (PUBLIC):
- Keine Preise/Konditionen nennen oder andeuten.
- Wenn nach Preisen gefragt wird: Hinweis, dass Preise & Konditionen nach Login oder über das Kontaktformular erfragt werden können.
- Produktdetails nie im Text-Block ausgeben, sondern nur in product_list.
"""

""" Format prompt for authenticated requests to the LLM to format the final answer as JSON"""
FORMAT_PROMPT_AUTH="""
Gib jetzt die finale Antwort als GENAU EIN JSON-OBJEKT aus (kein Text außerhalb).
Nutze dieses Schema:
{
  "type": "answer" | "clarification" | "error",
  "blocks": [
    {
      "kind": "text",
      "text": "Antwort in natürlicher Sprache."
    },
    {
      "kind": "product_list",
      "title": "string",
      "products": [
        {
          "id": "string",
          "name": "string",
          "price": "string|null",
          "currency": "string|null"
        }
      ]
    },
    {
      "kind": "info_box",
      "style": "info" | "warning" | "error",
      "title": "string",
      "text": "string"
    }
  ]
}

REGELN ZUM JSON-SCHEMA
- "type" beschreibt den Charakter der Antwort:
  - "answer": normale Antworten
  - "clarification": Rückfragen, wenn Informationen fehlen
  - "error": echte Fehler (z.B. Tool nicht verfügbar)
- "blocks" ist IMMER ein Array.
- Jeder Block hat ein "kind".
- Verwende IMMER mindestens einen "text"-Block.
- Wenn Produkte erwähnt oder empfohlen werden, MUSS zusätzlich ein "product_list"-Block enthalten sein, da im Antwort "text" KEINE Produktdetails ausgegeben werden dürfen.

SPRACHE & STIL
- Schreibe klar, knapp und freundlich.
- Wenige Emojis sind erlaubt, aber nicht erforderlich.
- Nutze die Blocks sinnvoll:
  - "text" für Erklärung
  - "product_list" für Produkte
  - "info_box" für Hinweise oder leere Ergebnisse

FEHLERFALL
- Wenn du unsicher bist, liefere trotzdem syntaktisch gültiges JSON
  und erkläre die Unsicherheit im "text"-Block.

WICHTIG (AUTH):
- Produktdetails nie im Text-Block ausgeben, sondern nur in product_list.
"""

""" MCP Tool Definitions and Call Logic for public requests"""
TOOLS_PUBLIC: list[ChatCompletionToolUnionParam] = [
    {
        "type": "function",
        "function": {
            "name": "search_products_public",
            "description": "Suche nach Produkten. Gibt KEINE Preise oder Konditionen zurück.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Suchbegriff (Produktname, Artikelnummer, etc.)"
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximale Anzahl Ergebnisse",
                        "default": 10
                    },
                    "locale": {
                        "type": "string",
                        "description": "Locale (z.B. de-DE)",
                        "default": "de-DE"
                    }
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_product_by_id_public",
            "description": "Lädt ein Produkt anhand der ID. Gibt KEINE Preise zurück.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "Produkt-ID"
                    },
                    "locale": {
                        "type": "string",
                        "default": "de-DE"
                    }
                },
                "required": ["id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_product_by_number_public",
            "description": "Lädt ein Produkt anhand der Artikelnummer. Gibt KEINE Preise zurück.",
            "parameters": {
                "type": "object",
                "properties": {
                    "product_number": {
                        "type": "string",
                        "description": "Artikelnummer"
                    },
                    "locale": {
                        "type": "string",
                        "default": "de-DE"
                    }
                },
                "required": ["product_number"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_categories",
            "description": "Listet Produktkategorien auf.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    }
]

TOOLS_AUTH: list[ChatCompletionToolUnionParam] = [
    {
        "type": "function",
        "function": {
            "name": "search_products_auth",
            "description": "Suche nach Produkten inkl. Preisen und Konditionen.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Suchbegriff (Produktname, Artikelnummer, etc.)"
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximale Anzahl Ergebnisse",
                        "default": 10
                    },
                    "locale": {
                        "type": "string",
                        "description": "Locale (z.B. de-DE)",
                        "default": "de-DE"
                    }
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_product_by_id_auth",
            "description": "Lädt ein Produkt anhand der ID inkl. Preis.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "Produkt-ID"
                    },
                    "locale": {
                        "type": "string",
                        "default": "de-DE"
                    }
                },
                "required": ["id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_product_by_number_auth",
            "description": "Lädt ein Produkt anhand der Artikelnummer inkl. Preis.",
            "parameters": {
                "type": "object",
                "properties": {
                    "product_number": {
                        "type": "string",
                        "description": "Artikelnummer"
                    },
                    "locale": {
                        "type": "string",
                        "default": "de-DE"
                    }
                },
                "required": ["product_number"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_categories",
            "description": "Listet Produktkategorien auf.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    }
]

logger = logging.getLogger("chat-backend")
logger.setLevel(getattr(logging, CHAT_LOGGING_LEVEL, logging.INFO))
logger.propagate = False

if not any(isinstance(h, logging.StreamHandler) for h in logger.handlers):
    handler = logging.StreamHandler()
    formatter = logging.Formatter("%(levelname)s %(name)s: %(message)s")
    handler.setFormatter(formatter)
    handler.addFilter(
        lambda record: record.levelno != logging.INFO
    )
    logger.addHandler(handler)

class ChatIn(BaseModel):
    """Input model for /chat endpoint."""
    message: str
    history: Optional[List[Dict[str, Any]]] = []
    model: str
    client: Dict[str, str] = {}

class Phase(Enum):
    """Phases of the chat_with_tools loop."""
    TOOL = auto()
    FINAL = auto()

class McpSessionCache:
    """MCP Session Cache to reuse connections."""
    def __init__(self, mcp_url: str):
        self.mcp_url = mcp_url
        self._lock = asyncio.Lock()

        self._transport_cm = None
        self._transport = None  # (read, write, _)
        self._session: Optional[ClientSession] = None
        self._initialized = False

    async def _ensure_connected(self) -> ClientSession:
        async with self._lock:
            if self._session is not None and self._initialized:
                return self._session

            # (Re-)open transport
            self._transport_cm = streamablehttp_client(self.mcp_url)
            self._transport = await self._transport_cm.__aenter__()
            read, write, _ = self._transport

            # (Re-)create session
            self._session = ClientSession(read, write)
            await self._session.__aenter__()
            await self._session.initialize()
            self._initialized = True

            return self._session

    async def close(self) -> None:
        async with self._lock:
            self._initialized = False

            if self._session is not None:
                try:
                    await self._session.__aexit__(None, None, None)
                except Exception:
                    pass
                self._session = None

            if self._transport_cm is not None:
                try:
                    await self._transport_cm.__aexit__(None, None, None)
                except Exception:
                    pass
                self._transport_cm = None
                self._transport = None

    async def call_tool(self, tool_name: str, args: Dict[str, Any]) -> Any:
        # Ensure session
        session = await self._ensure_connected()
        try:
            return await session.call_tool(tool_name, args)
        except Exception:
            # If anything goes wrong, reconnect once
            await self.close()
            session = await self._ensure_connected()
            return await session.call_tool(tool_name, args)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # startup: optionally warm up the MCP connection
    # await mcp_cache._ensure_connected()
    yield
    # shutdown:
    await mcp_cache.close()

client = OpenAI(base_url=OLLAMA_BASE_URL, api_key=OLLAMA_API_KEY)
mcp_cache = McpSessionCache(MCP_URL)

# CORS
cors_origins = [o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",")]
app = FastAPI(title="Shopware Chat Backend (Ollama + MCP)", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins if cors_origins != ["*"] else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/healthz")
def health():
    """
    Check endpoint to verify that the service is running.
    """
    return {"status": "ok", "model": OLLAMA_MODEL}


@app.post("/chat")
async def chat(in_: ChatIn, request: Request):
    """
    Main chat endpoint for Shopware Storefront Chat.

    :param in_: Input data containing message, history, model, and client info
    :type in_: ChatIn
    :param request: FastAPI Request object
    :type request: Request
    :return: JSON response with chat answer blocks
    """

    trace_cleanup()

    """
    Verify client token (if provided) and extract context
    """
    ctx_payload = None
    try:
        token = (in_.client or {}).get("contextToken", "")
        ctx_payload = verify(token, CHAT_AUTH_SECRET)
    except Exception:
        ctx_payload = None

    user_logged_in = bool(ctx_payload and ctx_payload.get("loggedIn"))

    request_id = request.headers.get("X-Request-Id") or str(time.time_ns())
    trace: list[dict] = []
    def trace_add(kind: str, data: dict) -> None:
        if TRACE_ENABLED:
            trace.append({
                "ts_ms": int(time.time() * 1000),
                "kind": kind,
                "data": data,
            })

    """ FORMAT_PROMPT and TOOLS for now all public"""

    FORMAT_PROMPT = FORMAT_PROMPT_PUBLIC
    TOOLS = TOOLS_PUBLIC

    logger.debug("👤 Context: \n%sLogged In:\n%s", in_.model_dump_json(ensure_ascii=False, indent=2), user_logged_in)


    if CHAT_DRY_RUN:
        return {
            "type": "answer",
            "blocks": [
                {"kind": "info_box", "style": "info", "title": "Dry-Run", "text": "LLM call skipped (CHAT_DRY_RUN=1)."},
                {"kind": "text", "text": f"Received: {in_.message}"},
                {"kind": "text", "text": f"User logged in: {bool(ctx_payload and ctx_payload.get('loggedIn'))}"},
            ],
        }

    logger.info("💬 Received chat request: %s", in_.message)
    logger.debug("💬 Full chat request:\n%s", in_.model_dump_json(ensure_ascii=False, indent=2))
    
    if in_.history:
        logger.info("📜 History:\n%s", json.dumps(in_.history, ensure_ascii=False, indent=2))

    """
    Prepare messages for the LLM: system prompt + history + new user message.
    """
    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": TOOL_PROMPT},
    ]

    """
    Append history from the frontend (if you send it there).
    """
    for h in in_.history or []:
        role = h.get("role")
        if role in ("user", "assistant"):
            messages.append({
                "role": role,
                "content": h.get("content", ""),
            })

    """
    Append current user message.
    """
    messages.append({"role": "user", "content": in_.message})

    """
    Determine effective model to use
    """
    requested_model = (in_.model or "").strip()
    effective_model = requested_model if requested_model else OLLAMA_MODEL

    """
    Call LLM (with tools); implementation is in chat_with_tools(...)
    """
    reply_text = await chat_with_tools(
        cast(List[ChatCompletionMessageParam], messages),
        model=effective_model,
        tools=TOOLS,
        format_prompt=FORMAT_PROMPT,
        trace_add=trace_add,
        request_id=request_id
    )

    """
    Try to interpret the LLM reply as JSON according to our schema
    """
    try:
        data = json.loads(reply_text.strip())

        """
        Minimal validity check: it must be a dict with "blocks"
        """
        if not isinstance(data, dict):
            raise ValueError("LLM reply is not a JSON object")
        if "blocks" not in data:
            raise ValueError("LLM reply has no 'blocks' field")
        
        data["request_id"] = request_id

        if TRACE_ENABLED:
            TRACE_STORE[request_id] = trace
            TRACE_CREATED[request_id] = time.time()
            # optional: also return it inline for eval runs
            data["trace"] = trace

        """
        Here you could optionally validate more strictly (type, kinds, etc.)
        """
        return data

    except Exception as exc:
        """
        If parsing fails, log and provide a fallback
        """
        logger.warning(
            "⚠️ LLM reply was not valid JSON, sending fallback. Error=%s, raw=%r",
            exc,
            reply_text.strip(),
        )

        """
        Fallback: we pack the original text from the LLM into a simple text block
        """
        fallback = {
            "type": "answer",
            "request_id": request_id,
            "blocks": [
                {
                    "kind": "text",
                    "text": str(reply_text.strip()),
                }
            ],
        }

        if TRACE_ENABLED:
            TRACE_STORE[request_id] = trace
            TRACE_CREATED[request_id] = time.time()
            fallback["trace"] = trace
        return fallback


@app.get("/trace/{request_id}")
def get_trace(request_id: str):
    """
    Route to retrieve the trace for a given request ID.
    
    :param request_id: Description
    :type request_id: str
    """
    trace_cleanup()

    if not TRACE_ENABLED:
        raise HTTPException(status_code=404, detail="Tracing disabled")

    tr = TRACE_STORE.get(request_id)
    if tr is None:
        raise HTTPException(status_code=404, detail="Trace not found")

    return {"request_id": request_id, "trace": tr}


def trace_cleanup() -> None:
    """
    Cleanup old traces from TRACE_STORE based on TTL.

    :return: None
    """
    if not TRACE_ENABLED:
        return
    now = time.time()
    to_delete = [rid for rid, created in TRACE_CREATED.items() if now - created > TRACE_TTL_SECONDS]
    for rid in to_delete:
        TRACE_CREATED.pop(rid, None)
        TRACE_STORE.pop(rid, None)


def truncate_log(
        messages: list[ChatCompletionMessageParam], 
        role: str = "system", 
        field: str = "content"
        ) -> list[ChatCompletionMessageParam]:
    """
    Helper function to truncate long log messages for better readability.

    :param messages: List of chat messages
    :param role: Role of messages to truncate
    :param field: Field in the message to truncate
    :return: List of messages with truncated content
    :rtype: List[ChatCompletionMessageParam]
    """
    copy_msgs = [dict(m) for m in messages]
    for m in copy_msgs:
        if m.get("role") == role:
            m[field] = (m.get(field, "")[:25] + "...") # type: ignore
    return copy_msgs # type: ignore


async def chat_with_tools(
        messages: list[ChatCompletionMessageParam], 
        model: str, tools: list[ChatCompletionToolUnionParam], 
        format_prompt: str, 
        trace_add: Optional[Callable[[str, dict], None]] = None,
        request_id: Optional[str] = None,
        ) -> str:
    """
    Ask the model; if it requests tools, execute via MCP and loop until final text.
    
    :param messages: Messages to send to the model
    :type messages: list[ChatCompletionMessageParam]
    :param model: Model name to use
    :type model: str
    :param tools: Tool definitions for the model
    :type tools: list[ChatCompletionToolUnionParam]
    :param format_prompt: Prompt to use for final formatting
    :type format_prompt: str
    :param trace_add: Optional function to add trace events
    :type trace_add: Optional[Callable[[str, dict], None]]
    :param request_id: Optional request ID for tracing
    :type request_id: Optional[str]
    :return: Answer text from the model
    :rtype: str
    """

    def _trace(kind: str, data: dict) -> None:
        if trace_add:
            trace_add(kind, data)

    phase = Phase.TOOL

    for _ in range(8):  # Limit to max 8 iterations
        logger.debug("🔄 Chat loop phase: %s", phase.name)
        logger.info("🧠 Sending to Ollama model %s", model)
        logger.debug("🧠 Sending to Ollama model %s:\n%s", model, json.dumps(messages, ensure_ascii=False, indent=2))

        t0 = time.perf_counter()
        _trace("ollama_request", {
            "request_id": request_id,
            "phase": "tool",
            "model": model,
            "messages_count": len(messages),
            "has_tools": True,
            "tool_choice": "auto",
            "temperature": 0.2,
        })

        resp = client.chat.completions.create(
            model=model,
            messages=messages,
            tools=tools,
            tool_choice="auto",
            temperature=0.2,
        )

        dt_ms = int((time.perf_counter() - t0) * 1000)
        _trace("ollama_response", {
            "request_id": request_id,
            "phase": "tool",
            "latency_ms": dt_ms,
            "finish_reason": resp.choices[0].finish_reason,
            "usage": getattr(resp, "usage", None),
            # keep it compact; you can store full model_dump if you want
            "message": resp.choices[0].message.model_dump() if hasattr(resp.choices[0].message, "model_dump") else {},
        })

        msg = resp.choices[0].message
        logger.info("💬 Received reply from Ollama!")
        logger.debug("💬 Full reply from Ollama:\n%s", json.dumps(resp.model_dump(), ensure_ascii=False, indent=2))

        if msg.tool_calls:
            for tc in msg.tool_calls:
                if tc.type != "function":
                    logger.warning("⚠️ Unexpected tool_call type: %s", tc.type)
                    continue
                name = tc.function.name
                try:
                    logger.info("🔧 Model requested tool: %s", name)
                    args = json.loads(tc.function.arguments or "{}")
                    logger.debug("🔧 Model requested tool: %s:\n%s", name, json.dumps(args, ensure_ascii=False, indent=2))
                except Exception as exc:
                    logger.error("❌ Failed to parse tool_call arguments: %s", exc)
                    args = {}
                    continue

                _trace("tool_call", {
                    "request_id": request_id,
                    "tool": name,
                    "args": args,
                })

                result = await call_mcp_tool(name, args)

                _trace("tool_result", {
                    "request_id": request_id,
                    "tool": name,
                    "result": result,
                })

                logger.info("📦 Tool returned result")
                logger.debug("📦 Tool result from MCP:\n%s", json.dumps(result, ensure_ascii=False, indent=2))

                messages.append(
                    cast(ChatCompletionMessageParam, {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "name": name,
                        "content": json.dumps(result, ensure_ascii=False),
                    })
                )

        phase = Phase.FINAL
        break

    if phase != Phase.FINAL:
        raise RuntimeError("Too many tool iterations; possible loop")
    
    final_messages: list[ChatCompletionMessageParam] = [
        {"role": "system", "content": format_prompt},
        *messages[1:],
    ]

    logger.debug("🔄 Chat loop phase: %s", phase.name)
    logger.info("🧠 Sending final formatting request to Ollama model %s", model)
    logger.debug("🧠 Sending final formatting request to Ollama model %s:\n%s", model, json.dumps(final_messages, ensure_ascii=False, indent=2))

    t0 = time.perf_counter()
    _trace("ollama_request", {
        "request_id": request_id,
        "phase": "final",
        "model": model,
        "messages_count": len(final_messages),
        "has_tools": False,
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    })

    final_resp = client.chat.completions.create(
        model=model,
        messages=final_messages,
        temperature=0.2,
        response_format={
            "type": "json_object"
        }
    )

    dt_ms = int((time.perf_counter() - t0) * 1000)
    _trace("ollama_response", {
        "request_id": request_id,
        "phase": "final",
        "latency_ms": dt_ms,
        "finish_reason": final_resp.choices[0].finish_reason,
        "usage": getattr(final_resp, "usage", None),
        "message": final_resp.choices[0].message.model_dump() if hasattr(final_resp.choices[0].message, "model_dump") else {},
    })

    final_msg = final_resp.choices[0].message
    logger.info("💬 Received final reply from Ollama!")
    logger.debug("💬 Full final reply from Ollama:\n%s", json.dumps(final_resp.model_dump(), ensure_ascii=False, indent=2))

    return final_msg.content or ""


async def call_mcp_tool(
        tool_name: str, 
        args: Dict[str, Any]
        ) -> Dict[str, Any]:
    """
    Calls an MCP tool and processes the result into a dict.
    
    :param tool_name: Description
    :type tool_name: str
    :param args: Description
    :type args: Dict[str, Any]
    :return: Description
    :rtype: Dict[str, Any]
    """
    result = await mcp_cache.call_tool(tool_name, args)

    def _wrap(v: Any) -> Dict[str, Any]:
        return v if isinstance(v, dict) else {"items": v}

    def _try_parse_json(s: str) -> Optional[Any]:
        s = s.strip()
        if not s:
            return None
        if (s.startswith("{") and s.endswith("}")) or (s.startswith("[") and s.endswith("]")):
            try:
                return json.loads(s)
            except Exception:
                return None
        return None

    # 1) Some MCP setups already return dicts
    if isinstance(result, dict):
        # common legacy shape: {"text": "<json string>"}
        txt = result.get("text")
        if isinstance(txt, str):
            parsed = _try_parse_json(txt)
            if parsed is not None:
                return _wrap(parsed)
        return result

    # 2) Official MCP result: blocks in result.content
    content = getattr(result, "content", None)
    if content:
        # Prefer json blocks
        for block in content:
            if getattr(block, "type", "") == "json":
                raw = getattr(block, "text", None) or getattr(block, "data", None)

                # raw can be a JSON string
                if isinstance(raw, str):
                    parsed = _try_parse_json(raw)
                    if parsed is not None:
                        return _wrap(parsed)

                # or already a python object
                if raw is not None:
                    return _wrap(raw)

        # Fallback: concatenate text blocks
        texts = [getattr(b, "text", "") for b in content if getattr(b, "type", "") == "text"]
        joined = "\n".join([t for t in texts if isinstance(t, str) and t.strip()]).strip()

        parsed = _try_parse_json(joined) if joined else None
        if parsed is not None:
            return _wrap(parsed)

        return {"text": joined}

    # 3) Last resort
    return {"text": str(result)}


def b64_decode(s: str) -> bytes:
    """
    Helper function to decode base64 URL-safe strings.
    
    :param s: Base64 URL-safe encoded string
    :type s: str
    :return: Decoded bytes
    :rtype: bytes
    """
    s += "="* (-len(s) % 4)
    return base64.urlsafe_b64decode(s.encode("utf-8"))


def verify(token: str, secret: str) -> Optional[Dict[str, Any]]:
    """
    Helper function to verify a JWT-like token using HMAC SHA256.
    
    :param token: JWT-like token string
    :type token: str
    :param secret: Secret key for HMAC verification
    :type secret: str
    :return: Decoded payload if verification succeeds, otherwise None
    :rtype: Dict[str, Any] | None
    """
    if not token or not secret:
        return None

    parts = token.split(".")
    if len(parts) != 3:
        return None

    h_b64, p_b64, sig_b64 = parts
    signing_input = f"{h_b64}.{p_b64}".encode("utf-8")

    expected = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    try:
        got = b64_decode(sig_b64)
    except Exception:
        return None

    if not hmac.compare_digest(expected, got):
        return None

    try:
        payload = json.loads(b64_decode(p_b64).decode("utf-8"))
    except Exception:
        return None

    exp = payload.get("exp")
    if not isinstance(exp, int) or int(time.time()) > exp:
        return None

    return payload