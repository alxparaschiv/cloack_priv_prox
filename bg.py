"""
/bg_generator — profile/background PNG generator.

Nine modes — three flat/simple + six artistic abstractions:
  • solid          — single flat palette color
  • gradient       — linear fade between two palette colors
  • radial         — bright center, darker edges
  • impressionist  — Monet-like dense colored brush strokes + soft blur
  • splatter       — Pollock-style splatter circles + drip lines
  • watercolor     — large translucent blobs, heavy Gaussian bleed
  • geometric      — Mondrian-style rectangles + thick black borders
  • voronoi        — organic mosaic cells from random seed points
  • color_field    — Rothko-style horizontal bands with soft edges

Every output is jittered ±12 RGB per channel so two pulls of the same
palette are never pixel-identical (anti-image-hash clustering — defeats
Meta's ability to correlate accounts by profile-picture binary).

Mirrors reel-bot-Carolina's bg helpers per [[patch-both-repos-together]].
"""

import io
import math
import random
import logging

from PIL import Image, ImageDraw, ImageFilter
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


PROFILE_COLOR_PALETTE = [
    ('🩷 Pink',      '#ff5fa2'),
    ('💜 Purple',    '#9b59b6'),
    ('💙 Blue',      '#3498db'),
    ('💚 Green',     '#2ecc71'),
    ('🧡 Orange',    '#ff8c42'),
    ('❤️ Red',        '#e74c3c'),
    ('🤎 Brown',     '#8b5a2b'),
    ('⚫️ Charcoal',  '#2c3e50'),
    ('⚪️ White',     '#f5f5f5'),
    ('🩶 Gray',      '#808080'),
]


# Mode keys + emoji labels — used by the picker UI + emit captions.
MODES_ALL = [
    ('solid',         '🟦 Solid color'),
    ('gradient',      '🌈 Gradient (two-color fade)'),
    ('radial',        '✨ Radial burst (light center)'),
    ('impressionist', '🌻 Impressionist (Monet brush strokes)'),
    ('splatter',      '🎨 Splatter (Pollock drips)'),
    ('watercolor',    '💧 Watercolor (soft bleeds)'),
    ('geometric',     '🟥 Geometric (Mondrian blocks)'),
    ('voronoi',       '🔷 Voronoi mosaic'),
    ('color_field',   '🟫 Color field (Rothko bands)'),
]


def _parse_hex(h):
    h = (h or '').strip().lstrip('#')
    if len(h) == 3: h = ''.join(c * 2 for c in h)
    if len(h) != 6: h = '808080'
    try: return [int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)]
    except ValueError: return [128, 128, 128]


def _jitter(rgb, amount=12):
    return tuple(max(0, min(255, c + random.randint(-amount, amount)))
                  for c in rgb)


def _hex(rgb):
    return '#{:02x}{:02x}{:02x}'.format(*rgb)


def _palette_hexes(n, exclude_hex=None):
    """Pick `n` distinct hexes from PROFILE_COLOR_PALETTE (without replacement
    until exhausted, then with). Optionally exclude one."""
    pool = [h for _, h in PROFILE_COLOR_PALETTE]
    if exclude_hex:
        pool = [h for h in pool if h != exclude_hex]
    if n <= len(pool):
        return random.sample(pool, n)
    out = list(pool)
    while len(out) < n:
        out.append(random.choice(pool))
    return out


# ─── Original three modes ─────────────────────────────────────────────

def solid_color_png(hex_color, size=1080, jitter=12):
    """Solid PNG with per-channel jitter."""
    rgb = _jitter(_parse_hex(hex_color), jitter)
    img = Image.new('RGB', (size, size), rgb)
    buf = io.BytesIO()
    img.save(buf, format='PNG', optimize=True)
    return buf.getvalue(), _hex(rgb)


