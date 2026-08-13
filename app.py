"""FastAPI gateway for the public AICA storefront chat.

The module is the boundary between the browser/Shopware chat contract and
two local services: Ollama's OpenAI-compatible API and the Shopware MCP
server.  It deliberately exposes only public catalogue tools.  The opaque
``client`` object, including any Shopware context token, is accepted for
contract compatibility but is not an authorization input.

Configuration and the long-lived clients are initialized when Uvicorn
imports ``app:app``.  Model output is untrusted at this boundary and is
reduced to the storefront block shapes by :func:`normalize_chat_reply`.
"""

import os, json, logging, asyncio, time
from typing import Any, Callable, Dict, List, Optional, cast
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from openai import OpenAI
from enum import Enum, auto

from mcp.client.streamable_http import streamablehttp_client
from mcp.client.session import ClientSession

from openai.types.chat import ChatCompletionMessageParam
from openai.types.chat import ChatCompletionSystemMessageParam
from openai.types.chat import ChatCompletionUserMessageParam
from openai.types.chat import ChatCompletionAssistantMessageParam
from openai.types.chat import ChatCompletionToolUnionParam

from backend.app.services.domain_knowledge_loader import JsonDomainTermsProvider
from backend.app.services.domain_knowledge_prompt import build_domain_knowledge_prompt_block
from backend.app.services.domain_knowledge_resolver import DomainKnowledgeResolver

load_dotenv()

CHAT_HOST = os.getenv("CHAT_HOST", "0.0.0.0")
CHAT_PORT = int(os.getenv("CHAT_PORT", "8002"))
CHAT_LOGGING_LEVEL = os.getenv("CHAT_LOGGING_LEVEL", "info").upper()
CHAT_DRY_RUN = os.getenv("CHAT_DRY_RUN", "0") in ("1","true","TRUE","yes","YES")
CORS_ORIGIN_REGEX = os.getenv("CORS_ORIGIN_REGEX", "").strip()

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1").rstrip("/")
OLLAMA_API_KEY = os.getenv("OLLAMA_API_KEY", "ollama")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1:8b")
OLLAMA_NUM_CTX = os.getenv("OLLAMA_NUM_CTX", "").strip()
OLLAMA_NUM_CTX_BY_MODEL = os.getenv("OLLAMA_NUM_CTX_BY_MODEL", "").strip()
OLLAMA_MODEL_ALIAS_BY_MODEL = os.getenv("OLLAMA_MODEL_ALIAS_BY_MODEL", "").strip()

MCP_URL = os.getenv("MCP_URL", "http://localhost:8005/mcp").rstrip("/")
DOMAIN_KNOWLEDGE_ENABLED = os.getenv("DOMAIN_KNOWLEDGE_ENABLED", "1") in ("1", "true", "TRUE", "yes", "YES")
DOMAIN_KNOWLEDGE_PATH = os.getenv("DOMAIN_KNOWLEDGE_PATH", "backend/data/domain_terms.json").strip()
DOMAIN_KNOWLEDGE_MAX_MATCHES_RAW = os.getenv("DOMAIN_KNOWLEDGE_MAX_MATCHES", "4").strip()
DOMAIN_KNOWLEDGE_ENABLE_FUZZY = os.getenv("DOMAIN_KNOWLEDGE_ENABLE_FUZZY", "1") in ("1", "true", "TRUE", "yes", "YES")
DOMAIN_KNOWLEDGE_FUZZY_THRESHOLD_RAW = os.getenv("DOMAIN_KNOWLEDGE_FUZZY_THRESHOLD", "0.93").strip()

TRACE_ENABLED = os.getenv("TRACE_ENABLED", "0") == "1"

TRACE_STORE: dict[str, list[dict]] = {}
TRACE_TTL_SECONDS = 60 * 10
TRACE_CREATED: dict[str, float] = {}

RESPONSE_TYPES = {"answer", "clarification", "error"}
INFO_BOX_STYLES = {"info", "warning", "error"}
FORM_FIELD_TYPES = {"text", "email", "textarea", "tel", "number"}
ALLOWED_HISTORY_ROLES = {"user", "assistant"}

