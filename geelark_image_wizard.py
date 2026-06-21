"""Per-profile image-add wizard for /geelark_profile_open.

Architecture (selection-first, execution-deferred):

  SELECTION phase — interactive, one profile at a time:
    For each profile in the batch:
      1. Standard background generator — pick style → keep / regen / skip
         (loops until keep-or-skip; each kept PNG is queued for the push step)
      2. Artistic background — yes / no  (generated at execution time)
      3. Drive folder — yes / no → if yes, navigate the Drive tree (any depth)
         → user clicks "📌 Use this folder" → all images in it get queued
      → "selection done for this profile", advance to next

  EXECUTION phase — runs only AFTER the last profile finishes selection:
    For each profile (in order):
      - If artistic was selected, generate it now via artistic_bg_gen
        (saved to Drive under
         "Images generated for account setup in GeeLark/<profile name>/")
      - If a Drive folder was picked, download every image in it to /tmp
      - Boot phone, ADB connect + glogin, push all queued images
        (bg PNGs + artistic + Drive imgs) to /sdcard/Pictures, media-scan
        broadcast, stop phone
      - Per-profile result reported back to Telegram

  Mode select sits in front:
    🆕 Create + add images  → existing /geelark_profile_open create flow
                              (image step is appended after the create batch
                              finishes — Commit 2 of the create-flow wire-up)
    🖼 Add images to existing GeeLark profiles  → straight into the
                                                  selection phase above

This module replaces the earlier Commit-1 skeleton entirely.
"""
import os
import io
import time
import logging
import subprocess
import asyncio
import base64
import pickle

import requests
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes

from geelark_open import (
    _geelark_post, _geelark_boot_wait,
    _gologin_find_profile_by_name, _gologin_get_proxy,
    _geelark_create_phone, _geelark_install_instagram,
)
import artistic_bg_gen

logger = logging.getLogger(__name__)


# State lives in context.user_data['imgwiz']:
# {
#   'mode': 'create_plus_images' | 'images_only',
#   'batch': [{
#     'name': str, 'phone_id': str,
#     'bg_paths': [str],
#     'artistic_yes': bool,
#     'drive_folder_id': str | None,
#     'drive_folder_name': str | None,
#   }],
#   'idx': int,                          # current profile being selected
#   'sub_step': str,
#   'last_bg_mode': str | None,
#   'last_bg_path': str | None,
#   'drive_nav_stack': [(id,name), ...], # breadcrumb for the Drive navigator
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


async def show_mode_select(update_or_msg):
    text = (
        "📲 *GeeLark profile opener*\n\n"
        "Pick what you want to do:\n\n"
        "🆕 *Create + add images* — make new GeeLark phones from existing "
        "GoLogin profiles AND seed each with images afterward\n\n"
        "🖼 *Add images to existing profiles* — skip the create step and just "
        "push images to phones you already have\n\n"
        "_For fully-automated image generation (no manual picking), use_ "
        "`/geelark_profile_ig_auto` _instead._"
    )
    msg = update_or_msg.message if hasattr(update_or_msg, 'message') else update_or_msg
    await msg.reply_text(text, reply_markup=mode_select_keyboard(),
                         parse_mode='Markdown')


async def automated_create_command(update, context):
    """Entry point for /geelark_profile_ig_auto — same name-collection
    + preflight as the wizard's create_plus_images mode, but skips the bg /
    artistic / Drive selection step entirely. After Done, each profile gets
    one auto-generated normal bg (random style + random palette) and one
    auto-generated artistic bg (random Drive type), both pushed to the phone."""
    context.user_data['imgwiz'] = {
        'mode': 'create_plus_images_automated',
        'batch': [],
        'idx': 0,
        'sub_step': 'pick_profile',
        'last_bg_mode': None,
        'last_bg_path': None,
        'drive_nav_stack': [],
    }
    context.user_data['expecting_imgwiz_phone_name'] = True
    await update.message.reply_text(
        "🤖 *Automated IG profile setup*\n\n"
        "Send the *GoLogin profile name* of the first GeeLark phone to "
        "create (e.g. `Caroline Goni 5`). I'll validate against GoLogin + "
        "grab the proxy.\n\n"
        "After each one you can add more — tap *Done* or type `done` to "
        "start. NOTHING gets created until ALL names are collected.\n\n"
        "*Per profile* the bot will then automatically: create the phone, "
        "install Instagram, generate 1 random normal bg + 1 random artistic "
        "bg, push both to the phone gallery, stop the phone.",
        parse_mode='Markdown',
        reply_markup=_imgwiz_done_kb())


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
    rows, row = [], []
    for code, label in BG_MODES:
        row.append(InlineKeyboardButton(label,
                                         callback_data=f'imgwiz:bg_style:{code}'))
        if len(row) == 3:
            rows.append(row); row = []
    if row: rows.append(row)
    rows.append([InlineKeyboardButton("⏭ Skip BG entirely",
                                       callback_data='imgwiz:bg_style:skip')])
    return InlineKeyboardMarkup(rows)


def _bg_review_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Keep", callback_data='imgwiz:bg_review:keep'),
         InlineKeyboardButton("🔄 Regenerate", callback_data='imgwiz:bg_review:regen'),
         InlineKeyboardButton("⏭ Skip", callback_data='imgwiz:bg_review:skip')],
    ])


def _generate_bg_png(mode):
    import random, bg
    label, hex_color = random.choice(bg.PROFILE_COLOR_PALETTE)
    if mode == 'gradient':
        choices = [h for _, h in bg.PROFILE_COLOR_PALETTE if h != hex_color]
        hex_b = random.choice(choices) if choices else hex_color
        direction = random.choice(['vertical', 'horizontal', 'diagonal'])
        png, _ = bg.gradient_png(hex_color, hex_b, direction=direction)
        return png, f'gradient {hex_color}→{hex_b}'
    if mode == 'radial':       png, _ = bg.radial_burst_png(hex_color);   return png, f'radial ({label})'
    if mode == 'impressionist':png, _ = bg.impressionist_png(hex_color);  return png, f'impressionist ({label})'
    if mode == 'splatter':     png, _ = bg.splatter_png(hex_color);       return png, f'splatter ({label})'
    if mode == 'watercolor':   png, _ = bg.watercolor_png(hex_color);     return png, f'watercolor ({label})'
    if mode == 'geometric':    png, _ = bg.geometric_png(hex_color);      return png, f'geometric ({label})'
    if mode == 'voronoi':      png, _ = bg.voronoi_png(hex_color);        return png, f'voronoi ({label})'
    if mode == 'color_field':  png, _ = bg.color_field_png(hex_color);    return png, f'color field ({label})'
    png, _ = bg.solid_color_png(hex_color)
    return png, f'solid ({label})'


async def _emit_bg(msg, mode, state):
    png, caption = _generate_bg_png(mode)
    local_path = f'/tmp/imgwiz_bg_{mode}_{int(time.time()*1000)}.png'
    with open(local_path, 'wb') as f: f.write(png)
    bio = io.BytesIO(png); bio.name = os.path.basename(local_path)
    await msg.reply_photo(photo=bio,
                          caption=f"🎨 *{caption}*\nKeep / regenerate (same style) / skip?",
                          parse_mode='Markdown',
                          reply_markup=_bg_review_kb())
    state['last_bg_path'] = local_path


# ─── Artistic-bg step (just a yes/no in selection phase) ───────────────────

def _artistic_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎨 Yes, generate an artistic image",
                              callback_data='imgwiz:artistic:yes'),
         InlineKeyboardButton("⏭ No, skip",
                              callback_data='imgwiz:artistic:no')],
    ])


# ─── Drive folder navigator ────────────────────────────────────────────────
# Uses artistic_bg_gen._drive_service() so it auths against reel-bot's Drive
# (REEL_GOOGLE_TOKEN_PICKLE). At each level we list subfolders + a "use this
# folder" button. Navigation is generic — works for any Drive tree depth.

