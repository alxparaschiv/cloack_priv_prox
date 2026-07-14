"""fb_poster_registry.py — persistent registry for /account_pack accounts.

Everything lives on Google Drive (Railway's local FS is wiped on redeploy) using
the existing 'drive'-scope token (no Sheets API needed):

  • FB META POSTER · accounts.json  — source of truth + the sequential counter
                                       (double backup, machine-readable)
  • FB META POSTER · accounts       — a NATIVE Google Sheet (CSV auto-converted
                                       by Drive on import) the user gets a link to
  • FB META POSTER · rambler pool.txt — user-supplied pool of `email:password`
                                       lines; one is consumed per account (from
                                       the bottom) and removed from the file.

Naming: accounts are numbered FB META POSTER 001, 002, … — the counter is just
`len(accounts) + 1`, persisted in the JSON so it survives restarts.

If Drive is unreachable we ABORT (caller must not rent numbers) so we never
assign duplicate numbers.
"""
import io
import csv
import json
import zipfile
import logging
import datetime

import accounts_sheet as _asheet   # reuse its drive-scope creds/service

logger = logging.getLogger(__name__)

NAME_PREFIX = 'FB META POSTER'
JSON_NAME = 'FB META POSTER · accounts.json'
SHEET_NAME = 'FB META POSTER · accounts'
# User-created file. Keep the primary name simple/typeable; also accept a few
# variants so it's found however they name it.
RAMBLER_POOL_NAME = 'rambler_pool.txt'
RAMBLER_POOL_ALIASES = ['rambler_pool.txt', 'rambler pool.txt',
                        'FB META POSTER · rambler pool.txt',
                        'FB META POSTER rambler pool.txt']

SHEET_HEADER = ['Account', 'Model', 'First Name', 'Last Name', 'Gender',
                'Heritage', 'Birthdate', 'Age', 'Password', 'Rambler Email',
                'Rambler Password', 'FB Phone (10-digit)', 'Rental ID',
                'App Name', 'Privacy Policy URL', 'FB Page Name 1',
                'FB Page Name 2', 'Page Category', 'Bio', 'Block Countries',
                'Block Words', 'Created (UTC)']

_SHEET_MIME = 'application/vnd.google-apps.spreadsheet'


def _drive():
    return _asheet._drive_service()


def _find_id(name, drive=None):
    drive = drive or _drive()
    if not drive:
        return None
    q = f"name='{name}' and trashed=false"
    res = drive.files().list(q=q, fields='files(id,name,mimeType)',
                             supportsAllDrives=True,
                             includeItemsFromAllDrives=True).execute()
    files = res.get('files') or []
    return files[0]['id'] if files else None


def _download_text(fid, drive):
    try:
        return drive.files().get_media(fileId=fid).execute().decode('utf-8', 'replace')
    except Exception as e:
        logger.warning(f"[fb_registry] download {fid} failed: {e}")
        return ''


def _media(data_bytes, mime):
    from googleapiclient.http import MediaInMemoryUpload
    return MediaInMemoryUpload(data_bytes, mimetype=mime, resumable=False)


# ─── JSON store (accounts + last batch) ─────────────────────────────────────

def _load_store(drive):
    """Return {'accounts': [...], 'last_batch': [...]}. Empty on first use."""
    fid = _find_id(JSON_NAME, drive)
    if not fid:
        return {'accounts': [], 'last_batch': []}, None
    raw = _download_text(fid, drive)
    try:
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        data = {}
    if isinstance(data, list):        # legacy bare list
        data = {'accounts': data, 'last_batch': []}
    data.setdefault('accounts', [])
    data.setdefault('last_batch', [])
    return data, fid


def _save_store(drive, store, fid):
    body_bytes = json.dumps(store, ensure_ascii=False, indent=2).encode('utf-8')
    media = _media(body_bytes, 'application/json')
    if fid:
        drive.files().update(fileId=fid, media_body=media,
                             supportsAllDrives=True).execute()
    else:
        drive.files().create(body={'name': JSON_NAME, 'mimeType': 'application/json'},
                             media_body=media, fields='id',
                             supportsAllDrives=True).execute()