""" Tool prompt for the LLM to behave as Shopware Storefront assistant """
TOOL_PROMPT = """
Du bist ein hilfreicher Assistent im Shopware-Storefront-Chat. Sprich Deutsch.

AUFGABE DIESES PROMPTS:
Du entscheidest, ob ein Tool-Aufruf nötig und erlaubt ist.
Wenn ein Tool-Aufruf nötig ist, gib NUR passende Tool-Calls zurück.
Wenn kein Tool-Aufruf erlaubt oder sinnvoll ist, gib KEINEN Tool-Call zurück.

ENTSCHEIDUNGSREGELN (WICHTIG):

0) Nicht durch Tools beantwortbare Fragen:
   Wenn die Nutzerfrage nicht durch die verfügbaren Tools beantwortbar ist:
   - Rufe KEINE Tools auf.
   - Dazu gehören insbesondere:
     Lieferzeit, Liefergebiet, Versandkosten, Preise, Konditionen, Rabatte,
     Angebote, Staffelpreise, Bestand/Lagerbestand, Chatbot-Nutzungsbedingungen,
     Datenschutz, AGB, rechtliche Hinweise, Öffnungszeiten, individuelle Beratung,
     rechtliche oder organisatorische Fragen.
   - Gib KEINEN Tool-Call zurück.
   - Antworte kurz, dass dafür Kontakt, ein Angebot oder weitere Klärung nötig ist.

1) Preise, Konditionen, Angebote, Versand und Bestand:
   Wenn die Nutzerfrage Preise, Konditionen, Rabatte, Staffelpreise, Versandkosten,
   Angebote, Nettopreise, MwSt., Lieferzeit oder Bestand/Lagerbestand betrifft:
   - Rufe KEINE Tools auf.
   - Auch dann nicht, wenn eine Artikelnummer, Produkt-ID oder ein Produktname genannt wird.
   - Die Produktdaten-Tools liefern keine Preise, keine Konditionen, keine Lieferzeiten,
     keine Versandkosten und keinen verlässlichen Bestand.
   - Antworte kurz, dass dafür eine Kontaktaufnahme bzw. ein individuelles Angebot nötig ist.

2) Produkt-, Kategorie- und Produktdetailfragen:
   Wenn die Nutzerfrage Produkte, Kategorien oder verfügbare Produktdetails betrifft:
   - Du darfst Tools nutzen, aber nur für tatsächlich verfügbare Shopdaten:
     Produktsuche, Produktdetails per UUID, Produktdetails per Artikelnummer, Kategorien.
   - Nutze KEINE Produktsuche für allgemeine Fragen wie Chatbot-Bedingungen,
     Datenschutz, Lieferzeit, Liefergebiet, Versandkosten, Preise oder Konditionen.
   - Erfinde niemals Produktdaten.
   - Gib bei einem Tool-Aufruf KEINE finale Antwort aus, sondern nur den Tool-Call.

3) Tool-Auswahl:
   - search_products_public:
     Nutze dieses Tool für freie Produktsuchen, z.B. Produktname, Warengruppe,
     Synonym, Zutat, Gebäckart, Material, Größe oder Packungsangabe.
   - get_product_by_id_public:
     Nutze dieses Tool nur, wenn der Nutzer eine echte Produkt-UUID nennt.
   - get_product_by_number_public:
     Nutze dieses Tool nur, wenn der Nutzer eine konkrete Artikelnummer/productNumber nennt.
   - list_categories:
     Nutze dieses Tool, wenn der Nutzer nach Kategorien, Warengruppen,
     Obergruppen, Unterkategorien oder der Shop-Struktur fragt.

4) Synonyme, Schreibweisen und Domain-Begriffe:
   - Nutzer verwenden oft Synonyme, regionale Begriffe, Singular/Plural,
     Tippfehler oder unscharfe Bezeichnungen.
   - Passe den Suchbegriff für search_products_public sinnvoll an.
   - Beispiele:
     "Marille" kann auch "Aprikose" bedeuten.
     "Krapfen" kann im Bäckereikontext mit "Berliner" zusammenhängen.
     "TK" kann "Tiefkühl" bedeuten.
   - Erweitere Suchbegriffe aber nur vorsichtig. Erfinde keine Produktdaten.

5) Bezug auf vorherige Nachrichten:
   - Der Nutzer kann sich auf Produkte oder Kategorien beziehen, die bereits in der History erwähnt wurden.
   - Nutze die History, wenn dort eindeutige Produktdaten oder Kategorieinformationen vorhanden sind.
   - Wenn die History ausreicht, ist kein neuer Tool-Call nötig.
   - Wenn die Referenz unklar ist, stelle lieber eine Rückfrage statt zu raten.

6) Mengenfragen:
   - Wenn der Nutzer eine Gesamtmenge nennt, z.B. "Ich brauche 100 kg Zucker":
     Suche nach dem Produktbegriff, nicht nach der Gesamtmenge als Produkt.
   - Nutze danach die tatsächlich gefundenen Packungsgrößen, um knapp zu erklären,
     welche Verkaufsmengen rechnerisch zur gewünschten Menge passen könnten.
   - Gib keine riesige Produktliste aus.
   - Wenn der Nutzer keine konkrete Trefferanzahl nennt, reichen maximal 5 passende Produkte.
   - Erfinde keine Packungsgrößen oder Umrechnungen auf Basis nicht gelieferter Produkte.

7) Gewünschte Trefferanzahl:
   - Wenn der Nutzer eine Anzahl nennt, z.B. "nenne mir 10", "bis zu 10 Treffer",
     "zeige 5 Produkte", dann ist diese Anzahl IMMER eine OBERGRENZE, keine Pflichtanzahl.
   - Setze das Tool-Limit passend zur gewünschten Obergrenze.
   - Ein Tool kann auch bei korrekten Argumenten weniger oder gar keine Produkte/Kategorien liefern.
   - In solchen Fällen dürfen später NUR die tatsächlich gelieferten Ergebnisse verwendet werden.
   - Erfinde keine Objekte, IDs, Artikelnummern, Kategorien oder Namen, um die gewünschte Anzahl zu erreichen.

8) "Alle Produkte", vollständige Listen und sehr große Anfragen:
   - Wenn der Nutzer "alle Produkte", "alle Preise", "alle Artikel im Shop",
     eine vollständige Shopliste oder eine vollständige Preisliste verlangt:
     Rufe KEINE Tools auf.
   - Erkläre kurz, dass der Chat keine vollständige Shop- oder Preisliste ausgeben kann.
   - Bitte um Eingrenzung oder verweise auf Kontakt.
   - Wenn der Nutzer "alle passenden X" fragt, darfst du nach X suchen,
     aber nur mit einem begrenzten Limit. Gib nur eine Auswahl bzw. die tatsächlich gelieferten Treffer zurück.

VERFÜGBARE TOOLS:
- search_products_public: Suche Produkte per Freitext (ohne Preise).
- get_product_by_id_public: Produkt per UUID (ohne Preise).
- get_product_by_number_public: Produkt(e) per exakter productNumber/Artikelnummer (ohne Preise).
- list_categories: Kategorien auflisten.

TECHNISCHE REGELN:
- Wenn du Tools aufrufst, setze finish_reason="tool_calls" und gib passende Argumente an.
- Bei einem Tool-Aufruf soll content leer sein oder höchstens minimal bleiben.
- Erzeuge in der Tool-Phase keine finale JSON-Antwort.
- Wenn kein Tool erlaubt oder sinnvoll ist, gib keine tool_calls zurück.

ALLGEMEIN:
- Sprich Deutsch.
- Antworte klar, knapp und freundlich.
- Erfinde niemals Produkt-, Kategorie-, Preis-, Bestands-, Liefer- oder Rechtsinformationen.
"""

