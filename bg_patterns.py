"""bg_patterns — a big library of procedural background patterns/prints.

Self-contained (PIL only, no numpy, no network) generators for the cheap
/bg_generator batch flow. Each generator is a zero-arg function returning
(png_bytes, info_str) at 1080×1080. They cover camo colorways, animal prints,
geometric prints (stripes/plaid/checker/chevron/argyle/…), and textures
(marble/tie-dye/noise/bokeh) so a "Mixed" batch has real variety instead of
the original ~9 flat/artistic modes.

Perf: shape-based patterns use ImageDraw (fast); pixel-noise patterns render
small then upscale, so every generator stays well under ~1s.

bg.py imports this and merges NEW_PATTERNS with its original modes into one
registry. Kept in a separate module so bg.py doesn't balloon.
"""
import io
import math
import random

from PIL import Image, ImageDraw, ImageFilter

SIZE = 1080

# ─── color helpers ────────────────────────────────────────────────────────
def _parse_hex(h):
    h = (h or '').strip().lstrip('#')
    if len(h) == 3:
        h = ''.join(c * 2 for c in h)
    if len(h) != 6:
        h = '808080'
    try:
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    except ValueError:
        return (128, 128, 128)


def _jit(rgb, a=10):
    return tuple(max(0, min(255, c + random.randint(-a, a))) for c in rgb)


def _save(img):
    if img.size != (SIZE, SIZE):
        img = img.resize((SIZE, SIZE))
    buf = io.BytesIO()
    img.convert('RGB').save(buf, format='PNG', optimize=True)
    return buf.getvalue()


def _vgrad(top, bottom, size=SIZE):
    """Fast vertical gradient via a 1×N column stretched horizontally."""
    a, b = _parse_hex(top) if isinstance(top, str) else top, \
           _parse_hex(bottom) if isinstance(bottom, str) else bottom
    col = Image.new('RGB', (1, size))
    col.putdata([tuple(int(a[i] + (b[i] - a[i]) * (y / (size - 1))) for i in range(3))
                 for y in range(size)])
    return col.resize((size, size))


# Curated color pools — DARK / MOODY ONLY. The accounts are goth / dark-themed,
# so no bright or pastel backgrounds. The full spectrum is kept (dark red,
# dark green, dark blue, dark gold, …) but every tone is deep and muted, plus
# lots of black / charcoal / grey.
BRIGHTS = [  # the "colorful" pool — deep jewel tones (darkened)
    '#7a1f2b', '#5a2d82', '#1f3a5f', '#1e5631', '#8a5a12', '#7a2f1a',
    '#6e6212', '#146b5f', '#7a1f5a', '#0f5a52', '#3a2f7a', '#8a2f5a',
    '#2f6b4a', '#123f6b', '#6b1f1f', '#4a3f7a', '#5a2f1a', '#2f5a6b']
PASTELS = [  # muted dusty mid-dark (used where a softer tone is needed)
    '#4a3a4a', '#3a4a3a', '#2f3f4f', '#4a463a', '#3f2f3f', '#4a3a2f',
    '#2f4a4a', '#3a3a4a', '#463a4a', '#3a4642']
DARKS = [  # black / charcoal / navy / graphite greys
    '#111111', '#1a1a1a', '#222222', '#2a2a2a', '#333333', '#3a3a3a',
    '#4a4a4a', '#0f1420', '#1b2a4a', '#22160f', '#0f2417', '#241a2e',
    '#2d3436', '#2c3e50']
EARTH = ['#5a4326', '#6b4f2a', '#3a2a15', '#4a3a22', '#7a6a3a', '#3f3320']
# Medium desaturated accents — visible on dark bases but NOT bright/colorful.
# Used only where a pattern needs internal contrast (confetti/terrazzo chips).
MUTED = ['#8a6a5a', '#6a7a6a', '#5a6a7a', '#8a7a5a', '#7a5a6a', '#6a6a5a',
         '#9a8a7a', '#7a7a8a', '#8a7a4a', '#6a5a6a']


def _rc(pool):
    return _parse_hex(random.choice(pool))