# ─── Rambler pool ───────────────────────────────────────────────────────────

def _load_rambler(drive):
    """Return (lines, fid). Each line is a raw 'email:password[:junk]' string.
    Tries a few filename variants so the user's file is found however named."""
    fid = None
    for name in RAMBLER_POOL_ALIASES:
        fid = _find_id(name, drive)
        if fid:
            break
    if not fid:
        return [], None
    text = _download_text(fid, drive)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and ':' in ln]
    return lines, fid


def _save_rambler(drive, lines, fid):
    if fid is None:
        return  # never had a pool; nothing to write back
    media = _media(('\n'.join(lines) + ('\n' if lines else '')).encode('utf-8'),
                   'text/plain')
    drive.files().update(fileId=fid, media_body=media,
                         supportsAllDrives=True).execute()


def _parse_rambler(line):
    """Format: email:password:junk — take ONLY email + password, ignore the
    rest (e.g. 'a@rambler.ru:pass:extra' → ('a@rambler.ru', 'pass'))."""
    parts = line.split(':')
    email = parts[0].strip() if parts else ''
    pw = parts[1].strip() if len(parts) > 1 else ''
    return email, pw


# ─── Native Google Sheet (CSV → convert) ────────────────────────────────────

def _sheet_rows(accounts):
    rows = [SHEET_HEADER]
    for a in accounts:
        rows.append([
            a.get('account', ''), a.get('model', ''), a.get('first', ''),
            a.get('last', ''), a.get('gender', ''), a.get('heritage', ''),
            a.get('birthdate_display', a.get('birthdate', '')),
            str(a.get('age', '')), a.get('password', ''),
            a.get('rambler_email', ''), a.get('rambler_password', ''),
            a.get('phone10', ''), a.get('rental_id', ''),
            a.get('app_name', ''), a.get('privacy_url', ''),
            a.get('page_name_1', ''), a.get('page_name_2', ''),
            a.get('page_category', ''), a.get('bio', ''),
            a.get('block_countries', ''), a.get('block_words', ''),
            a.get('created_utc', ''),
        ])
    return rows


def _write_sheet(drive, accounts):
    """Rebuild the native Google Sheet from all accounts. Returns its URL."""
    buf = io.StringIO()
    csv.writer(buf).writerows(_sheet_rows(accounts))
    media = _media(buf.getvalue().encode('utf-8'), 'text/csv')
    fid = _find_id(SHEET_NAME, drive)
    if fid:
        drive.files().update(fileId=fid, media_body=media,
                             supportsAllDrives=True).execute()
    else:
        created = drive.files().create(
            body={'name': SHEET_NAME, 'mimeType': _SHEET_MIME},
            media_body=media, fields='id', supportsAllDrives=True).execute()
        fid = created['id']
    return f'https://docs.google.com/spreadsheets/d/{fid}/edit'


# ─── Public API ─────────────────────────────────────────────────────────────

def reserve(count):
    """Read the tracker + rambler pool up front. Returns a dict:
      {ok, start, ramblers:[(email,pw)|(None,None)], remaining_pool, pool_fid,
       had_pool, err}
    ok=False (with err) if Drive is unreachable — caller MUST abort (no rentals).
    """
    drive = _drive()
    if not drive:
        return {'ok': False, 'err': 'Google Drive not configured (GOOGLE_TOKEN_PICKLE).'}
    try:
        store, _fid = _load_store(drive)
    except Exception as e:
        return {'ok': False, 'err': f'tracker read failed: {type(e).__name__}: {e}'}
    start = len(store['accounts']) + 1
    # Existing "first last" names (lowercased) so the generator never repeats.
    existing_names = {
        (f"{a.get('first','')} {a.get('last','')}").strip().lower()
        for a in store['accounts']}
    try:
        lines, pool_fid = _load_rambler(drive)
    except Exception as e:
        lines, pool_fid = [], None
        logger.warning(f"[fb_registry] rambler load failed: {e}")
    # Consume from the BOTTOM: last line → first account.
    ramblers, remaining = [], list(lines)
    for _ in range(count):
        if remaining:
            ramblers.append(_parse_rambler(remaining.pop()))
        else:
            ramblers.append((None, None))
    return {'ok': True, 'start': start, 'ramblers': ramblers,
            'remaining_pool': remaining, 'pool_fid': pool_fid,
            'had_pool': pool_fid is not None,
            'existing_names': existing_names, 'err': None}


