"""
App web local para generar collages de productos para Instagram.
Uso:  ./venv/bin/python app.py   ->  abre http://127.0.0.1:5000
"""
import base64
import io
import json
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

# Credenciales de Render (para leer/guardar la cola de programadas)
RENDER_KEY = os.environ.get("RENDER_API_KEY")
RENDER_SRV = os.environ.get("RENDER_SERVICE_ID")
RENDER_BASE = f"https://api.render.com/v1/services/{RENDER_SRV}" if RENDER_SRV else None

# GitHub: almacen PERMANENTE para imagenes programadas (el disco de Render
# es temporal y se borra al reiniciar/desplegar).
GH_TOKEN = os.environ.get("GH_TOKEN")
GH_REPO = os.environ.get("GH_REPO", "")


def _github_put_file(repo_path, data_bytes, message):
    """Sube un archivo al repo. Devuelve la URL raw publica o None."""
    if not (GH_TOKEN and GH_REPO):
        return None
    try:
        r = creq.put(
            f"https://api.github.com/repos/{GH_REPO}/contents/{repo_path}",
            headers={"Authorization": f"Bearer {GH_TOKEN}",
                     "Accept": "application/vnd.github+json",
                     "User-Agent": "collage-tool"},
            json={"message": message,
                  "content": base64.b64encode(data_bytes).decode(),
                  "branch": "main"}, timeout=60)
        if r.status_code in (200, 201):
            return f"https://raw.githubusercontent.com/{GH_REPO}/main/{repo_path}"
    except Exception:
        return None
    return None


_GH_HDR = {"Accept": "application/vnd.github+json", "User-Agent": "collage-tool"}
GALLERY_CAP = 40  # cuantos collages se conservan en la galeria


def _gh_raw(repo_path):
    return f"https://raw.githubusercontent.com/{GH_REPO}/main/{repo_path}"


def _github_list(folder):
    """Lista archivos .png de una carpeta del repo: [{name, path, sha}]."""
    if not (GH_TOKEN and GH_REPO):
        return []
    try:
        r = creq.get(f"https://api.github.com/repos/{GH_REPO}/contents/{folder}",
                     headers={**_GH_HDR, "Authorization": f"Bearer {GH_TOKEN}"},
                     timeout=30)
        if r.status_code != 200:
            return []
        data = r.json()
        if not isinstance(data, list):
            return []
        return [{"name": x["name"], "path": x["path"], "sha": x["sha"]}
                for x in data if str(x.get("name", "")).endswith(".png")]
    except Exception:
        return []


def _github_delete(path, sha):
    if not (GH_TOKEN and GH_REPO and path and sha):
        return
    try:
        creq.delete(f"https://api.github.com/repos/{GH_REPO}/contents/{path}",
                    headers={**_GH_HDR, "Authorization": f"Bearer {GH_TOKEN}"},
                    json={"message": f"borrar {path}", "sha": sha, "branch": "main"},
                    timeout=30)
    except Exception:
        pass


def _cap_gallery():
    """Mantiene la galeria en <= GALLERY_CAP, sin borrar las programadas."""
    files = _github_list("gallery")
    if len(files) <= GALLERY_CAP:
        return
    protected = set()
    for it in _get_schedule():
        iu = it.get("image_url") or ""
        if iu:
            protected.add(iu.split("/")[-1])

    def ts_of(f):
        try:
            return int(f["name"].split("_")[0])
        except Exception:
            return 0

    files.sort(key=ts_of)  # mas viejos primero
    to_delete = len(files) - GALLERY_CAP
    for f in files:
        if to_delete <= 0:
            break
        if f["name"] in protected:
            continue
        _github_delete(f["path"], f["sha"])
        to_delete -= 1


def _render_env_get(key):
    if not (RENDER_KEY and RENDER_BASE):
        return None
    try:
        r = creq.get(f"{RENDER_BASE}/env-vars?limit=100",
                     headers={"Authorization": f"Bearer {RENDER_KEY}"}, timeout=30)
        for it in r.json():
            ev = it.get("envVar", {})
            if ev.get("key") == key:
                return ev.get("value")
    except Exception:
        return None
    return None


def _render_env_set(key, value):
    if not (RENDER_KEY and RENDER_BASE):
        return False
    try:
        creq.put(f"{RENDER_BASE}/env-vars/{key}",
                 headers={"Authorization": f"Bearer {RENDER_KEY}",
                          "Content-Type": "application/json"},
                 json={"value": value}, timeout=30)
        return True
    except Exception:
        return False


def _get_schedule():
    raw = _render_env_get("IG_SCHEDULE") or "[]"
    try:
        return json.loads(raw)
    except Exception:
        return []


def _set_schedule(items):
    return _render_env_set("IG_SCHEDULE", json.dumps(items))

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


def _purge_old(keep_seconds=7 * 24 * 3600):
    """Borra collages generados con mas de 7 dias para no llenar el disco."""
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

    resp = {"image": f"data:image/png;base64,{base64.b64encode(png).decode()}",
            "name": _last["name"]}

    if GH_TOKEN and GH_REPO:
        # galeria PERMANENTE en GitHub (no se borra al reiniciar Render)
        ts = int(time.time())
        gid = uuid.uuid4().hex[:10]
        repo_path = f"gallery/{ts}_{gid}.png"
        image_url = _github_put_file(repo_path, png, f"galeria {gid}")
        if image_url:
            _cap_gallery()
            resp["image_url"] = image_url
        else:
            return jsonify({"error": "No pude guardar el collage en la galeria."}), 500
    else:
        # respaldo local: disco temporal
        _purge_old()
        fname = f"{uuid.uuid4().hex}.png"
        with open(os.path.join(GEN_DIR, fname), "wb") as fh:
            fh.write(png)
        resp["image_path"] = f"/static/generated/{fname}"

    return jsonify(resp)


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
    public_url = _src_public_url(body)
    if not public_url:
        return jsonify({"error": "Genera el collage primero."}), 400

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
        # esperar a que el contenedor este listo
        for _ in range(15):
            st = creq.get(f"{GRAPH}/{j1['id']}",
                          params={"fields": "status_code", "access_token": token},
                          timeout=20).json()
            if st.get("status_code") == "FINISHED":
                break
            if st.get("status_code") in ("ERROR", "EXPIRED"):
                return jsonify({"error": "Instagram no pudo procesar la imagen."}), 400
            time.sleep(2)
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


