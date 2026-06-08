"""
App web local para generar collages de productos para Instagram.
Uso:  ./venv/bin/python app.py   ->  abre http://127.0.0.1:5000
"""
import base64
import io
import time
from urllib.parse import urlparse

from flask import Flask, jsonify, request, send_file

import collage
from scraper import scrape

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB

# guardamos el ultimo collage en memoria para descargar
_last = {"png": None, "name": "collage.png"}


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
        )
    except Exception as e:
        return jsonify({"error": f"Error armando el collage: {e}"}), 500

    buf = io.BytesIO()
    out.save(buf, format="PNG")
    png = buf.getvalue()
    _last["png"] = png
    _last["name"] = f"collage_{int(time.time())}.png"

    b64 = base64.b64encode(png).decode()
    return jsonify({"image": f"data:image/png;base64,{b64}", "name": _last["name"]})


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