DRIVE_FOLDERS_PAGE_SIZE = 25  # cap inline-keyboard rows per page


def _drive_list_subfolders(parent_id):
    svc = artistic_bg_gen._drive_service()
    res = svc.files().list(
        q=(f"'{parent_id}' in parents and trashed=false and "
           f"mimeType='application/vnd.google-apps.folder'"),
        fields='files(id,name)',
        orderBy='name',
        pageSize=200,
        supportsAllDrives=True, includeItemsFromAllDrives=True,
    ).execute()
    return res.get('files') or []


def _drive_list_root_folders():
    """List folders directly under the user's My Drive root.
    We use 'root' as the parent id — Drive's documented alias for the user's root."""
    return _drive_list_subfolders('root')


def _drive_nav_kb(folders, can_pick_current):
    """Build inline keyboard: one button per subfolder, plus 'Use this folder'
    + '⬆ Up' (if not at root). Each subfolder button descends one level."""
    rows = []
    for f in folders[:DRIVE_FOLDERS_PAGE_SIZE]:
        rows.append([InlineKeyboardButton(
            f"📁 {f['name'][:40]}",
            callback_data=f"imgwiz:drive_descend:{f['id']}",
        )])
    if can_pick_current:
        rows.append([InlineKeyboardButton(
            "📌 Use THIS folder for the picture upload",
            callback_data='imgwiz:drive_pick_current')])
        rows.append([InlineKeyboardButton(
            "⬆ Up one level", callback_data='imgwiz:drive_up')])
    rows.append([InlineKeyboardButton(
        "⏭ Skip Drive (continue)", callback_data='imgwiz:drive_skip')])
    return InlineKeyboardMarkup(rows)


def _drive_breadcrumb(stack):
    return ' › '.join((nm or 'Drive root') for _, nm in stack) if stack else 'Drive root'


async def _send_drive_nav(msg, state, parent_id, parent_name):
    """Send the folder list at `parent_id`. parent_name purely for breadcrumb."""
    folders = await asyncio.to_thread(_drive_list_subfolders, parent_id)
    # Track breadcrumb: if user descended, append; if regenerated at same level, don't
    if not state.get('drive_nav_stack'):
        state['drive_nav_stack'] = [(parent_id, parent_name)]
    elif state['drive_nav_stack'][-1][0] != parent_id:
        state['drive_nav_stack'].append((parent_id, parent_name))
    crumb = _drive_breadcrumb(state['drive_nav_stack'])
    can_pick = (state['drive_nav_stack'] and state['drive_nav_stack'][-1][0] != 'root')
    if not folders and not can_pick:
        await msg.reply_text(
            f"📁 *{crumb}*\n\n_(empty)_ — pick a different folder or skip.",
            parse_mode='Markdown',
            reply_markup=_drive_nav_kb([], False))
        return
    await msg.reply_text(
        f"📁 *{crumb}*\n\n"
        f"Pick a subfolder to descend, '📌 Use THIS folder' to choose the "
        f"current one (all its images get pushed), '⬆ Up' to go back, or "
        f"⏭ Skip to skip Drive entirely.",
        parse_mode='Markdown',
        reply_markup=_drive_nav_kb(folders, can_pick))


# ─── ADB helpers (independent of ig_automation) ─────────────────────────────

def _adb(adb_addr, *args, timeout=15):
    return subprocess.run(['adb', '-s', adb_addr] + list(args),
                          capture_output=True, text=True, timeout=timeout)


def _start_and_get_adb(phone_id):
    _, err = _geelark_post('/phone/start', {'ids': [phone_id]})
    if err: return None, None, None, f'/phone/start: {err}'
    _geelark_boot_wait(phone_id, sleep_s=60)
    return _adb_bring_up_only(phone_id)


def _adb_bring_up_only(phone_id):
    """Phone is already running — just enable ADB + poll for the endpoint.
    Used when the create-flow's IG install already booted the phone, so we
    don't re-boot for the image push."""
    _, err = _geelark_post('/adb/setStatus', {'ids': [phone_id], 'open': True})
    if err: return None, None, None, f'/adb/setStatus: {err}'
    for attempt in range(8):
        time.sleep(8 if attempt else 3)
        data, _ = _geelark_post('/adb/getData', {'ids': [phone_id]})
        if data:
            items = data.get('items') or []
            if items:
                it = items[0]
                ip = it.get('ip'); port = it.get('port')
                pwd = it.get('pwd') or it.get('password')
                if ip: return ip, port, pwd, None
    return None, None, None, '/adb/getData never returned an endpoint'


def _geelark_upload_to_phone_gallery(phone_id, image_paths, send_progress=None):
    """Upload files to a GeeLark cloud phone using GeeLark's native upload
    API (so they land in the Gallery / MediaStore properly, visible to IG
    and other media-aware apps).

    Flow per file (from GeeLark openapi docs, /Cloud Phone API/File Management/):
      1. POST /open/v1/upload/getUrl {fileType:'jpg'|'png'}
         → returns {uploadUrl (presigned OSS PUT URL), resourceUrl (public CDN URL)}
      2. PUT the raw bytes to uploadUrl.
         IMPORTANT: do NOT send Content-Type header — the OSS presigned
         signature was generated without one, and including it produces
         a SignatureDoesNotMatch 403.
      3. POST /open/v1/phone/uploadFile {id: phone_id, fileUrl: resourceUrl}
         → returns {taskId}. Phone must be running (env not running → 42002).
      4. POST /open/v1/phone/uploadFile/result {taskId} until status==1.

    This replaces the prior ADB-push + MEDIA_SCANNER_SCAN_FILE broadcast
    approach, which deposited files at /sdcard/Pictures but failed to
    register them with MediaStore because the broadcast routinely timed
    out on these phones, leaving images invisible to Instagram's picker.

    Returns (pushed_count, err_or_None).
    """
    if not image_paths:
        return 0, None
    pushed = 0
    for path in image_paths:
        if not os.path.exists(path):
            continue
        ext = os.path.splitext(path)[1].lstrip('.').lower() or 'jpg'
        # GeeLark supports: jpg, jpeg, png, gif, bmp, webp, heif, heic, mp4, webm, xml, apk, xapk
        # Map anything else to jpg as a safe default
        if ext not in ('jpg','jpeg','png','gif','bmp','webp','heif','heic'):
            ext = 'jpg'

        # Step 1: get a pre-signed upload URL + a resource URL
        data, err = _geelark_post('/upload/getUrl', {'fileType': ext})
        if err or not data:
            logger.warning(f"[imgwiz] getUrl err for {path}: {err}")
            continue
        upload_url = data.get('uploadUrl')
        resource_url = data.get('resourceUrl')
        if not upload_url or not resource_url:
            logger.warning(f"[imgwiz] getUrl missing URLs for {path}: {data}")
            continue

        # Step 2: PUT raw bytes to OSS (no Content-Type — signature is sensitive)
        try:
            with open(path, 'rb') as f:
                body = f.read()
            r = requests.put(upload_url, data=body, timeout=120)
            if r.status_code not in (200, 201):
                logger.warning(f"[imgwiz] OSS PUT for {path} got {r.status_code}: {r.text[:200]}")
                continue
        except Exception as e:
            logger.warning(f"[imgwiz] OSS PUT err for {path}: {e}")
            continue

        # Step 3: tell GeeLark to ingest into the phone's gallery.
        # Phone may still be booting after our 60s wait (GeeLark boot times
        # vary); retry on 42002 'env not running' for up to ~90s extra
        # before giving up on this file.
        task_id = None
        for retry in range(10):
            data, err = _geelark_post('/phone/uploadFile',
                                      {'id': phone_id, 'fileUrl': resource_url})
            if err and 'env not running' in err.lower():
                logger.info(f"[imgwiz] /phone/uploadFile 42002 (phone still booting?) "
                            f"attempt {retry+1}/10 — sleeping 10s")
                time.sleep(10)
                continue
            if err or not data:
                logger.warning(f"[imgwiz] /phone/uploadFile err for {path}: {err}")
                break
            task_id = data.get('taskId')
            if task_id:
                break
            logger.warning(f"[imgwiz] no taskId from /phone/uploadFile: {data}")
            break
        if not task_id:
            continue

        # Step 4: poll until status==1 (success) or status indicates failure.
        # NOTE — status=1 from GeeLark sometimes returns immediately even when
        # the OSS→phone file copy hasn't finished. Tested 2026-06-06 on 4 phones:
        # only 1 actually had the file on disk after status=1. So we ALSO wait
        # an extra ~20s before declaring success, giving GeeLark time to
        # finalize the async copy into /sdcard/Download/.
        complete = False
        for _ in range(20):  # up to ~60s
            time.sleep(3)
            data, err = _geelark_post('/phone/uploadFile/result', {'taskId': task_id})
            if not data:
                continue
            status = data.get('status')
            if status in (1, '1'):
                complete = True; break
            if status in (-1, '-1', 2, '2'):
                logger.warning(f"[imgwiz] /uploadFile failed for {path}: {data}")
                break
        if complete:
            # Extra settle time — GeeLark's OSS→phone copy is async and the
            # status=1 ack can fire before the file actually lands.
            time.sleep(20)
            pushed += 1
        else:
            logger.warning(f"[imgwiz] /uploadFile never reached success for {path}")
    return pushed, None


