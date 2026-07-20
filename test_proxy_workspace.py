"""Stage 1: the browser proxy check targets a WORKSPACE + FOLDER.
  • _resolve_workspace_id maps 'Virtual Assistants' → its id (cached), via GET /workspaces
  • create_gologin_profile sends ?workspaceId=<id> + body folderName='Myself'
  • _next_validated_profile_index scopes numbering to /workspaces/{wid}/profiles
  • no workspace → legacy behavior (no query param, /browser/v2 numbering)
Mocks requests so nothing leaves the process."""
import proxy as pm

ok = True
def chk(n, c):
    global ok; ok = ok and c; print(('  PASS — ' if c else '  FAIL — ') + n)

pm.GOLOGIN_API_KEY = 'tok'          # pretend configured
pm._GOLOGIN_WS_ID_CACHE.clear()

CALLS = {'get': [], 'post': []}
class Resp:
    def __init__(self, code=200, js=None, text=''):
        self.status_code = code; self._js = js if js is not None else {}; self.text = text
    def json(self): return self._js

WS_JSON = [{'id': 'wid-VA-123', 'name': 'Virtual Assistants'},
           {'id': 'wid-legacy', 'name': 'michaelmeyers4455'}]

def fake_get(url, headers=None, params=None, timeout=None):
    CALLS['get'].append((url, params))
    if url.endswith('/workspaces'):
        return Resp(200, WS_JSON)
    if '/workspaces/' in url and url.endswith('/profiles'):
        return Resp(200, {'profiles': [{'name': 'Validated Profile 7'}]})   # scoped
    if url.endswith('/browser/v2'):
        return Resp(200, {'profiles': [{'name': 'Validated Profile 99'}]})  # global (should NOT be used when scoped)
    return Resp(404, text='nope')

def fake_post(url, json=None, headers=None, timeout=None):
    CALLS['post'].append((url, json))
    return Resp(201, {'id': 'prof-1'})

pm.requests.get = fake_get
pm.requests.post = fake_post

P = pm.ProxyManager() if hasattr(pm, 'ProxyManager') else None
# ProxyManager may need args; fall back to the class holding the methods
if P is None:
    import inspect
    cls = next(o for _n, o in inspect.getmembers(pm)
               if inspect.isclass(o) and hasattr(o, 'create_gologin_profile'))
    P = cls.__new__(cls)

# 1) resolve workspace by name (+ cache)
wid = P._resolve_workspace_id('Virtual Assistants')
chk("resolves 'Virtual Assistants' → its id", wid == 'wid-VA-123')
n_before = len(CALLS['get'])
wid2 = P._resolve_workspace_id('virtual assistants')      # case-insensitive + cached
chk("resolve is cached (no 2nd /workspaces call)", wid2 == 'wid-VA-123' and len(CALLS['get']) == n_before)
chk("unknown workspace → None", P._resolve_workspace_id('Nope') is None)

# 2) create targets workspace (query) + folder (body)
CALLS['post'].clear()
pid, msg = P.create_gologin_profile('Validated Profile 7', 'host:12321:u:p',
                                    workspace_id='wid-VA-123', folder_name='Myself')
url, body = CALLS['post'][0]
chk("create hits ?workspaceId=<id>", 'workspaceId=wid-VA-123' in url)
chk("create sets folderName=Myself in body", body.get('folderName') == 'Myself')
chk("create still attaches the proxy", body.get('proxy', {}).get('host') == 'host')

# 3) no workspace → legacy (no query param, no folderName)
CALLS['post'].clear()
P.create_gologin_profile('X', 'host:12321:u:p')
url0, body0 = CALLS['post'][0]
chk("no workspace → plain /browser/custom (no query)", url0.endswith('/browser/custom'))
chk("no workspace → no folderName", 'folderName' not in body0)

# 4) numbering scoped to the workspace when set
CALLS['get'].clear()
nxt, seen, err = P._next_validated_profile_index('Validated Profile', workspace_id='wid-VA-123')
scoped_used = any('/workspaces/wid-VA-123/profiles' in u for u, _ in CALLS['get'])
chk("scoped numbering uses /workspaces/{wid}/profiles", scoped_used)
chk("scoped numbering continues past the workspace's max (7→8)", nxt == 8)

# 5) numbering global when no workspace
CALLS['get'].clear()
nxt2, _s, _e = P._next_validated_profile_index('Validated Profile')
chk("global numbering uses /browser/v2", any(u.endswith('/browser/v2') for u, _ in CALLS['get']))
chk("global numbering continues past global max (99→100)", nxt2 == 100)

print('\n' + ('✅ ALL PASS' if ok else '❌ FAIL'))