# ─── CAMO ───────────────────────────────────────────────────────────────────
_CAMO_WAYS = {
    'woodland': ['#2f3618', '#3f4a2f', '#26261c', '#1c2410', '#12160c'],
    'desert':   ['#3a3320', '#4a4230', '#2a2416', '#2f2a1c', '#1e1a10'],
    'urban':    ['#2a2a2a', '#3f3f3f', '#4d4d4d', '#1a1a1a', '#5a5a5a'],
    'navy':     ['#12203a', '#22406e', '#0f1a30', '#1b2a4a', '#2a3f5f'],
    'mauve':    ['#3a2430', '#4a2f3f', '#2a1a24', '#1e1218', '#3f2434'],
}


def _camo(way):
    cols = _CAMO_WAYS[way]
    img = Image.new('RGB', (SIZE, SIZE), _parse_hex(cols[0]))
    d = ImageDraw.Draw(img)
    for c in cols[1:]:
        rgb = _parse_hex(c)
        for _ in range(random.randint(10, 16)):
            cx, cy = random.randint(0, SIZE), random.randint(0, SIZE)
            for _ in range(random.randint(5, 9)):
                ox = cx + random.randint(-130, 130)
                oy = cy + random.randint(-130, 130)
                rw, rh = random.randint(60, 170), random.randint(60, 170)
                d.ellipse([ox - rw, oy - rh, ox + rw, oy + rh], fill=rgb)
    img = img.filter(ImageFilter.GaussianBlur(2))
    return _save(img), f'camo-{way}'