""" Format prompt for public requests to the LLM to format the final answer as JSON"""
FORMAT_PROMPT_PUBLIC = """
Gib jetzt die finale Antwort als GENAU EIN JSON-OBJEKT aus.
Gib KEINEN Text außerhalb dieses JSON-Objekts aus.
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
          "productNumber": "string|null",
          "purchaseUnit": "string|null",
          "unitShortCode": "string|null"
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

REGELN ZUM JSON-SCHEMA:
- "type" beschreibt den Charakter der Antwort:
  - "answer": normale Antwort
  - "clarification": Rückfrage, wenn Informationen fehlen oder die Anfrage unklar ist
  - "error": echter Fehler, z.B. Tool nicht verfügbar oder Tool-Ergebnis nicht nutzbar
- "blocks" ist IMMER ein Array.
- Jeder Block hat ein "kind".
- Verwende IMMER mindestens einen "text"-Block.
- Der "text"-Block erklärt kurz die Antwort, enthält aber KEINE ausführlichen Produktdetails.
- Wenn Produkte empfohlen, genannt oder strukturiert angezeigt werden, MUSS zusätzlich ein "product_list"-Block verwendet werden.
- Produktdetails wie id, name, productNumber, purchaseUnit oder unitShortCode gehören in "product_list", nicht in den normalen Text.
- Gib nur Felder aus, die im Schema vorgesehen sind.
- Gib im PUBLIC-Modus NIEMALS ein "price"-Feld aus.

PRODUKTLISTEN UND TOOL-ERGEBNISSE (SEHR WICHTIG):
- Verwende für "product_list" ausschließlich Einträge, die aus Tool-Ergebnissen oder eindeutig aus der Chat-History stammen.
- Erfinde NIEMALS Produkte, Produkt-IDs, Artikelnummern, Namen, Kategorien, Kategorie-IDs, Einheiten oder Packungsgrößen.
- Erfinde NIEMALS zusätzliche Einträge, um eine vom Nutzer gewünschte Anzahl zu erreichen.
- Eine vom Nutzer gewünschte Trefferanzahl ist IMMER eine OBERGRENZE, keine Pflichtanzahl.
  Beispiel:
  Wenn der Nutzer "bis zu 10 Treffer" fragt, das Tool aber nur 5 echte Treffer liefert,
  gib NUR diese 5 Treffer aus.
- Gib NIEMALS mehr Einträge in "product_list" aus, als im relevanten Tool-Ergebnis oder in der eindeutigen History vorhanden sind.
- Wenn das Tool keine Treffer liefert, gib keine product_list mit erfundenen Beispielen aus.
  Nutze stattdessen einen "text"-Block und optional eine "info_box".
- Wenn das Tool weniger Treffer liefert als gewünscht, erkläre kurz, dass nur diese Treffer gefunden wurden.
- Gib keine Platzhalter aus.
  Verboten sind insbesondere:
  "PRO-12345", "ARTIKEL-UUID", "ARTIKEL-NR.", "Artikelname",
  "UUID", "Name", "Nr.", "Beispielprodukt" oder ähnliche Schema-/Beispielwerte.
- Wenn ein optionales Feld nicht bekannt ist, setze es auf null.
- Falls bei Produktdetails die Einheit fehlt, darfst du sie NUR aus dem Produktnamen ableiten,
  wenn sie dort eindeutig steht, z.B. "25 kg", "10 kg", "1 Ltr.", "100 Stk.".
- Wenn keine eindeutige Einheit im Namen steht, nutze null.

KATEGORIEN:
- Das Frontend unterstützt aktuell keinen eigenen "category_list"-Block.
- Wenn Kategorien strukturiert angezeigt werden sollen, dürfen sie daher im vorhandenen
  "product_list"-Block ausgegeben werden.
- In diesem Fall ist "product_list" als allgemeine Ergebnisliste zu verstehen.
- Verwende für Kategorien ausschließlich Werte aus dem Tool-Ergebnis von list_categories
  oder eindeutig aus der Chat-History.
- Für Kategorieeinträge gilt:
  - "id": Kategorie-ID aus dem Tool-Ergebnis, falls vorhanden
  - "name": Kategoriename aus dem Tool-Ergebnis
  - "productNumber": null
  - "purchaseUnit": null
  - "unitShortCode": null
- Erfinde keine Kategorie-IDs oder Kategorienamen.
- Gib Kategorie-IDs nicht im normalen Text aus.
- Wenn der Nutzer nur wissen will, ob eine Kategorie existiert, reicht ein kurzer Text
  und optional eine kleine product_list mit den passenden Kategorien.

ANTWORTEN OHNE NEUEN TOOL-AUFRUF:
- Falls du ohne neuen Tool-Aufruf eine product_list ausgibst,
  darfst du ausschließlich Daten verwenden, die bereits exakt in der Chat-History
  als Tool-Ergebnis oder vorherige product_list vorhanden sind.
- Wenn keine vollständigen Produkt- oder Kategoriedaten in der History vorhanden sind,
  gib keine product_list aus.
- Rekonstruiere keine IDs, Artikelnummern oder Namen aus dem Gedächtnis.

PREISE, KONDITIONEN, ANGEBOTE, VERSAND, LIEFERZEIT UND BESTAND:
- Keine Preise, Nettopreise, Bruttopreise, MwSt.-Beträge, Rabatte, Staffelpreise,
  Konditionen, Versandkosten oder Angebote nennen oder andeuten.
- Wenn nach Preisen, Konditionen, Angeboten, Rabatten, Staffelpreisen, Versandkosten,
  Lieferzeit, Liefergebiet oder Bestand gefragt wird:
  - Gib einen kurzen "text"-Block aus.
  - Erkläre, dass diese Informationen individuell geklärt werden müssen.
  - Gib in der Regel zusätzlich einen "formular"-Block aus.
  - Gib KEINE product_list aus, außer es wurden bereits eindeutig Produktdaten benötigt
    und diese stammen aus einem Tool-Ergebnis oder der History.
- Auch wenn eine Artikelnummer genannt wird, dürfen keine Preise, Bestände oder Lieferzeiten erfunden werden.

"ALLE PRODUKTE" UND GROSSE LISTEN:
- Wenn der Nutzer "alle Produkte", "alle Preise", eine vollständige Produktliste
  oder eine vollständige Preisliste verlangt:
  - Gib keine große product_list aus.
  - Erkläre kurz, dass der Chat keine vollständige Shop- oder Preisliste ausgeben kann.
  - Bitte um Eingrenzung oder biete Kontaktaufnahme an.
- Wenn der Nutzer "alle passenden X" fragt und ein Tool-Ergebnis vorliegt,
  zeige nur die tatsächlich gelieferten, begrenzten Treffer und formuliere sie als Auswahl.

KONTAKTFORMULAR (PUBLIC):
- Das Formular wird ausgegeben, wenn:
  - der Nutzer explizit Kontakt aufnehmen möchte,
  - Preise, Konditionen, Angebote, Versand, Lieferzeit, Liefergebiet oder Bestand gefragt sind,
  - die Anfrage nicht ausreichend mit den verfügbaren Tool-Daten beantwortet werden kann,
  - eine individuelle Klärung nötig ist.
- Das Formular wird NICHT verwendet, um fehlende Produktdaten zu erfinden.
- Im "text"-Block kurz erklären, warum Kontakt nötig ist.
- "reason": kurze Begründung, z.B. "Preise und Konditionen sind kundenabhängig".
- Felder minimal:
  name, email, message
- Sinnvolle zusätzliche Felder:
  phone, company, productRef, quantity, deliveryZip
- endpoint: "/paul-ai-chat/contact"
- method: "POST"

INFO_BOX:
- Nutze "info_box" für Hinweise, leere Ergebnisse, Warnungen oder Einschränkungen.
- Bei leeren Produktergebnissen:
  - text-Block: kurze Erklärung
  - optional info_box mit style "info" oder "warning"
  - keine erfundene product_list

SPRACHE & STIL:
- Schreibe Deutsch.
- Schreibe klar, knapp und freundlich.
- Wenige Emojis sind erlaubt, aber nicht erforderlich.
- Markdown innerhalb von "text" und "info_box.text" ist erlaubt.
- Keine langen Produktauflistungen im normalen Text.

FEHLERFALL:
- Wenn ein Tool nicht verfügbar ist oder ein Tool-Ergebnis nicht verarbeitet werden kann:
  - "type": "error"
  - mindestens ein "text"-Block mit kurzer Erklärung
  - optional eine "info_box" mit style "error"
- Wenn du unsicher bist, liefere trotzdem syntaktisch gültiges JSON
  und erkläre die Unsicherheit knapp im "text"-Block.

WICHTIG (PUBLIC):
- Keine Preise/Konditionen nennen oder andeuten.
- Keine Produkt-, Kategorie- oder ID-Daten erfinden.
- Produktdetails nie im normalen Text-Block ausgeben.
- Verwende nur echte Tool-/History-Daten.
- Die gewünschte Trefferanzahl ist eine Obergrenze, keine Pflicht.
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

logger = logging.getLogger("chat-backend")
logger.setLevel(getattr(logging, CHAT_LOGGING_LEVEL, logging.INFO))
logger.propagate = False

if not any(isinstance(h, logging.StreamHandler) for h in logger.handlers):
    handler = logging.StreamHandler()
    formatter = logging.Formatter("%(levelname)s %(name)s: %(message)s")
    handler.setFormatter(formatter)
    #handler.addFilter(
    #    lambda record: record.levelno != logging.INFO
    #)
    logger.addHandler(handler)


def parse_optional_positive_int(value: str, *, var_name: str) -> Optional[int]:
    """Return a positive integer or ``None`` for an empty/invalid env value.

    Invalid values are intentionally non-fatal: startup logs a warning and
    lets the caller use its documented fallback.
    """
    if not value:
        return None
    try:
        num = int(value)
        if num <= 0:
            raise ValueError("must be > 0")
        return num
    except Exception:
        logger.warning("⚠️ Ignoring invalid %s=%r (must be a positive integer)", var_name, value)
        return None


def parse_probability(value: str, *, var_name: str, default: float) -> float:
    """Parse an inclusive ``[0, 1]`` probability, falling back on bad input."""
    if not value:
        return default
    try:
        prob = float(value)
    except Exception:
        logger.warning("⚠️ Ignoring invalid %s=%r (must be a float in [0,1])", var_name, value)
        return default

    if 0.0 <= prob <= 1.0:
        return prob

    logger.warning("⚠️ Ignoring invalid %s=%r (must be in [0,1])", var_name, value)
    return default


def parse_num_ctx_by_model(raw: str) -> Dict[str, int]:
    """Parse comma-separated ``model=num_ctx`` overrides.

    Malformed entries are ignored individually, so one bad mapping does not
    prevent the service from starting.  A repeated model key keeps its last
    valid value.
    """
    out: Dict[str, int] = {}
    if not raw:
        return out

    for part in raw.split(","):
        entry = part.strip()
        if not entry:
            continue
        if "=" not in entry:
            logger.warning("⚠️ Ignoring invalid OLLAMA_NUM_CTX_BY_MODEL entry %r (expected model=num_ctx)", entry)
            continue

        model, num_ctx_raw = entry.split("=", 1)
        model = model.strip()
        num_ctx_raw = num_ctx_raw.strip()

        if not model:
            logger.warning("⚠️ Ignoring OLLAMA_NUM_CTX_BY_MODEL entry with empty model: %r", entry)
            continue

        num_ctx = parse_optional_positive_int(num_ctx_raw, var_name="OLLAMA_NUM_CTX_BY_MODEL")
        if num_ctx is None:
            logger.warning("⚠️ Ignoring invalid num_ctx for model %r in OLLAMA_NUM_CTX_BY_MODEL", model)
            continue

        out[model] = num_ctx

    return out


def parse_model_alias_by_model(raw: str) -> Dict[str, str]:
    """Parse comma-separated ``requested-model=runtime-alias`` mappings.

    Aliases let operators route a storefront model name to an Ollama model
    created with a baked-in context length.
    """
    out: Dict[str, str] = {}
    if not raw:
        return out

    for part in raw.split(","):
        entry = part.strip()
        if not entry:
            continue
        if "=" not in entry:
            logger.warning("⚠️ Ignoring invalid OLLAMA_MODEL_ALIAS_BY_MODEL entry %r (expected model=alias)", entry)
            continue

        model, alias = entry.split("=", 1)
        model = model.strip()
        alias = alias.strip()
        if not model or not alias:
            logger.warning("⚠️ Ignoring invalid OLLAMA_MODEL_ALIAS_BY_MODEL entry %r (model/alias empty)", entry)
            continue
        out[model] = alias

    return out


def normalize_model_name(model: str) -> str:
    """Strip only Ollama's standard library registry prefix for map lookup."""
    m = (model or "").strip()
    if m.startswith("registry.ollama.ai/library/"):
        return m[len("registry.ollama.ai/library/"):]
    return m