# Legacy ADB-push helper — kept for posterity but no longer used by the wizard.
# The MEDIA_SCANNER_SCAN_FILE broadcast it relied on was prone to 15-30s
# hangs on these phones, which left the files on /sdcard/Pictures but not
# registered with MediaStore. The new _geelark_upload_to_phone_gallery flow
# above uses GeeLark's native upload API and never has this problem.
def _push_images_to_phone(adb_ip, adb_port, glogin_pwd, image_paths):
    """DEPRECATED — use _geelark_upload_to_phone_gallery instead.

    Push files to /sdcard/Pictures via adb. The media-scanner broadcast is
    BEST EFFORT — wrapped in try/except because it can take 15+s on a slow
    phone and we don't want a single broadcast timeout to crash the whole
    wizard. The push itself is the load-bearing operation; the broadcast
    just hints to Android's media DB that new files exist. Without it,
    the files are still on disk and the Gallery app picks them up after
    a manual refresh / on next launch."""
    if not image_paths: return 0, None
    adb_addr = f'{adb_ip}:{adb_port}'
    try:
        subprocess.run(['adb', 'disconnect', adb_addr],
                       capture_output=True, timeout=10)
        time.sleep(1)
        r = subprocess.run(['adb', 'connect', adb_addr],
                           capture_output=True, text=True, timeout=30)
        if ('connected' not in (r.stdout + r.stderr).lower()
                and 'already' not in (r.stdout + r.stderr).lower()):
            return 0, f'adb connect: {r.stderr or r.stdout}'
        subprocess.run(['adb', '-s', adb_addr, 'wait-for-device'],
                       capture_output=True, timeout=60)
        time.sleep(2)
        if glogin_pwd:
            try:
                r = _adb(adb_addr, 'shell', 'glogin', glogin_pwd, timeout=20)
                if 'success' not in (r.stdout + r.stderr).lower():
                    return 0, f'glogin: {r.stdout} {r.stderr}'
            except subprocess.TimeoutExpired:
                return 0, 'glogin timed out'
        try:
            _adb(adb_addr, 'shell', 'mkdir', '-p', '/sdcard/Pictures')
        except subprocess.TimeoutExpired:
            pass  # mkdir may already exist — non-fatal
        pushed = 0
        for path in image_paths:
            if not os.path.exists(path): continue
            safe_name = os.path.basename(path).replace(' ', '_')
            remote = f'/sdcard/Pictures/{safe_name}'
            try:
                r = subprocess.run(['adb', '-s', adb_addr, 'push', path, remote],
                                   capture_output=True, text=True, timeout=120)
            except subprocess.TimeoutExpired:
                logger.warning(f"[imgwiz] adb push {safe_name} timed out")
                continue
            if r.returncode != 0:
                logger.warning(f"[imgwiz] adb push {safe_name} rc={r.returncode}: {r.stderr[:200]}")
                continue
            pushed += 1
            # Best-effort media-scanner broadcast — DO NOT let its timeout
            # crash the wizard. If this fails, the file is still on disk;
            # Android's media DB will pick it up on next gallery refresh.
            try:
                _adb(adb_addr, 'shell', 'am', 'broadcast', '-a',
                     'android.intent.action.MEDIA_SCANNER_SCAN_FILE',
                     '-d', f'file://{remote}', timeout=10)
            except subprocess.TimeoutExpired:
                logger.info(f"[imgwiz] media scan broadcast for {safe_name} timed out (non-fatal)")
            except Exception as e:
                logger.info(f"[imgwiz] media scan broadcast err: {e} (non-fatal)")
        return pushed, None
    finally:
        try:
            subprocess.run(['adb', 'disconnect', adb_addr],
                           capture_output=True, timeout=10)
        except Exception: pass


# ─── Drive image downloader (for the picked Drive folder) ───────────────────

def _download_drive_folder_images(folder_id, dest_dir='/tmp'):
    """Download every image in a Drive folder to local files. Returns list of paths."""
    svc = artistic_bg_gen._drive_service()
    page_token = None
    saved = []
    while True:
        q = (f"'{folder_id}' in parents and trashed=false and "
             f"(mimeType contains 'image/' or "
             f"name contains '.jpg' or name contains '.jpeg' or name contains '.png')")
        res = svc.files().list(q=q, fields='nextPageToken, files(id,name,mimeType)',
                               pageSize=100, pageToken=page_token,
                               supportsAllDrives=True,
                               includeItemsFromAllDrives=True).execute()
        for f in res.get('files') or []:
            try:
                b = artistic_bg_gen._download_image_bytes(svc, f['id'])
                local_name = f"imgwiz_drive_{f['id']}_{f['name'].replace(' ', '_')}"
                local_path = os.path.join(dest_dir, local_name)
                with open(local_path, 'wb') as fh: fh.write(b)
                saved.append(local_path)
            except Exception as e:
                logger.warning(f"[imgwiz] Drive download {f['name']} err: {e}")
        page_token = res.get('nextPageToken')
        if not page_token: break
    return saved


# ─── Callback dispatcher ────────────────────────────────────────────────────

