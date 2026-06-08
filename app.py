"""
App web local para generar collages de productos para Instagram.
Uso:  ./venv/bin/python app.py   ->  abre http://127.0.0.1:5000
"""
import base64
import io
import os
import time
import uuid
from urllib.parse import urlparse

from curl_cffi import requests as creq
from flask import Flask, jsonify, request, send_file

import collage
from scraper import scrape

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB

# carpeta publica para los collages generados (servida en /static/generated/)
GEN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "generated")
os.makedirs(GEN_DIR, exist_ok=True)

# API de Instagram con "Instagram Login" (no requiere pagina de Facebook)
GRAPH = "https://graph.instagram.com/v21.0"

# guardamos el ultimo collage en memoria para descargar
_last = {"png": None, "name": "collage.png"}
_ig_cache = {"user_id": None}


def _ig_user_id(token):
    """Obtiene el ID de la cuenta de Instagram a partir del token (con cache)."""
    if os.environ.get("IG_USER_ID"):
        return os.environ["IG_USER_ID"]
    if _ig_cache["user_id"]:
        return _ig_cache["user_id"]
    r = creq.get(f"{GRAPH}/me", params={"fields": "user_id,username",
                                        "access_token": token}, timeout=30)
    j = r.json()
    uid = j.get("user_id") or j.get("id")
    if uid:
        _ig_cache["user_id"] = uid
    return uid


def _purge_old(keep_seconds=3600):
    """Borra collages generados con mas de 1 hora para no llenar el disco."""
    now = time.time()
    try:
        for f in os.listdir(GEN_DIR):
            p = os.path.join(GEN_DIR, f)
            if os.path.isfile(p) and now - os.path.getmtime(p) > keep_seconds:
                os.remove(p)
    except Exception:
        pass


@app.get("/")
def index():
    with open("static/index.html", encoding="utf-8") as f:
        return f.read()


@app.post("/api/scrape")
def api_scrape():
    """Recibe un link, devuelve fotos (como data-uri), precio y tallas."""
    url = (request.json or {}).get("url", "").strip()
    if not url:
        return jsonify({"error": "Falta el link"}), 400
    if not url.startswith("http"):
        url = "https://" + url
    try:
        data = scrape(url)
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    # descargar y miniaturizar las primeras 8 imagenes como preview base64
    thumbs = []
    for src in data["images"][:8]:
        try:
            img = collage.download_image(src)
            preview = img.copy()
            preview.thumbnail((300, 300))
            buf = io.BytesIO()
            preview.save(buf, format="JPEG", quality=80)
            b64 = base64.b64encode(buf.getvalue()).decode()
            thumbs.append({"src": src, "thumb": f"data:image/jpeg;base64,{b64}"})
        except Exception:
            continue

    store = urlparse(url).netloc.replace("www.", "").split(".")[0]
    return jsonify({
        "title": data.get("title", ""),
        "price": data.get("price", ""),
        "price_value": data.get("price_value"),
        "currency": data.get("currency", "USD"),
        "sizes": data.get("sizes", []),
        "store": store,
        "images": thumbs,
    })


@app.post("/api/collage")
def api_collage():
    """Recibe las urls elegidas + datos y genera el collage."""
    body = request.json or {}
    image_urls = body.get("images", [])
    if not image_urls:
        return jsonify({"error": "Selecciona al menos una foto"}), 400

    imgs = []
    for src in image_urls[:4]:
        try:
            imgs.append(collage.download_image(src))
        except Exception:
            continue
    if not imgs:
        return jsonify({"error": "No pude descargar las fotos seleccionadas"}), 400

    sizes = [s.strip() for s in body.get("sizes", []) if str(s).strip()]
    sold_out = [s.strip() for s in body.get("sold_out", []) if str(s).strip()]
    try:
        out = collage.build_collage(
            images=imgs,
            price=body.get("price", "").strip(),
            sizes=sizes,
            sold_out=sold_out,
            title=body.get("title", "").strip(),
            layaway=body.get("layaway", "Apartado a 15 dias").strip(),
            store=body.get("store", "").strip(),
            template=body.get("template", "dark"),
            condition=body.get("condition", "nuevo"),
        )
    except Exception as e:
        return jsonify({"error": f"Error armando el collage: {e}"}), 500

    buf = io.BytesIO()
    out.save(buf, format="PNG")
    png = buf.getvalue()
    _last["png"] = png
    _last["name"] = f"collage_{int(time.time())}.png"

    # guardar como archivo publico (para poder publicarlo en Instagram)
    _purge_old()
    fname = f"{uuid.uuid4().hex}.png"
    with open(os.path.join(GEN_DIR, fname), "wb") as fh:
        fh.write(png)
    image_path = f"/static/generated/{fname}"

    b64 = base64.b64encode(png).decode()
    return jsonify({
        "image": f"data:image/png;base64,{b64}",
        "name": _last["name"],
        "image_path": image_path,
    })


