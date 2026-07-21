"""VA account packages validate their OWN fresh proxy per package and NEVER draw
from the operator's shared DAILY PROXY POOL (2026-07-21). Proves:
  • _gen validates a proxy (proxy.validate_one_proxy_for_package) BEFORE building
  • the validated proxy is passed to generate_packages as proxies_override (used,
    not the pool)
  • if validation fails, _gen returns False (package DEFERRED, not built)
  • poll_once leaves a deferred request UNSEEN so it retries; a validated one is seen
  • generate_packages(proxies_override=[...]) uses the override, ignoring reserve pool
Mocks the validator + Drive so nothing leaves the process."""
import account_pack, package_queue as pq, proxy as proxy_mod
import fb_poster_registry as R

ok = True
def chk(n, c):
    global ok; ok = ok and c; print(('  PASS — ' if c else '  FAIL — ') + n)

# ── generate_packages uses proxies_override, NOT the reserve pool ──
POOL_PROXY = 'socks5://POOL:pool@host:12321'      # what the shared pool would give
OWN_PROXY  = 'socks5://OWN:own@host_country-us_session-FRESH1_lifetime-168h@geo:12321'
reserve = {'ok': True, 'start': 1, 'kind': 'primary', 'va_label': 'VA001',
           'ramblers': [('a@rambler.ru', 'pw')], 'proxies': [POOL_PROXY],
           'remaining_pool': [], 'pool_fid': None, 'had_pool': True,
           'existing_names': set(), 'err': None}
# stub the heavy internals of generate_packages
account_pack._gen_names = lambda c, e=None: [('F', 'L', 'her', 'female')]
account_pack._gen_app_names = lambda c: ['App']
account_pack._gen_bios = lambda c: ['bio']
account_pack._gen_page_names = lambda m, h, c: [('P1', 'P2')]
account_pack.password_gen.make_passwords = lambda c: (['pw!'], None)
account_pack.rental._rent_seven_day = lambda s: ('rid', '+15551234567', None)
account_pack.rental._balance_str = lambda: '$0'
account_pack.privacy._create_privacy_policy_dispatch = lambda app_name=None: ('https://p', None, {})
account_pack._gen_one_profile_bg = lambda name, procedural_only=False: ''
captured = {}
R.reserve = lambda n, kind='primary', va_label=None: dict(reserve)
R.commit = lambda records, remaining, fid, va_label=None: (captured.update(recs=records) or ('https://s', None))

recs, *_ = account_pack.generate_packages(1, dict(reserve), 'Carolina',
    emit=lambda *a, **k: None, post_one=lambda *a, **k: None,
    proxies_override=[OWN_PROXY])
chk("proxies_override is used as the package proxy", recs[0]['proxy'] == OWN_PROXY)
chk("the shared-pool proxy is NOT used", recs[0]['proxy'] != POOL_PROXY)

# ── _gen validates its OWN proxy before building; defers on failure ──
calls = {'validate': 0, 'gen': 0}
def fake_validate(on_update=None):
    calls['validate'] += 1
    return OWN_PROXY if calls['validate'] != 2 else None   # 2nd call fails
proxy_mod.validate_one_proxy_for_package = fake_validate
def fake_gen(count, reserve, model, **kw):
    calls['gen'] += 1; captured['override'] = kw.get('proxies_override'); return [{}]
account_pack.generate_packages = fake_gen

r1 = pq._gen('primary', 'Carolina', 'req-1', va_label='VA001')
chk("_gen validates a proxy before building", calls['validate'] == 1)
chk("_gen succeeds when a proxy validates", r1 is True)
chk("_gen passes the validated proxy as override", captured.get('override') == [OWN_PROXY])

r2 = pq._gen('primary', 'Carolina', 'req-2', va_label='VA001')
chk("_gen DEFERS (returns False) when no proxy validates", r2 is False)

# ── poll_once: deferred request stays UNSEEN (retries); validated one is seen ──
proxy_mod.validate_one_proxy_for_package = lambda on_update=None: None   # always fail → defer
pq._read_queue = lambda: [{'req_id': 'daily-x', 'source': 'daily', 'model': 'Carolina',
                           'va_label': 'VA001', 'output_folder_name': 'Out'}]
_seen = set()
pq._read_seen = lambda: (set(_seen), None)
pq._save_seen = lambda s, fid: (_seen.clear() or _seen.update(s) or 'fid')
pq._handle_of = lambda req: None
n_a, n_bm = pq.poll_once()
chk("deferred package builds 0 accounts", n_a == 0)
chk("deferred request is NOT marked seen (will retry)", 'daily-x' not in _seen)

# now proxy validates → request processed + seen
proxy_mod.validate_one_proxy_for_package = lambda on_update=None: OWN_PROXY
account_pack.generate_packages = lambda *a, **k: [{}]
n_a2, _ = pq.poll_once()
chk("once a proxy validates, the account is built", n_a2 == 1)
chk("processed request is marked seen", 'daily-x' in _seen)

print('\n' + ('✅ ALL PASS' if ok else '❌ FAIL'))
