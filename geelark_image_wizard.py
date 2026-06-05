"""Image-push wizard for GeeLark phones.

Entry from /geelark_profile_open's mode-select. Two paths today:

  • "Create + images"  — current /geelark_profile_open batch creation, with a
                          per-profile image step at the end (lands in Commit 2)
  • "Images only"      — pick existing GeeLark phones by name + push images
                          to each one's gallery (this commit's focus)

Image step per profile:
  1. bg_generator inline — user picks style, image is generated, then
     inline buttons offer  ✅ Keep  /  🔄 Regenerate (same style)  /  ⏭ Skip
     Loop until keep-or-skip.
  2. (Commit 2) Drive folder navigator — model → niche → image folder
  3. Phone is started once, all kept images are pushed via `adb push`, phone
     is stopped — same start/ADB-bring-up/stop pattern /ig_setup_private uses.

Everything new lives in this module + a tiny dispatcher tweak in bot.py and
in geelark_open.geelark_profile_open_command. The original create-flow code
in geelark_open is untouched — the mode-select just sits in front of it.
"""
import os
import io
import time
import logging
import subprocess
import asyncio

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes

from geelark_open import _geelark_post, _geelark_boot_wait

logger = logging.getLogger(__name__)


# State lives in context.user_data['imgwiz']:
# {
#   'mode':         'create_plus_images' | 'images_only',
#   'batch':        [{'name': str, 'phone_id': str}],   # profiles to work on
#   'idx':          int,                                 # current profile index
#   'images':       [local_path, ...],                   # images queued for current profile
#   'last_bg_mode': str,                                 # so 'regenerate' uses the same style
#   'sub_step':     'pick_phone' | 'bg_pick_style' | 'bg_review' | 'drive_*' | 'next_or_done'
# }

# ─── Mode-select entry ──────────────────────────────────────────────────────

def mode_select_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🆕 Create + add images",
                              callback_data='imgwiz:mode:create_plus_images')],
        [InlineKeyboardButton("🖼 Add images to existing profiles",
                              callback_data='imgwiz:mode:images_only')],
        [InlineKeyboardButton("❌ Cancel", callback_data='imgwiz:cancel')],
    ])


async def show_mode_select(update_or_query):
    """Reply (or edit) with the two-button mode-select. Called from
    geelark_open.geelark_profile_open_command."""
    text = (
        "📲 *GeeLark profile opener*\n\n"
        "Pick what you want to do:\n\n"
        "🆕 *Create + add images* — make new GeeLark phones from existing "
        "GoLogin profiles AND seed each with backgrounds (+ optional Drive images)\n\n"
        "🖼 *Add images to existing profiles* — skip the create step and just push "
        "background + Drive images to GeeLark phones you already have"
    )
    msg = update_or_query.message if hasattr(update_or_query, 'message') else update_or_query
    await msg.reply_text(text, reply_markup=mode_select_keyboard(),
                         parse_mode='Markdown')


# ─── Drive navigator (Commit 2 will fill this in) ───────────────────────────
# Placeholder for now so the wizard can finish a no-Drive run end-to-end.

async def _ask_drive_yes_no(msg, prefix='drive_branch'):
    await msg.reply_text(
        "📁 Also add images from a Drive folder for this profile?\n"
        "_(Drive navigator lands in next commit — for now this will skip)_",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("⏭ Skip Drive (continue)",
                                 callback_data=f'imgwiz:{prefix}:skip'),
        ]]),
        parse_mode='Markdown')


# ─── bg_generator integration ───────────────────────────────────────────────

BG_MODES = [
    ('solid',         '🟦 Solid'),
    ('gradient',      '🌈 Gradient'),
    ('radial',        '✨ Radial'),
    ('impressionist', '🌻 Impressionist'),
    ('splatter',      '🎨 Splatter'),
    ('watercolor',    '💧 Watercolor'),
    ('geometric',     '🟥 Geometric'),
    ('voronoi',       '🔷 Voronoi'),
    ('color_field',   '🟫 Color field'),
]


def _bg_pick_style_kb():
    rows = []
    row = []
    for code, label in BG_MODES:
        row.append(InlineKeyboardButton(label,
                                         callback_data=f'imgwiz:bg_style:{code}'))
        if len(row) == 3:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("⏭ Skip BG entirely",
                                       callback_data='imgwiz:bg_style:skip')])
    return InlineKeyboardMarkup(rows)


