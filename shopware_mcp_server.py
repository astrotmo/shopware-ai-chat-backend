"""Streamable-HTTP MCP server exposing a narrow Shopware catalogue view.

The chat service is this module's MCP client.  Tool implementations authenticate
to Shopware's Admin API with OAuth client credentials, then normalize responses
before returning them to the model.  No price, stock, delivery, customer, or
order field is exposed by the product normalizer.

FastMCP listens on all interfaces at port 8005 and this module adds no transport
authentication of its own.  Deployment networking is therefore part of the
security boundary.
"""

import logging
import os, time, argparse, httpx

from typing import Optional, Dict, Any, List, cast
from dotenv import load_dotenv

# Prefer official SDK; fall back to community fastmcp if necessary
try:
    from mcp.server.fastmcp import FastMCP  # official
except Exception:
    from fastmcp import FastMCP  # type: ignore # optional fallback if installed

try:
    from mcp.types import TextContent, JsonContent  # type: ignore
except Exception:
    JsonContent = None  # type: ignore

load_dotenv()

DEFAULT_LOCALE = os.getenv("DEFAULT_LOCALE", "de-DE")
SHOPWARE_BASE_URL = os.getenv("SHOPWARE_BASE_URL", "").rstrip("/")
SHOPWARE_CLIENT_ID = os.getenv("SHOPWARE_CLIENT_ID", "")
SHOPWARE_CLIENT_SECRET = os.getenv("SHOPWARE_CLIENT_SECRET", "")
MCP_LOGGING_LEVEL = os.getenv("MCP_LOGGING_LEVEL", "info").upper()

if not SHOPWARE_BASE_URL or not SHOPWARE_CLIENT_ID or not SHOPWARE_CLIENT_SECRET:
    # Fail before opening the MCP listener: every current tool depends on the
    # same Admin API integration credentials.
    raise RuntimeError("Missing SHOPWARE_BASE_URL, SHOPWARE_CLIENT_ID or SHOPWARE_CLIENT_SECRET in .env")

logger = logging.getLogger("mcp-server")
logger.setLevel(getattr(logging, MCP_LOGGING_LEVEL, logging.INFO))
logger.propagate = False

if not any(isinstance(h, logging.StreamHandler) for h in logger.handlers):
    handler = logging.StreamHandler()
    formatter = logging.Formatter("%(levelname)s %(name)s: %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)

mcp = FastMCP("shopware-products-mcp", host="0.0.0.0", port=8005)
# Process-local bearer cache.  It is intentionally never returned in a tool
# result; a container restart or process restart clears it.
_token_cache = {"access_token": None, "exp": 0}

async def get_access_token() -> str:
    """Return a cached Shopware client-credentials access token.

    The token is reused until 30 seconds before its advertised expiry.  A
    missing ``expires_in`` is treated as 600 seconds.  Refreshes use a
    ten-second HTTP timeout; non-success responses propagate through MCP.
    There is no refresh lock or special 401 retry in the current cache.
    """
    now = int(time.time())
    if _token_cache["access_token"] and now < _token_cache["exp"] - 30:
        return _token_cache["access_token"]

    url = f"{SHOPWARE_BASE_URL}/api/oauth/token"
    payload = {
        "grant_type": "client_credentials",
        "client_id": SHOPWARE_CLIENT_ID,
        "client_secret": SHOPWARE_CLIENT_SECRET,
    }
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(url, json=payload, headers={"Content-Type":"application/json"})
        r.raise_for_status()
        data = r.json()
    _token_cache["access_token"] = data["access_token"]
    _token_cache["exp"] = now + int(data.get("expires_in", 600))
    return _token_cache["access_token"]

async def _auth_headers() -> Dict[str, str]:
    """Build JSON Admin API headers without exposing them to MCP callers."""
    token = await get_access_token()
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

async def sw_search(resource: str, criteria: Dict[str, Any]) -> Dict[str, Any]:
    """POST criteria to ``/api/search/{resource}`` with a 15-second timeout.

    This is the Shopware Admin API, not a Store API request in a
    ``SalesChannelContext``.  Visibility and customer-specific pricing are
    therefore not implicitly enforced by this helper.
    """
    url = f"{SHOPWARE_BASE_URL}/api/search/{resource}"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(url, headers=await _auth_headers(), json=criteria)
        resp.raise_for_status()
        return resp.json()

async def sw_get(resource: str, id_: str) -> Dict[str, Any]:
    """GET one Admin API entity from ``/api/{resource}/{id}``.

    Callers must normalize the result before crossing the MCP/model boundary.
    """
    url = f"{SHOPWARE_BASE_URL}/api/{resource}/{id_}"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, headers=await _auth_headers())
        resp.raise_for_status()
        return resp.json()

def _norm_product(p: Dict[str, Any]) -> Dict[str, Any]:
    """Reduce an Admin API product to the public model-facing field set.

    Translated names take precedence.  The allowed output is ``id``, ``name``,
    ``productNumber``, ``purchaseUnit``, ``unitShortCode``, and ``unitName``;
    notably it contains no price, stock, visibility, or customer data.
    """
    t = p.get("translated") or {}
    name = t.get("name") or p.get("name")
    pid = p.get("id")
    unit = p.get("unit") or {}

    out = {
        "id": pid,
        "name": name,
        "productNumber": p.get("productNumber"),    
        "purchaseUnit": p.get("purchaseUnit"),
        "unitShortCode": unit.get("shortCode"),
        "unitName": unit.get("name"),
    }

    return out