# ─── ANIMAL PRINTS ────────────────────────────────────────────────────────
def _leopard():
    base = _jit(_parse_hex('#4a3820'), 8)
    img = Image.new('RGB', (SIZE, SIZE), base)
    d = ImageDraw.Draw(img)
    mid, dark = (108, 78, 34), (22, 15, 8)
    step = 108
    for gy in range(-1, SIZE // step + 2):
        for gx in range(-1, SIZE // step + 2):
            cx = gx * step + random.randint(-28, 28)
            cy = gy * step + (step // 2 if gx % 2 else 0) + random.randint(-28, 28)
            r = random.randint(20, 34)
            d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=mid)
            n = random.randint(5, 8)
            for k in range(n):
                a = 2 * math.pi * k / n + random.uniform(-0.25, 0.25)
                px = cx + math.cos(a) * (r + 5)
                py = cy + math.sin(a) * (r + 5)
                rr = random.randint(7, 13)
                d.ellipse([px - rr, py - rr, px + rr, py + rr], fill=dark)
    return _save(img), 'leopard'


def _cheetah():
    img = Image.new('RGB', (SIZE, SIZE), _jit(_parse_hex('#43331d'), 8))
    d = ImageDraw.Draw(img)
    dark = (20, 14, 7)
    for _ in range(320):
        x, y = random.randint(0, SIZE), random.randint(0, SIZE)
        r = random.randint(8, 20)
        d.ellipse([x - r, y - r, x + r, y + int(r * 1.2)], fill=dark)
    return _save(img), 'cheetah'


def _wavy_stripes(base, stripe, thin=False, orange=False):
    img = Image.new('RGB', (SIZE, SIZE), base)
    d = ImageDraw.Draw(img)
    x = -40
    while x < SIZE + 60:
        w = random.randint(14, 30) if thin else random.randint(30, 62)
        pts = []
        amp = random.randint(12, 26)
        for y in range(0, SIZE + 1, 50):
            off = math.sin(y / 95.0) * amp + random.randint(-6, 6)
            pts.append((x + off, y))
        for y in range(SIZE, -1, -50):
            off = math.sin(y / 95.0) * amp + random.randint(-6, 6)
            pts.append((x + w + off, y))
        d.polygon(pts, fill=stripe)
        x += w + (random.randint(16, 34) if thin else random.randint(30, 58))
    return img


def _zebra():
    img = _wavy_stripes((60, 60, 62), (16, 16, 16))
    return _save(img), 'zebra'


def _tiger():
    img = _wavy_stripes(_jit(_parse_hex('#6e3410'), 6), (16, 11, 6), thin=True)
    return _save(img), 'tiger'


def _blobs(base, blob, n=(6, 10)):
    img = Image.new('RGB', (SIZE, SIZE), base)
    d = ImageDraw.Draw(img)
    for _ in range(random.randint(*n)):
        cx, cy = random.randint(0, SIZE), random.randint(0, SIZE)
        for _ in range(random.randint(5, 9)):
            ox = cx + random.randint(-90, 90)
            oy = cy + random.randint(-90, 90)
            rw, rh = random.randint(45, 120), random.randint(45, 120)
            d.ellipse([ox - rw, oy - rh, ox + rw, oy + rh], fill=blob)
    return img


def _cow():
    img = _blobs((54, 54, 56), (16, 16, 16))
    return _save(img), 'cow'


def _dalmatian():
    img = Image.new('RGB', (SIZE, SIZE), (58, 58, 60))
    d = ImageDraw.Draw(img)
    for _ in range(180):
        x, y = random.randint(0, SIZE), random.randint(0, SIZE)
        r = random.randint(6, 18)
        d.ellipse([x - r, y - r, x + r, y + r], fill=(14, 14, 14))
    return _save(img), 'dalmatian'


def _snake():
    img = Image.new('RGB', (SIZE, SIZE), _parse_hex('#3e3524'))
    d = ImageDraw.Draw(img)
    tones = ['#2a1f10', '#1a1208', '#4a3f28', '#3a2f1a', '#0f0a05']
    s = 46
    for row, y in enumerate(range(-s, SIZE + s, s)):
        for col, x in enumerate(range(-s, SIZE + s, s)):
            xo = x + (s // 2 if row % 2 else 0)
            c = _jit(_parse_hex(random.choice(tones)), 8)
            d.polygon([(xo, y - s // 2), (xo + s // 2, y),
                       (xo, y + s // 2), (xo - s // 2, y)], fill=c)
    return _save(img), 'snake'


def _giraffe():
    img = Image.new('RGB', (SIZE, SIZE), _parse_hex('#5a4a2e'))
    d = ImageDraw.Draw(img)
    patch = _parse_hex('#2f2213')
    step = 150
    for gy in range(-1, SIZE // step + 2):
        for gx in range(-1, SIZE // step + 2):
            cx = gx * step + random.randint(-30, 30)
            cy = gy * step + random.randint(-30, 30)
            n = random.randint(5, 7)
            r = random.randint(55, 80)
            pts = []
            for k in range(n):
                a = 2 * math.pi * k / n + random.uniform(-0.2, 0.2)
                rr = r + random.randint(-18, 18)
                pts.append((cx + math.cos(a) * rr, cy + math.sin(a) * rr))
            d.polygon(pts, fill=patch)
    return _save(img), 'giraffe'


# ─── GEOMETRIC PRINTS ─────────────────────────────────────────────────────
def _stripes(orient):
    a, b = _rc(BRIGHTS), _rc(random.choice([DARKS, BRIGHTS, PASTELS]))
    w = random.randint(40, 90)
    if orient == 'diag':
        img = Image.new('RGB', (SIZE, SIZE), tuple(a))
        d = ImageDraw.Draw(img)
        step = w * 2
        for i in range(-SIZE, SIZE * 2, step):
            d.polygon([(i, 0), (i + w, 0), (i + w - SIZE, SIZE), (i - SIZE, SIZE)],
                      fill=tuple(b))
        return _save(img), 'stripes-diag'
    img = Image.new('RGB', (SIZE, SIZE), tuple(a))
    d = ImageDraw.Draw(img)
    for i, p in enumerate(range(0, SIZE, w)):
        if i % 2:
            if orient == 'v':
                d.rectangle([p, 0, p + w, SIZE], fill=tuple(b))
            else:
                d.rectangle([0, p, SIZE, p + w], fill=tuple(b))
    return _save(img), f'stripes-{orient}'


def _checkerboard():
    a, b = _rc(BRIGHTS), _rc(random.choice([DARKS, PASTELS]))
    n = random.choice([6, 8, 10])
    s = SIZE // n
    img = Image.new('RGB', (SIZE, SIZE), tuple(a))
    d = ImageDraw.Draw(img)
    for r in range(n + 1):
        for c in range(n + 1):
            if (r + c) % 2:
                d.rectangle([c * s, r * s, c * s + s, r * s + s], fill=tuple(b))
    return _save(img), 'checker'


def _gingham():
    col = _rc(MUTED)
    img = Image.new('RGBA', (SIZE, SIZE), (26, 26, 28, 255))
    d = ImageDraw.Draw(img, 'RGBA')
    w = random.randint(48, 80)
    for p in range(0, SIZE, w):
        d.rectangle([p, 0, p + w // 2, SIZE], fill=(col[0], col[1], col[2], 130))
        d.rectangle([0, p, SIZE, p + w // 2], fill=(col[0], col[1], col[2], 130))
    return _save(img.convert('RGB')), 'gingham'


def _plaid():
    img = Image.new('RGBA', (SIZE, SIZE), tuple(_rc(DARKS)) + (255,))
    d = ImageDraw.Draw(img, 'RGBA')
    cols = [tuple(_parse_hex(c)) for c in random.sample(BRIGHTS, 3)]
    for band in range(0, SIZE, 120):
        c = random.choice(cols)
        wdt = random.randint(20, 60)
        d.rectangle([band, 0, band + wdt, SIZE], fill=c + (90,))
        d.rectangle([0, band, SIZE, band + wdt], fill=c + (90,))
    for line in range(0, SIZE, 40):
        d.line([(line, 0), (line, SIZE)], fill=(255, 255, 255, 60), width=3)
        d.line([(0, line), (SIZE, line)], fill=(255, 255, 255, 60), width=3)
    return _save(img.convert('RGB')), 'plaid'


def _chevron():
    a, b = _rc(BRIGHTS), _rc(random.choice([DARKS, BRIGHTS]))
    img = Image.new('RGB', (SIZE, SIZE), tuple(a))
    d = ImageDraw.Draw(img)
    h = random.randint(60, 100)
    zig = h
    row = 0
    for y in range(-h, SIZE + h, h * 2):
        pts = []
        x = 0
        up = True
        while x <= SIZE + zig:
            pts.append((x, y + (0 if up else zig)))
            x += zig
            up = not up
        low = [(px, py + h) for px, py in reversed(pts)]
        d.polygon(pts + low, fill=tuple(b))
        row += 1
    return _save(img), 'chevron'


def _polka():
    a = _rc(random.choice([DARKS, BRIGHTS]))
    b = _rc(MUTED)                     # muted dots keep contrast on the dark base
    img = Image.new('RGB', (SIZE, SIZE), tuple(a))
    d = ImageDraw.Draw(img)
    step = random.choice([90, 120, 150])
    r = step // 4
    for row, y in enumerate(range(0, SIZE + step, step)):
        for x in range(0, SIZE + step, step):
            xo = x + (step // 2 if row % 2 else 0)
            d.ellipse([xo - r, y - r, xo + r, y + r], fill=tuple(b))
    return _save(img), 'polka'


def _argyle():
    img = Image.new('RGB', (SIZE, SIZE), tuple(_rc(DARKS)))
    d = ImageDraw.Draw(img)
    a, b = tuple(_rc(BRIGHTS)), tuple(_rc(BRIGHTS))
    s = 150
    for row, y in enumerate(range(0, SIZE + s, s)):
        for col, x in enumerate(range(0, SIZE + s, s)):
            c = a if (row + col) % 2 else b
            d.polygon([(x, y - s // 2), (x + s // 2, y),
                       (x, y + s // 2), (x - s // 2, y)], fill=c)
    for i in range(-SIZE, SIZE * 2, s):
        d.line([(i, 0), (i + SIZE, SIZE)], fill=(245, 245, 245), width=2)
        d.line([(i, SIZE), (i + SIZE, 0)], fill=(245, 245, 245), width=2)
    return _save(img), 'argyle'


def _triangles():
    img = Image.new('RGB', (SIZE, SIZE), (20, 20, 20))
    d = ImageDraw.Draw(img)
    pool = random.choice([BRIGHTS, PASTELS, EARTH])
    s = random.choice([120, 150, 180])
    for y in range(0, SIZE, s):
        for x in range(0, SIZE, s):
            c1, c2 = tuple(_rc(pool)), tuple(_rc(pool))
            if random.random() < 0.5:
                d.polygon([(x, y), (x + s, y), (x, y + s)], fill=c1)
                d.polygon([(x + s, y), (x + s, y + s), (x, y + s)], fill=c2)
            else:
                d.polygon([(x, y), (x + s, y), (x + s, y + s)], fill=c1)
                d.polygon([(x, y), (x, y + s), (x + s, y + s)], fill=c2)
    return _save(img), 'triangles'


def _hexagons():
    img = Image.new('RGB', (SIZE, SIZE), (25, 25, 30))
    d = ImageDraw.Draw(img)
    pool = random.choice([BRIGHTS, PASTELS, EARTH])
    r = random.choice([55, 70, 85])
    w = r * 2
    h = int(r * math.sqrt(3))
    for row, cy in enumerate(range(0, SIZE + h, h)):
        for cx in range(0, SIZE + w, int(w * 0.75)):
            xo = cx + (int(w * 0.375) if row % 2 else 0)
            pts = [(xo + r * math.cos(math.pi / 3 * k),
                    cy + r * math.sin(math.pi / 3 * k)) for k in range(6)]
            d.polygon(pts, fill=tuple(_jit(_rc(pool), 12)),
                      outline=(20, 20, 20))
    return _save(img), 'hexagons'


def _grid():
    img = _vgrad(random.choice(PASTELS + BRIGHTS), random.choice(PASTELS + BRIGHTS))
    d = ImageDraw.Draw(img)
    line = _rc(DARKS)
    step = random.choice([60, 80, 100])
    for p in range(0, SIZE, step):
        d.line([(p, 0), (p, SIZE)], fill=tuple(line), width=3)
        d.line([(0, p), (SIZE, p)], fill=tuple(line), width=3)
    return _save(img), 'grid'


def _concentric():
    a, b = _rc(BRIGHTS), _rc(random.choice([DARKS, BRIGHTS]))
    img = Image.new('RGB', (SIZE, SIZE), tuple(a))
    d = ImageDraw.Draw(img)
    cx, cy = random.randint(300, 780), random.randint(300, 780)
    step = random.choice([40, 55, 70])
    r = 1500
    i = 0
    while r > 0:
        d.ellipse([cx - r, cy - r, cx + r, cy + r],
                  fill=tuple(b) if i % 2 else tuple(a))
        r -= step
        i += 1
    return _save(img), 'concentric'


def _terrazzo():
    img = Image.new('RGB', (SIZE, SIZE), _jit(_parse_hex('#26262a'), 6))
    d = ImageDraw.Draw(img)
    pool = BRIGHTS + EARTH + MUTED + MUTED   # muted chips so they read on dark
    for _ in range(420):
        x, y = random.randint(0, SIZE), random.randint(0, SIZE)
        r = random.randint(6, 20)
        c = tuple(_rc(pool))
        k = random.random()
        if k < 0.5:
            d.ellipse([x - r, y - r, x + r, y + r], fill=c)
        else:
            pts = [(x + random.randint(-r, r), y + random.randint(-r, r))
                   for _ in range(random.randint(3, 5))]
            d.polygon(pts, fill=c)
    return _save(img), 'terrazzo'


def _confetti():
    img = Image.new('RGB', (SIZE, SIZE), tuple(_rc(['#141414', '#1a1a1a', '#0f1420'])))
    d = ImageDraw.Draw(img)
    for _ in range(260):
        x, y = random.randint(0, SIZE), random.randint(0, SIZE)
        c = tuple(_rc(BRIGHTS + MUTED))
        s = random.randint(8, 22)
        shape = random.random()
        if shape < 0.4:
            d.ellipse([x, y, x + s, y + s], fill=c)
        elif shape < 0.7:
            d.rectangle([x, y, x + s, y + s // 2], fill=c)
        else:
            d.line([(x, y), (x + s, y + s)], fill=c, width=4)
    return _save(img), 'confetti'


def _waves():
    cols = [tuple(_parse_hex(c)) for c in random.sample(BRIGHTS, random.randint(4, 6))]
    img = Image.new('RGB', (SIZE, SIZE), cols[0])
    d = ImageDraw.Draw(img)
    band = SIZE // len(cols) + 20
    amp = random.randint(30, 70)
    for i, c in enumerate(cols):
        y0 = i * band
        pts = [(0, SIZE + 50)]
        for x in range(0, SIZE + 20, 20):
            pts.append((x, y0 + math.sin(x / 120.0 + i) * amp))
        pts += [(SIZE, SIZE + 50)]
        d.polygon(pts, fill=c)
    return _save(img), 'waves'


def _diagonal_blocks():
    cols = [tuple(_parse_hex(c)) for c in random.sample(BRIGHTS + EARTH, 5)]
    img = Image.new('RGB', (SIZE, SIZE), cols[0])
    d = ImageDraw.Draw(img)
    w = random.randint(120, 200)
    for i, x in enumerate(range(-SIZE, SIZE * 2, w)):
        c = cols[i % len(cols)]
        d.polygon([(x, 0), (x + w, 0), (x + w - SIZE, SIZE), (x - SIZE, SIZE)], fill=c)
    return _save(img), 'diag-blocks'


def _halftone():
    a = _rc(BRIGHTS)
    img = Image.new('RGB', (SIZE, SIZE), tuple(a))
    d = ImageDraw.Draw(img)
    dot = tuple(_rc(random.choice([DARKS, BRIGHTS])))
    step = 42
    for row, y in enumerate(range(0, SIZE + step, step)):
        for x in range(0, SIZE + step, step):
            r = int((y / SIZE) * (step * 0.55)) + 2
            d.ellipse([x - r, y - r, x + r, y + r], fill=dot)
    return _save(img), 'halftone'


# ─── TEXTURES ─────────────────────────────────────────────────────────────
def _marble():
    S = 256
    n = Image.new('L', (S, S))
    n.putdata([random.randint(0, 255) for _ in range(S * S)])
    n = n.filter(ImageFilter.GaussianBlur(5))
    # lighter vein tone vs near-black base → visible dark-marble veining
    c1 = _parse_hex(random.choice(['#5a5a66', '#544a40', '#465565', '#4e4656']))
    c2 = _parse_hex(random.choice(['#0e0e12', '#12203a', '#241a2e', '#141018']))
    out = Image.new('RGB', (S, S))
    op = out.load()
    npx = n.load()
    for y in range(S):
        for x in range(S):
            v = npx[x, y] / 255.0
            t = (math.sin(v * math.pi * 5) + 1) / 2
            op[x, y] = tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3))
    out = out.resize((SIZE, SIZE), Image.BICUBIC)
    return _save(out), 'marble'


def _tie_dye():
    S = 340
    cols = [_parse_hex(c) for c in random.sample(BRIGHTS, 5)]
    img = Image.new('RGB', (S, S))
    px = img.load()
    cx, cy = S / 2, S / 2
    for y in range(S):
        for x in range(S):
            dx, dy = x - cx, y - cy
            r = math.hypot(dx, dy)
            a = math.atan2(dy, dx)
            idx = int(r * 0.06 + a * 1.6) % len(cols)
            px[x, y] = tuple(cols[idx])
    img = img.filter(ImageFilter.GaussianBlur(2)).resize((SIZE, SIZE))
    return _save(img), 'tie-dye'


def _noise():
    S = 216
    base = _rc(random.choice([BRIGHTS, PASTELS, EARTH]))
    data = [tuple(max(0, min(255, base[i] + random.randint(-42, 42))) for i in range(3))
            for _ in range(S * S)]
    img = Image.new('RGB', (S, S))
    img.putdata(data)
    return _save(img.resize((SIZE, SIZE), Image.NEAREST)), 'noise'


def _bokeh():
    base = _vgrad(random.choice(DARKS), random.choice(BRIGHTS))
    overlay = Image.new('RGBA', (SIZE, SIZE), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay, 'RGBA')
    for _ in range(70):
        r = random.randint(20, 95)
        x, y = random.randint(0, SIZE), random.randint(0, SIZE)
        c = _rc(BRIGHTS)
        od.ellipse([x - r, y - r, x + r, y + r],
                   fill=(c[0], c[1], c[2], random.randint(30, 85)))
    overlay = overlay.filter(ImageFilter.GaussianBlur(7))
    out = Image.alpha_composite(base.convert('RGBA'), overlay).convert('RGB')
    return _save(out), 'bokeh'


def _ombre():
    stops = [random.choice(BRIGHTS + PASTELS) for _ in range(random.randint(3, 4))]
    seg = SIZE // (len(stops) - 1)
    parts = []
    for i in range(len(stops) - 1):
        parts.append(_vgrad(stops[i], stops[i + 1], size=seg).crop((0, 0, SIZE, seg)))
    img = Image.new('RGB', (SIZE, SIZE))
    for i, p in enumerate(parts):
        img.paste(p, (0, i * seg))
    return _save(img), 'ombre'


def _tricolor():
    cols = [tuple(_parse_hex(c)) for c in random.sample(BRIGHTS + EARTH, 3)]
    img = Image.new('RGB', (SIZE, SIZE), cols[0])
    d = ImageDraw.Draw(img)
    b = SIZE // 3
    for i, c in enumerate(cols):
        d.rectangle([0, i * b, SIZE, (i + 1) * b], fill=c)
    return _save(img), 'tricolor'


# ─── registry ──────────────────────────────────────────────────────────────
# Each entry: (key, label, category, fn). fn() -> (png_bytes, info_str).
NEW_PATTERNS = [
    # camo
    ('camo_woodland', '🪖 Camo — woodland', 'camo', lambda: _camo('woodland')),
    ('camo_desert',   '🏜 Camo — desert',   'camo', lambda: _camo('desert')),
    ('camo_urban',    '🏙 Camo — urban',    'camo', lambda: _camo('urban')),
    ('camo_navy',     '🌊 Camo — navy',     'camo', lambda: _camo('navy')),
    ('camo_mauve',    '🖤 Camo — dark mauve', 'camo', lambda: _camo('mauve')),
    # animal
    ('leopard',   '🐆 Leopard',   'animal', _leopard),
    ('cheetah',   '🐆 Cheetah',   'animal', _cheetah),
    ('zebra',     '🦓 Zebra',     'animal', _zebra),
    ('tiger',     '🐯 Tiger',     'animal', _tiger),
    ('cow',       '🐄 Cow',       'animal', _cow),
    ('dalmatian', '🐶 Dalmatian', 'animal', _dalmatian),
    ('snake',     '🐍 Snake',     'animal', _snake),
    ('giraffe',   '🦒 Giraffe',   'animal', _giraffe),
    # geometric prints
    ('stripes_v',    '📏 Stripes — vertical',   'pattern', lambda: _stripes('v')),
    ('stripes_h',    '📏 Stripes — horizontal', 'pattern', lambda: _stripes('h')),
    ('stripes_diag', '📐 Stripes — diagonal',   'pattern', lambda: _stripes('diag')),
    ('checker',      '🏁 Checkerboard',         'pattern', _checkerboard),
    ('gingham',      '🧇 Gingham',              'pattern', _gingham),
    ('plaid',        '🧣 Plaid / tartan',       'pattern', _plaid),
    ('chevron',      '📶 Chevron',              'pattern', _chevron),
    ('polka',        '⚫️ Polka dots',           'pattern', _polka),
    ('argyle',       '💠 Argyle',               'pattern', _argyle),
    ('triangles',    '🔺 Triangles (low-poly)', 'pattern', _triangles),
    ('hexagons',     '⬡ Hexagons',             'pattern', _hexagons),
    ('grid',         '🔲 Grid',                 'pattern', _grid),
    ('concentric',   '🎯 Concentric rings',     'pattern', _concentric),
    ('terrazzo',     '🧱 Terrazzo',             'pattern', _terrazzo),
    ('confetti',     '🎉 Confetti',             'pattern', _confetti),
    ('waves',        '🌊 Waves',                'pattern', _waves),
    ('diag_blocks',  '🚧 Diagonal blocks',      'pattern', _diagonal_blocks),
    ('halftone',     '⚙️ Halftone dots',        'pattern', _halftone),
    # textures / colors
    ('marble',   '🏛 Marble',   'texture', _marble),
    ('tie_dye',  '🌀 Tie-dye',  'texture', _tie_dye),
    ('noise',    '📺 Grain / noise', 'texture', _noise),
    ('bokeh',    '🔮 Bokeh',    'texture', _bokeh),
    ('ombre',    '🎨 Ombré',    'color', _ombre),
    ('tricolor', '🚩 Tricolor blocks', 'color', _tricolor),
]