def gradient_png(hex_a, hex_b, size=1080, jitter=12, direction='vertical'):
    """Linear gradient PNG between two hex colors with per-stop jitter."""
    aj = _jitter(_parse_hex(hex_a), jitter)
    bj = _jitter(_parse_hex(hex_b), jitter)
    img = Image.new('RGB', (size, size), aj)
    px = img.load()
    if direction == 'horizontal':
        for x in range(size):
            t = x / max(1, size - 1)
            rgb = tuple(int(aj[i] + (bj[i] - aj[i]) * t) for i in range(3))
            for y in range(size):
                px[x, y] = rgb
    elif direction == 'diagonal':
        maxd = (size - 1) * 2
        for x in range(size):
            for y in range(size):
                t = (x + y) / maxd
                rgb = tuple(int(aj[i] + (bj[i] - aj[i]) * t) for i in range(3))
                px[x, y] = rgb
    else:  # vertical
        for y in range(size):
            t = y / max(1, size - 1)
            rgb = tuple(int(aj[i] + (bj[i] - aj[i]) * t) for i in range(3))
            for x in range(size):
                px[x, y] = rgb
    buf = io.BytesIO()
    img.save(buf, format='PNG', optimize=True)
    return buf.getvalue(), '{}→{}'.format(_hex(aj), _hex(bj))


def radial_burst_png(hex_color, size=1080, jitter=12, brightness_boost=80):
    """Radial-burst PNG: bright center → darker edges."""
    bj = list(_jitter(_parse_hex(hex_color), jitter))
    center = [min(255, c + brightness_boost) for c in bj]
    cx, cy = (size - 1) / 2, (size - 1) / 2
    max_d = math.sqrt(cx * cx + cy * cy)
    img = Image.new('RGB', (size, size), tuple(bj))
    px = img.load()
    for x in range(size):
        for y in range(size):
            dx, dy = x - cx, y - cy
            d = math.sqrt(dx * dx + dy * dy) / max_d
            rgb = tuple(int(center[i] + (bj[i] - center[i]) * d) for i in range(3))
            px[x, y] = rgb
    buf = io.BytesIO()
    img.save(buf, format='PNG', optimize=True)
    return buf.getvalue(), _hex(bj)


# ─── New artistic modes (2026-05-21) ──────────────────────────────────

def impressionist_png(hex_dominant, size=1080):
    """Monet/Renoir-style dense colored brush strokes + soft Gaussian blur.

    Picks 4-7 palette hues around the dominant color (or random palette
    sample if dominant is None), splashes 700-1400 small filled ellipses,
    blurs slightly to fuse strokes into impressionist brushwork."""
    bg = _jitter(_parse_hex(hex_dominant), 18)
    img = Image.new('RGB', (size, size), bg)
    draw = ImageDraw.Draw(img, 'RGBA')
    n_hues = random.randint(4, 7)
    palette = _palette_hexes(n_hues, exclude_hex=hex_dominant)
    palette.append(hex_dominant)  # dominant stays
    n_strokes = random.randint(700, 1400)
    for _ in range(n_strokes):
        cx = random.randint(-40, size + 40)
        cy = random.randint(-40, size + 40)
        rx = random.randint(14, 65)
        ry = random.randint(4, 22)
        c = _jitter(_parse_hex(random.choice(palette)), 35)
        alpha = random.randint(110, 220)
        draw.ellipse([cx - rx, cy - ry, cx + rx, cy + ry],
                      fill=c + (alpha,))
    img = img.filter(ImageFilter.GaussianBlur(
        radius=random.uniform(1.0, 2.6)))
    buf = io.BytesIO()
    img.save(buf, format='PNG', optimize=True)
    return buf.getvalue(), _hex(bg)


def splatter_png(hex_dominant, size=1080):
    """Pollock-inspired splatter: random circles in 4-6 palette colors +
    thin drip lines. Slight blur to soften the harshest edges."""
    bg = _jitter(_parse_hex(hex_dominant), 12)
    # Off-white-ish backgrounds look most Pollock-y, but allow dominant
    # to win sometimes
    if random.random() < 0.55:
        bg = _jitter((random.randint(225, 250),) * 3, 8)
    img = Image.new('RGB', (size, size), bg)
    draw = ImageDraw.Draw(img, 'RGBA')
    n_hues = random.randint(4, 6)
    palette = _palette_hexes(n_hues, exclude_hex=None)
    # Drip lines
    n_lines = random.randint(20, 50)
    for _ in range(n_lines):
        x1 = random.randint(-50, size + 50)
        y1 = random.randint(-50, size + 50)
        x2 = x1 + random.randint(-300, 300)
        y2 = y1 + random.randint(-300, 300)
        w = random.randint(1, 5)
        c = _jitter(_parse_hex(random.choice(palette)), 25)
        draw.line([(x1, y1), (x2, y2)], fill=c + (random.randint(150, 230),),
                   width=w)
    # Splatter blobs
    n_blobs = random.randint(80, 200)
    for _ in range(n_blobs):
        cx = random.randint(-30, size + 30)
        cy = random.randint(-30, size + 30)
        r = random.randint(4, 45)
        c = _jitter(_parse_hex(random.choice(palette)), 30)
        a = random.randint(170, 240)
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=c + (a,))
    # Tiny droplet noise
    n_tiny = random.randint(150, 400)
    for _ in range(n_tiny):
        cx = random.randint(0, size - 1); cy = random.randint(0, size - 1)
        r = random.randint(1, 4)
        c = _jitter(_parse_hex(random.choice(palette)), 30)
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=c + (240,))
    img = img.filter(ImageFilter.GaussianBlur(radius=0.6))
    buf = io.BytesIO()
    img.save(buf, format='PNG', optimize=True)
    return buf.getvalue(), _hex(bg)


