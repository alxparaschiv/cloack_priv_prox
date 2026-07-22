"""Private-proxy pipeline in cloak (2026-07-22): package_queue._gen must, for
pipeline='privateproxy', SKIP the (expensive) IPRoyal in-browser validation and
stamp the flat-cost fxdx PRIVATE_PROXY_STR instead — and pass pipeline through to
generate_packages so it lands on the account record. iproyal path unchanged."""
import os
import account_pack
import fb_poster_registry as R
import proxy as proxy_mod
import package_queue as pq

ok = True
def ck(n, c):
    global ok; ok = ok and c; print(('  PASS — ' if c else '  FAIL — ') + n)

FXDX = 'http://6m4ups3rlv.cn.fxdx.in:19634:lo0895195550:yGiYCe5Tg7Q8'
os.environ['PRIVATE_PROXY_STR'] = FXDX

calls = {'validate': 0}
proxy_mod.validate_one_proxy_for_package = lambda *a, **k: (calls.__setitem__('validate', calls['validate'] + 1) or 'socks5://u:p@iproyal:12321')
R.reserve = lambda count, kind='primary', va_label=None: {'ok': True, 'start': 1,
    'kind': kind, 'va_label': va_label, 'ramblers': [], 'proxies': [], 'remaining_pool': [],
    'pool_fid': None}

captured = {}
def fake_generate(count, reserve, model, **kw):
    captured.clear(); captured.update(kw); captured['model'] = model
    return [], True, '$0', ''
account_pack.generate_packages = fake_generate

# ── privateproxy: no IPRoyal validation, fxdx proxy used, pipeline threaded ──
print("── pipeline='privateproxy' ──")
calls['validate'] = 0
r = pq._gen('primary', 'Carolina', 'req-1', folder='Output X', va_label='VA001',
            pipeline='privateproxy')
ck("returns True (built)", r is True)
ck("IPRoyal validation was NOT called", calls['validate'] == 0)
ck("fxdx proxy used as proxies_override", captured.get('proxies_override') == [FXDX])
ck("pipeline threaded to generate_packages", captured.get('pipeline') == 'privateproxy')

# ── privateproxy with unset env → deferred (returns False) ──
os.environ['PRIVATE_PROXY_STR'] = ''
ck("privateproxy defers when PRIVATE_PROXY_STR unset",
   pq._gen('primary', 'Carolina', 'req-2', pipeline='privateproxy') is False)
os.environ['PRIVATE_PROXY_STR'] = FXDX

# ── iproyal: IPRoyal validation IS called, its proxy used ──
print("── pipeline='iproyal' (default) ──")
calls['validate'] = 0
r = pq._gen('primary', 'Carolina', 'req-3', folder='Output Y', va_label='VA001',
            pipeline='iproyal')
ck("returns True", r is True)
ck("IPRoyal validation WAS called", calls['validate'] == 1)
ck("iproyal proxy used (not fxdx)",
   captured.get('proxies_override') == ['socks5://u:p@iproyal:12321'])
ck("pipeline threaded as iproyal", captured.get('pipeline') == 'iproyal')

print('\n' + ('✅ ALL PASS' if ok else '❌ FAIL'))
raise SystemExit(0 if ok else 1)
