"""Regression: cloak defers a package until a proxy is validated (2026-07-16 fix)."""
import sys, time
import package_queue as pq
fails=[]
def ck(c,m): print(("  ✓ " if c else "  ✗ ")+m); (fails.append(m) if not c else None)
now = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
old = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time()-1200))  # 20min ago
ck(pq._req_stale({'finished_utc': now}) is False, "fresh request → NOT stale (wait for proxy)")
ck(pq._req_stale({'finished_utc': old}) is True, "20-min-old request → stale (build anyway, safety valve)")
ck(pq._req_stale({}) is False, "no timestamp → not stale")
ck(callable(pq._available_proxies), "_available_proxies guard exists")
print("RESULT:", "❌ FAIL" if fails else "✅ ALL PASS"); sys.exit(1 if fails else 0)
