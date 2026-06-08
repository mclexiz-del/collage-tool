"""
Extrae fotos, precio, titulo y tallas de un link de producto.
Estrategia (en orden):
  1. Gymshark / sitios Next.js -> parsea el JSON embebido __NEXT_DATA__
  2. Tiendas Shopify -> endpoint /products/handle.json
  3. Cualquier sitio -> og:image / JSON-LD de producto

Usa curl_cffi (imita la huella TLS de Chrome) para pasar protecciones
anti-bot tipo CloudFront/Akamai como la de gymshark.com.
"""
import json
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from curl_cffi import requests as creq

IMPERSONATE = "chrome"

# Una sola sesion: la primera visita a la home deja cookies que ayudan.
_session = None


def get_session():
    global _session
    if _session is None:
        _session = creq.Session(impersonate=IMPERSONATE, timeout=25)
    return _session


def fetch(url, referer=None):
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    }
    if referer:
        headers["Referer"] = referer
    return get_session().get(url, headers=headers)


CURRENCY_SYMBOLS = {"USD": "$", "MXN": "$", "EUR": "€", "GBP": "£"}


def _fmt_price(num, currency_code="USD"):
    if num is None:
        return ""
    sym = CURRENCY_SYMBOLS.get(currency_code, "$")
    suffix = f" {currency_code}" if currency_code in ("USD", "MXN") else ""
    try:
        num = float(num)
    except (TypeError, ValueError):
        return ""
    if num == int(num):
        return f"{sym}{int(num):,}{suffix}"
    return f"{sym}{num:,.2f}{suffix}"


def _clean_shopify_money(value):
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if isinstance(value, str) and value.isdigit():
        num = num / 100.0
    return num


# ----------------------------------------------------------------------------
# 1) Next.js / Gymshark
# ----------------------------------------------------------------------------
def scrape_nextdata(url):
    try:
        r = fetch(url)
        if r.status_code != 200:
            return None
        soup = BeautifulSoup(r.text, "lxml")
        tag = soup.find("script", id="__NEXT_DATA__")
        if not tag or not tag.string:
            return None
        data = json.loads(tag.string)
    except Exception:
        return None

    # localizar el nodo del producto
    prod = None
    try:
        prod = data["props"]["pageProps"]["productData"]["product"]
        variants = data["props"]["pageProps"]["productData"].get("variants", [])
    except Exception:
        prod, variants = _find_product_node(data), []
    if not isinstance(prod, dict):
        return None

    title = prod.get("title") or ""

    # imagenes
    images = []
    for m in (prod.get("media") or prod.get("images") or []):
        if isinstance(m, dict):
            src = m.get("url") or m.get("src")
        else:
            src = m
        if src and not src.startswith("http"):
            src = "https:" + src if src.startswith("//") else src
        if src and src not in images:
            images.append(src)

    # tallas
    sizes = []
    if prod.get("sizesInStock"):
        sizes = [str(s).upper() for s in prod["sizesInStock"]]
    elif prod.get("availableSizes"):
        sizes = [str(s.get("size", "")).upper()
                 for s in prod["availableSizes"] if s.get("inStock", True)]

    # precio + moneda
    price_num = prod.get("price")
    currency = "USD"
    if variants and isinstance(variants[0], dict):
        currency = variants[0].get("currencyCode", currency)

    if not images:
        return None
    return {
        "title": title,
        "images": images,
        "sizes": sizes,
        "price": _fmt_price(price_num, currency),
        "source": "nextdata",
    }


def _find_product_node(obj):
    """Busca recursivamente un dict que parezca producto (title + media/images)."""
    if isinstance(obj, dict):
        keys = obj.keys()
        if "title" in keys and ("media" in keys or "images" in keys):
            return obj
        for v in obj.values():
            r = _find_product_node(v)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_product_node(v)
            if r:
                return r
    return None


# ----------------------------------------------------------------------------
# 2) Shopify .json
# ----------------------------------------------------------------------------
def scrape_shopify(url):
    parsed = urlparse(url)
    m = re.search(r"/products/([^/?#]+)", parsed.path)
    if not m:
        return None
    handle = m.group(1)
    json_url = f"{parsed.scheme}://{parsed.netloc}/products/{handle}.json"
    try:
        r = fetch(json_url, referer=url)
        if r.status_code != 200:
            return None
        data = r.json().get("product")
        if not data:
            return None
    except Exception:
        return None

    images = [img["src"] for img in data.get("images", []) if img.get("src")]
    sizes = []
    for opt in data.get("options", []):
        if opt.get("name", "").lower() in ("size", "talla", "tallas", "tamaño"):
            sizes = [str(v).upper() for v in opt.get("values", [])]
            break
    price_num = None
    variants = data.get("variants", [])
    currency = "USD"
    if variants:
        price_num = _clean_shopify_money(variants[0].get("price"))
    if not images:
        return None
    return {
        "title": data.get("title", ""),
        "images": images,
        "sizes": sizes,
        "price": _fmt_price(price_num, currency),
        "source": "shopify",
    }


# ----------------------------------------------------------------------------
# 3) Generico: og:image + JSON-LD
# ----------------------------------------------------------------------------
def scrape_generic(url):
    try:
        r = fetch(url)
        r.raise_for_status()
    except Exception as e:
        raise RuntimeError(f"No pude abrir el link: {e}")

    soup = BeautifulSoup(r.text, "lxml")
    images, sizes = [], []
    title, price_num, currency = "", None, "USD"

    if soup.title:
        title = soup.title.get_text(strip=True)
    og_title = soup.find("meta", property="og:title")
    if og_title and og_title.get("content"):
        title = og_title["content"]

    for tag in soup.find_all("meta", property="og:image"):
        c = tag.get("content")
        if c:
            images.append(urljoin(url, c))

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            blob = json.loads(script.string or "{}")
        except Exception:
            continue
        items = blob if isinstance(blob, list) else [blob]
        for item in items:
            if not isinstance(item, dict):
                continue
            if str(item.get("@type", "")).lower() == "product":
                title = item.get("name", title)
                img = item.get("image")
                if isinstance(img, str):
                    images.append(urljoin(url, img))
                elif isinstance(img, list):
                    images.extend(urljoin(url, i) for i in img if isinstance(i, str))
                offers = item.get("offers", {})
                if isinstance(offers, list) and offers:
                    offers = offers[0]
                if isinstance(offers, dict):
                    price_num = offers.get("price")
                    currency = offers.get("priceCurrency", currency)

    if not images:
        for img in soup.find_all("img"):
            src = img.get("src") or img.get("data-src")
            if src and any(src.lower().split("?")[0].endswith(e)
                           for e in (".jpg", ".jpeg", ".png", ".webp")):
                images.append(urljoin(url, src))

    seen, uniq = set(), []
    for i in images:
        if i not in seen:
            seen.add(i)
            uniq.append(i)

    return {
        "title": title,
        "images": uniq,
        "sizes": sizes,
        "price": _fmt_price(price_num, currency) if price_num else "",
        "source": "generic",
    }


def scrape(url):
    # warm-up: visitar la home para obtener cookies (ayuda con anti-bot)
    try:
        parsed = urlparse(url)
        get_session().get(f"{parsed.scheme}://{parsed.netloc}/",
                          headers={"Accept-Language": "es-ES,es;q=0.9"})
    except Exception:
        pass

    for fn in (scrape_nextdata, scrape_shopify):
        try:
            res = fn(url)
            if res and res.get("images"):
                return res
        except Exception:
            continue
    return scrape_generic(url)
