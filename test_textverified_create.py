"""Regression for the /fb_page_verify hang (2026-07-20): the v2 create endpoint
returns ONLY an href (no id, no number) and provisions the number ASYNC. The old
client fetched the detail ONCE → number=None → the command stalled. The fix takes
the id from the href and POLLS the detail until the number appears, with a generous
POST timeout (the create endpoint intermittently stalls >30s). Mocks _request."""
import textverified_client as tvc

ok = True
def chk(n, c):
    global ok; ok = ok and c; print(('  PASS — ' if c else '  FAIL — ') + n)

c = tvc.TextVerifiedClient.__new__(tvc.TextVerifiedClient)
c.find_service_id = lambda s: s
tvc.time.sleep = lambda *_: None       # no real waiting in the poll loop

# Record how _request is called + drive a create-then-async-number sequence
CALLS = []
_detail_seq = iter([
    {'state': 'verificationPending'},                       # 1st detail: no number yet
    {'state': 'verificationPending'},                       # 2nd: still none
    {'number': '3345904345', 'state': 'verificationReady'}, # 3rd: number provisioned
])
def fake_request(method, path, timeout=30, **kw):
    CALLS.append((method, path, timeout))
    if method == 'POST':
        # v2 create returns ONLY an href — no id, no number
        return {'method': 'GET', 'href': 'https://x/api/pub/v2/verifications/lr_ABC123'}
    if method == 'GET':
        return next(_detail_seq)
    return {}
c._request = fake_request

v = c.create_verification('facebook', 'sms')
chk("id is taken from the href (no id/number in create body)", v['id'] == 'lr_ABC123')
chk("number is polled until provisioned", v['number'] == '3345904345')
chk("polled the detail more than once (async number)",
    sum(1 for m, p, _ in CALLS if m == 'GET') >= 3)

# the create POST uses a generous timeout (endpoint intermittently stalls >30s)
post = next((t for m, p, t in CALLS if m == 'POST'), None)
chk("create POST uses a long timeout (>30s)", post is not None and post >= 60)

# graceful when the number never provisions within the window
c2 = tvc.TextVerifiedClient.__new__(tvc.TextVerifiedClient)
c2.find_service_id = lambda s: s
def fake_req2(method, path, timeout=30, **kw):
    return ({'href': 'https://x/verifications/lr_NONUM'} if method == 'POST'
            else {'state': 'verificationPending'})   # number never arrives
c2._request = fake_req2
v2 = c2.create_verification('facebook', 'sms', number_wait=0)
chk("no-number case still returns the id (caller can decide)", v2['id'] == 'lr_NONUM')
chk("no-number case returns number=None (no crash)", v2['number'] is None)

print('\n' + ('✅ ALL PASS' if ok else '❌ FAIL'))