def watercolor_png(hex_dominant, size=1080):
    """Soft translucent blobs in 3-6 palette colors + heavy Gaussian blur
    so colors bleed into each other watercolor-style."""
    bg_base = _parse_hex(hex_dominant)
    # Lighten background — watercolor is on cream paper
    bg = tuple(min(255, c + random.randint(40, 90)) for c in bg_base)
    img = Image.new('RGB', (size, size), bg)
    draw = ImageDraw.Draw(img, 'RGBA')
    n_hues = random.randint(3, 6)
    palette = _palette_hexes(n_hues, exclude_hex=None)
    palette.insert(0, hex_dominant)
    n_blobs = random.randint(7, 18)
    for _ in range(n_blobs):
        cx = random.randint(-100, size + 100)
        cy = random.randint(-100, size + 100)
        rx = random.randint(150, 450)
        ry = random.randint(150, 450)
        c = _jitter(_parse_hex(random.choice(palette)), 25)
        a = random.randint(70, 170)
        draw.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], fill=c + (a,))
    blur_radius = random.randint(30, 70)
    img = img.filter(ImageFilter.GaussianBlur(radius=blur_radius))
    buf = io.BytesIO()
    img.save(buf, format='PNG', optimize=True)
    return buf.getvalue(), _hex(bg)


