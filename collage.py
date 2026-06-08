"""
Arma un collage VERTICAL (1080x1920) para Stories de Instagram a partir
de fotos de producto, con cuadro de precio, tallas y badge de
'Apartado a 15 dias'.
Soporta varias PLANTILLAS de estilo. Todo se dibuja por codigo con Pillow
-> texto exacto y nitido, sin OpenAI.
"""
import io
import os

from curl_cffi import requests as creq
from PIL import Image, ImageDraw, ImageFont, ImageOps

CANVAS_W = 1080
CANVAS_H = 1920

_HERE = os.path.dirname(os.path.abspath(__file__))

# Fuentes incluidas en el proyecto (funcionan en Mac y en Linux/nube).
# Si no estan, cae a las del sistema.
FONT_BOLD = [
    os.path.join(_HERE, "fonts", "LiberationSans-Bold.ttf"),
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]
FONT_REG = [
    os.path.join(_HERE, "fonts", "LiberationSans-Regular.ttf"),
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]

# --------------------------------------------------------------------------
# PLANTILLAS
# Cada plantilla define colores, marco, estilo de header y de panel.
# --------------------------------------------------------------------------
TEMPLATES = {
    "dark": {
        "label": "Oscuro",
        "bg": (17, 17, 17), "outer": 0, "gap": 10, "radius": 0,
        "header": "bar", "header_h": 150, "panel": "bar", "panel_h": 340,
        "panel_bg": (17, 17, 17), "text": (255, 255, 255), "sub": (170, 170, 170),
        "chip_bg": (255, 255, 255), "chip_text": (17, 17, 17),
        "accent": (227, 28, 60), "badge_text": (255, 255, 255),
        "store_text": (255, 255, 255),
    },
    "light": {
        "label": "Claro",
        "bg": (244, 244, 246), "outer": 46, "gap": 20, "radius": 26,
        "header": "float", "header_h": 130, "panel": "card", "panel_h": 360,
        "panel_bg": (255, 255, 255), "text": (20, 20, 20), "sub": (130, 130, 130),
        "chip_bg": (20, 20, 20), "chip_text": (255, 255, 255),
        "accent": (20, 20, 20), "badge_text": (255, 255, 255),
        "store_text": (20, 20, 20),
    },
    "bold": {
        "label": "Bold",
        "bg": (0, 0, 0), "outer": 0, "gap": 6, "radius": 0,
        "header": "float", "header_h": 130, "panel": "bar", "panel_h": 370,
        "panel_bg": (0, 0, 0), "text": (255, 255, 255), "sub": (150, 150, 150),
        "chip_bg": (215, 255, 0), "chip_text": (0, 0, 0),
        "accent": (215, 255, 0), "badge_text": (0, 0, 0),
        "store_text": (255, 255, 255),
    },
    "boutique": {
        "label": "Boutique",
        "bg": (244, 238, 228), "outer": 50, "gap": 18, "radius": 18,
        "header": "float", "header_h": 130, "panel": "card", "panel_h": 360,
        "panel_bg": (255, 255, 255), "text": (45, 36, 30), "sub": (150, 132, 116),
        "chip_bg": (45, 36, 30), "chip_text": (244, 238, 228),
        "accent": (176, 124, 92), "badge_text": (255, 255, 255),
        "store_text": (45, 36, 30),
    },
}


def _font(size, bold=True):
    for path in (FONT_BOLD if bold else FONT_REG):
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


def download_image(url):
    if url.startswith("//"):
        url = "https:" + url
    r = creq.get(url, impersonate="chrome", timeout=30)
    r.raise_for_status()
    return Image.open(io.BytesIO(r.content)).convert("RGB")


def _cover(img, w, h):
    return ImageOps.fit(img, (w, h), Image.LANCZOS)


def _rounded_mask(w, h, radius):
    m = Image.new("L", (w, h), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, w - 1, h - 1], radius=radius, fill=255)
    return m


def _paste_photo(canvas, img, x, y, w, h, radius):
    fitted = _cover(img, w, h)
    if radius > 0:
        canvas.paste(fitted, (x, y), _rounded_mask(w, h, radius))
    else:
        canvas.paste(fitted, (x, y))


def _grid_boxes(n, w, h, gap):
    if n <= 1:
        return [(0, 0, w, h)]
    if n == 2:
        ch = (h - gap) // 2
        return [(0, 0, w, ch), (0, ch + gap, w, h - ch - gap)]
    if n == 3:
        top_h = (h - gap) * 3 // 5
        cw = (w - gap) // 2
        return [
            (0, 0, w, top_h),
            (0, top_h + gap, cw, h - top_h - gap),
            (cw + gap, top_h + gap, w - cw - gap, h - top_h - gap),
        ]
    cw = (w - gap) // 2
    ch = (h - gap) // 2
    return [
        (0, 0, cw, ch),
        (cw + gap, 0, w - cw - gap, ch),
        (0, ch + gap, cw, h - ch - gap),
        (cw + gap, ch + gap, w - cw - gap, h - ch - gap),
    ]