@app.get("/api/ig_status")
def ig_status():
    """Dice si Instagram esta conectado (y de que cuenta)."""
    token = os.environ.get("IG_ACCESS_TOKEN")
    if not token:
        return jsonify({"configured": False})
    try:
        r = creq.get(f"{GRAPH}/me", params={"fields": "user_id,username",
                                            "access_token": token}, timeout=20)
        j = r.json()
        if j.get("username") or j.get("user_id") or j.get("id"):
            return jsonify({"configured": True, "username": j.get("username", "")})
        return jsonify({"configured": False, "error": _ig_err("validando el token", j)})
    except Exception:
        return jsonify({"configured": True, "username": ""})


@app.post("/api/publish")
def api_publish():
    """Publica el collage como Historia en la cuenta de Instagram configurada."""
    token = os.environ.get("IG_ACCESS_TOKEN")
    if not token:
        return jsonify({"error": "Falta configurar Instagram (token). Avisa para conectarlo."}), 400

    body = request.json or {}
    image_path = body.get("image_path", "")
    if not image_path.startswith("/static/generated/"):
        return jsonify({"error": "Genera el collage primero."}), 400

    # URL publica del collage (forzar https para que Instagram lo pueda leer)
    public_url = f"https://{request.host}{image_path}"

    try:
        ig_user = _ig_user_id(token)
        if not ig_user:
            return jsonify({"error": "No pude identificar tu cuenta de Instagram con ese token."}), 400
        # 1) crear contenedor de Historia
        r1 = creq.post(f"{GRAPH}/{ig_user}/media",
                       data={"image_url": public_url, "media_type": "STORIES",
                             "access_token": token}, timeout=60)
        j1 = r1.json()
        if "id" not in j1:
            return jsonify({"error": _ig_err("creando la historia", j1)}), 400
        # 2) publicar
        r2 = creq.post(f"{GRAPH}/{ig_user}/media_publish",
                       data={"creation_id": j1["id"], "access_token": token}, timeout=60)
        j2 = r2.json()
        if "id" not in j2:
            return jsonify({"error": _ig_err("publicando", j2)}), 400
    except Exception as e:
        return jsonify({"error": f"Error conectando con Instagram: {e}"}), 500

    return jsonify({"ok": True, "id": j2["id"]})


def _ig_err(ctx, payload):
    err = (payload or {}).get("error", {})
    msg = err.get("error_user_msg") or err.get("message") or str(payload)
    return f"Instagram rechazo {ctx}: {msg}"


@app.get("/download")
def download():
    if not _last["png"]:
        return "No hay collage generado todavia", 404
    return send_file(
        io.BytesIO(_last["png"]),
        mimetype="image/png",
        as_attachment=True,
        download_name=_last["name"],
    )


def _lan_ip():
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


if __name__ == "__main__":
    ip = _lan_ip()
    print("\n  Collage Tool corriendo:")
    print(f"    En esta Mac:        http://127.0.0.1:5050")
    print(f"    En tu celular WiFi: http://{ip}:5050\n")
    app.run(host="0.0.0.0", port=5050, debug=False)