async def imgwiz_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data or ''
    state = context.user_data.get('imgwiz')

    # Done-button + preflight UI need to call query.answer() themselves with
    # contextual popup text, so DON'T auto-answer for those branches.
    if (data == 'imgwiz:done_collect'
            or data.startswith('imgwiz:pre:')):
        await _handle_done_or_preflight(query, context, data, state)
        return

    await query.answer()

    # 50/50 batch flow theme picker (create_plus_images mode only).
    if data.startswith('imgwiz:theme:'):
        if not state:
            await query.edit_message_text("⚠️ wizard state expired — run /geelark_profile_ig_open again.")
            return
        token = data.split(':', 2)[2]
        await _handle_theme_pick(query, context, state, token)
        return

    if data == 'imgwiz:cancel':
        context.user_data.pop('imgwiz', None)
        context.user_data.pop('expecting_imgwiz_phone_name', None)
        await query.edit_message_text("❌ cancelled.")
        return

    if data.startswith('imgwiz:mode:'):
        mode = data.split(':', 2)[2]
        if mode not in ('create_plus_images', 'create_plus_images_automated',
                         'images_only'):
            await query.edit_message_text("⚠️ unknown mode."); return
        context.user_data['imgwiz'] = {
            'mode': mode,
            'batch': [],
            'idx': 0,
            'sub_step': 'pick_profile',
            'last_bg_mode': None,
            'last_bg_path': None,
            'drive_nav_stack': [],
        }
        context.user_data['expecting_imgwiz_phone_name'] = True
        if mode == 'create_plus_images':
            await query.edit_message_text(
                "🆕 *Create + add images*\n\n"
                "Send the *GoLogin profile name* of the first GeeLark phone "
                "to create (e.g. `Caroline Goni 5`). I'll validate against "
                "GoLogin + grab the proxy.\n\n"
                "After each one you can add more — tap *Done* or type `done` "
                "to start. NOTHING gets created until ALL selections are done.",
                parse_mode='Markdown',
                reply_markup=_imgwiz_done_kb())
        else:
            await query.edit_message_text(
                "🖼 *Add images to existing GeeLark profiles*\n\n"
                "Send the GeeLark profile name of the first phone "
                "(e.g. `caro motorcycle`). After each one you can add more — "
                "tap *Done* or type `done` to start the selection wizard.",
                parse_mode='Markdown',
                reply_markup=_imgwiz_done_kb())
        return

    if not state:
        await query.edit_message_text("⚠️ wizard state expired — run /geelark_profile_open again.")
        return

    # ─── bg flow ────────────────────────────────────────────────────────────
    if data.startswith('imgwiz:bg_style:'):
        mode = data.split(':', 2)[2]
        if mode == 'skip':
            await _ask_artistic(query.message, state)
            return
        state['last_bg_mode'] = mode
        await _emit_bg(query.message, mode, state)
        return

    if data.startswith('imgwiz:bg_review:'):
        action = data.split(':', 2)[2]
        if action == 'keep':
            entry = state['batch'][state['idx']]
            if state.get('last_bg_path'):
                entry.setdefault('bg_paths', []).append(state['last_bg_path'])
            await query.edit_message_caption(
                caption=f"✅ kept — {os.path.basename(state['last_bg_path'])}",
                reply_markup=None)
            await _ask_artistic(query.message, state)
            return
        if action == 'regen':
            await _emit_bg(query.message, state.get('last_bg_mode') or 'solid', state)
            return
        if action == 'skip':
            await query.edit_message_caption(caption="⏭ skipped this BG.",
                                              reply_markup=None)
            await _ask_artistic(query.message, state)
            return

    # ─── artistic flow ──────────────────────────────────────────────────────
    if data.startswith('imgwiz:artistic:'):
        choice = data.split(':', 2)[2]
        entry = state['batch'][state['idx']]
        entry['artistic_yes'] = (choice == 'yes')
        await query.edit_message_text(
            f"{'🎨 will generate an artistic background at execution time' if entry['artistic_yes'] else '⏭ skipped artistic'} "
            f"for `{entry['name']}`.",
            parse_mode='Markdown')
        await _ask_drive(query.message, state)
        return

    # ─── Drive navigator ────────────────────────────────────────────────────
    if data == 'imgwiz:drive_skip':
        entry = state['batch'][state['idx']]
        entry['drive_folder_id'] = None
        entry['drive_folder_name'] = None
        state['drive_nav_stack'] = []
        await query.edit_message_text(
            f"⏭ skipped Drive picker for `{entry['name']}`.",
            parse_mode='Markdown')
        await _advance_or_execute(query.message, context)
        return

    if data == 'imgwiz:drive_start':
        await _send_drive_nav(query.message, state, 'root', None)
        return

    if data == 'imgwiz:drive_up':
        if state.get('drive_nav_stack') and len(state['drive_nav_stack']) > 1:
            state['drive_nav_stack'].pop()
            parent_id, parent_name = state['drive_nav_stack'][-1]
        else:
            state['drive_nav_stack'] = []
            parent_id, parent_name = 'root', None
        # Pop the destination too so _send_drive_nav re-appends it cleanly
        if state.get('drive_nav_stack'):
            state['drive_nav_stack'].pop()
        await _send_drive_nav(query.message, state, parent_id, parent_name)
        return

    if data.startswith('imgwiz:drive_descend:'):
        folder_id = data.split(':', 2)[2]
        # Resolve folder name from the just-shown level for breadcrumb
        await _send_drive_nav(query.message, state, folder_id, '…')
        return

    if data == 'imgwiz:drive_pick_current':
        if not state.get('drive_nav_stack'):
            await query.message.reply_text(
                "⚠️ no folder selected — pick one or skip.")
            return
        folder_id, folder_name = state['drive_nav_stack'][-1]
        entry = state['batch'][state['idx']]
        entry['drive_folder_id'] = folder_id
        entry['drive_folder_name'] = folder_name or '(root)'
        state['drive_nav_stack'] = []
        await query.message.reply_text(
            f"📌 picked Drive folder `{entry['drive_folder_name']}` for "
            f"`{entry['name']}` — every image in it will be pushed at "
            f"execution time.",
            parse_mode='Markdown')
        await _advance_or_execute(query.message, context)
        return


# ─── Wizard transitions ─────────────────────────────────────────────────────

async def _ask_artistic(msg, state):
    state['sub_step'] = 'artistic_question'
    entry = state['batch'][state['idx']]
    await msg.reply_text(
        f"*Step 2 — Artistic background for `{entry['name']}`*\n\n"
        f"Generate one new image based on 6 random refs from "
        f"`{artistic_bg_gen.REF_FOLDER_NAME}` (`nano-banana-pro`, ~$0.10)?\n\n"
        f"_The image is generated silently at execution time — no preview._",
        parse_mode='Markdown',
        reply_markup=_artistic_kb())


async def _ask_drive(msg, state):
    state['sub_step'] = 'drive_question'
    entry = state['batch'][state['idx']]
    await msg.reply_text(
        f"*Step 3 — Drive folder for `{entry['name']}`*\n\n"
        f"Pick a Drive folder whose images should all get pushed to this phone?",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🗂 Yes — browse Drive",
                                  callback_data='imgwiz:drive_start')],
            [InlineKeyboardButton("⏭ No, skip Drive",
                                  callback_data='imgwiz:drive_skip')],
        ]))


async def _advance_or_execute(msg, context):
    """End of selection for the current profile. Either advance to the next
    profile (re-enter bg style pick), or kick off execution."""
    state = context.user_data.get('imgwiz')
    if not state: return
    state['idx'] += 1
    state['last_bg_mode'] = None
    state['last_bg_path'] = None
    if state['idx'] < len(state['batch']):
        next_entry = state['batch'][state['idx']]
        await msg.reply_text(
            f"➡️ next profile: `{next_entry['name']}` "
            f"({state['idx']+1}/{len(state['batch'])})\n\n"
            f"*Step 1 — Standard background.* Pick a style:",
            parse_mode='Markdown',
            reply_markup=_bg_pick_style_kb())
        return
    # All selection done — execute
    await _execute_batch(msg, context)


# ─── EXECUTION phase ────────────────────────────────────────────────────────

async def _execute_batch(msg, context):
    state = context.user_data.get('imgwiz')
    if not state:
        await msg.reply_text("⚠️ state lost before execution.")
        return
    batch = state['batch']
    await msg.reply_text(
        f"🚀 *Executing image work on {len(batch)} profile(s)…*\n"
        f"For each profile I'll: generate artistic (if chosen), download Drive "
        f"images (if chosen), boot phone, push all to /sdcard/Pictures, stop phone.",
        parse_mode='Markdown')

    mode = state.get('mode', 'images_only')
    results = []
    for i, entry in enumerate(batch):
        try:
            await _process_one_profile(msg, mode, i, len(batch), entry, results)
        except Exception as e:
            logger.exception(f"[imgwiz] profile {entry.get('name')} crashed")
            await msg.reply_text(
                f"   ❌ `{entry.get('name')}`: unexpected error — `{type(e).__name__}: {e}` "
                f"— moving on to next profile",
                parse_mode='Markdown')
            results.append({'name': entry.get('name'),
                             'phone_id': entry.get('phone_id'),
                             'images_pushed': 0,
                             'err': f"{type(e).__name__}: {e}"})
            # Try to stop the phone anyway if we know its id
            if entry.get('phone_id'):
                try: _geelark_post('/phone/stop', {'ids': [entry['phone_id']]})
                except Exception: pass

    # ─── Final summary
    ok_count = sum(1 for r in results if not r['err'])
    lines = []
    if ok_count == len(results):
        lines.append(f"🟢 *ALL DONE — {ok_count}/{len(results)} profile(s) processed.*")
    elif ok_count > 0:
        lines.append(f"🟡 *{ok_count}/{len(results)} processed, {len(results)-ok_count} had issues.*")
    else:
        lines.append(f"🔴 *0/{len(results)} succeeded.*")
    lines.append("")
    for r in results:
        if r['err']:
            lines.append(f"  ❌ `{r['name']}` — {r['err']}")
        else:
            lines.append(f"  ✅ `{r['name']}` — pushed {r['images_pushed']} image(s)")
    await msg.reply_text('\n'.join(lines), parse_mode='Markdown')
    context.user_data.pop('imgwiz', None)
    return


