"""
/bg_generator — profile/background PNG generator with three modes:
  • solid    — single flat palette color
  • gradient — linear fade between two palette colors
  • radial   — bright center, darker edges

Each output is jittered ±12 RGB per channel so two pulls of the same
palette are never pixel-identical (anti-image-hash clustering —
defeats Meta's ability to correlate accounts by profile-picture binary).

Mirrors reel-bot-Carolina's _solid_color_png / _gradient_png /
_radial_burst_png implementations per [[patch-both-repos-together]].
"""

import io
import math
import random
import logging

from PIL import Image
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


def _parse_hex(h):
    h = (h or '').strip().lstrip('#')
    if len(h) == 3: h = ''.join(c * 2 for c in h)
    if len(h) != 6: h = '808080'
    try: return [int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)]
    except ValueError: return [128, 128, 128]


def solid_color_png(hex_color, size=1080, jitter=12):
    """Solid PNG with per-channel jitter."""
    base = _parse_hex(hex_color)
    rgb = tuple(max(0, min(255, c + random.randint(-jitter, jitter)))
                for c in base)
    img = Image.new('RGB', (size, size), rgb)
    buf = io.BytesIO()
    img.save(buf, format='PNG', optimize=True)
    return buf.getvalue(), '#{:02x}{:02x}{:02x}'.format(*rgb)


def gradient_png(hex_a, hex_b, size=1080, jitter=12, direction='vertical'):
    """Linear gradient PNG between two hex colors with per-stop jitter.
    direction: 'vertical' | 'horizontal' | 'diagonal'."""
    a = _parse_hex(hex_a)
    b = _parse_hex(hex_b)
    aj = tuple(max(0, min(255, c + random.randint(-jitter, jitter))) for c in a)
    bj = tuple(max(0, min(255, c + random.randint(-jitter, jitter))) for c in b)
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
    label = '#{:02x}{:02x}{:02x}→#{:02x}{:02x}{:02x}'.format(*aj, *bj)
    return buf.getvalue(), label


def radial_burst_png(hex_color, size=1080, jitter=12, brightness_boost=80):
    """Radial-burst PNG: bright center → darker edges."""
    base = _parse_hex(hex_color)
    bj = [max(0, min(255, c + random.randint(-jitter, jitter))) for c in base]
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
    return buf.getvalue(), '#{:02x}{:02x}{:02x}'.format(*bj)


def _emit_kb(mode):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🎲 Generate another ({mode})",
                              callback_data=f"bg_gen:random:{mode}")],
        [InlineKeyboardButton("🎨 Pick specific color",
                              callback_data=f"bg_gen:pick:{mode}")],
        [InlineKeyboardButton("⬅ Change style",
                              callback_data="bg_gen:menu")],
    ])


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
    """/bg_generator — pick a style (solid/gradient/radial) then generate.
    Direct shortcut: /bg_generator solid|gradient|radial."""
    arg = (context.args[0].lower() if context.args else '').strip()
    if arg in ('solid', 'gradient', 'radial'):
        await _emit(update.message, mode=arg)
        return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🟦 Solid color",
                              callback_data="bg_gen:mode:solid")],
        [InlineKeyboardButton("🌈 Gradient (two-color fade)",
                              callback_data="bg_gen:mode:gradient")],
        [InlineKeyboardButton("✨ Radial burst (light center)",
                              callback_data="bg_gen:mode:radial")],
    ])
    await update.message.reply_text(
        "🎨 <b>Background generator</b>\n\n"
        "Pick a style — each generation is jittered ±12 RGB so two "
        "pulls of the same palette are never pixel-identical.\n\n"
        "<b>Solid</b> — single flat color\n"
        "<b>Gradient</b> — linear fade between two palette colors\n"
        "<b>Radial</b> — bright center, darker corners\n\n"
        "<i>Or run </i><code>/bg_generator solid</code>, "
        "<code>/bg_generator gradient</code>, or "
        "<code>/bg_generator radial</code><i> directly.</i>",
        parse_mode='HTML', reply_markup=kb)


async def bg_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle bg_gen:* inline buttons."""
    query = update.callback_query
    await query.answer()
    data = query.data or ''

    if data == 'bg_gen:menu':
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🟦 Solid color",
                                  callback_data="bg_gen:mode:solid")],
            [InlineKeyboardButton("🌈 Gradient (two-color fade)",
                                  callback_data="bg_gen:mode:gradient")],
            [InlineKeyboardButton("✨ Radial burst",
                                  callback_data="bg_gen:mode:radial")],
        ])
        await query.message.reply_text(
            "🎨 <b>Pick a background style:</b>",
            parse_mode='HTML', reply_markup=kb)
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