def _src_public_url(body):
    """URL publica de la imagen: GitHub (permanente) o disco de Render."""
    image_url = body.get("image_url", "") or ""
    image_path = body.get("image_path", "") or ""
    if image_url.startswith("http"):
        return image_url
    if image_path.startswith("/static/generated/"):
        return f"https://{request.host}{image_path}"
    return None


@app.get("/api/generated")
def api_generated():
    """Lista los collages de la galeria, mas recientes primero."""
    if GH_TOKEN and GH_REPO:
        sched_map = {it.get("image_url"): it.get("publish_at") for it in _get_schedule()}
        items = []
        for f in _github_list("gallery"):
            url = _gh_raw(f["path"])
            try:
                ts = int(f["name"].split("_")[0])
            except Exception:
                ts = 0
            items.append({"path": url, "image_url": url, "ts": ts,
                          "scheduled_at": sched_map.get(url)})
        items.sort(key=lambda x: -x["ts"])
        return jsonify({"items": items[:60]})

    # respaldo local
    items = []
    sched_map = {it.get("image_path"): it.get("publish_at") for it in _get_schedule()}
    try:
        for f in os.listdir(GEN_DIR):
            if f.endswith(".png"):
                p = os.path.join(GEN_DIR, f)
                path = f"/static/generated/{f}"
                items.append({"path": path, "image_url": None,
                              "ts": int(os.path.getmtime(p)),
                              "scheduled_at": sched_map.get(path)})
    except Exception:
        pass
    items.sort(key=lambda x: -x["ts"])
    return jsonify({"items": items[:60]})


@app.post("/api/delete_generated")
def api_delete_generated():
    """Borra un collage de la galeria."""
    body = request.json or {}
    image_url = body.get("image_url", "") or ""
    image_path = body.get("image_path", "") or ""
    if image_url.startswith("http") and GH_TOKEN and GH_REPO:
        fname = image_url.split("/")[-1]
        for f in _github_list("gallery"):
            if f["name"] == fname:
                _github_delete(f["path"], f["sha"])
                break
        return jsonify({"ok": True})
    if image_path.startswith("/static/generated/"):
        p = os.path.join(GEN_DIR, os.path.basename(image_path))
        try:
            if os.path.exists(p):
                os.remove(p)
        except Exception as e:
            return jsonify({"error": str(e)}), 500
        return jsonify({"ok": True})
    return jsonify({"error": "Ruta invalida."}), 400


@app.post("/api/schedule")
def api_schedule():
    """Agenda un collage para publicarse como historia en una fecha/hora."""
    if not (RENDER_KEY and RENDER_BASE):
        return jsonify({"error": "La programacion no esta disponible (falta config del servidor)."}), 400
    body = request.json or {}
    publish_at = body.get("publish_at")
    try:
        publish_at = int(publish_at)
    except (TypeError, ValueError):
        return jsonify({"error": "Fecha/hora invalida."}), 400
    if publish_at < int(time.time()) - 60:
        return jsonify({"error": "Esa hora ya paso. Elige una futura."}), 400

    sid = uuid.uuid4().hex[:10]
    image_url = body.get("image_url", "") or ""
    image_path = body.get("image_path", "") or ""

    if image_url.startswith("http"):
        # ya es permanente (galeria de GitHub); solo se referencia
        item = {"id": sid, "image_url": image_url, "image_path": None,
                "repo_path": None, "publish_at": publish_at, "attempts": 0}
    elif image_path.startswith("/static/generated/") and GH_TOKEN and GH_REPO:
        local = os.path.join(GEN_DIR, os.path.basename(image_path))
        if not os.path.exists(local):
            return jsonify({"error": "Ese collage ya no esta disponible. Generalo de nuevo."}), 400
        with open(local, "rb") as fh:
            url = _github_put_file(f"sched/{sid}.png", fh.read(), f"programar {sid}")
        if not url:
            return jsonify({"error": "No pude guardar la imagen para programar."}), 500
        item = {"id": sid, "image_url": url, "image_path": image_path,
                "repo_path": f"sched/{sid}.png", "publish_at": publish_at, "attempts": 0}
    else:
        return jsonify({"error": "Genera el collage primero."}), 400

    items = _get_schedule()
    items.append(item)
    if not _set_schedule(items):
        return jsonify({"error": "No pude guardar la programacion."}), 500
    return jsonify({"ok": True})


@app.get("/api/scheduled")
def api_scheduled():
    """Devuelve las publicaciones programadas pendientes."""
    items = sorted(_get_schedule(), key=lambda x: x.get("publish_at", 0))
    return jsonify({"items": items})


@app.post("/api/unschedule")
def api_unschedule():
    """Cancela una publicacion programada."""
    body = request.json or {}
    sid = body.get("id")
    items = [it for it in _get_schedule() if it.get("id") != sid]
    _set_schedule(items)
    return jsonify({"ok": True})


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