async def _process_one_profile(msg, mode, i, total, entry, results):
    """Per-profile work; wrapped by the caller in try/except so any single
    profile's crash doesn't kill the rest of the batch."""
    await msg.reply_text(
        f"⏳ [{i+1}/{total}] `{entry['name']}` — starting",
        parse_mode='Markdown')
    all_paths = list(entry.get('bg_paths') or [])
    per_profile_err = None

    # 0. (create-plus-images only) create the GeeLark phone + install IG.
    # Duplicate guard: if a GeeLark phone with this name already exists,
    # reuse it instead of creating a duplicate. Mirrors the IG / FB batches
    # in geelark_open.py.
    if mode in ('create_plus_images', 'create_plus_images_automated') \
            and not entry.get('phone_id'):
        from geelark_open import (_geelark_find_phone_by_name,
                                   _geelark_delete_phone)
        pre_action = entry.get('preflight_action')

        # Path A — user explicitly chose Skip in preflight: reuse existing.
        if pre_action == 'skip' and entry.get('preflight_existing_phone_id'):
            existing_id = entry['preflight_existing_phone_id']
            await msg.reply_text(
                f"   ⏭ keeping existing GeeLark phone for `{entry['name']}` "
                f"(`{existing_id}`) per your preflight choice. Skipping "
                f"create + IG install; image push will boot the phone.",
                parse_mode='Markdown')
            entry['phone_id'] = existing_id

        # Path B — user chose Recreate (or didn't preflight but a duplicate
        # exists): delete the existing phone first, then create fresh.
        elif pre_action == 'recreate' or pre_action is None:
            existing_id = entry.get('preflight_existing_phone_id')
            if existing_id is None:
                # Preflight wasn't run (edge case) — fall back to live lookup
                # so we never silently double-create.
                existing_id, _ = await asyncio.to_thread(
                    _geelark_find_phone_by_name, entry['name'])
            if existing_id:
                await msg.reply_text(
                    f"   🗑 deleting existing GeeLark phone `{existing_id}` "
                    f"for `{entry['name']}` before recreating…",
                    parse_mode='Markdown')
                ok, dm = await asyncio.to_thread(_geelark_delete_phone, existing_id)
                if not ok:
                    per_profile_err = f'phone delete (before recreate): {dm}'
                    await msg.reply_text(f"   ❌ delete failed: `{dm}`",
                                          parse_mode='Markdown')
                    results.append({'name': entry['name'], 'phone_id': None,
                                     'images_pushed': 0, 'err': per_profile_err})
                    return
                await msg.reply_text(f"   ✅ deleted. Creating fresh…",
                                      parse_mode='Markdown')

        # Path C — fresh create (was new in preflight, OR after delete above).
        # Skip-path already returned above; recreate-path falls through here.
        if not entry.get('phone_id'):
            await msg.reply_text(
                f"   🆕 creating GeeLark phone for `{entry['name']}` with GoLogin proxy…",
                parse_mode='Markdown')
            phone_id, err = await asyncio.to_thread(
                _geelark_create_phone, entry['name'], entry['proxy'])
            if err or not phone_id:
                per_profile_err = f'phone create: {err}'
                await msg.reply_text(f"   ❌ phone create failed: `{err}`",
                                      parse_mode='Markdown')
                results.append({'name': entry['name'], 'phone_id': None,
                                 'images_pushed': 0, 'err': per_profile_err})
                return
            entry['phone_id'] = phone_id
            await msg.reply_text(
                f"   ✅ phone created (`{phone_id}`). Starting + booting…",
                parse_mode='Markdown')
            _, start_err = _geelark_post('/phone/start', {'ids': [phone_id]})
            if start_err:
                per_profile_err = f'/phone/start: {start_err}'
                await msg.reply_text(f"   ❌ /phone/start failed: `{start_err}`",
                                      parse_mode='Markdown')
                results.append({'name': entry['name'], 'phone_id': phone_id,
                                 'images_pushed': 0, 'err': per_profile_err})
                return
            await asyncio.to_thread(_geelark_boot_wait, phone_id, 60)
            await msg.reply_text(
                f"   ✅ booted. Installing Instagram (~2-4 min)…",
                parse_mode='Markdown')
            ig_ok, ig_msg = await asyncio.to_thread(_geelark_install_instagram, phone_id)
            if not ig_ok:
                await msg.reply_text(
                    f"   ⚠️ IG install: {ig_msg} (continuing with image push anyway)",
                    parse_mode='Markdown')
            else:
                await msg.reply_text(f"   ✅ Instagram installed.",
                                      parse_mode='Markdown')
            # phone already running from boot — keep it up for the ADB push.
            # (Skip this flag in the existing-reuse branch — the existing
            # phone is currently stopped and the push step boots it itself.)
            entry['_already_running'] = True

    # 0.5 Automated mode: auto-generate 1 normal bg + 1 random-type artistic
    # bg BEFORE the manual-flagged blocks below. The manual blocks remain
    # no-ops for this mode (we never set artistic_yes / drive_folder_id).
    if mode == 'create_plus_images_automated':
        import random as _r, time as _t
        BG_MODES = ['solid', 'gradient', 'radial', 'impressionist', 'splatter',
                    'watercolor', 'geometric', 'voronoi', 'color_field']
        bg_mode = _r.choice(BG_MODES)
        try:
            png, caption = _generate_bg_png(bg_mode)
            safe_name = entry['name'].replace(' ', '_').replace('/', '_')
            bg_path = f'/tmp/auto_bg_{safe_name}_{int(_t.time()*1000)}.png'
            with open(bg_path, 'wb') as f: f.write(png)
            all_paths.append(bg_path)
            await msg.reply_text(
                f"   🎨 auto normal bg: *{caption}* → queued",
                parse_mode='Markdown')
        except Exception as e:
            await msg.reply_text(
                f"   ⚠️ auto normal bg err: `{type(e).__name__}: {e}`",
                parse_mode='Markdown')
        await msg.reply_text(
            f"   🎨 auto artistic bg (random type) for `{entry['name']}`…",
            parse_mode='Markdown')
        try:
            _drive_id, local_path, err = await asyncio.to_thread(
                artistic_bg_gen.generate_artistic_bg_random_type,
                entry['name'], None)
            if err:
                await msg.reply_text(f"   ⚠️ auto artistic err: `{err}`",
                                      parse_mode='Markdown')
            elif local_path:
                all_paths.append(local_path)
                await msg.reply_text(
                    f"   ✅ auto artistic generated + saved to Drive",
                    parse_mode='Markdown')
        except Exception as e:
            await msg.reply_text(
                f"   ⚠️ auto artistic crashed: `{type(e).__name__}: {e}`",
                parse_mode='Markdown')

    # 0.7 create_plus_images 50/50 split: honor the auto-assigned role.
    # 'standard' → one PNG from the restricted 5-mode palette pushed to phone
    # 'artistic' → one image from the chosen Drive theme (or random if user
    #             picked 'random per artistic profile')
    if mode == 'create_plus_images' and entry.get('auto_bg_role') == 'standard':
        import random as _r, time as _t
        bg_mode = entry.get('auto_bg_mode') or _r.choice(AUTO_STANDARD_BG_MODES)
        try:
            png, caption = _generate_bg_png(bg_mode)
            safe_name = entry['name'].replace(' ', '_').replace('/', '_')
            bg_path = f'/tmp/auto_bg_{safe_name}_{int(_t.time()*1000)}.png'
            with open(bg_path, 'wb') as f: f.write(png)
            all_paths.append(bg_path)
            await msg.reply_text(
                f"   🎲 standard bg ({bg_mode}): *{caption}* → queued",
                parse_mode='Markdown')
        except Exception as e:
            await msg.reply_text(
                f"   ⚠️ standard bg err: `{type(e).__name__}: {e}`",
                parse_mode='Markdown')

    if mode == 'create_plus_images' and entry.get('auto_bg_role') == 'artistic':
        theme = entry.get('_artistic_theme') or {}
        kind = theme.get('kind', 'random')
        await msg.reply_text(
            f"   🎨 artistic bg for `{entry['name']}`"
            + (f" (theme: `{theme.get('folder_name')}`)"
               if kind == 'folder' else " (theme: 🎲 random)") + "…",
            parse_mode='Markdown')
        try:
            if kind == 'folder':
                _drive_id, local_path, err = await asyncio.to_thread(
                    artistic_bg_gen.generate_artistic_bg_from_folder,
                    theme['folder_id'], theme.get('folder_name'),
                    entry['name'], None)
            else:
                _drive_id, local_path, err = await asyncio.to_thread(
                    artistic_bg_gen.generate_artistic_bg_random_type,
                    entry['name'], None)
            if err:
                await msg.reply_text(f"   ⚠️ artistic err: `{err}`",
                                      parse_mode='Markdown')
            elif local_path:
                all_paths.append(local_path)
                await msg.reply_text(
                    f"   ✅ artistic generated + saved to Drive",
                    parse_mode='Markdown')
        except Exception as e:
            await msg.reply_text(
                f"   ⚠️ artistic crashed: `{type(e).__name__}: {e}`",
                parse_mode='Markdown')

    # 1. Artistic generation (if flagged)
    if entry.get('artistic_yes'):
        await msg.reply_text(
            f"   🎨 generating artistic image for `{entry['name']}`…",
            parse_mode='Markdown')
        try:
            drive_id, local_path, err = await asyncio.to_thread(
                artistic_bg_gen.generate_artistic_bg,
                entry['name'],   # save to Drive subfolder named after the profile
                None)
            if err:
                await msg.reply_text(f"   ⚠️ artistic err: `{err}`",
                                      parse_mode='Markdown')
            elif local_path:
                all_paths.append(local_path)
                await msg.reply_text(
                    f"   ✅ artistic generated + saved to Drive (id `{drive_id}`)",
                    parse_mode='Markdown')
        except Exception as e:
            await msg.reply_text(f"   ⚠️ artistic crashed: `{type(e).__name__}: {e}`",
                                  parse_mode='Markdown')

    # 2. Drive folder images (if picked)
    if entry.get('drive_folder_id'):
        await msg.reply_text(
            f"   📁 downloading images from `{entry.get('drive_folder_name')}`…",
            parse_mode='Markdown')
        try:
            paths = await asyncio.to_thread(
                _download_drive_folder_images,
                entry['drive_folder_id'], '/tmp')
            all_paths.extend(paths)
            await msg.reply_text(
                f"   ✅ {len(paths)} image(s) downloaded from Drive",
                parse_mode='Markdown')
        except Exception as e:
            await msg.reply_text(
                f"   ⚠️ Drive download err: `{type(e).__name__}: {e}`",
                parse_mode='Markdown')

    # 3. Upload images to the phone via GeeLark's native API (visible in Gallery)
    if all_paths:
        # The /phone/uploadFile endpoint requires the phone to be running. If
        # we got here from images_only mode, the phone is stopped → boot it.
        if not entry.get('_already_running'):
            await msg.reply_text(
                f"   📲 booting `{entry['name']}` (~60s) before upload…",
                parse_mode='Markdown')
            _, start_err = _geelark_post('/phone/start', {'ids': [entry['phone_id']]})
            if start_err:
                per_profile_err = f'/phone/start: {start_err}'
                await msg.reply_text(f"   ❌ {per_profile_err}",
                                      parse_mode='Markdown')
                pushed = 0
            else:
                await asyncio.to_thread(_geelark_boot_wait, entry['phone_id'], 60)
                entry['_already_running'] = True
        if entry.get('_already_running'):
            await msg.reply_text(
                f"   📤 uploading {len(all_paths)} image(s) to `{entry['name']}` "
                f"via GeeLark's gallery API…",
                parse_mode='Markdown')
            pushed, perr = await asyncio.to_thread(
                _geelark_upload_to_phone_gallery,
                entry['phone_id'], all_paths)
            if perr:
                per_profile_err = perr
                await msg.reply_text(f"   ⚠️ upload err: {perr}",
                                      parse_mode='Markdown')
            else:
                await msg.reply_text(
                    f"   ✅ uploaded {pushed}/{len(all_paths)} image(s) — visible in Gallery + IG picker",
                    parse_mode='Markdown')
        _geelark_post('/phone/stop', {'ids': [entry['phone_id']]})
        await msg.reply_text(f"   🛑 stopped `{entry['name']}`.",
                              parse_mode='Markdown')
    elif entry.get('_already_running'):
        # create-plus-images mode but no images queued — still stop the
        # phone since we started it for IG install
        pushed = 0
        _geelark_post('/phone/stop', {'ids': [entry['phone_id']]})
        await msg.reply_text(
            f"   ⏭ no images for `{entry['name']}` — IG installed, phone stopped.",
            parse_mode='Markdown')
    else:
        pushed = 0
        await msg.reply_text(
            f"   ⏭ no images queued for `{entry['name']}` — nothing to push.",
            parse_mode='Markdown')

    results.append({
        'name': entry['name'], 'phone_id': entry['phone_id'],
        'images_pushed': pushed,
        'err': per_profile_err,
    })