DEFAULT_NUM_CTX = parse_optional_positive_int(OLLAMA_NUM_CTX, var_name="OLLAMA_NUM_CTX")
MODEL_NUM_CTX_OVERRIDES = parse_num_ctx_by_model(OLLAMA_NUM_CTX_BY_MODEL)
MODEL_ALIAS_OVERRIDES = parse_model_alias_by_model(OLLAMA_MODEL_ALIAS_BY_MODEL)
DOMAIN_KNOWLEDGE_MAX_MATCHES = parse_optional_positive_int(
    DOMAIN_KNOWLEDGE_MAX_MATCHES_RAW,
    var_name="DOMAIN_KNOWLEDGE_MAX_MATCHES",
) or 4
DOMAIN_KNOWLEDGE_FUZZY_THRESHOLD = parse_probability(
    DOMAIN_KNOWLEDGE_FUZZY_THRESHOLD_RAW,
    var_name="DOMAIN_KNOWLEDGE_FUZZY_THRESHOLD",
    default=0.93,
)


def resolve_num_ctx(model: str) -> Optional[int]:
    """Resolve context length by exact key, normalized key, then global default."""
    normalized = normalize_model_name(model)
    if model in MODEL_NUM_CTX_OVERRIDES:
        return MODEL_NUM_CTX_OVERRIDES[model]
    if normalized in MODEL_NUM_CTX_OVERRIDES:
        return MODEL_NUM_CTX_OVERRIDES[normalized]
    return DEFAULT_NUM_CTX