def _norm_category(c: Dict[str, Any]) -> Dict[str, Any]:
    """Reduce an Admin API category to identity, hierarchy, and active state."""
    t = c.get("translated") or {}
    name = t.get("name") or c.get("name")
    return {
        "id": c.get("id"),
        "name": name,
        "parentId": c.get("parentId"),
        "level": c.get("level"),
        "active": c.get("active"),
    }

@mcp.tool()
async def search_products_public(query: str, limit: int = 10, locale: str = DEFAULT_LOCALE):
    """
    Search products by term for public users.

    :param query: Search term
    :param limit: Maximum number of products to return (1-100)
    :param locale: Locale for translations
    :return: Dictionary with list of products and count
    :rtype: Dict[str, Any]
    """
    # ``locale`` is part of the MCP signature but is not forwarded to
    # Shopware.  No active, visibility, or sales-channel filter is added, and
    # ``count`` below describes returned items rather than total hits.
    lim = max(1, min(int(limit), 100))
    criteria = {
        "limit": lim,
        "term": query,
        "includes": {"product": ["id","productNumber","name","translated", "purchaseUnit", "unit"]},
    }
    data = await sw_search("product", criteria)
    items = [_norm_product(p) for p in data.get("data", [])]
    logger.info("Successfully searched products")
    logger.debug("Successfully searched products with query: %s", query)
    logger.debug("Result: \n%s", items)
    
    result = {"items": items, "count": len(items)}
    if JsonContent:
        return JsonContent(result)
    return result


@mcp.tool()
async def get_product_by_id_public(id: str, locale: str = DEFAULT_LOCALE):
    """
    Fetch a single product by UUID for public users.
    
    :param id: Product UUID
    :param locale: Locale for translations
    :return: Normalized product data or error message
    :rtype: Dict[str, Any]
    """
    # UUID syntax is not validated before constructing the Admin API URL, and
    # the accepted ``locale`` is not forwarded.
    res = await sw_get("product", id)
    p = res.get("data") if isinstance(res, dict) and "data" in res else res
    if not p:
        return {"error": f"Product {id} not found"}
    logger.info("Successfully searched products")
    logger.debug("Successfully searched products with id: %s", id)

    result = _norm_product(p)
    if JsonContent:
        return JsonContent(result)
    return result


@mcp.tool()
async def get_product_by_number_public(product_number: str, limit: int = 1, locale: str = DEFAULT_LOCALE):
    """
    Fetch product(s) by exact productNumber for public users.
    
    :param product_number: Product number to search for
    :param limit: Maximum number of products to return (1-10)
    :param locale: Locale for translations
    :return: Dictionary with list of products and count
    :rtype: Dict[str, Any]
    """
    # The hard-coded schema in app.py omits ``limit`` even though the MCP
    # function accepts it.  ``locale`` is accepted but not forwarded.
    lim = max(1, min(int(limit), 10))
    criteria = {
        "limit": lim,
        "filter": [{"type": "equals", "field": "product.productNumber", "value": product_number}],
        "includes": {"product": ["id","productNumber","name", "translated", "purchaseUnit", "unit"]},
    }
    data = await sw_search("product", criteria)
    items = [_norm_product(p) for p in data.get("data", [])]
    logger.info("Successfully searched products")
    logger.debug("Successfully searched products with product_number: %s", product_number)

    result = {"items": items, "count": len(items)}
    if JsonContent:
        return JsonContent(result)
    return result


@mcp.tool()
async def list_categories(parent_id: Optional[str] = None, limit: int = 50, locale: str = DEFAULT_LOCALE):
    """
    List categories (optionally children of parent_id).
    
    :param parent_id: Parent category ID to filter by
    :param limit: Maximum number of categories to return (1-100)
    :param locale: Locale for translations
    :return: Dictionary with list of categories and count
    :rtype: Dict[str, Any]
    """
    # The hard-coded schema in app.py advertises no arguments, so model-selected
    # calls normally use these defaults.  Active state is returned but not
    # filtered, and ``locale`` is not forwarded.
    lim = max(1, min(int(limit), 100))
    filters: List[Dict[str, Any]] = []
    if parent_id:
        filters.append({"type": "equals", "field": "parentId", "value": parent_id})
    criteria = {
        "limit": lim,
        "filter": filters,
        "includes": {"category": ["id","name","parentId","level","active","translated"]},
    }
    data = await sw_search("category", criteria)
    items = [_norm_category(c) for c in data.get("data", [])]
    logger.info("Successfully listed categories")
    logger.debug("Successfully listed categories with parent_id: %s", parent_id)

    result = {"items": items, "count": len(items)}
    if JsonContent:
        return JsonContent(result)
    return result

if __name__ == "__main__":
    # Compose runs this path directly; the chat client connects to /mcp using
    # MCP's streamable HTTP transport.
    mcp.run(transport="streamable-http", mount_path="/mcp")