# ─── Text router entry: name-collection for the batch ──────────────────────

async def imgwiz_text_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        await _wiz_done_kickoff(update.message, context, state)
        return True
    # Mode-dependent name lookup:
    #   create_plus_images → validate GoLogin profile + grab proxy (phone
    #                        will be created at execution time)
    #   images_only        → validate existing GeeLark phone
    mode = state.get('mode', 'images_only')
    if mode in ('create_plus_images', 'create_plus_images_automated'):
        await update.message.reply_text(
            f"🔍 finding GoLogin profile `{text}`…", parse_mode='Markdown')
        gologin_id, err = await asyncio.to_thread(_gologin_find_profile_by_name, text)
        if err or not gologin_id:
            await update.message.reply_text(
                f"❌ {err or 'not found'}\nTry another name, or tap *Done* / type `done`.",
                parse_mode='Markdown',
                reply_markup=_imgwiz_done_kb())
            return True
        proxy, err = await asyncio.to_thread(_gologin_get_proxy, gologin_id)
        if err or not proxy:
            await update.message.reply_text(
                f"❌ proxy fetch failed: {err}", parse_mode='Markdown')
            return True
        if any(e['name'].lower() == text.lower() for e in state['batch']):
            await update.message.reply_text(f"⚠️ `{text}` already queued — skipping.",
                                              parse_mode='Markdown')
            return True
        state['batch'].append({
            'name': text,
            'gologin_id': gologin_id,
            'proxy': proxy,
            'phone_id': None,         # filled in at execution time
            'bg_paths': [],
            'artistic_yes': False,
            'drive_folder_id': None,
            'drive_folder_name': None,
        })
    else:
        await update.message.reply_text(f"🔍 finding GeeLark phone `{text}`…",
                                         parse_mode='Markdown')
        phone_id, err = await asyncio.to_thread(_find_geelark_phone_by_name, text)
        if err or not phone_id:
            await update.message.reply_text(
                f"❌ {err or 'not found'}\nTry another name, or tap *Done* / type `done`.",
                parse_mode='Markdown',
                reply_markup=_imgwiz_done_kb())
            return True
        if any(e['name'].lower() == text.lower() for e in state['batch']):
            await update.message.reply_text(f"⚠️ `{text}` already queued — skipping.",
                                              parse_mode='Markdown')
            return True
        state['batch'].append({
            'name': text, 'phone_id': phone_id,
            'gologin_id': None, 'proxy': None,
            'bg_paths': [],
            'artistic_yes': False,
            'drive_folder_id': None,
            'drive_folder_name': None,
        })
    queue = '\n'.join(f"  {i+1}. `{e['name']}`" for i, e in enumerate(state['batch']))
    await update.message.reply_text(
        f"✅ added `{text}`. Queue ({len(state['batch'])}):\n{queue}\n\n"
        f"Send another name, or tap *Done* / type `done`.",
        parse_mode='Markdown',
        reply_markup=_imgwiz_done_kb())
    return True


