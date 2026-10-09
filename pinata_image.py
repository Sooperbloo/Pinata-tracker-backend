"""
pinata_image.py — draws a copy of the Pinata Tracker mod display as a PNG.

Geometry is measured from a real screenshot of the mod (174x140 px, native size):
  * border: 2px white rectangle from (3,3) to (170,137)
  * bar track: x 12..161 (150px wide), 9px tall, colour #2B2D31
  * bar fill: 150 * count/100 px wide
  * text rows at y=12 (title), 33, 66, 99; bars at y=51, 84, 117 (33px row pitch)
Everything is drawn at native size and then scaled up with nearest-neighbour, so the pixel look is kept.

Needs only Pillow. Drop a Minecraft-style .ttf next to this file as `minecraft.ttf` (or set
PINATA_FONT_PATH) to get the in-game lettering; without it a built-in fallback font is used.
"""
import io
import os

from PIL import Image, ImageDraw, ImageFont

NATIVE_W, NATIVE_H = 174, 140
BAR_X0, BAR_W, BAR_H = 12, 150, 9
TEXT_X = 12
TITLE_Y = 9
ROW_TEXT_Y = (30, 63, 96)
ROW_BAR_Y = (51, 84, 117)

BG_TOP, BG_BOTTOM = (37, 27, 43), (21, 14, 21)
BORDER = (255, 255, 255)
TRACK = (43, 45, 49)
TEXT = (255, 255, 255)
STALE_TEXT = (150, 150, 150)

GREEN, YELLOW, RED = (87, 242, 135), (254, 231, 92), (237, 66, 69)
MAINT_RED, MAINT_GREY = (255, 85, 85), (170, 170, 170)  # maintenance screen text colours

# Bar colour by count. These cut-offs are a best guess from one screenshot (77 = green, 81 = yellow).
YELLOW_FROM = 80
RED_FROM = 95

FONT_CANDIDATES = [
    os.environ.get("PINATA_FONT_PATH", ""),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "minecraft.ttf"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSansMono-Bold.ttf",
]
FONT_SIZE = 14  # native px; tuned so a row of text is ~11px tall like the mod's


def _load_font(size=FONT_SIZE):
    for path in FONT_CANDIDATES:
        if path and os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def bar_colour(count):
    if count >= RED_FROM:
        return RED
    if count >= YELLOW_FROM:
        return YELLOW
    return GREEN


def render_pinata_image(realms, scale=4, maintenance=False):
    """
    realms: ordered list of (name, count_or_None, stale_bool)
    maintenance: draw the 'Under Maintenance' screen instead of the counts.
    Returns PNG bytes.
    """
    img = Image.new("RGB", (NATIVE_W, NATIVE_H), BG_TOP)
    d = ImageDraw.Draw(img)

    for y in range(NATIVE_H):  # vertical gradient background
        t = y / (NATIVE_H - 1)
        c = tuple(round(a + (b - a) * t) for a, b in zip(BG_TOP, BG_BOTTOM))
        d.line([(0, y), (NATIVE_W - 1, y)], fill=c)

    d.rectangle([3, 3, 170, 137], outline=BORDER, width=2)

    font = _load_font()
    d.text((TEXT_X, TITLE_Y), "Pinata Tracker", font=font, fill=TEXT)

    if maintenance:
        d.text((TEXT_X, 33), "Under Maintenance", font=font, fill=MAINT_RED)
        d.text((TEXT_X, 57), "Check back soon", font=font, fill=MAINT_GREY)
        realms = []

    for i, (name, count, stale) in enumerate(realms[:3]):
        ty, by = ROW_TEXT_Y[i], ROW_BAR_Y[i]
        known = isinstance(count, int)
        label = f"{name}: {count}/100" if known else f"{name}: ?/100"
        d.text((TEXT_X, ty), label, font=font, fill=STALE_TEXT if (stale or not known) else TEXT)
        d.rectangle([BAR_X0, by, BAR_X0 + BAR_W - 1, by + BAR_H - 1], fill=TRACK)
        if known and count > 0:
            fill_w = max(1, round(BAR_W * min(count, 100) / 100))
            colour = bar_colour(count)
            if stale:
                colour = tuple((c + 90) // 2 for c in colour)  # dimmed when data is old
            d.rectangle([BAR_X0, by, BAR_X0 + fill_w - 1, by + BAR_H - 1], fill=colour)

    if scale != 1:
        img = img.resize((NATIVE_W * scale, NATIVE_H * scale), Image.NEAREST)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