def resolve_runtime_model(model: str) -> str:
    """Resolve an exact or normalized alias, otherwise preserve the input."""
    normalized = normalize_model_name(model)
    if model in MODEL_ALIAS_OVERRIDES:
        return MODEL_ALIAS_OVERRIDES[model]
    if normalized in MODEL_ALIAS_OVERRIDES:
        return MODEL_ALIAS_OVERRIDES[normalized]
    return model


def resolve_local_path(path_value: str) -> Path:
    """Resolve relative paths against the directory containing ``app.py``.

    Under Compose that directory is ``/app`` (a bind mount of the backend
    repository), not the similarly named path on the WSL host.
    """
    path = Path(path_value)
    if path.is_absolute():
        return path
    return Path(__file__).resolve().parent / path


def build_domain_knowledge_resolver() -> Optional[DomainKnowledgeResolver]:
    """Build and eagerly validate the optional domain resolver.

    A startup load failure is logged and degrades to no domain injection
    rather than preventing the chat API from importing.  Once initialized,
    the resolver checks the JSON source version on every request.
    """
    if not DOMAIN_KNOWLEDGE_ENABLED:
        logger.info("🧩 Domain knowledge resolver is disabled via DOMAIN_KNOWLEDGE_ENABLED=0")
        return None

    source_path = resolve_local_path(DOMAIN_KNOWLEDGE_PATH)
    provider = JsonDomainTermsProvider(source_path)
    resolver = DomainKnowledgeResolver(
        provider,
        enable_fuzzy=DOMAIN_KNOWLEDGE_ENABLE_FUZZY,
        fuzzy_threshold=DOMAIN_KNOWLEDGE_FUZZY_THRESHOLD,
        auto_reload=True,
    )

    try:
        resolver.reload(force=True)
        logger.info(
            "🧩 Domain knowledge resolver enabled: path=%s max_matches=%s fuzzy=%s threshold=%s",
            source_path,
            DOMAIN_KNOWLEDGE_MAX_MATCHES,
            DOMAIN_KNOWLEDGE_ENABLE_FUZZY,
            DOMAIN_KNOWLEDGE_FUZZY_THRESHOLD,
        )
        return resolver
    except Exception:
        logger.exception(
            "⚠️ Domain knowledge resolver failed to initialize from %s; continuing without it",
            source_path,
        )
        return None


