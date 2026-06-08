"""
Renueva el token de Instagram (caduca cada ~60 dias) y lo guarda en
data/ig_token.txt. Lo corre el cron del servidor cada mes.
"""
import json
import os
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(os.path.dirname(HERE), "data")
TOKEN_FILE = os.path.join(DATA, "ig_token.txt")


def read_token():
    try:
        with open(TOKEN_FILE) as f:
            t = f.read().strip()
            if t:
                return t
    except Exception:
        pass
    return os.environ.get("IG_ACCESS_TOKEN", "")


def main():
    cur = read_token()
    if not cur:
        print("No hay token actual.")
        return
    url = ("https://graph.instagram.com/refresh_access_token"
           "?grant_type=ig_refresh_token&access_token=" + cur)
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            data = json.loads(r.read().decode())
    except Exception as e:
        print("Error pidiendo token nuevo:", e)
        return
    new = data.get("access_token")
    if not new:
        print("No se renovo:", data)
        return
    os.makedirs(DATA, exist_ok=True)
    with open(TOKEN_FILE, "w") as f:
        f.write(new)
    print(f"Token renovado. Vence en ~{data.get('expires_in', 0)//86400} dias.")


if __name__ == "__main__":
    main()