def geometric_png(hex_dominant, size=1080):
    """Mondrian-style geometric: divide canvas with thick black lines,
    fill some rectangles with primary colors + leave others off-white."""
    img = Image.new('RGB', (size, size), _jitter((245, 245, 235), 8))
    draw = ImageDraw.Draw(img)
    # Choose 2-4 vertical + 2-4 horizontal split lines
    n_v = random.randint(2, 4)
    n_h = random.randint(2, 4)
    v_lines = sorted([random.randint(int(size * 0.15), int(size * 0.85))
                       for _ in range(n_v)])
    h_lines = sorted([random.randint(int(size * 0.15), int(size * 0.85))
                       for _ in range(n_h)])
    v_lines = [0] + v_lines + [size]
    h_lines = [0] + h_lines + [size]
    # Mondrian-ish primary palette + dominant
    primaries = ['#e63946', '#ffd60a', '#1d3557', '#f5f5f5', hex_dominant,
                  '#000000']
    rect_count = 0
    for i in range(len(v_lines) - 1):
        for j in range(len(h_lines) - 1):
            x0, x1 = v_lines[i], v_lines[i + 1]
            y0, y1 = h_lines[j], h_lines[j + 1]
            # ~40% chance to fill with a color, else leave background
            if random.random() < 0.45:
                fill = _jitter(_parse_hex(random.choice(primaries)), 15)
                draw.rectangle([x0, y0, x1, y1], fill=fill)
                rect_count += 1
    # Thick black grid lines
    line_w = random.randint(14, 26)
    for v in v_lines[1:-1]:
        draw.rectangle([v - line_w // 2, 0, v + line_w // 2, size],
                        fill=(0, 0, 0))
    for h in h_lines[1:-1]:
        draw.rectangle([0, h - line_w // 2, size, h + line_w // 2],
                        fill=(0, 0, 0))
    # Outer border
    draw.rectangle([0, 0, size - 1, size - 1], outline=(0, 0, 0),
                    width=line_w)
    buf = io.BytesIO()
    img.save(buf, format='PNG', optimize=True)
    return buf.getvalue(), f'mondrian-{rect_count}'


def voronoi_png(hex_dominant, size=1080):
    """Voronoi cell mosaic. Seeded with random points, each pixel takes
    color of nearest seed. Computed at low-res then upscaled for speed."""
    n_seeds = random.randint(25, 80)
    seeds = []
    n_hues = random.randint(4, 7)
    palette = _palette_hexes(n_hues, exclude_hex=hex_dominant)
    palette.append(hex_dominant)
    low_res = 192  # 192x192 lookup grid → upscaled to 1080
    for _ in range(n_seeds):
        sx = random.randint(0, low_res - 1)
        sy = random.randint(0, low_res - 1)
        color = _jitter(_parse_hex(random.choice(palette)), 18)
        seeds.append((sx, sy, color))
    small = Image.new('RGB', (low_res, low_res))
    px = small.load()
    for x in range(low_res):
        for y in range(low_res):
            best_d2 = None; best_c = (128, 128, 128)
            for sx, sy, c in seeds:
                dx, dy = sx - x, sy - y
                d2 = dx * dx + dy * dy
                if best_d2 is None or d2 < best_d2:
                    best_d2 = d2; best_c = c
            px[x, y] = best_c
    img = small.resize((size, size), Image.NEAREST)
    # Light blur smooths the cell boundaries — stained-glass effect
    if random.random() < 0.6:
        img = img.filter(ImageFilter.GaussianBlur(radius=random.uniform(1.5, 4)))
    buf = io.BytesIO()
    img.save(buf, format='PNG', optimize=True)
    return buf.getvalue(), f'voronoi-{n_seeds}'


def color_field_png(hex_dominant, size=1080):
    """Rothko-style color field: 2-4 horizontal bands of varying heights,
    each its own palette color, separated by very soft Gaussian-blurred
    edges → signature soft-glow Rothko look."""
    n_bands = random.randint(2, 4)
    n_hues = max(n_bands, random.randint(n_bands, n_bands + 2))
    palette = _palette_hexes(n_hues, exclude_hex=None)
    palette[0] = hex_dominant  # dominant is one of the bands
    random.shuffle(palette)
    # Pick band heights (proportions sum to 1)
    cuts = sorted(random.sample(range(int(size * 0.1), int(size * 0.9)),
                                  n_bands - 1)) if n_bands > 1 else []
    cuts = [0] + cuts + [size]
    img = Image.new('RGB', (size, size), (0, 0, 0))
    draw = ImageDraw.Draw(img)
    for i in range(n_bands):
        y0, y1 = cuts[i], cuts[i + 1]
        c = _jitter(_parse_hex(palette[i % len(palette)]), 12)
        draw.rectangle([0, y0, size, y1], fill=c)
    # Heavy blur softens the band edges into the Rothko glow
    img = img.filter(ImageFilter.GaussianBlur(
        radius=random.randint(40, 90)))
    buf = io.BytesIO()
    img.save(buf, format='PNG', optimize=True)
    return buf.getvalue(), f'rothko-{n_bands}b'


# ─── UI helpers ───────────────────────────────────────────────────────

def _emit_kb(mode):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🎲 Generate another ({mode})",
                              callback_data=f"bg_gen:random:{mode}")],
        [InlineKeyboardButton("🎨 Pick specific color",
                              callback_data=f"bg_gen:pick:{mode}")],
        [InlineKeyboardButton("⬅ Change style",
                              callback_data="bg_gen:menu")],
    ])


def _mode_menu_kb():
    rows = []
    for key, label in MODES_ALL:
        rows.append([InlineKeyboardButton(label,
            callback_data=f"bg_gen:mode:{key}")])
    return InlineKeyboardMarkup(rows)