def _find_geelark_phone_by_name(name):
    from geelark_open import _geelark_find_phone_by_name as _f
    return _f(name)


# ─── Done button + preflight existence check (IG / create_plus_images) ─────

def _imgwiz_done_kb():
    """Inline button used on every prompt that previously told the user to
    'type done'. Tap-to-finish, additive to the typed sentinel."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Done — start", callback_data="imgwiz:done_collect")
    ]])


async def _wiz_done_kickoff(target_msg, context, state):
    """Called when the user finishes adding profiles (typed `done` or tapped
    the Done button). For 'create_plus_images' mode, runs a pre-flight check
    against GeeLark for each name; on collisions, shows a per-profile
    Recreate/Skip decision UI. For 'images_only' mode (intentionally
    operating on existing phones), goes straight to the bg-style picker.
    """
    mode = state.get('mode', 'images_only')
    if mode not in ('create_plus_images', 'create_plus_images_automated'):
        await _wiz_proceed_to_selection(target_msg, context, state)
        return

    await target_msg.reply_text(
        f"🔎 checking GeeLark for existing phones in your batch of "
        f"{len(state['batch'])}…",
        parse_mode='Markdown')
    from geelark_open import _preflight_partition
    new_indices, existing = await asyncio.to_thread(_preflight_partition, state['batch'])

    if not existing:
        await target_msg.reply_text(
            f"✅ no existing phones found — every entry is new. Continuing.",
            parse_mode='Markdown')
        await _wiz_proceed_to_selection(target_msg, context, state)
        return

    # Default decision: recreate (matches "I wanted a NEW profile" intent).
    # User can flip each one to Skip via the inline buttons.
    state['preflight'] = {
        'new_indices': new_indices,
        'existing': [{'idx': e['idx'], 'name': e['name'],
                      'phone_id': e['phone_id'], 'action': 'recreate'}
                     for e in existing],
    }
    await _wiz_render_preflight(target_msg, state)


def _wiz_preflight_kb(state):
    """Radio-style: a ✅ prefix marks the currently-selected option. Easier
    to spot on mobile than the prior uppercase-vs-lowercase styling."""
    rows = []
    pre = state['preflight']
    for j, e in enumerate(pre['existing']):
        is_rec = e['action'] == 'recreate'
        rec_lbl = '✅ 🗑 Recreate' if is_rec else '🗑 Recreate'
        skp_lbl = '✅ ⏭ Skip'      if not is_rec else '⏭ Skip'
        rows.append([
            InlineKeyboardButton(rec_lbl, callback_data=f"imgwiz:pre:tog:{j}:r"),
            InlineKeyboardButton(skp_lbl, callback_data=f"imgwiz:pre:tog:{j}:s"),
        ])
    rows.append([
        InlineKeyboardButton("✅ Proceed with selections", callback_data="imgwiz:pre:go"),
    ])
    rows.append([
        InlineKeyboardButton("❌ Cancel batch", callback_data="imgwiz:pre:cancel"),
    ])
    return InlineKeyboardMarkup(rows)


def _wiz_preflight_text(state):
    pre = state['preflight']
    new_count = len(pre['new_indices'])
    lines = [
        f"⚠️ *{len(pre['existing'])} of {len(state['batch'])} profiles already "
        f"exist on GeeLark.*",
        "",
        "Per existing profile, pick *Recreate* (delete + create fresh with your "
        "GoLogin proxy) or *Skip* (keep it as-is, push images on top):",
        "",
    ]
    for j, e in enumerate(pre['existing']):
        marker = "🗑 will RECREATE" if e['action'] == 'recreate' else "⏭ will SKIP"
        lines.append(f"{j+1}. `{e['name']}` (phone id `{e['phone_id']}`) — {marker}")
    if new_count:
        lines.append("")
        lines.append(f"*Plus {new_count} new profile(s) that will be created normally:*")
        for i in pre['new_indices']:
            lines.append(f"  • `{state['batch'][i]['name']}`")
    lines.append("")
    lines.append("Tap *Proceed* once your decisions are set.")
    return '\n'.join(lines)


async def _wiz_render_preflight(target_msg, state):
    await target_msg.reply_text(_wiz_preflight_text(state),
                                 parse_mode='Markdown',
                                 reply_markup=_wiz_preflight_kb(state))


async def _wiz_proceed_to_selection(target_msg, context, state):
    """Three branches based on mode:
      - create_plus_images_automated → skip everything, _execute_batch
        (each profile gets normal AND artistic, fully random)
      - create_plus_images → ask once for the artistic theme, then auto
        50/50 split (half profiles standard bg from a restricted 5-mode
        palette, half artistic from chosen theme), then _execute_batch.
        No more per-profile manual picker, no Drive-folder step.
      - images_only → existing per-profile manual bg picker.
    """
    if state.get('mode') == 'create_plus_images_automated':
        await target_msg.reply_text(
            f"🤖 *Automated mode — kicking off execution on "
            f"{len(state['batch'])} profile(s).*\n\n"
            f"Per profile: 1 random normal bg (jittered palette) + 1 random "
            f"artistic bg (random Drive type) → pushed to phone gallery.",
            parse_mode='Markdown')
        await _execute_batch(target_msg, context)
        return
    if state.get('mode') == 'create_plus_images':
        # No more intermediate questions after preflight: theme is always
        # 'random per artistic profile', the 50/50 split happens here, and
        # we go straight to execution. Per user spec — every additional
        # question between Done and execute is friction.
        state['artistic_theme'] = {'kind': 'random'}
        _assign_5050_to_batch(state)
        n_art = sum(1 for e in state['batch']
                    if e.get('auto_bg_role') == 'artistic')
        n_std = len(state['batch']) - n_art
        lines = [
            f"🚀 *Kicking off — {len(state['batch'])} profile(s).*",
            f"📊 Split: {n_art} artistic (random theme/profile) · "
            f"{n_std} standard (random style/profile)",
            '',
        ]
        for e in state['batch']:
            if e.get('auto_bg_role') == 'artistic':
                lines.append(f"  • `{e['name']}` → 🎨 artistic")
            else:
                lines.append(f"  • `{e['name']}` → 🎲 standard "
                              f"(`{e.get('auto_bg_mode')}`)")
        await target_msg.reply_text('\n'.join(lines), parse_mode='Markdown')
        await _execute_batch(target_msg, context)
        return
    first = state['batch'][0]
    await target_msg.reply_text(
        f"🚀 *Starting selection for {len(state['batch'])} profile(s).*\n\n"
        f"➡️ first: `{first['name']}` (1/{len(state['batch'])})\n\n"
        f"*Step 1 — Standard background.* Pick a style:",
        parse_mode='Markdown',
        reply_markup=_bg_pick_style_kb())


# ─── 50/50 batch flow for create_plus_images ───────────────────────────────

# Standard-bg modes the user accepts for the automated split. The other
# four modes (impressionist, splatter, geometric, voronoi) are intentionally
# excluded — they were too noisy in the user's prior runs.
AUTO_STANDARD_BG_MODES = ['color_field', 'watercolor', 'radial',
                           'gradient', 'solid']


async def _ask_artistic_theme(target_msg, context, state):
    """List every `Images bg *` folder on Drive + a 'Random per artistic
    profile' option. After the pick, auto-assign 50/50 and execute."""
    import asyncio
    try:
        types = await asyncio.to_thread(
            lambda: artistic_bg_gen._list_art_type_folders(
                artistic_bg_gen._drive_service()))
    except Exception as e:
        await target_msg.reply_text(
            f"⚠️ couldn't list artistic themes from Drive: "
            f"`{type(e).__name__}: {e}`. Cancelling — run the command again.",
            parse_mode='Markdown')
        return
    if not types:
        await target_msg.reply_text(
            f"⚠️ no `Images bg *` folders on Drive — can't pick an artistic "
            f"theme. Add at least one folder (e.g. `Images bg goth artistic`) "
            f"and rerun.",
            parse_mode='Markdown')
        return

    state['_theme_options'] = types
    rows = []
    for idx, t in enumerate(types):
        label = t['name'].replace('Images bg ', '🎨 ')
        rows.append([InlineKeyboardButton(
            label, callback_data=f"imgwiz:theme:{idx}")])
    rows.append([InlineKeyboardButton(
        "🎲 Random per artistic profile",
        callback_data="imgwiz:theme:random")])
    rows.append([InlineKeyboardButton("❌ Cancel",
                                       callback_data="imgwiz:cancel")])

    await target_msg.reply_text(
        f"🎨 *Pick an artistic theme for this batch* "
        f"({len(state['batch'])} profile(s))\n\n"
        f"After this, the bot auto-assigns 50/50: half the profiles get a "
        f"standard bg from "
        f"`{', '.join(AUTO_STANDARD_BG_MODES)}`, the other half get an "
        f"artistic bg from the theme you pick below.",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(rows))


