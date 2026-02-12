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
_token_cache = {"access_token": None, "exp": 0}

async def get_access_token() -> str:
    """
    Get OAuth2 access token from Shopware, with simple caching.
    
    :return: Access token string
    :rtype: str
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
    """
    Get authorization headers for Shopware API requests.
    
    :return: Headers including the Bearer token for authorization
    :rtype: Dict[str, str]
    """
    token = await get_access_token()
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

async def sw_search(resource: str, criteria: Dict[str, Any]) -> Dict[str, Any]:
    """
    Shopware search API call.
    
    :param resource: Resource to search (e.g., "product")
    :type resource: str
    :param criteria: Search criteria as a dictionary
    :type criteria: Dict[str, Any]
    :return: Search results as a dictionary
    :rtype: Dict[str, Any]
    """
    url = f"{SHOPWARE_BASE_URL}/api/search/{resource}"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(url, headers=await _auth_headers(), json=criteria)
        resp.raise_for_status()
        return resp.json()

async def sw_get(resource: str, id_: str) -> Dict[str, Any]:
    """
    Shopware get API call.
    
    :param resource: Resource to get (e.g., "product")
    :type resource: str
    :param id_: ID of the resource
    :type id_: str
    :return: Resource data as a dictionary
    :rtype: Dict[str, Any]
    """
    url = f"{SHOPWARE_BASE_URL}/api/{resource}/{id_}"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, headers=await _auth_headers())
        resp.raise_for_status()
        return resp.json()

def _norm_product(p: Dict[str, Any], include: bool) -> Dict[str, Any]:
    """
    Normalize a product dictionary from Shopware API to a simplified format.

    :param p: Raw product data from Shopware API
    :type p: Dict[str, Any]
    :return: Normalized product data
    :rtype: Dict[str, Any]
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

    if include:
        price_raw = p.get("price", [])[0]
        price = str(price_raw.get("gross")) if price_raw else None
        price_eur = price.replace(".", ",") if price is not None else None
        out.update({
            "price": price_eur,
            "stock": p.get("stock"),
            "active": p.get("active"),
        })
    
    return out

def _norm_category(c: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalize a category dictionary from Shopware API to a simplified format.

    :param c: Raw category data from Shopware API
    :type c: Dict[str, Any]
    :return: Normalized category data
    :rtype: Dict[str, Any]
    """
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
    lim = max(1, min(int(limit), 100))
    criteria = {
        "limit": lim,
        "term": query,
        "includes": {"product": ["id","productNumber","name","translated", "purchaseUnit", "unit"]},
    }
    data = await sw_search("product", criteria)
    items = [_norm_product(p, include=False) for p in data.get("data", [])]
    logger.info("Successfully searched products")
    logger.debug("Successfully searched products with query: %s", query)
    logger.debug("Result: \n%s", items)
    
    result = {"items": items, "count": len(items)}
    if JsonContent:
        return JsonContent(result)
    return result


@mcp.tool()
async def search_products_auth(query: str, limit: int = 10, locale: str = DEFAULT_LOCALE):
    """
    Search products by term for authenticated users.

    :param query: Search term
    :param limit: Maximum number of products to return (1-100)
    :param locale: Locale for translations
    :return: Dictionary with list of products and count
    :rtype: Dict[str, Any]
    """
    lim = max(1, min(int(limit), 100))
    criteria = {
        "limit": lim,
        "term": query,
        "includes": {"product": ["id","productNumber","name","stock","active","price","translated", "purchaseUnit", "unit"]},
    }
    data = await sw_search("product", criteria)
    items = [_norm_product(p, include=True) for p in data.get("data", [])]
    logger.info("Successfully searched products")
    logger.debug("Successfully searched products with query: %s", query)
    
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
    res = await sw_get("product", id)
    p = res.get("data") if isinstance(res, dict) and "data" in res else res
    if not p:
        return {"error": f"Product {id} not found"}
    logger.info("Successfully searched products")
    logger.debug("Successfully searched products with id: %s", id)

    result = _norm_product(p, include=False)
    if JsonContent:
        return JsonContent(result)
    return result


@mcp.tool()
async def get_product_by_id_auth(id: str, locale: str = DEFAULT_LOCALE):
    """
    Fetch a single product by UUID for authenticated users.
    
    :param id: Product UUID
    :param locale: Locale for translations
    :return: Normalized product data or error message
    :rtype: Dict[str, Any]
    """
    res = await sw_get("product", id)
    p = res.get("data") if isinstance(res, dict) and "data" in res else res
    if not p:
        return {"error": f"Product {id} not found"}
    logger.info("Successfully searched products")
    logger.debug("Successfully searched products with id: %s", id)

    result = _norm_product(p, include=True)
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
    lim = max(1, min(int(limit), 10))
    criteria = {
        "limit": lim,
        "filter": [{"type": "equals", "field": "product.productNumber", "value": product_number}],
        "includes": {"product": ["id","productNumber","name", "translated", "purchaseUnit", "unit"]},
    }
    data = await sw_search("product", criteria)
    items = [_norm_product(p, include=False) for p in data.get("data", [])]
    logger.info("Successfully searched products")
    logger.debug("Successfully searched products with product_number: %s", product_number)

    result = {"items": items, "count": len(items)}
    if JsonContent:
        return JsonContent(result)
    return result


@mcp.tool()
async def get_product_by_number_auth(product_number: str, limit: int = 1, locale: str = DEFAULT_LOCALE):
    """
    Fetch product(s) by exact productNumber for authenticated users.
    
    :param product_number: Product number to search for
    :param limit: Maximum number of products to return (1-10)
    :param locale: Locale for translations
    :return: Dictionary with list of products and count
    :rtype: Dict[str, Any]
    """
    lim = max(1, min(int(limit), 10))
    criteria = {
        "limit": lim,
        "filter": [{"type": "equals", "field": "product.productNumber", "value": product_number}],
        "includes": {"product": ["id","productNumber","name","stock","active","price","translated", "purchaseUnit", "unit"]},
    }
    data = await sw_search("product", criteria)
    items = [_norm_product(p, include=True) for p in data.get("data", [])]
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
    # Run MCP with streamable HTTP Transport
    mcp.run(transport="streamable-http", mount_path="/mcp")
