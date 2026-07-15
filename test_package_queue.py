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


# ── in-memory registry (replaces Drive-backed R.reserve/R.commit) ──
STORE = {'accounts': []}


def fake_reserve(count, kind='primary'):
    is_b = (kind == 'backup_manager')
    same = [a for a in STORE['accounts']
            if (a.get('kind') == 'backup_manager') == is_b]
    base = len(STORE['accounts'])
    return {'ok': True, 'start': len(same) + 1, 'kind': kind,
            'ramblers': [(f'user{base+i}@rambler.ru', f'pw{base+i}')
                         for i in range(count)],
            'proxies': [f'socks5://u:p@host:{base+i}' for i in range(count)],
            'remaining_pool': [], 'pool_fid': None, 'had_pool': False,
            'existing_names': set(), 'err': None}


def fake_commit(records, remaining, fid):
    STORE['accounts'].extend(records)
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
accts = [a for a in STORE['accounts'] if a.get('kind') == 'primary']
bms = [a for a in STORE['accounts'] if a.get('kind') == 'backup_manager']
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
check("every primary has package_type=='' ",
      all(a.get('package_type') == '' for a in accts))
check("every primary carries output_folder_name + source_req_id",
      all(a.get('output_folder_name') and a.get('source_req_id') for a in accts))
check("BM has package_type=='backup_manager'",
      bms[0].get('package_type') == 'backup_manager')
check("BM has NO page_bg_image_url (intentional)",
      not bms[0].get('page_bg_image_url'))
check("BM still carries a proxy + one-row rambler_login + dev_app_role",
      bms[0].get('proxy') and bms[0].get('rambler_login')
      and bms[0].get('dev_app_role'))

# ── run pass 2 — idempotency ──
print("\n=== PASS 2 — idempotency (seen-set) ===")
before = len(STORE['accounts'])
n2a, n2b = package_queue.poll_once()
check("second pass produces nothing (0/0)", (n2a, n2b) == (0, 0))
check("registry row count unchanged", len(STORE['accounts']) == before)

print(f"\n{'✅ ALL PASS' if _FAIL == 0 else '❌ SOME FAILED'}  "
      f"({_PASS}/{_PASS + _FAIL} checks)")
sys.exit(1 if _FAIL else 0)
