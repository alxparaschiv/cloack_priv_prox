"""One-time FB Page verification (/fb_page_verify) on the cloak bot: shows the
single-use number, then the auto-fetched code. Mocks the TextVerified client."""
import asyncio
import page_verify

ok = True
def chk(n, c):
    global ok; ok = ok and c; print(('  PASS — ' if c else '  FAIL — ') + n)

class FakeClient:
    def __init__(self): self.created = 0
    def create_verification(self, service, capability):
        self.created += 1; return {'id': 'v1', 'number': '+15093871549'}
    def poll_sms(self, vid, timeout=300): return '567332'
_fc = FakeClient()
page_verify.tvc._client = lambda: _fc

class FakeMsg:
    def __init__(self): self.texts = []
    async def edit_text(self, t, **k): self.texts.append(t)
class FakeIncoming:
    def __init__(self): self.replies = []; self._msg = FakeMsg()
    async def reply_text(self, t, **k): self.replies.append(t); return self._msg
class FakeUpd:
    def __init__(self, cid):
        self.effective_chat = type('C', (), {'id': cid})(); self.message = FakeIncoming()
class FakeBot:
    def __init__(self): self.sent = []
    async def send_message(self, cid, t, **k): self.sent.append((cid, t))
class FakeCtx:
    def __init__(self): self.bot = FakeBot()

ctx = FakeCtx(); u = FakeUpd(5000)
asyncio.run(page_verify.fb_page_verify_command(u, ctx))
chk("acquired a single-use verification number", _fc.created == 1)
chk("shows the number", any('15093871549' in t for t in u.message._msg.texts))
chk("returns the auto-fetched code", any('567332' in t for _, t in ctx.bot.sent))
chk("labels it single-use / not a rental",
    any('single-use' in t.lower() or ('not' in t.lower() and 'rental' in t.lower())
        for t in (u.message.replies + u.message._msg.texts)))

print('\n' + ('✅ ALL PASS' if ok else '❌ FAIL'))
