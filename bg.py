"""
/bg_generator — solid-color profile/background PNG generator.

Each output is jittered ±12 RGB so two pulls of the same palette pick
are never pixel-identical (anti-image-hash clustering — defeats Meta's
ability to correlate accounts by profile-picture binary).
"""

import io
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


def solid_color_png(hex_color, size=1080, jitter=12):
    """Render a solid-color PNG of the given hex at size×size px.
    Returns (bytes, actual_hex). Jittered ±12 RGB per channel by default
    so binary hash differs between pulls of the same palette pick."""
    h = (hex_color or '').strip().lstrip('#')
    if len(h) == 3:
        h = ''.join(c * 2 for c in h)
    if len(h) != 6:
        h = '808080'
    try:
        base = (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    except ValueError:
        base = (128, 128, 128)
    rgb = tuple(
        max(0, min(255, c + random.randint(-jitter, jitter)))
        for c in base
    )
    img = Image.new('RGB', (size, size), rgb)
    buf = io.BytesIO()
    img.save(buf, format='PNG', optimize=True)
    actual_hex = '#{:02x}{:02x}{:02x}'.format(*rgb)
    return buf.getvalue(), actual_hex


def _control_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎲 Generate another (random)",
                              callback_data="bg_gen:random")],
        [InlineKeyboardButton("🎨 Pick specific color",
                              callback_data="bg_gen:pick")],
    ])


async def bg_generator_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/bg_generator — generate a randomized solid-color PNG and send it."""
    label, hex_color = random.choice(PROFILE_COLOR_PALETTE)
    png_bytes, actual_hex = solid_color_png(hex_color)
    bio = io.BytesIO(png_bytes)
    bio.name = f'bg_{actual_hex.lstrip("#")}.png'
    await update.message.reply_photo(
        photo=bio,
        caption=(f"🎨 <b>Background generated</b>\n\n"
                 f"Palette: <b>{label}</b>\n"
                 f"Base hex: <code>{hex_color}</code>\n"
                 f"Actual hex (post-jitter): <code>{actual_hex}</code>\n"
                 f"Size: 1080×1080 PNG\n\n"
                 f"<i>Tap-and-hold the photo to download. Each generation "
                 f"is jittered ±12 RGB — defeats Meta image-hash clustering.</i>"),
        parse_mode='HTML', reply_markup=_control_kb())


async def bg_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle bg_gen:* inline buttons."""
    query = update.callback_query
    await query.answer()
    data = query.data or ''
    if data == 'bg_gen:random':
        label, hex_color = random.choice(PROFILE_COLOR_PALETTE)
        png_bytes, actual_hex = solid_color_png(hex_color)
        bio = io.BytesIO(png_bytes)
        bio.name = f'bg_{actual_hex.lstrip("#")}.png'
        await query.message.reply_photo(
            photo=bio,
            caption=(f"🎨 <b>Background generated</b>\n\n"
                     f"Palette: <b>{label}</b>\n"
                     f"Base hex: <code>{hex_color}</code>\n"
                     f"Actual hex: <code>{actual_hex}</code>\n"
                     f"Size: 1080×1080 PNG"),
            parse_mode='HTML', reply_markup=_control_kb())
        return
    if data == 'bg_gen:pick':
        rows, row = [], []
        for label, hex_color in PROFILE_COLOR_PALETTE:
            row.append(InlineKeyboardButton(
                label, callback_data=f"bg_gen:hex:{hex_color}"))
            if len(row) == 2:
                rows.append(row); row = []
        if row:
            rows.append(row)
        rows.append([InlineKeyboardButton("⬅ Back",
                                          callback_data="bg_gen:random")])
        await query.message.reply_text(
            "🎨 <b>Pick a palette color</b>\n\n"
            "Each pick will still be jittered ±12 RGB so the output "
            "isn't pixel-identical to a prior generation.",
            parse_mode='HTML', reply_markup=InlineKeyboardMarkup(rows))
        return
    if data.startswith('bg_gen:hex:'):
        hex_color = data.split(':', 2)[2]
        label = next((lbl for lbl, hx in PROFILE_COLOR_PALETTE
                      if hx == hex_color), '(custom)')
        png_bytes, actual_hex = solid_color_png(hex_color)
        bio = io.BytesIO(png_bytes)
        bio.name = f'bg_{actual_hex.lstrip("#")}.png'
        await query.message.reply_photo(
            photo=bio,
            caption=(f"🎨 <b>Background generated</b>\n\n"
                     f"Palette: <b>{label}</b>\n"
                     f"Base hex: <code>{hex_color}</code>\n"
                     f"Actual hex: <code>{actual_hex}</code>\n"
                     f"Size: 1080×1080 PNG"),
            parse_mode='HTML', reply_markup=_control_kb())