def _generate_bg_png(mode):
    """Call bg.py's generator for the requested mode and return (png_bytes, label).
    Mirrors bg._emit's switch statement but doesn't send anything to TG."""
    import random
    import bg
    label, hex_color = random.choice(bg.PROFILE_COLOR_PALETTE)
    if mode == 'gradient':
        choices = [h for _, h in bg.PROFILE_COLOR_PALETTE if h != hex_color]
        hex_b = random.choice(choices) if choices else hex_color
        direction = random.choice(['vertical', 'horizontal', 'diagonal'])
        png, _ = bg.gradient_png(hex_color, hex_b, direction=direction)
        return png, f'gradient {hex_color}→{hex_b}'
    if mode == 'radial':
        png, _ = bg.radial_burst_png(hex_color); return png, f'radial ({label})'
    if mode == 'impressionist':
        png, _ = bg.impressionist_png(hex_color); return png, f'impressionist ({label})'
    if mode == 'splatter':
        png, _ = bg.splatter_png(hex_color); return png, f'splatter ({label})'
    if mode == 'watercolor':
        png, _ = bg.watercolor_png(hex_color); return png, f'watercolor ({label})'
    if mode == 'geometric':
        png, _ = bg.geometric_png(hex_color); return png, f'geometric ({label})'
    if mode == 'voronoi':
        png, _ = bg.voronoi_png(hex_color); return png, f'voronoi ({label})'
    if mode == 'color_field':
        png, _ = bg.color_field_png(hex_color); return png, f'color field ({label})'
    png, _ = bg.solid_color_png(hex_color)
    return png, f'solid ({label})'


def _bg_review_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Keep", callback_data='imgwiz:bg_review:keep'),
         InlineKeyboardButton("🔄 Regenerate", callback_data='imgwiz:bg_review:regen'),
         InlineKeyboardButton("⏭ Skip", callback_data='imgwiz:bg_review:skip')],
    ])


async def _emit_bg(msg, mode, last_png_path_holder):
    """Generate + send a bg preview; stash the PNG to /tmp + return its path."""
    png, caption = _generate_bg_png(mode)
    local_path = f'/tmp/imgwiz_bg_{mode}_{int(time.time()*1000)}.png'
    with open(local_path, 'wb') as f: f.write(png)
    bio = io.BytesIO(png); bio.name = os.path.basename(local_path)
    await msg.reply_photo(photo=bio,
                          caption=f"🎨 *{caption}*\nKeep / regenerate (same style) / skip?",
                          parse_mode='Markdown',
                          reply_markup=_bg_review_kb())
    last_png_path_holder[0] = local_path


# ─── ADB image push (independent of ig_automation so we don't entangle) ─────

def _adb(adb_addr, *args, timeout=15):
    return subprocess.run(['adb', '-s', adb_addr] + list(args),
                          capture_output=True, text=True, timeout=timeout)


def _start_and_get_adb(phone_id):
    """Boot phone (60s), enable ADB, return (ip, port, pwd, err)."""
    _, err = _geelark_post('/phone/start', {'ids': [phone_id]})
    if err:
        return None, None, None, f'/phone/start: {err}'
    _geelark_boot_wait(phone_id, sleep_s=60)
    _, err = _geelark_post('/adb/setStatus', {'ids': [phone_id], 'open': True})
    if err:
        return None, None, None, f'/adb/setStatus: {err}'
    for attempt in range(8):
        time.sleep(8 if attempt else 3)
        data, _ = _geelark_post('/adb/getData', {'ids': [phone_id]})
        if data:
            items = data.get('items') or []
            if items:
                it = items[0]
                ip = it.get('ip'); port = it.get('port')
                pwd = it.get('pwd') or it.get('password')
                if ip:
                    return ip, port, pwd, None
    return None, None, None, '/adb/getData never returned an endpoint'


