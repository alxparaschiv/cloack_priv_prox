"""Offline test for the autonomous /daily package poller + the ported fields.

Stubs all network/LLM/Drive/rental/bg so nothing leaves the process. Proves:
  1) 5 /daily requests (5th wants_backup_manager) -> 5 primary + 1 BM committed,
     each PRIMARY carries one-row rambler_login / proxy / dev_app_role /
     page_bg_image_url / package_type=='' / output_folder_name / source_req_id;
     the BM has package_type=='backup_manager' and EMPTY page_bg_image_url.
  2) a second pass adds NOTHING (seen-set idempotency).
Real LLM/bg/Drive/rental run on deploy — here they're stubbed.
"""
import sys
import types

import account_pack
import fb_poster_registry as R
import package_queue

_PASS = _FAIL = 0


def check(name, cond):
    global _PASS, _FAIL
    print(("  PASS — " if cond else "  FAIL — ") + name)
    _PASS += bool(cond)
    _FAIL += (not cond)


# ── in-memory PER-VA registry (replaces Drive-backed R.reserve/R.commit) ──
# STORE is keyed by va_label so each VA numbers from 001 independently.
STORE = {}


def _va_store(va_label):
    return STORE.setdefault(va_label or 'VA001', {'accounts': []})


def fake_reserve(count, kind='primary', va_label=None):
    st = _va_store(va_label)
    is_b = (kind == 'backup_manager')
    same = [a for a in st['accounts']
            if (a.get('kind') == 'backup_manager') == is_b]
    base = len(st['accounts'])
    tag = (va_label or 'VA001')
    return {'ok': True, 'start': len(same) + 1, 'kind': kind, 'va_label': va_label,
            'ramblers': [(f'user{tag}{base+i}@rambler.ru', f'pw{base+i}')
                         for i in range(count)],
            'proxies': [f'socks5://u:p@host:{tag}{base+i}' for i in range(count)],
            'remaining_pool': [], 'pool_fid': None, 'had_pool': False,
            'existing_names': set(), 'err': None}


def fake_commit(records, remaining, fid, va_label=None):
    _va_store(va_label)['accounts'].extend(records)
    return 'https://sheet', None


R.reserve = fake_reserve
R.commit = fake_commit

# ── stub the LLM / rental / privacy / bg inside account_pack ──
account_pack._gen_names = lambda count, existing=None: [
    (f'First{i}', f'Last{i}', 'heritage', 'female') for i in range(count)]
account_pack._gen_app_names = lambda count: [f'AppName{i}' for i in range(count)]
account_pack._gen_bios = lambda count: [f'bio {i}' for i in range(count)]
account_pack._gen_page_names = lambda model, handle, count: [
    (f'Page1_{i}', f'Page2_{i}') for i in range(count)]
account_pack.password_gen.make_passwords = lambda count: (
    [f'Passw0rd!{i}' for i in range(count)], None)
account_pack.rental._rent_seven_day = lambda svc: ('rental123', '+15551234567', None)
account_pack.rental._balance_str = lambda: '$0.00'
account_pack.privacy._create_privacy_policy_dispatch = lambda app_name=None: (
    f'https://privacy/{app_name}', None, {})

_fake_bg = types.ModuleType('artistic_bg_gen')
_fake_bg.generate_artistic_bg_random_type = lambda profile_subfolder_name=None: (
    f'bgid_{profile_subfolder_name}', '/tmp/x.png', None)
sys.modules['artistic_bg_gen'] = _fake_bg

# ── stub package_queue's queue + seen I/O ──
REQUESTS = [{
    'req_id': f'daily-Carolina-Goth-{i}', 'source': 'daily', 'model': 'Carolina',
    'niche': 'Goth', 'cloak_slug': f'slug{i}', 'cloak_link': f'https://x/slug{i}',
    'output_folder_name': f'Output Carolina Goth {i}', 'stack_index': i,
    'wants_backup_manager': (i == 5),
} for i in range(1, 6)]
SEEN = set()
package_queue._read_queue = lambda: list(REQUESTS)
package_queue._read_seen = lambda: (set(SEEN), None)


def _save_seen(seen, fid):
    SEEN.clear()
    SEEN.update(seen)
    return 'seenfid'


package_queue._save_seen = _save_seen


# ── run pass 1 ──
print("=== PASS 1 — 5 /daily requests (5th → +backup-manager) ===")
n_acct, n_bm = package_queue.poll_once()
_va1 = _va_store('VA001')['accounts']   # requests carry no va_label → default VA001
accts = [a for a in _va1 if a.get('kind') == 'primary']
bms = [a for a in _va1 if a.get('kind') == 'backup_manager']
check(f"5 primary accounts produced (got {n_acct})", n_acct == 5 and len(accts) == 5)
check(f"1 backup-manager produced (got {n_bm})", n_bm == 1 and len(bms) == 1)
check("primary ids are FB META POSTER NNN",
      all(a['account'].startswith('FB META POSTER') for a in accts))
