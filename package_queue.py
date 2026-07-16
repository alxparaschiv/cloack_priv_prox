"""package_queue.py — autonomous HOP-2 poller (2026-07-15).

Reads reel-bot's `📦 DAILY PACKAGE QUEUE.json` (one request per finished /daily
stack) and, per NEW request, generates a full account package by REUSING
account_pack.generate_packages — so `/account_pack` (manual) and this autonomous
path share ONE code path (registry, native Sheet, rambler/proxy pools, page
names, block words, bg image, all the new fields). Every 5th stack
(request.wants_backup_manager) also generates a backup-manager account.

Idempotent via a cloak-owned seen-set (`📦 DAILY PACKAGE SEEN.json`) so each
request is processed exactly once. No env toggle — runs on the single cloak
instance's startup (see bot.py). generate_packages already persists via R.commit,
so this module adds no registry code.
"""
import json
import logging

import account_pack
import fb_poster_registry as R

logger = logging.getLogger(__name__)

QUEUE_NAME = '📦 DAILY PACKAGE QUEUE.json'   # written by reel-bot
SEEN_NAME = '📦 DAILY PACKAGE SEEN.json'      # written by cloak only (no race)


def _read_json(name, default):
    drive = R._drive()
    if not drive:
        return default, None
    fid = R._find_id(name, drive)
    if not fid:
        return default, None
    raw = R._download_text(fid, drive)
    try:
        return (json.loads(raw) if raw.strip() else default), fid
    except Exception as e:
        logger.warning(f"[pkg-queue] parse {name}: {e}")
        return default, fid


def _write_json(name, obj, fid=None):
    drive = R._drive()
    if not drive:
        return None
    media = R._media(json.dumps(obj, ensure_ascii=False, indent=2).encode('utf-8'),
                     'application/json')
    if fid is None:
        fid = R._find_id(name, drive)
    if fid:
        drive.files().update(fileId=fid, media_body=media,
                             supportsAllDrives=True).execute()
        return fid
    created = drive.files().create(
        body={'name': name, 'mimeType': 'application/json'},
        media_body=media, fields='id', supportsAllDrives=True).execute()
    return created.get('id')


def _read_queue():
    obj, _ = _read_json(QUEUE_NAME, {'requests': []})
    reqs = obj.get('requests') if isinstance(obj, dict) else None
    return reqs if isinstance(reqs, list) else []


def _read_seen():
    obj, fid = _read_json(SEEN_NAME, {'seen': []})
    seen = obj.get('seen') if isinstance(obj, dict) else None
    return (set(seen) if isinstance(seen, list) else set()), fid


def _save_seen(seen, fid):
    return _write_json(SEEN_NAME, {'seen': sorted(seen)}, fid)


def _handle_of(req):
    return (req.get('cloak_slug') or req.get('cloak_link') or '').strip()


def _gen(kind, model, req_id, handle=None, folder=None,
         va_label='VA001', va_chat_id=None):
    """Reserve + generate ONE package via account_pack (no Telegram objects),
    into the VA's OWN per-VA registry (so each VA numbers from 001)."""
    reserve = R.reserve(1, kind, va_label=va_label)
    if not reserve.get('ok'):
        logger.warning(f"[pkg-queue] reserve({kind}) failed: {reserve.get('err')}")
        return False
    account_pack.generate_packages(
        1, reserve, model,
        emit=lambda *a, **k: None, post_one=lambda *a, **k: None,
        handles=([handle] if handle is not None else None),
        output_folders=([folder] if folder is not None else None),
        source_req_ids=[req_id],
        va_label=va_label, va_chat_id=va_chat_id)
    return True


def _available_proxies(va_label):
    """Count validated proxies FREE for this VA (pool minus already-assigned) —
    mirrors reserve()'s proxy math so we can tell if a real proxy will be assigned.
    Fail-OPEN (returns a big number) on any error so a check bug never blocks delivery."""
    try:
        drive = R._drive()
        if not drive:
            return 99
        plines, _ = R._load_proxy(drive)
        store, _ = R._load_store(drive, R.registry_json_name(va_label))
        used = {a.get('proxy', '') for a in store.get('accounts', []) if a.get('proxy')}
        return len([p for p in plines if p and p not in used])
    except Exception as e:
        logger.warning(f"[pkg-queue] proxy availability check failed: {e}")
        return 99


def _req_stale(req, max_age_sec=900):
    """True if the request is older than max_age (default 15min) — used so a proxy-
    source outage can't defer a package forever; after the window we build anyway."""
    try:
        import time as _t, calendar as _cal
        fin = req.get('finished_utc') or ''
        if not fin:
            return False
        return (_t.time() - _cal.timegm(_t.strptime(fin, '%Y-%m-%dT%H:%M:%SZ'))) > max_age_sec
    except Exception:
        return False


def poll_once():
    """One pass: process every unseen /daily request. Returns (n_accounts, n_bms)."""
    reqs = _read_queue()
    if not reqs:
        return 0, 0
    seen, seen_fid = _read_seen()
    n_acct = n_bm = 0
    for req in reqs:
        rid = req.get('req_id')
        if not rid or rid in seen:
            continue
        if str(req.get('source') or 'daily') != 'daily':
            continue                                  # scope guard: /daily only
        model = req.get('model') or 'Carolina'
        va_label = req.get('va_label') or 'VA001'   # per-VA registry → numbers from 001
        va_chat_id = req.get('va_chat_id')
        # HOLD-FOR-PROXY (2026-07-16): reel-bot's autofill validates a proxy per
        # request (~a few min via GoLogin). Building instantly ships a "pending"
        # proxy card (the round-2 symptom). Defer — leave this request UNSEEN so the
        # next 120s poll retries — until a free proxy exists. Safety valve: after the
        # request is >15min old, build anyway so a proxy-source outage can't wedge
        # delivery forever.
        if not _req_stale(req) and _available_proxies(va_label) < 1:
            logger.info(f"[pkg-queue] {rid}: no free validated proxy yet — deferring "
                        f"(retry next cycle once the autofill fills the pool)")
            continue
        if _gen('primary', model, rid,
                handle=_handle_of(req),
                folder=(req.get('output_folder_name') or ''),
                va_label=va_label, va_chat_id=va_chat_id):
            n_acct += 1
        if req.get('wants_backup_manager'):
            if _gen('backup_manager', account_pack.BACKUP_MODEL, rid + '-bm',
                    va_label=va_label, va_chat_id=va_chat_id):
                n_bm += 1
        seen.add(rid)
        seen_fid = _save_seen(seen, seen_fid)        # persist after each request
    if n_acct or n_bm:
        logger.info(f"[pkg-queue] produced {n_acct} account(s) + {n_bm} BM(s)")
    return n_acct, n_bm


async def poll_loop(bot=None, admin_chat_id=None, poll_seconds=120):
    """Guarded infinite poll loop — runs on the single cloak instance."""
    import asyncio
    logger.info(f"[pkg-queue] poll loop started (every {poll_seconds}s)")
    while True:
        try:
            n_acct, n_bm = await asyncio.get_event_loop().run_in_executor(
                None, poll_once)
            if (n_acct or n_bm) and bot and admin_chat_id:
                try:
                    await bot.send_message(
                        chat_id=admin_chat_id,
                        text=(f"🏭 packaged {n_acct} account(s) + {n_bm} "
                              f"backup-manager(s) from /daily"))
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"[pkg-queue] poll iter: {e}")
        await asyncio.sleep(poll_seconds)