def _push_images_to_phone(adb_ip, adb_port, glogin_pwd, image_paths):
    """Connect ADB → glogin → push each file → broadcast media scan → disconnect.
    Returns (pushed_count, err_or_None).
    """
    if not image_paths:
        return 0, 'no images to push'
    adb_addr = f'{adb_ip}:{adb_port}'
    subprocess.run(['adb', 'disconnect', adb_addr], capture_output=True, timeout=10)
    time.sleep(1)
    r = subprocess.run(['adb', 'connect', adb_addr],
                       capture_output=True, text=True, timeout=30)
    if 'connected' not in (r.stdout + r.stderr).lower() and 'already' not in (r.stdout + r.stderr).lower():
        return 0, f'adb connect: {r.stderr or r.stdout}'
    subprocess.run(['adb', '-s', adb_addr, 'wait-for-device'],
                   capture_output=True, timeout=60)
    time.sleep(2)
    if glogin_pwd:
        r = _adb(adb_addr, 'shell', 'glogin', glogin_pwd, timeout=20)
        if 'success' not in (r.stdout + r.stderr).lower():
            return 0, f'glogin: {r.stdout} {r.stderr}'
    _adb(adb_addr, 'shell', 'mkdir', '-p', '/sdcard/Pictures')
    pushed = 0
    for path in image_paths:
        if not os.path.exists(path): continue
        safe_name = os.path.basename(path).replace(' ', '_')
        remote = f'/sdcard/Pictures/{safe_name}'
        r = subprocess.run(['adb', '-s', adb_addr, 'push', path, remote],
                           capture_output=True, text=True, timeout=120)
        if r.returncode == 0:
            _adb(adb_addr, 'shell', 'am', 'broadcast', '-a',
                 'android.intent.action.MEDIA_SCANNER_SCAN_FILE',
                 '-d', f'file://{remote}')
            pushed += 1
    subprocess.run(['adb', 'disconnect', adb_addr], capture_output=True, timeout=10)
    return pushed, None


# ─── Callback dispatcher ────────────────────────────────────────────────────

async def imgwiz_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ''
    state = context.user_data.get('imgwiz')

    if data == 'imgwiz:cancel':
        context.user_data.pop('imgwiz', None)
        await query.edit_message_text("❌ cancelled.")
        return

    if data.startswith('imgwiz:mode:'):
        mode = data.split(':', 2)[2]
        if mode == 'create_plus_images':
            # Hand off to the original /geelark_profile_open create-flow.
            # Commit 2 will wrap that flow with the per-profile image step.
            await query.edit_message_text(
                "🆕 *Create + images* — using the existing create flow for now. "
                "The per-profile image step lands in the next commit; today this "
                "path just creates the phones as before.",
                parse_mode='Markdown')
            from geelark_open import geelark_profile_open_command_legacy_entry
            await geelark_profile_open_command_legacy_entry(query.message, context)
            return
        if mode == 'images_only':
            context.user_data['imgwiz'] = {
                'mode': 'images_only',
                'batch': [],
                'idx': 0,
                'images': [],
                'last_bg_mode': None,
                'sub_step': 'pick_phone',
            }
            context.user_data['expecting_imgwiz_phone_name'] = True
            await query.edit_message_text(
                "🖼 *Add images to existing GeeLark profiles*\n\n"
                "Send the *GoLogin/GeeLark profile name* of the first phone "
                "(e.g. `caro motorcycle`). After each one you can add more, "
                "or type `done` to start the image work on the batch.",
                parse_mode='Markdown')
            return

    if not state:
        await query.edit_message_text("⚠️ wizard state expired — run /geelark_profile_open again.")
        return

    # bg style picked → generate + show review
    if data.startswith('imgwiz:bg_style:'):
        mode = data.split(':', 2)[2]
        if mode == 'skip':
            # No bg, jump to Drive ask (Commit 2 fills in Drive)
            await _ask_drive_yes_no(query.message)
            return
        state['last_bg_mode'] = mode
        holder = [None]
        await _emit_bg(query.message, mode, holder)
        state['last_bg_path'] = holder[0]
        return

    # bg review action: keep / regen / skip
    if data.startswith('imgwiz:bg_review:'):
        action = data.split(':', 2)[2]
        if action == 'keep':
            if state.get('last_bg_path'):
                state['images'].append(state['last_bg_path'])
            await query.edit_message_caption(
                caption=f"✅ kept — {os.path.basename(state['last_bg_path'])}",
                reply_markup=None)
            # After keep, ask Drive question (Commit 2 wires it up)
            await _ask_drive_yes_no(query.message)
            return
        if action == 'regen':
            mode = state.get('last_bg_mode') or 'solid'
            holder = [None]
            await _emit_bg(query.message, mode, holder)
            state['last_bg_path'] = holder[0]
            return
        if action == 'skip':
            await query.edit_message_caption(
                caption="⏭ skipped this BG.", reply_markup=None)
            await _ask_drive_yes_no(query.message)
            return

    # Drive skip → push what we have, advance to next profile
    if data.startswith('imgwiz:drive_branch:skip'):
        await _push_and_advance(query.message, context)
        return