def _string_or_none(value: Any) -> Optional[str]:
    """Coerce non-null model values to the storefront's string representation."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)


def _string_or_empty(value: Any) -> str:
    """Coerce a model value to a string, using ``""`` for JSON null."""
    text = _string_or_none(value)
    return text if text is not None else ""


def _bool_value(value: Any) -> bool:
    """Apply the response normalizer's permissive boolean coercion."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def sanitize_history(history: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """Keep only string ``user``/``assistant`` entries in their original order.

    This is a shape and role allow-list, not a size or content sanitizer:
    accepted content is neither stripped nor truncated, and arbitrary keys on
    an accepted item are discarded before messages reach the model.
    """
    sanitized: List[Dict[str, str]] = []
    for item in history:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = item.get("content")
        if role not in ALLOWED_HISTORY_ROLES or not isinstance(content, str):
            continue
        sanitized.append({
            "role": role,
            "content": content,
        })
    return sanitized


def text_response_payload(
    text: str,
    *,
    request_id: str,
    response_type: str = "answer",
    trace: Optional[list[dict]] = None,
) -> Dict[str, Any]:
    """Build the canonical one-text-block fallback response.

    The request ID is always present here.  A trace is embedded only when
    tracing was enabled at process startup and a trace list was supplied.
    """
    payload: Dict[str, Any] = {
        "type": response_type if response_type in RESPONSE_TYPES else "answer",
        "request_id": request_id,
        "blocks": [
            {
                "kind": "text",
                "text": text,
            }
        ],
    }
    if TRACE_ENABLED and trace is not None:
        payload["trace"] = trace
    return payload


def normalize_blocks(raw_blocks: Any) -> List[Dict[str, Any]]:
    """Allow-list and coerce model blocks to the storefront response contract.

    Supported kinds are ``text``, ``info_box``, ``product_list``, and the
    project-specific ``formular`` spelling.  Unknown blocks and unknown keys
    are dropped.  Product values are stringified and currently include a
    ``price`` key even though the public prompt and MCP tools prohibit prices.
    Form ``endpoint`` and ``method`` values are not forwarded by the current
    normalizer.
    """
    if not isinstance(raw_blocks, list):
        return []

    normalized: List[Dict[str, Any]] = []
    for block in raw_blocks:
        if not isinstance(block, dict):
            continue

        kind = block.get("kind")
        if kind == "text":
            text = _string_or_none(block.get("text"))
            if text is None:
                continue
            normalized.append({"kind": "text", "text": text})
            continue

        if kind == "info_box":
            text = _string_or_none(block.get("text"))
            if text is None:
                continue
            normalized.append({
                "kind": "info_box",
                "style": block.get("style") if block.get("style") in INFO_BOX_STYLES else "info",
                "title": _string_or_empty(block.get("title")),
                "text": text,
            })
            continue

        if kind == "product_list":
            products_out: List[Dict[str, str]] = []
            for product in block.get("products") or []:
                if not isinstance(product, dict):
                    continue
                normalized_product: Dict[str, str] = {}
                for key in ("id", "name", "productNumber", "purchaseUnit", "unitShortCode", "price"):
                    value = _string_or_none(product.get(key))
                    if value is not None:
                        normalized_product[key] = value
                if normalized_product:
                    products_out.append(normalized_product)

            normalized.append({
                "kind": "product_list",
                "title": _string_or_empty(block.get("title")),
                "products": products_out,
            })
            continue

        if kind == "formular":
            fields_out: List[Dict[str, Any]] = []
            for field in block.get("fields") or []:
                if not isinstance(field, dict):
                    continue

                field_key = _string_or_none(field.get("key"))
                field_label = _string_or_none(field.get("label"))
                field_type = _string_or_none(field.get("type")) or "text"
                if field_key is None or field_label is None:
                    continue

                fields_out.append({
                    "key": field_key,
                    "label": field_label,
                    "type": field_type if field_type in FORM_FIELD_TYPES else "text",
                    "placeholder": _string_or_none(field.get("placeholder")),
                    "required": _bool_value(field.get("required")),
                    "value": _string_or_none(field.get("value")),
                })

            normalized.append({
                "kind": "formular",
                "title": _string_or_empty(block.get("title")),
                "reason": _string_or_empty(block.get("reason")),
                "submitLabel": _string_or_empty(block.get("submitLabel")),
                "fields": fields_out,
            })

    return normalized


def normalize_chat_reply(reply_text: str, *, request_id: str, trace: Optional[list[dict]] = None) -> Dict[str, Any]:
    """Convert raw model text to the Shopware-facing response envelope.

    Valid JSON objects with supported blocks are reduced through
    :func:`normalize_blocks`.  Legacy ``reply``/``message`` objects and
    non-JSON output become a single text block.  This fallback is about
    transport stability; it does not claim that the upstream content is
    semantically correct.
    """
    raw = (reply_text or "").strip()

    try:
        data = json.loads(raw)
    except Exception:
        logger.warning("⚠️ LLM reply was not valid JSON, sending text fallback. raw=%r", raw)
        return text_response_payload(raw, request_id=request_id, trace=trace)

    if not isinstance(data, dict):
        logger.warning("⚠️ LLM reply JSON was not an object, sending text fallback. raw=%r", raw)
        return text_response_payload(raw, request_id=request_id, trace=trace)

    response_type = data.get("type") if data.get("type") in RESPONSE_TYPES else "answer"

    blocks = normalize_blocks(data.get("blocks"))
    if blocks:
        payload: Dict[str, Any] = {
            "type": response_type,
            "request_id": request_id,
            "blocks": blocks,
        }
        if TRACE_ENABLED and trace is not None:
            payload["trace"] = trace
        return payload

    fallback_text = _string_or_none(data.get("reply")) or _string_or_none(data.get("message"))
    if fallback_text is not None:
        return text_response_payload(
            fallback_text,
            request_id=request_id,
            response_type=response_type,
            trace=trace,
        )

    logger.warning("⚠️ LLM reply JSON had no supported fields, sending text fallback. raw=%r", raw)
    return text_response_payload(raw, request_id=request_id, response_type=response_type, trace=trace)


class ChatIn(BaseModel):
    """Request body accepted by :func:`chat`.

    ``message`` and ``model`` are required strings.  ``history`` and
    ``client`` default to empty containers, and unknown root fields are
    ignored.  ``client`` remains opaque; in particular, ``contextToken`` is
    neither inspected nor trusted by this backend.
    """
    model_config = ConfigDict(extra="ignore")

    message: str
    history: List[Dict[str, Any]] = Field(default_factory=list)
    model: str
    client: Dict[str, Any] = Field(default_factory=dict)

class Phase(Enum):
    """Labels used in logs while moving from selection to final formatting."""
    TOOL = auto()
    FINAL = auto()

class McpSessionCache:
    """Reuse one streamable-HTTP MCP client session for the process lifetime.

    Connection establishment and teardown are locked.  Tool calls themselves
    are not serialized; on failure a caller closes the shared session,
    reconnects, and retries that call exactly once.
    """
    def __init__(self, mcp_url: str):
        self.mcp_url = mcp_url
        self._lock = asyncio.Lock()

        self._transport_cm = None
        self._transport = None  # (read, write, _)
        self._session: Optional[ClientSession] = None
        self._initialized = False

    async def _ensure_connected(self) -> ClientSession:
        """Return the initialized session, creating its transport if needed."""
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
        """Best-effort close the MCP session and transport during retry/shutdown."""
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
        """Call a named MCP tool, reconnecting once after any exception."""
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
    """Close the cached MCP connection when FastAPI shuts down."""
    # startup: optionally warm up the MCP connection
    # await mcp_cache._ensure_connected()
    yield
    # shutdown:
    await mcp_cache.close()

client = OpenAI(base_url=OLLAMA_BASE_URL, api_key=OLLAMA_API_KEY)
mcp_cache = McpSessionCache(MCP_URL)
domain_knowledge_resolver = build_domain_knowledge_resolver()

# CORS
cors_origins = [o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",")]
cors_allow_origin_regex = CORS_ORIGIN_REGEX or (r"https?://.*" if cors_origins == ["*"] else None)
app = FastAPI(title="Shopware Chat Backend (Ollama + MCP)", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[] if cors_origins == ["*"] else cors_origins,
    allow_origin_regex=cors_allow_origin_regex,
    allow_credentials=True,
    allow_methods=["POST", "OPTIONS", "GET"],
    allow_headers=["*"],
)


@app.exception_handler(HTTPException)
async def http_exception_handler(_: Request, exc: HTTPException) -> JSONResponse:
    """Expose application ``HTTPException`` failures as ``{"message": ...}``."""
    detail = exc.detail if isinstance(exc.detail, str) else "Request failed"
    return JSONResponse(status_code=exc.status_code, content={"message": detail})


@app.exception_handler(RequestValidationError)
async def request_validation_exception_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    """Return the proxy-facing validation envelope for malformed chat bodies."""
    return JSONResponse(
        status_code=422,
        content={
            "message": "Invalid request body",
            "details": exc.errors(),
        },
    )


@app.get("/healthz")
def health():
    """Report process liveness and effective non-secret configuration.

    This endpoint does not call Ollama, MCP, or Shopware.  A 200 response
    proves that FastAPI imported successfully, not that a chat can complete.
    """
    return {
        "status": "ok",
        "model": OLLAMA_MODEL,
        "num_ctx_default": DEFAULT_NUM_CTX,
        "num_ctx_by_model": MODEL_NUM_CTX_OVERRIDES,
        "model_alias_by_model": MODEL_ALIAS_OVERRIDES,
        "domain_knowledge_enabled": domain_knowledge_resolver is not None,
        "domain_knowledge_path": str(resolve_local_path(DOMAIN_KNOWLEDGE_PATH)),
        "domain_knowledge_max_matches": DOMAIN_KNOWLEDGE_MAX_MATCHES,
        "domain_knowledge_fuzzy": DOMAIN_KNOWLEDGE_ENABLE_FUZZY,
        "domain_knowledge_fuzzy_threshold": DOMAIN_KNOWLEDGE_FUZZY_THRESHOLD,
    }


@app.post("/chat")
async def chat(in_: ChatIn, request: Request):
    """Orchestrate one public storefront chat request.

    Browser/proxy input becomes model messages, optional domain matches become
    trusted system context, and optional product/category lookups cross the
    MCP boundary.  The final raw model text is always normalized before it is
    returned to Shopware.

    The ``X-Request-Id`` header is trusted as an observability identifier, not
    as authentication.  Unexpected model or MCP failures are wrapped as HTTP
    502 responses.  Traces are stored after both model phases finish but
    before response normalization.
    """

    trace_cleanup()

    request_id = request.headers.get("X-Request-Id") or str(time.time_ns())
    trace: list[dict] = []
    def trace_add(kind: str, data: dict) -> None:
        if TRACE_ENABLED:
            trace.append({
                "ts_ms": int(time.time() * 1000),
                "kind": kind,
                "data": data,
            })

    # The browser/Shopware context token is accepted inside ``client`` only for
    # contract compatibility.  It grants no private or customer-specific tools.
    format_prompt = FORMAT_PROMPT_PUBLIC
    tools = TOOLS_PUBLIC

    if CHAT_DRY_RUN:
        return {
            "type": "answer",
            "blocks": [
                {"kind": "info_box", "style": "info", "title": "Dry-Run", "text": "LLM call skipped (CHAT_DRY_RUN=1)."},
                {"kind": "text", "text": f"Received: {in_.message}"},
                {"kind": "text", "text": "Private tool access is disabled."},
            ],
        }

    logger.info("💬 Received chat request: %s", in_.message)
    logger.debug("💬 Full chat request:\n%s", in_.model_dump_json(ensure_ascii=False, indent=2))

    resolved_domain_matches = []
    domain_knowledge_prompt = ""
    if domain_knowledge_resolver is not None:
        resolved_domain_matches = domain_knowledge_resolver.resolve_message(
            in_.message,
            max_matches=DOMAIN_KNOWLEDGE_MAX_MATCHES,
        )
        if resolved_domain_matches:
            domain_knowledge_prompt = build_domain_knowledge_prompt_block(resolved_domain_matches)
            logger.info("🧩 Domain knowledge matched entries: %s", len(resolved_domain_matches))
            logger.debug(
                "🧩 Domain knowledge matches:\n%s",
                json.dumps([match.to_dict() for match in resolved_domain_matches], ensure_ascii=False, indent=2),
            )
            trace_add(
                "domain_knowledge_matches",
                {
                    "request_id": request_id,
                    "matches": [match.to_dict() for match in resolved_domain_matches],
                },
            )

    # The tool-selection phase sees the fixed policy first, then optional
    # backend-resolved domain context, safe history, and the current message.
    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": TOOL_PROMPT},
    ]
    if domain_knowledge_prompt:
        messages.append({"role": "system", "content": domain_knowledge_prompt})

    # Only user/assistant string entries survive the browser-history boundary.
    for h in sanitize_history(in_.history):
        messages.append(h)

    # The current message is intentionally distinct from replayed history.
    messages.append({"role": "user", "content": in_.message})

    logger.debug("📥 Actual Messages:\n%s", json.dumps(messages, ensure_ascii=False, indent=2))

    # An empty required model string falls back to the configured default.
    # Alias and context lookups use the requested/effective name.
    requested_model = (in_.model or "").strip()
    effective_model = requested_model if requested_model else OLLAMA_MODEL
    runtime_model = resolve_runtime_model(effective_model)
    effective_num_ctx = resolve_num_ctx(effective_model)
    logger.debug(
        "🧠 Effective model=%s runtime_model=%s num_ctx=%s",
        effective_model,
        runtime_model,
        effective_num_ctx if effective_num_ctx is not None else "default",
    )
    if effective_num_ctx is not None and runtime_model == effective_model:
        logger.debug(
            "ℹ️ num_ctx=%s configured for %s; OpenAI-compatible /v1 endpoint may keep model default unless using an alias model with baked PARAMETER num_ctx",
            effective_num_ctx,
            effective_model,
        )

    # ``chat_with_tools`` performs the selection call and then an unconditional
    # second call that formats the final JSON response.
    try:
        reply_text = await chat_with_tools(
            cast(List[ChatCompletionMessageParam], messages),
            model=runtime_model,
            tools=tools,
            format_prompt=format_prompt,
            num_ctx=effective_num_ctx,
            trace_add=trace_add,
            request_id=request_id
        )
        if TRACE_ENABLED:
            TRACE_STORE[request_id] = trace
            TRACE_CREATED[request_id] = time.time()
        return normalize_chat_reply(reply_text, request_id=request_id, trace=trace)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("❌ Chat backend failed for request %s", request_id)
        raise HTTPException(status_code=502, detail=f"Chat backend failed: {exc}") from exc


@app.get("/trace/{request_id}")
def get_trace(request_id: str):
    """Return a model-complete request's current in-memory trace.

    The route has no authentication.  Events can contain generated model
    messages, tool arguments, and Shopware tool results, so production access
    must be constrained outside this service.  Disabled tracing and unknown or
    expired IDs both use HTTP 404 with different messages.
    """
    trace_cleanup()

    if not TRACE_ENABLED:
        raise HTTPException(status_code=404, detail="Tracing disabled")

    tr = TRACE_STORE.get(request_id)
    if tr is None:
        raise HTTPException(status_code=404, detail="Trace not found")

    return {"request_id": request_id, "trace": tr}


def trace_cleanup() -> None:
    """Lazily evict traces older than :data:`TRACE_TTL_SECONDS`.

    Cleanup runs only when ``/chat`` or ``/trace/{request_id}`` is requested;
    there is no background task or persistent trace store.
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
    """Return shallow message copies with selected log content shortened.

    This helper affects debug rendering only; the unmodified ``messages`` list
    is still sent to Ollama.
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
        num_ctx: Optional[int] = None,
        trace_add: Optional[Callable[[str, dict], None]] = None,
        request_id: Optional[str] = None,
        ) -> str:
    """Run tool selection once, execute requested MCP tools, then format JSON.

    A successful invocation makes exactly two synchronous OpenAI-compatible
    calls from this async function.  The first call may request zero or more
    tools; all valid calls in that single response execute sequentially.  The
    second call is unconditional after a successful selection/tool phase and
    receives the formatting prompt, retained conversation messages, and any
    tool results.  The first assistant message itself is not retained, and no
    second tool-selection round occurs.

    ``num_ctx`` is sent in Ollama-specific ``extra_body`` options on both model
    calls.  ``trace_add`` receives compact request/response and tool events but
    is a no-op when tracing is disabled.
    """

    def _trace(kind: str, data: dict) -> None:
        if trace_add:
            trace_add(kind, data)

    phase = Phase.TOOL

    # Although represented as a phase loop, the unconditional break below
    # currently gives the model one tool-selection round.
    for _ in range(8):  # Limit to max 8 iterations
        logger.debug("🔄 Chat loop phase: %s", phase.name)
        logger.info("🧠 Sending to Ollama model %s", model)
        logger.debug("🧠 Sending to Ollama model %s:\n%s", model, json.dumps(truncate_log(messages), ensure_ascii=False, indent=2))

        t0 = time.perf_counter()
        _trace("ollama_request", {
            "request_id": request_id,
            "phase": "tool",
            "model": model,
            "messages_count": len(messages),
            "has_tools": True,
            "tool_choice": "auto",
            "temperature": 0.2,
            "num_ctx": num_ctx,
        })

        request_kwargs: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "temperature": 0.2,
        }
        if num_ctx is not None:
            request_kwargs["extra_body"] = {"options": {"num_ctx": num_ctx}}

        resp = client.chat.completions.create(**request_kwargs)

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

        # Selection-phase text is intentionally not reused.  The final phase
        # regenerates the answer under the structured-output prompt.
        phase = Phase.FINAL
        break

    if phase != Phase.FINAL:
        raise RuntimeError("Too many tool iterations; possible loop")
    
    final_messages: list[ChatCompletionMessageParam] = [
        {"role": "system", "content": format_prompt},
        # Drop TOOL_PROMPT while retaining domain context, sanitized history,
        # the current user message, and any appended MCP tool results.
        *messages[1:],
    ]

    logger.debug("🔄 Chat loop phase: %s", phase.name)
    logger.info("🧠 Sending final formatting request to Ollama model %s", model)
    logger.debug("🧠 Sending final formatting request to Ollama model %s:\n%s", model, json.dumps(truncate_log(final_messages), ensure_ascii=False, indent=2))

    t0 = time.perf_counter()
    _trace("ollama_request", {
        "request_id": request_id,
        "phase": "final",
        "model": model,
        "messages_count": len(final_messages),
        "has_tools": False,
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
        "num_ctx": num_ctx,
    })

    final_request_kwargs: Dict[str, Any] = {
        "model": model,
        "messages": final_messages,
        "temperature": 0.2,
        "response_format": {
            "type": "json_object"
        },
    }
    if num_ctx is not None:
        final_request_kwargs["extra_body"] = {"options": {"num_ctx": num_ctx}}

    final_resp = client.chat.completions.create(**final_request_kwargs)

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
    """Call MCP and normalize SDK/legacy result shapes to a dictionary.

    Native dictionaries pass through (with JSON decoded from a legacy
    ``text`` wrapper when possible).  Official MCP JSON blocks are preferred
    over text blocks; list/scalar JSON values are wrapped as ``{"items": ...}``.
    Non-JSON text remains under ``text`` so the final model phase receives a
    stable JSON-serializable tool payload.
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