async def _emit(target, mode='solid', hex_color=None, hex_color_b=None,
                  label=None):
    if hex_color is None or label is None:
        label, hex_color = random.choice(PROFILE_COLOR_PALETTE)
    if mode == 'gradient':
        if hex_color_b is None:
            choices = [h for l, h in PROFILE_COLOR_PALETTE if h != hex_color]
            hex_color_b = random.choice(choices) if choices else hex_color
        direction = random.choice(['vertical', 'horizontal', 'diagonal'])
        png, actual_label = gradient_png(hex_color, hex_color_b,
                                          direction=direction)
        caption = (f"🌈 <b>Gradient background</b>\n\n"
                   f"Stops: <code>{hex_color}</code> → <code>{hex_color_b}</code>\n"
                   f"Direction: <b>{direction}</b>\n"
                   f"Post-jitter: <code>{actual_label}</code>\n"
                   f"Size: 1080×1080 PNG")
        fname = 'bg_gradient.png'
    elif mode == 'radial':
        png, actual_hex = radial_burst_png(hex_color)
        caption = (f"✨ <b>Radial-burst background</b>\n\n"
                   f"Palette: <b>{label}</b>\n"
                   f"Base hex: <code>{hex_color}</code>\n"
                   f"Edge hex (post-jitter): <code>{actual_hex}</code>\n"
                   f"Center: lighter by +80 per channel\n"
                   f"Size: 1080×1080 PNG")
        fname = f'bg_radial_{actual_hex.lstrip("#")}.png'
    elif mode == 'impressionist':
        png, actual_hex = impressionist_png(hex_color)
        caption = (f"🌻 <b>Impressionist background</b>\n\n"
                   f"Style: <b>Monet-like dense brush strokes</b>\n"
                   f"Dominant: <b>{label}</b> (<code>{hex_color}</code>)\n"
                   f"Background post-jitter: <code>{actual_hex}</code>\n"
                   f"Size: 1080×1080 PNG")
        fname = f'bg_impressionist_{actual_hex.lstrip("#")}.png'
    elif mode == 'splatter':
        png, actual_hex = splatter_png(hex_color)
        caption = (f"🎨 <b>Splatter background</b>\n\n"
                   f"Style: <b>Pollock-style splatter + drip</b>\n"
                   f"Dominant accent: <b>{label}</b> (<code>{hex_color}</code>)\n"
                   f"Background post-jitter: <code>{actual_hex}</code>\n"
                   f"Size: 1080×1080 PNG")
        fname = f'bg_splatter_{actual_hex.lstrip("#")}.png'
    elif mode == 'watercolor':
        png, actual_hex = watercolor_png(hex_color)
        caption = (f"💧 <b>Watercolor background</b>\n\n"
                   f"Style: <b>soft translucent bleeds</b>\n"
                   f"Dominant: <b>{label}</b> (<code>{hex_color}</code>)\n"
                   f"Background post-jitter: <code>{actual_hex}</code>\n"
                   f"Size: 1080×1080 PNG")
        fname = f'bg_watercolor_{actual_hex.lstrip("#")}.png'
    elif mode == 'geometric':
        png, actual_label = geometric_png(hex_color)
        caption = (f"🟥 <b>Geometric background</b>\n\n"
                   f"Style: <b>Mondrian-style blocks + grid</b>\n"
                   f"Accent: <b>{label}</b> (<code>{hex_color}</code>)\n"
                   f"Layout: <code>{actual_label}</code>\n"
                   f"Size: 1080×1080 PNG")
        fname = f'bg_geometric_{actual_label}.png'
    elif mode == 'voronoi':
        png, actual_label = voronoi_png(hex_color)
        caption = (f"🔷 <b>Voronoi mosaic background</b>\n\n"
                   f"Style: <b>organic cell mosaic</b>\n"
                   f"Dominant accent: <b>{label}</b> (<code>{hex_color}</code>)\n"
                   f"Cells: <code>{actual_label}</code>\n"
                   f"Size: 1080×1080 PNG")
        fname = f'bg_voronoi_{actual_label}.png'
    elif mode == 'color_field':
        png, actual_label = color_field_png(hex_color)
        caption = (f"🟫 <b>Color-field background</b>\n\n"
                   f"Style: <b>Rothko-style soft bands</b>\n"
                   f"Dominant: <b>{label}</b> (<code>{hex_color}</code>)\n"
                   f"Bands: <code>{actual_label}</code>\n"
                   f"Size: 1080×1080 PNG")
        fname = f'bg_colorfield_{actual_label}.png'
    else:  # solid
        png, actual_hex = solid_color_png(hex_color)
        caption = (f"🟦 <b>Solid background</b>\n\n"
                   f"Palette: <b>{label}</b>\n"
                   f"Base hex: <code>{hex_color}</code>\n"
                   f"Post-jitter: <code>{actual_hex}</code>\n"
                   f"Size: 1080×1080 PNG")
        fname = f'bg_solid_{actual_hex.lstrip("#")}.png'
    bio = io.BytesIO(png); bio.name = fname
    await target.reply_photo(photo=bio, caption=caption,
                              parse_mode='HTML', reply_markup=_emit_kb(mode))