async def _push_and_advance(msg, context):
    """Push the queued images for the current profile, stop the phone, advance
    to the next profile (or finish the batch)."""
    state = context.user_data.get('imgwiz')
    if not state:
        await msg.reply_text("⚠️ state lost"); return
    entry = state['batch'][state['idx']]
    images = state['images']
    if images:
        await msg.reply_text(
            f"📲 booting `{entry['name']}` to push {len(images)} image(s)…",
            parse_mode='Markdown')
        ip, port, pwd, err = await asyncio.to_thread(_start_and_get_adb, entry['phone_id'])
        if err:
            await msg.reply_text(f"❌ `{entry['name']}`: ADB bring-up failed — {err}",
                                  parse_mode='Markdown')
        else:
            pushed, perr = await asyncio.to_thread(_push_images_to_phone,
                                                    ip, port, pwd, images)
            if perr:
                await msg.reply_text(f"⚠️ `{entry['name']}`: push err — {perr}",
                                      parse_mode='Markdown')
            else:
                await msg.reply_text(
                    f"✅ pushed {pushed}/{len(images)} image(s) to `{entry['name']}`.",
                    parse_mode='Markdown')
        # Always stop the phone
        _geelark_post('/phone/stop', {'ids': [entry['phone_id']]})
        await msg.reply_text(f"🛑 stopped `{entry['name']}`.", parse_mode='Markdown')
    else:
        await msg.reply_text(
            f"⏭ no images queued for `{entry['name']}` — skipping push.",
            parse_mode='Markdown')

    # Advance
    state['idx'] += 1
    state['images'] = []
    state['last_bg_mode'] = None
    state['last_bg_path'] = None
    if state['idx'] >= len(state['batch']):
        context.user_data.pop('imgwiz', None)
        await msg.reply_text(
            "🟢 *All done.* Image work complete on the batch.",
            parse_mode='Markdown')
        return
    # Next profile — restart the image flow
    next_entry = state['batch'][state['idx']]
    await msg.reply_text(
        f"➡️ next profile: `{next_entry['name']}` "
        f"({state['idx']+1}/{len(state['batch'])}).\nPick a background style:",
        reply_markup=_bg_pick_style_kb(),
        parse_mode='Markdown')


# ─── Text router entry: name-collection for images-only mode ────────────────

async def imgwiz_text_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the user typing GeeLark profile names while in images_only
    name-collection mode. Returns True if handled."""
    if not context.user_data.get('expecting_imgwiz_phone_name'):
        return False
    state = context.user_data.get('imgwiz')
    if not state:
        context.user_data.pop('expecting_imgwiz_phone_name', None)
        return False
    text = (update.message.text or '').strip()
    if text.lower() in {'done', 'finish', 'stop', 'no'}:
        context.user_data.pop('expecting_imgwiz_phone_name', None)
        if not state['batch']:
            context.user_data.pop('imgwiz', None)
            await update.message.reply_text("⚠️ batch empty — nothing to do.")
            return True
        first = state['batch'][0]
        await update.message.reply_text(
            f"🚀 starting image work on {len(state['batch'])} profile(s).\n\n"
            f"➡️ first: `{first['name']}` (1/{len(state['batch'])}).\n"
            f"Pick a background style:",
            reply_markup=_bg_pick_style_kb(),
            parse_mode='Markdown')
        return True
    # Look up the GeeLark phone by name
    await update.message.reply_text(f"🔍 finding GeeLark phone `{text}`…",
                                     parse_mode='Markdown')
    phone_id, err = await asyncio.to_thread(_find_geelark_phone_by_name, text)
    if err or not phone_id:
        await update.message.reply_text(
            f"❌ {err or 'not found'}\nTry another name, or `done`.",
            parse_mode='Markdown')
        return True
    if any(e['name'].lower() == text.lower() for e in state['batch']):
        await update.message.reply_text(f"⚠️ `{text}` already queued — skipping.",
                                          parse_mode='Markdown')
        return True
    state['batch'].append({'name': text, 'phone_id': phone_id})
    queue = '\n'.join(f"  {i+1}. `{e['name']}`" for i, e in enumerate(state['batch']))
    await update.message.reply_text(
        f"✅ added `{text}`. Queue ({len(state['batch'])}):\n{queue}\n\n"
        f"Send another name, or `done`.",
        parse_mode='Markdown')
    return True


def _find_geelark_phone_by_name(name):
    """Local helper — wraps geelark_open's phone-by-name lookup so we can
    asyncio.to_thread it cleanly."""
    from geelark_open import _geelark_find_phone_by_name as _f
    return _f(name)
