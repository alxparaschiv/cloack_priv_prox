"""/rambler_login dispenses logins from the pool AND removes them (write-back), so
the same login is never handed out twice or reused by /account_pack. Mocks Drive I/O."""
import asyncio
import fb_poster_registry as R
import rambler_pool

ok = True
def chk(n, c):
    global ok; ok = ok and c; print(('  PASS — ' if c else '  FAIL — ') + n)

# ── in-memory 'pool file' ──
POOL = ['a@rambler.ru:pw1', 'b@rambler.ru:pw2', 'c@rambler.ru:pw3', 'd@rambler.ru:pw4']
SAVED = {'lines': None}
R._drive = lambda: object()                         # non-None → "configured"
R._load_rambler = lambda drive: (list(POOL), 'fid1')
def _save(drive, lines, fid):
    SAVED['lines'] = list(lines)
    POOL[:] = lines                                 # reflect the write-back into the pool
R._save_rambler = _save

# 1) dispense 2 → returns 2, pool shrinks by 2, the SAME two are gone
res = R.dispense_ramblers(2)
chk("dispenses the requested count", res['ok'] and len(res['logins']) == 2)
chk("write-back happened (pool consumed)", SAVED['lines'] is not None and len(POOL) == 2)
chk("remaining count reported", res['remaining'] == 2)
dispensed = {f"{e}:{p}" for e, p in res['logins']}
chk("dispensed logins are REMOVED from the pool",
    not (dispensed & set(POOL)))

# 2) a second dispense returns DIFFERENT logins (no reuse)
res2 = R.dispense_ramblers(2)
dispensed2 = {f"{e}:{p}" for e, p in res2['logins']}
chk("second dispense never repeats the first", not (dispensed & dispensed2))
chk("pool now empty", len(POOL) == 0)

# 3) empty pool → dispense returns none, ok True, empty logins
res3 = R.dispense_ramblers(1)
chk("empty pool → no logins, still ok", res3['ok'] and res3['logins'] == [])

# 4) the command renders the logins + 'removed/consumed' framing, honors [count]
class FakeMsg:
    def __init__(self): self.texts = []
    async def reply_text(self, t, **k): self.texts.append(t)
class FakeUpd:
    def __init__(self): self.message = FakeMsg()
class FakeCtx:
    def __init__(self, args): self.args = args
POOL[:] = ['x@rambler.ru:px', 'y@rambler.ru:py', 'z@rambler.ru:pz']
u = FakeUpd()
asyncio.run(rambler_pool.rambler_login_command(u, FakeCtx(['2'])))
out = "\n".join(u.message.texts)
chk("command dispenses [count]=2 logins", out.count('rambler.ru') == 2)
chk("command shows the login as email:password", 'x@rambler.ru:px' in out or 'y@rambler.ru:py' in out or 'z@rambler.ru:pz' in out)
chk("command leaves 1 in the pool", len(POOL) == 1)

# 5) no-pool case → clear error, nothing crashes
R._load_rambler = lambda drive: ([], None)
res5 = R.dispense_ramblers(1)
chk("no pool file → ok False + helpful err", (not res5['ok']) and 'rambler_pool' in res5['err'])

print('\n' + ('✅ ALL PASS' if ok else '❌ FAIL'))