async def bg_generator_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/bg_generator — pick a style then generate. Direct shortcut:
    /bg_generator <mode>  (mode is any of MODES_ALL keys)."""
    arg = (context.args[0].lower() if context.args else '').strip()
    valid_modes = {k for k, _ in MODES_ALL}
    if arg in valid_modes:
        await _emit(update.message, mode=arg)
        return
    body = ("🎨 <b>Background generator</b>\n\n"
            "Pick a style — each generation is jittered ±12 RGB so two "
            "pulls of the same palette are never pixel-identical.\n\n"
            "<b>Flat / simple</b>\n"
            "🟦 Solid — single color\n"
            "🌈 Gradient — two-color fade\n"
            "✨ Radial — bright center → dark edges\n\n"
            "<b>Artistic abstractions</b>\n"
            "🌻 Impressionist — Monet brush strokes\n"
            "🎨 Splatter — Pollock drip + dots\n"
            "💧 Watercolor — soft translucent bleeds\n"
            "🟥 Geometric — Mondrian-style blocks\n"
            "🔷 Voronoi — organic cell mosaic\n"
            "🟫 Color field — Rothko-style bands\n\n"
            "<i>Or run </i><code>/bg_generator &lt;mode&gt;</code><i> directly.</i>")
    await update.message.reply_text(body, parse_mode='HTML',
                                     reply_markup=_mode_menu_kb())


async def bg_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle bg_gen:* inline buttons."""
    query = update.callback_query
    await query.answer()
    data = query.data or ''

    if data == 'bg_gen:menu':
        await query.message.reply_text(
            "🎨 <b>Pick a background style:</b>",
            parse_mode='HTML', reply_markup=_mode_menu_kb())
        return

    if data.startswith('bg_gen:mode:'):
        mode = data.split(':', 2)[2]
        await _emit(query.message, mode=mode)
        return

    if data.startswith('bg_gen:random:'):
        mode = data.split(':', 2)[2]
        await _emit(query.message, mode=mode)
        return

    if data.startswith('bg_gen:pick:'):
        mode = data.split(':', 2)[2]
        rows, row = [], []
        for label, hex_color in PROFILE_COLOR_PALETTE:
            row.append(InlineKeyboardButton(
                label, callback_data=f"bg_gen:hex:{mode}:{hex_color}"))
            if len(row) == 2:
                rows.append(row); row = []
        if row:
            rows.append(row)
        rows.append([InlineKeyboardButton("⬅ Change style",
                                          callback_data="bg_gen:menu")])
        await query.message.reply_text(
            f"🎨 <b>Pick a palette color (<i>{mode}</i> mode)</b>",
            parse_mode='HTML', reply_markup=InlineKeyboardMarkup(rows))
        return

    if data.startswith('bg_gen:hex:'):
        rest = data.split(':', 2)[2]
        if ':' in rest:
            mode, hex_color = rest.split(':', 1)
        else:
            mode, hex_color = 'solid', rest  # legacy
        label = next((lbl for lbl, hx in PROFILE_COLOR_PALETTE
                      if hx == hex_color), '(custom)')
        await _emit(query.message, mode=mode, hex_color=hex_color, label=label)
        return

    # Legacy callbacks (old buttons in chat history)
    if data == 'bg_gen:random':
        await _emit(query.message, mode='solid'); return
    if data == 'bg_gen:pick':
        rows, row = [], []
        for label, hex_color in PROFILE_COLOR_PALETTE:
            row.append(InlineKeyboardButton(
                label, callback_data=f"bg_gen:hex:solid:{hex_color}"))
            if len(row) == 2:
                rows.append(row); row = []
        if row:
            rows.append(row)
        rows.append([InlineKeyboardButton("⬅ Change style",
                                          callback_data="bg_gen:menu")])
        await query.message.reply_text(
            "🎨 <b>Pick a palette color</b>",
            parse_mode='HTML', reply_markup=InlineKeyboardMarkup(rows))
        return