def commit(records, remaining_pool, pool_fid):
    """Append records to the JSON tracker, rewrite the rambler pool (minus the
    consumed lines), rebuild the Sheet, and remember this batch for /batch_sms.
    Returns (sheet_url or None, err or None)."""
    drive = _drive()
    if not drive:
        return None, 'Drive unavailable at commit'
    try:
        store, fid = _load_store(drive)
        store['accounts'].extend(records)
        store['last_batch'] = [
            {'account': r['account'], 'phone10': r.get('phone10', ''),
             'rental_id': r.get('rental_id', '')}
            for r in records
        ]
        _save_store(drive, store, fid)
        if pool_fid is not None:
            _save_rambler(drive, remaining_pool, pool_fid)
        url = _write_sheet(drive, store['accounts'])
        return url, None
    except Exception as e:
        logger.warning(f"[fb_registry] commit failed: {e}")
        return None, f'{type(e).__name__}: {e}'


def last_batch():
    """Return the most recent batch's [{account, phone10, rental_id}] for
    /batch_sms, or [] if unavailable."""
    drive = _drive()
    if not drive:
        return []
    try:
        store, _ = _load_store(drive)
        return store.get('last_batch') or []
    except Exception as e:
        logger.warning(f"[fb_registry] last_batch read failed: {e}")
        return []


def rambler_count():
    """How many Rambler credentials remain in the pool. None if no pool file."""
    drive = _drive()
    if not drive:
        return None
    try:
        lines, fid = _load_rambler(drive)
        return len(lines) if fid is not None else None
    except Exception as e:
        logger.warning(f"[fb_registry] rambler_count failed: {e}")
        return None


def sheet_url():
    drive = _drive()
    if not drive:
        return None
    fid = _find_id(SHEET_NAME, drive)
    return f'https://docs.google.com/spreadsheets/d/{fid}/edit' if fid else None


# ─── Per-account text file + batch zip ──────────────────────────────────────

def account_txt(rec):
    """Plain-text card for one account (no heritage — internal only)."""
    lines = [
        rec['account'],
        '=' * len(rec['account']),
        f"Name: {rec.get('first','')} {rec.get('last','')}",
        f"Gender: {rec.get('gender','')}",
        f"Birthdate: {rec.get('birthdate_display','')} (age {rec.get('age','')})",
        f"Password: {rec.get('password','')}",
        f"Rambler email: {rec.get('rambler_email','') or '(none left in pool)'}",
        f"Rambler password: {rec.get('rambler_password','')}",
        f"FB phone (10-digit): {rec.get('phone10','') or '(rental failed)'}",
        f"App name: {rec.get('app_name','')}",
        f"Privacy policy: {rec.get('privacy_url','') or '(not generated)'}",
        f"FB page name (option 1): {rec.get('page_name_1','')}",
        f"FB page name (option 2): {rec.get('page_name_2','')}",
        f"Page category: {rec.get('page_category','')}",
        f"Bio: {rec.get('bio','')}",
        f"Block countries: {rec.get('block_countries','')}",
        f"Block words: {rec.get('block_words','')}",
        f"Created (UTC): {rec.get('created_utc','')}",
    ]
    return '\n'.join(lines)


def combined_txt(records):
    """All accounts in ONE text file, separated — so the whole batch is a
    single downloadable file the user can keep locally."""
    sep = '\n\n' + '=' * 46 + '\n\n'
    return sep.join(account_txt(r) for r in records)


def build_zip(records):
    """Zip one .txt per account. Returns (bytes, filename)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for r in records:
            zf.writestr(f"{r['account']}.txt", account_txt(r))
    if len(records) > 1:
        fname = (f"{records[0]['account']}..{records[-1]['account'].split()[-1]}"
                 .replace(' ', '_') + '.zip')
    else:
        fname = records[0]['account'].replace(' ', '_') + '.zip'
    return buf.getvalue(), fname