def _assign_5050_to_batch(state):
    """Mutate state['batch']: assign auto_bg_role ('standard' or 'artistic')
    to each entry, even split (artistic wins the odd one out). Standard
    entries also get a randomly-chosen auto_bg_mode; artistic entries get
    a copy of the chosen theme so _process_one_profile can read it
    directly off the entry."""
    import random
    n = len(state['batch'])
    indices = list(range(n))
    random.shuffle(indices)
    n_artistic = (n + 1) // 2          # artistic wins ties
    artistic_ids = set(indices[:n_artistic])
    theme = state.get('artistic_theme') or {'kind': 'random'}
    for i, entry in enumerate(state['batch']):
        if i in artistic_ids:
            entry['auto_bg_role'] = 'artistic'
            entry['_artistic_theme'] = dict(theme)
        else:
            entry['auto_bg_role'] = 'standard'
            entry['auto_bg_mode'] = random.choice(AUTO_STANDARD_BG_MODES)


async def _handle_theme_pick(q, context, state, token):
    """Capture the artistic theme choice + run the 50/50 split + execute."""
    types = state.get('_theme_options') or []
    if token == 'random':
        state['artistic_theme'] = {'kind': 'random'}
        pretty = '🎲 Random per artistic profile'
    else:
        try:
            i = int(token)
        except ValueError:
            await q.edit_message_text("⚠️ stale theme pick — run the command again.")
            return
        if i < 0 or i >= len(types):
            await q.edit_message_text("⚠️ stale theme pick — run the command again.")
            return
        state['artistic_theme'] = {'kind': 'folder',
                                    'folder_id': types[i]['id'],
                                    'folder_name': types[i]['name']}
        pretty = f"🎨 {types[i]['name']}"
    state.pop('_theme_options', None)

    _assign_5050_to_batch(state)
    n_artistic = sum(1 for e in state['batch']
                      if e.get('auto_bg_role') == 'artistic')
    n_standard = len(state['batch']) - n_artistic
    lines = [
        f"✅ *Theme:* {pretty}",
        f"📊 *Split:* {n_artistic} artistic · {n_standard} standard",
        '',
        '*Per-profile assignment:*',
    ]
    for e in state['batch']:
        if e.get('auto_bg_role') == 'artistic':
            lines.append(f"  • `{e['name']}` → 🎨 artistic")
        else:
            lines.append(f"  • `{e['name']}` → 🎲 standard "
                          f"(`{e.get('auto_bg_mode')}`)")
    lines.append('')
    lines.append('Kicking off execution now…')
    await q.edit_message_text('\n'.join(lines), parse_mode='Markdown')
    await _execute_batch(q.message, context)


async def _handle_done_or_preflight(q, context, data, state):
    """Done-button taps + preflight decision callbacks for the wizard.
    This handler owns query.answer() because the toggle branch wants to
    show a contextual popup ("Kami: Recreate selected") — that gives the
    user click feedback even on re-clicks where the keyboard would render
    identically and Telegram silently rejects the edit with "message is
    not modified".
    Patterns:
      imgwiz:done_collect          — same as typing 'done'
      imgwiz:pre:tog:{j}:{r|s}     — flip existing entry #j to recreate/skip
      imgwiz:pre:go                — finish preflight, continue to bg picker
      imgwiz:pre:cancel            — cancel batch
    """
    if data == 'imgwiz:done_collect':
        await q.answer()
        if not state or not state.get('batch'):
            await q.message.reply_text("⚠️ no active batch.")
            return
        context.user_data.pop('expecting_imgwiz_phone_name', None)
        await _wiz_done_kickoff(q.message, context, state)
        return

    if not state or not state.get('preflight'):
        await q.answer("⚠️ session expired")
        await q.message.reply_text("⚠️ wizard state expired — run the command again.")
        return

    if data == 'imgwiz:pre:cancel':
        await q.answer("❌ cancelled")
        context.user_data.pop('imgwiz', None)
        await q.edit_message_text("❌ batch cancelled. Nothing was created.")
        return

    if data.startswith('imgwiz:pre:tog:'):
        parts = data.split(':')
        if len(parts) != 5:
            await q.answer()
            return
        try:
            j = int(parts[3])
        except ValueError:
            await q.answer()
            return
        action = 'recreate' if parts[4] == 'r' else 'skip'
        pre = state['preflight']
        if 0 <= j < len(pre['existing']):
            pre['existing'][j]['action'] = action
            entry_name = pre['existing'][j].get('name', f"#{j+1}")
            label = '🗑 Recreate' if action == 'recreate' else '⏭ Skip'
            await q.answer(f"{label}: {entry_name[:40]}", show_alert=False)
        else:
            await q.answer()
        try:
            await q.edit_message_text(_wiz_preflight_text(state),
                                       parse_mode='Markdown',
                                       reply_markup=_wiz_preflight_kb(state))
        except Exception:
            # 400 "message is not modified" if user re-clicked same option.
            # That's fine — popup already gave feedback.
            pass
        return

    if data == 'imgwiz:pre:go':
        await q.answer("✅ Proceeding…")
        pre = state['preflight']
        # Write decisions back to each batch entry so _run_one_entry can honor them.
        for e in pre['existing']:
            entry = state['batch'][e['idx']]
            entry['preflight_action'] = e['action']
            entry['preflight_existing_phone_id'] = e['phone_id']
        # New entries get a no-op marker so the executor can branch cleanly.
        for i in pre['new_indices']:
            state['batch'][i]['preflight_action'] = 'create_new'
        state.pop('preflight', None)
        n_rec = sum(1 for e in pre['existing'] if e['action'] == 'recreate')
        n_skp = sum(1 for e in pre['existing'] if e['action'] == 'skip')
        n_new = len(pre['new_indices'])
        await q.edit_message_text(
            f"✅ decisions saved: *{n_rec}* recreate, *{n_skp}* skip, "
            f"*{n_new}* new.\n\nContinuing to image selection…",
            parse_mode='Markdown')
        await _wiz_proceed_to_selection(q.message, context, state)
        return
