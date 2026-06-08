"""
Publica las historias PROGRAMADAS cuya hora ya llego.
Lo corre GitHub Actions cada 15 minutos.

Lee la cola (IG_SCHEDULE), el token y el ID de cuenta desde las variables
de entorno del servicio en Render, publica las que ya tocan, y vuelve a
guardar la cola con las pendientes.
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

APP_BASE = os.environ.get("APP_BASE", "https://collage-tool.onrender.com")
GRAPH = "https://graph.instagram.com/v21.0"
RENDER_KEY = os.environ["RENDER_API_KEY"]
RENDER_SRV = os.environ["RENDER_SERVICE_ID"]
RENDER_BASE = f"https://api.render.com/v1/services/{RENDER_SRV}"
MAX_ATTEMPTS = 3


def _req(url, data=None, method="GET", headers=None):
    headers = headers or {}
    body = None
    if data is not None:
        body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:
            raise RuntimeError(f"HTTP {e.code}")


def warm_up(url, tries=15, wait=5):
    """Despierta el servidor de Render (se duerme en el plan gratis) y
    espera a que la imagen este accesible antes de publicar."""
    for _ in range(tries):
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=30) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(wait)
    return False


def render_env():
    out = {}
    data = _req(f"{RENDER_BASE}/env-vars?limit=100",
                headers={"Authorization": f"Bearer {RENDER_KEY}"})
    for it in data:
        ev = it.get("envVar", {})
        out[ev.get("key")] = ev.get("value")
    return out


def render_set(key, value):
    req = urllib.request.Request(
        f"{RENDER_BASE}/env-vars/{key}",
        data=json.dumps({"value": value}).encode(),
        method="PUT",
        headers={"Authorization": f"Bearer {RENDER_KEY}",
                 "Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=60).read()


def publish_story(token, ig_user, image_url):
    j1 = _req(f"{GRAPH}/{ig_user}/media", method="POST",
              data={"image_url": image_url, "media_type": "STORIES",
                    "access_token": token})
    if "id" not in j1:
        raise RuntimeError(f"contenedor: {j1}")
    j2 = _req(f"{GRAPH}/{ig_user}/media_publish", method="POST",
              data={"creation_id": j1["id"], "access_token": token})
    if "id" not in j2:
        raise RuntimeError(f"publish: {j2}")
    return j2["id"]


def main():
    env = render_env()
    token = env.get("IG_ACCESS_TOKEN")
    ig_user = env.get("IG_USER_ID")
    try:
        schedule = json.loads(env.get("IG_SCHEDULE") or "[]")
    except Exception:
        schedule = []

    if not schedule:
        print("Cola vacia, nada que publicar.")
        return
    if not token or not ig_user:
        print("Falta token o user id; no publico.")
        return

    now = int(time.time())
    due = [it for it in schedule if it.get("publish_at", 0) <= now]
    if due:
        # despertar el servidor (Render gratis se duerme) usando la 1a imagen
        print("Despertando el servidor de Render...")
        warm_up(APP_BASE + due[0]["image_path"])

    remaining = []
    changed = False
    for it in schedule:
        if it.get("publish_at", 0) > now:
            remaining.append(it)               # aun no toca
            continue
        url = APP_BASE + it["image_path"]
        try:
            pid = publish_story(token, ig_user, url)
            print(f"Publicada {it['id']} -> media {pid}")
            changed = True
        except Exception as e:
            it["attempts"] = it.get("attempts", 0) + 1
            print(f"Fallo {it['id']} (intento {it['attempts']}): {e}")
            if it["attempts"] < MAX_ATTEMPTS:
                remaining.append(it)            # reintentar luego
                changed = True                  # persistir el contador de intentos
            else:
                print(f"Descarto {it['id']} tras {MAX_ATTEMPTS} intentos.")
                changed = True

    if changed:
        render_set("IG_SCHEDULE", json.dumps(remaining))
        print(f"Cola actualizada: quedan {len(remaining)}.")
    else:
        print("Sin cambios en la cola.")


if __name__ == "__main__":
    main()