check("BM id is FB BACKUP MANAGER NNN",
      bms[0]['account'].startswith('FB BACKUP MANAGER'))

# field shape on primaries
rl_ok = all(a.get('rambler_login') and '\n' not in a['rambler_login']
            and ':' in a['rambler_login']
            and '@' in a['rambler_login'].split(':', 1)[0] for a in accts)
check("every primary has ONE-ROW rambler_login (email@..:password)", rl_ok)
check("every primary has a proxy", all(a.get('proxy') for a in accts))
check("proxies are distinct across the batch",
      len({a['proxy'] for a in accts}) == len(accts))
check("every primary has a dev_app_role in the curated set",
      all(a.get('dev_app_role') in account_pack._DEV_APP_ROLES for a in accts))
check("every primary has a page_bg_image_url",
      all(a.get('page_bg_image_url') for a in accts))
check("every primary ALSO has an account_bg_image_url (2 profile pics)",
      all(a.get('account_bg_image_url') for a in accts))
check("every primary has package_type=='' ",
      all(a.get('package_type') == '' for a in accts))
check("every primary carries output_folder_name + source_req_id",
      all(a.get('output_folder_name') and a.get('source_req_id') for a in accts))
check("BM has package_type=='backup_manager'",
      bms[0].get('package_type') == 'backup_manager')
check("BM has NO page_bg_image_url (intentional)",
      not bms[0].get('page_bg_image_url'))
check("BM has NO account_bg_image_url either (intentional)",
      not bms[0].get('account_bg_image_url'))
check("BM still carries a proxy + one-row rambler_login + dev_app_role",
      bms[0].get('proxy') and bms[0].get('rambler_login')
      and bms[0].get('dev_app_role'))
_SCHED_ALL = account_pack._SCHED_3X + account_pack._SCHED_4X
check("every primary has a PREDEFINED schedule in the 3x/4x set",
      all(a.get('schedule') in _SCHED_ALL for a in accts))
_draws = [account_pack._pick_schedule() for _ in range(400)]
_n3 = sum(1 for d in _draws if d in account_pack._SCHED_3X)
check("_pick_schedule is ~50/50 3x/4x over 400 draws (140<n3<260)",
      140 < _n3 < 260)
check("BM has empty schedule (runs no workflow)", bms[0].get('schedule') == '')

# ── run pass 2 — idempotency ──
print("\n=== PASS 2 — idempotency (seen-set) ===")
before = len(_va_store('VA001')['accounts'])
n2a, n2b = package_queue.poll_once()
check("second pass produces nothing (0/0)", (n2a, n2b) == (0, 0))
check("registry row count unchanged",
      len(_va_store('VA001')['accounts']) == before)

# ── run pass 3 — MULTI-VA: two VAs each start at 001 ──
print("\n=== PASS 3 — per-VA numbering (each VA starts at 001) ===")
SEEN.clear()
MULTI = [
    {'req_id': 'daily-A-1', 'source': 'daily', 'model': 'Carolina',
     'va_label': 'VA002', 'va_chat_id': 111, 'cloak_slug': 'a1',
     'output_folder_name': 'Output A 1', 'wants_backup_manager': False},
    {'req_id': 'daily-A-2', 'source': 'daily', 'model': 'Carolina',
     'va_label': 'VA002', 'va_chat_id': 111, 'cloak_slug': 'a2',
     'output_folder_name': 'Output A 2', 'wants_backup_manager': False},
    {'req_id': 'daily-B-1', 'source': 'daily', 'model': 'Kira',
     'va_label': 'VA003', 'va_chat_id': 222, 'cloak_slug': 'b1',
     'output_folder_name': 'Output B 1', 'wants_backup_manager': False},
]
package_queue._read_queue = lambda: list(MULTI)
package_queue.poll_once()
va2 = [a for a in _va_store('VA002')['accounts'] if a.get('kind') == 'primary']
va3 = [a for a in _va_store('VA003')['accounts'] if a.get('kind') == 'primary']
check("VA002 first account is FB META POSTER 001",
      va2 and va2[0]['account'] == 'FB META POSTER 001')
check("VA002 second account is 002 (same VA increments)",
      len(va2) == 2 and va2[1]['account'] == 'FB META POSTER 002')
check("VA003 first account ALSO restarts at 001 (independent VA)",
      va3 and va3[0]['account'] == 'FB META POSTER 001')
check("va_label + va_chat_id stamped on rows",
      va2[0].get('va_label') == 'VA002' and va2[0].get('va_chat_id') == 111
      and va3[0].get('va_label') == 'VA003')

print(f"\n{'✅ ALL PASS' if _FAIL == 0 else '❌ SOME FAILED'}  "
      f"({_PASS}/{_PASS + _FAIL} checks)")
sys.exit(1 if _FAIL else 0)
