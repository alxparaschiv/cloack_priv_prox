"""Offline test for /account_pack_min — the minimal package (operator brings their
own FB number + proxy, no links). Proves generate_packages(minimal=True):
  • rents NO Facebook number, assigns NO proxy, generates NO privacy link
  • KEEPS name / gender / dob / password / rambler / app name / FB page name / bio
  • renders 'use your own number/proxy' in the card + .txt
  • the non-minimal path still rents + proxies + links (unchanged)
Stubs all network/LLM/Drive/rental/bg so nothing leaves the process."""
import sys
import types

import account_pack
import fb_poster_registry as R

_PASS = _FAIL = 0
def check(name, cond):
    global _PASS, _FAIL
    print(("  PASS — " if cond else "  FAIL — ") + name)
    _PASS += bool(cond); _FAIL += (not cond)

RENT_CALLS = []
PRIV_CALLS = []

def fake_reserve(count, kind='primary', va_label=None):
    return {'ok': True, 'start': 1, 'kind': kind, 'va_label': va_label,
            'ramblers': [(f'user{i}@rambler.ru', f'pw{i}') for i in range(count)],
            'proxies': [f'socks5://u:p@host:{i}' for i in range(count)],
            'remaining_pool': [], 'pool_fid': None, 'had_pool': True,
            'existing_names': set(), 'err': None}

R.reserve = fake_reserve
R.commit = lambda records, remaining, fid, va_label=None: ('https://sheet', None)
account_pack._gen_names = lambda count, existing=None: [
    (f'First{i}', f'Last{i}', 'heritage', 'female') for i in range(count)]
account_pack._gen_app_names = lambda count: [f'AppName{i}' for i in range(count)]
account_pack._gen_bios = lambda count: [f'bio {i}' for i in range(count)]
account_pack._gen_page_names = lambda model, handle, count: [
    (f'Page1_{i}', f'Page2_{i}') for i in range(count)]
account_pack.password_gen.make_passwords = lambda count: (
    [f'Passw0rd!{i}' for i in range(count)], None)
def _fake_rent(svc):
    RENT_CALLS.append(svc); return ('rental123', '+15551234567', None)
account_pack.rental._rent_seven_day = _fake_rent
account_pack.rental._balance_str = lambda: '$0.00'
def _fake_priv(app_name=None):
    PRIV_CALLS.append(app_name); return (f'https://privacy/{app_name}', None, {})
account_pack.privacy._create_privacy_policy_dispatch = _fake_priv
BG_CALLS = []
def _fake_bg(name, procedural_only=False):
    BG_CALLS.append(procedural_only); return f'bgid_{name}'
account_pack._gen_one_profile_bg = _fake_bg             # no Drive
_noop = lambda *a, **k: None

# ── minimal batch ──
recs_min, phone_ok, _bal, _sheet = account_pack.generate_packages(
    2, fake_reserve(2), 'Carolina', _noop, _noop, minimal=True)

check("minimal: rented NO Facebook number", RENT_CALLS == [])
check("minimal: KEEPS the privacy link (Meta needs it)", len(PRIV_CALLS) == 2)
check("minimal: phone_ok is 0", phone_ok == 0)
r = recs_min[0]
check("minimal flag set on record", r.get('minimal') is True)
check("minimal: phone10 empty", r.get('phone10') == '')
check("minimal: proxy empty", r.get('proxy') == '')
check("minimal: privacy_url PRESENT", bool(r.get('privacy_url')))
check("minimal KEEPS name/gender/dob/password",
      r['first'] and r['gender'] and r['birthdate'] and r['password'])
check("minimal KEEPS rambler login", ':' in (r.get('rambler_login') or ''))
check("minimal KEEPS app name + FB page name + bio",
      r.get('app_name') and r.get('page_name_1') and r.get('bio'))
check("minimal: profile images use procedural-only (fast, no AI artistic)",
      BG_CALLS and all(v is True for v in BG_CALLS))

txt = R.account_txt(r)
check("txt: FB phone says 'use your own number'", 'use your own number' in txt)
check("txt: proxy says 'use your own proxy'", 'use your own proxy' in txt)
check("txt: privacy link is present (kept)", 'https://privacy/' in txt)
check("txt: NO 'rental failed' misfire", 'rental failed' not in txt)
card = account_pack._format_card(1, 2, r)
check("card: shows 'use your own number'", 'use your own number' in card)
check("card: shows 'use your own proxy'", 'use your own proxy' in card)

# ── the FULL (non-minimal) path is unchanged ──
RENT_CALLS.clear(); PRIV_CALLS.clear(); BG_CALLS.clear()
recs_full, phone_ok_f, _b, _s = account_pack.generate_packages(
    2, fake_reserve(2), 'Carolina', _noop, _noop, minimal=False)
f0 = recs_full[0]
check("full: still rents a number", len(RENT_CALLS) == 2 and phone_ok_f == 2)
check("full: still has a proxy", bool(f0.get('proxy')))
check("full: still has a privacy link", bool(f0.get('privacy_url')))
check("full: minimal flag is False", f0.get('minimal') is False)
check("full: profile images use fast bg_generator ONLY (no slow AI artistic)",
      BG_CALLS and all(v is True for v in BG_CALLS))

print(f"\n{'✅ ALL PASS' if not _FAIL else '❌ ' + str(_FAIL) + ' FAIL'}  ({_PASS} passed)")
sys.exit(1 if _FAIL else 0)
