"""End-to-end real-API test for the /proxy pipeline. Run inside the
acc-setup-bot Railway container via `railway ssh "python test_proxy_e2e.py"`.

What it does (REAL EXTERNAL CALLS — costs ~$0.01 + 1 GoLogin slot):
  1. Pulls one fresh IPRoyal mobile proxy
  2. Runs every pre-gate (exit-IP probe → mobile carrier check → IPQS →
     AbuseIPDB → ip-api → DNSBL → latency p95 → multi-destination)
  3. Runs every browser gate (Google "hello" → Facebook login probe →
     reCAPTCHA v3 score) — launches Camoufox
  4. If all gates pass, creates ONE GoLogin profile named
     'TEST E2E <iso-timestamp>' with the validated proxy attached
  5. Prints structured PASS/FAIL for each stage so failures are obvious

Safe to re-run; each run creates a new dated profile so it doesn't overwrite
existing ones. Set DRY_RUN=1 in the env to skip the GoLogin profile creation
(useful for "did the pipeline work?" without burning a profile slot)."""
import os, sys, asyncio, time, datetime, json, traceback

sys.path.insert(0, '/app')   # Railway working dir
import proxy as P
print("=" * 70)
print(f"  /proxy pipeline E2E test  ·  acc-setup-bot real-API run")
print(f"  started at {datetime.datetime.utcnow().isoformat()}Z")
print("=" * 70)

# Sanity-check env vars
missing = [k for k in ("GOLOGIN_API_KEY","IPROYAL_USERNAME","IPROYAL_PASSWORD",
                       "IPQS_API_KEY","ABUSEIPDB_API_KEY","FB_PROXY_TEST_PHONE",
                       "FB_PROXY_TEST_PASSWORD") if not os.environ.get(k)]
if missing:
    print(f"❌ Missing env vars: {missing}")
    sys.exit(2)
print("✓ all required env vars present")

dry = os.environ.get('DRY_RUN') == '1'
target = 1
prefix = f"TEST E2E {datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M')}"
print(f"  mode:   {'DRY RUN (no GoLogin profile created)' if dry else 'LIVE (will create 1 GoLogin profile)'}")
print(f"  target: {target} validated profile")
print(f"  name:   '{prefix} 1'")
print()

events = []
async def upd(text):
    # strip telegram HTML for terminal readability
    import re
    plain = re.sub(r'<[^>]+>', '', text)
    print(f"  [pipeline] {plain[:200]}")
    events.append(plain)

async def photo(b, caption=''):
    events.append(f"[photo] {caption}")

P._PROXY_PIPELINE_SINGLETON = None
pipe = P._pipeline()

if dry:
    # Monkey-patch GoLogin creation so the test exercises every gate but
    # doesn't burn a profile slot.
    def _fake_create(self, name, proxy_str=None):
        print(f"  [DRY RUN] would create GoLogin profile {name!r}")
        return (f"dry_run_{int(time.time())}", "dry-run, no real profile")
    P.ProxyPipeline.create_gologin_profile = _fake_create

print("=" * 70)
print("RUNNING REAL PIPELINE — this can take 1-3 minutes (Camoufox launches)")
print("=" * 70)
t0 = time.time()
try:
    created, attempts, success, msg = asyncio.run(
        pipe.run_batch_proxy_check_pipeline(
            upd, photo,
            target_profiles=target, name_prefix=prefix,
            max_total_attempts=15))    # cap so a bad pool can't run forever
    elapsed = time.time() - t0
    print()
    print("=" * 70)
    print(f"  RESULT  ({elapsed:.1f}s elapsed)")
    print("=" * 70)
    print(f"  attempts: {attempts}")
    print(f"  created:  {len(created)}")
    for c in created:
        print(f"    • {c.get('name')}")
        print(f"        id:    {c.get('id')}")
        print(f"        exit:  {c.get('exit_ip')}")
        print(f"        score: {c.get('score')}")
    print(f"  success:  {success}")
    if msg:
        # strip HTML
        import re
        print(f"  msg:      {re.sub(r'<[^>]+>', '', msg)}")
    print()
    if success and len(created) > 0:
        print("✅ E2E TEST PASSED — pipeline works end-to-end with real APIs.")
        print("   Check your GoLogin dashboard to see the new test profile.")
        sys.exit(0)
    else:
        print("⚠ E2E TEST PARTIAL — pipeline ran but no profile validated.")
        print("   This can happen if the IPRoyal pool's current proxies are degraded.")
        print(f"   {attempts} proxies tried; check the per-attempt 🚫 reasons above.")
        sys.exit(1)
except Exception as e:
    print()
    print("=" * 70)
    print("❌ E2E TEST CRASHED")
    print("=" * 70)
    traceback.print_exc()
    sys.exit(3)