def _pill(draw, x, y, text, font, bg, fg, pad=28, h=None):
    tw = int(draw.textlength(text, font=font))
    bbox = draw.textbbox((0, 0), text, font=font)
    th = bbox[3] - bbox[1]
    ph = h or (th + 34)
    pw = tw + pad * 2
    draw.rounded_rectangle([x, y, x + pw, y + ph], radius=ph // 2.6, fill=bg)
    draw.text((x + pad, y + (ph - th) // 2 - bbox[1]), text, font=font, fill=fg)
    return pw, ph


def build_collage(images, price="", sizes=None, title="",
                  layaway="Apartado a 15 dias", store="", template="dark"):
    cfg = TEMPLATES.get(template, TEMPLATES["dark"])
    sizes = sizes or []
    images = [im for im in images if im is not None][:4]
    if not images:
        raise ValueError("No hay imagenes para el collage")

    W, H = CANVAS_W, CANVAS_H
    outer = cfg["outer"]
    canvas = Image.new("RGB", (W, H), cfg["bg"])

    # --- Zona de la cuadricula de fotos ---
    grid_x0 = outer
    grid_x1 = W - outer
    grid_top = outer + (cfg["header_h"] if cfg["header"] == "bar" else 0)
    grid_bottom = H - cfg["panel_h"]
    gw = grid_x1 - grid_x0
    gh = grid_bottom - grid_top

    for img, (x, y, w, h) in zip(images, _grid_boxes(len(images), gw, gh, cfg["gap"])):
        _paste_photo(canvas, img, grid_x0 + x, grid_top + y, w, h, cfg["radius"])

    draw = ImageDraw.Draw(canvas, "RGBA")

    # --- HEADER (marca + badge apartado) ---
    bfont = _font(38, bold=True)
    sfont = _font(40, bold=True)
    if cfg["header"] == "bar":
        draw.rectangle([0, 0, W, cfg["header_h"]], fill=cfg["panel_bg"])
        if store:
            draw.text((48, 52), store.upper(), font=sfont, fill=cfg["store_text"])
        if layaway:
            txt = layaway.upper()
            pw = int(draw.textlength(txt, font=bfont)) + 56
            _pill(draw, W - 48 - pw, 36, txt, bfont, cfg["accent"], cfg["badge_text"], h=78)
    else:  # float: pills sobre la foto
        ty = outer + 26
        if store:
            _pill(draw, outer + 26, ty, store.upper(), sfont,
                  cfg["bg"], cfg["store_text"], h=72)
        if layaway:
            txt = layaway.upper()
            pw = int(draw.textlength(txt, font=bfont)) + 56
            _pill(draw, W - outer - 26 - pw, ty, txt, bfont,
                  cfg["accent"], cfg["badge_text"], h=72)

    # --- PANEL inferior (titulo + precio + tallas) ---
    if cfg["panel"] == "bar":
        px0, py0, px1, py1 = 0, grid_bottom, W, H
        pmargin = 48
    else:  # card flotante
        px0, py0 = outer, grid_bottom + cfg["gap"]
        px1, py1 = W - outer, H - outer
        draw.rounded_rectangle([px0, py0, px1, py1], radius=32, fill=cfg["panel_bg"])
        pmargin = 44

    cx = px0 + pmargin
    cy = py0 + (40 if cfg["panel"] == "bar" else 38)

    if title:
        t = title if len(title) <= 38 else title[:35] + "..."
        draw.text((cx, cy), t.upper(), font=_font(38, bold=True), fill=cfg["sub"])
        cy += 52

    if price:
        draw.text((cx, cy), price, font=_font(116, bold=True), fill=cfg["text"])
        cy += 150

    if sizes:
        sizes = [str(s).strip() for s in sizes if str(s).strip()][:10]
        draw.text((cx, cy), "TALLAS DISPONIBLES", font=_font(28, bold=False),
                  fill=cfg["sub"])
        cy += 44
        usable = (px1 - pmargin) - cx
        fsize, pad, gap = 40, 22, 14
        while fsize >= 24:
            cf = _font(fsize, bold=True)
            total = sum(int(draw.textlength(s, font=cf)) + pad * 2 for s in sizes)
            total += gap * (len(sizes) - 1)
            if total <= usable:
                break
            fsize -= 2
            pad = max(12, pad - 1)
            gap = max(8, gap - 1)
        cfont = _font(fsize, bold=True)
        chip_h = fsize + 30
        sx = cx
        for s in sizes:
            tw = int(draw.textlength(s, font=cfont))
            cw = tw + pad * 2
            draw.rounded_rectangle([sx, cy, sx + cw, cy + chip_h], radius=14,
                                   fill=cfg["chip_bg"])
            bb = draw.textbbox((0, 0), s, font=cfont)
            th = bb[3] - bb[1]
            draw.text((sx + pad, cy + (chip_h - th) // 2 - bb[1]), s,
                      font=cfont, fill=cfg["chip_text"])
            sx += cw + gap

    return canvas
