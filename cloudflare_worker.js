// ─────────────────────────────────────────────────────────────────────────
// Cloudflare Worker — Bio-link cloaking + landing-page renderer
// ─────────────────────────────────────────────────────────────────────────
//
// FLOW:
//   1. Bio link in social: yourdomain.link/vampire?utm_source=ig
//   2. This Worker catches the request at the Cloudflare edge
//   3. Detects: bot/crawler vs real human
//   4. If bot: serves a benign HTML page (a generic "personal blog" feel)
//      so Meta/Google crawlers see nothing flagable
//   5. If human: serves a per-model landing page (photo + bio + 3 links:
//      X, private IG, OnlyFans — OF rendered big at top, matching the
//      link.me/LinkBeacon style the user already uses). All 3 outgoing
//      links carry UTM params propagated from the incoming URL.
//
// SETUP (in Cloudflare dashboard after deploy):
//   1. Workers & Pages → Create application → Worker → name it 'cloak'
//   2. Paste this file as the Worker script
//   3. Settings → Variables and Secrets → add (for each model):
//        MODEL_CAROLINA_DISPLAY = "Caro"
//        MODEL_CAROLINA_HANDLE = "@vampychyuwu"
//        MODEL_CAROLINA_BIO = "My links and more content below"
//        MODEL_CAROLINA_PHOTO_URL = "https://drive.google.com/uc?id=<file_id>"
//        MODEL_CAROLINA_X = "https://x.com/<handle>"
//        MODEL_CAROLINA_IG = "https://instagram.com/<private_handle>"
//        OF_LINK_CAROLINA = "https://onlyfans.com/<handle>"
//        (same set for KIERA)
//   4. KV Namespace bindings → bind a new KV namespace as SLUG_MAP
//   5. Triggers → add Routes → "yourdomain.link/*" → this Worker
//
// SLUG MAP (in KV under SLUG_MAP):
//   key = slug (e.g. "vampire") → value = model name (e.g. "carolina")
//   The bot's /cloak command writes/updates these via Cloudflare's KV API.

const BOT_UA_PATTERNS = [
  // Meta crawlers — explicit. facebookexternalhit is the link-preview
  // bot that scrapes URLs pasted into FB/Messenger/Instagram.
  /facebookexternalhit/i, /meta-externalagent/i, /meta-externalfetcher/i,
  /facebookbot/i, /facebookcatalog/i, /instagrambot/i,
  /tiktokbot/i, /tiktokspider/i, /bytespider/i, /bytedance/i,
  // Other big-name crawlers
  /googlebot/i, /google-inspectiontool/i, /adsbot-google/i,
  /bingbot/i, /bingpreview/i, /slackbot/i, /slack-imgproxy/i,
  /twitterbot/i, /linkedinbot/i, /linkedin-bot/i,
  /whatsapp/i, /telegrambot/i, /discordbot/i, /redditbot/i,
  /applebot/i, /yandexbot/i, /duckduckbot/i, /duckduckgo-favicons-bot/i,
  /baiduspider/i, /pinterestbot/i, /pinterest\.com/i, /snapchatbot/i,
  /quora-bot/i, /threadsbot/i,
  // Generic bot keywords in the UA string
  /\bbot\b/i, /\bcrawler\b/i, /\bspider\b/i, /\bscraper\b/i,
  /\bpreview\b/i, /\bunfurl/i, /linkpreview/i, /linkchecker/i,
  // Headless browsers + automation frameworks
  /headlesschrome/i, /phantomjs/i, /selenium/i, /puppeteer/i, /playwright/i,
  /chrome-lighthouse/i, /pagespeed/i,
  // HTTP libraries — almost always bots / scrapers. 2026-05-14: removed
  // `okhttp` and `java/` from the list — they appear in legit Android
  // in-app browsers (Reddit, Twitter, etc.), and blocking would silently
  // break real users tapping our link from inside those apps.
  /\bcurl\b/i, /\bwget\b/i, /python-requests/i, /python-urllib/i,
  /aiohttp/i, /httpx/i, /go-http-client/i, /node-fetch/i, /\baxios\b/i,
  /\bruby\b/i, /\bperl\b/i, /libwww/i,
];

// Meta-owned Autonomous System Numbers. ANY request from these ASNs is
// almost certainly Facebook/Instagram infrastructure scraping us — even
// if they spoof a normal browser UA. The ASNs are stable; Meta has held
// them for years. Sourced from PeeringDB + bgp.he.net.
//   AS32934   — Facebook, Inc. (primary)
//   AS54115   — Facebook Operations LLC
//   AS63293   — Facebook (CDN edge)
//   AS149642  — Meta Platforms, Inc.
//   AS149835  — Facebook Asia (APAC infra)
const META_ASNS = new Set([32934, 54115, 63293, 149642, 149835]);

function isBot(request) {
  const ua = request.headers.get('User-Agent') || '';
  if (!ua) return true;
  for (const p of BOT_UA_PATTERNS) if (p.test(ua)) return true;
  if (request.cf && request.cf.botManagement &&
      request.cf.botManagement.verifiedBot) return true;
  // ASN gate — catches Meta crawlers that spoof a real-browser UA from
  // their own servers. AS32934 (Facebook) is the big one; the rest are
  // Meta-owned subsidiaries / edge networks. Real visitors browse from
  // residential / mobile ISPs, never from these ASNs.
  if (request.cf && typeof request.cf.asn === 'number'
      && META_ASNS.has(request.cf.asn)) {
    return true;
  }
  // 2026-05-13: removed Accept-Language check. Some in-app mobile
  // browsers (Telegram preview view, FB/IG in-app webviews on certain
  // OS versions) strip it, which would silently misclassify real
  // human traffic as bots. The remaining signals (missing UA, explicit
  // bot UA strings, Cloudflare's curated verifiedBot list, Meta ASN)
  // have near-zero false-positive rate on real browsers.
  return false;
}

// ─── Benign HTML (what bots see) ──────────────────────────────────────────
// Looks like a tiny personal landing page. Indexable. No spicy links.
function benignHTML() {
  return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Personal Page</title>
<style>
  body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
       background:#fafafa;color:#333;max-width:640px;margin:60px auto;
       padding:24px;line-height:1.6}
  h1{color:#555;margin-bottom:8px}
  p{margin:12px 0}
  a{color:#0066cc;text-decoration:none}
  a:hover{text-decoration:underline}
  .foot{margin-top:60px;color:#999;font-size:0.85em}
</style>
</head>
<body>
<h1>Hi, thanks for stopping by</h1>
<p>This is my personal page. I occasionally share photography and art —
   check back later or find me on
   <a href="https://www.pinterest.com">Pinterest</a> and
   <a href="https://unsplash.com">Unsplash</a> when I have new work to post.</p>
<p>If you got here from one of my social profiles, please make sure you
   used the correct link.</p>
<p class="foot">© ${new Date().getFullYear()} — personal page</p>
</body>
</html>`;
}

// ─── Intermediate chain pages ─────────────────────────────────────────────
// 2026-05-14: Sophie Rain-style chain — landing → /r → /v → /go. Stays in
// the in-app webview the whole way (no escape attempt). The hostname stays
// on our cloak domain through /r and /v; only the final 302 (/go) sends
// the user to onlyfans.com. UTMs + fbclid travel through as query params;
// each page re-emits them on the next link so the chain is stateless.

function redirectingHTML(nextUrl) {
  // "Redirecting to OnlyFans..." spinner page. 1.5s pause then JS hops
  // to /v. meta-refresh is a fallback for browsers that block our JS.
  return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Redirecting…</title>
<meta name="theme-color" content="#000">
<meta http-equiv="refresh" content="2;url=${escapeAttr(nextUrl)}">
<meta name="robots" content="noindex">
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
       background:#000;color:#fff;min-height:100vh;
       display:flex;align-items:center;justify-content:center;padding:24px}
  .card{border:1px solid rgba(255,255,255,.14);border-radius:18px;
        padding:34px 26px;text-align:center;max-width:360px;width:100%;
        background:rgba(255,255,255,.02)}
  .eye{width:56px;height:56px;border-radius:50%;
       background:rgba(255,255,255,.07);
       display:inline-flex;align-items:center;justify-content:center;
       margin-bottom:18px}
  .eye svg{width:30px;height:30px;stroke:#fff;fill:none;
           stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
  h1{font-size:19px;font-weight:700;margin-bottom:8px;letter-spacing:.2px}
  p{font-size:14px;color:rgba(255,255,255,.65);margin-bottom:22px;line-height:1.45}
  .spin-row{display:inline-flex;align-items:center;gap:10px;
            justify-content:center}
  .spinner{width:22px;height:22px;border:2px solid rgba(255,255,255,.18);
           border-top-color:#fff;border-radius:50%;
           animation:spin 1s linear infinite}
  @keyframes spin{to{transform:rotate(360deg)}}
  .dots{display:inline-flex;gap:5px}
  .dots span{width:6px;height:6px;border-radius:50%;
             background:rgba(255,255,255,.55);
             animation:pulse 1.4s infinite ease-in-out both}
  .dots span:nth-child(2){animation-delay:.2s}
  .dots span:nth-child(3){animation-delay:.4s}
  @keyframes pulse{0%,80%,100%{opacity:.3}40%{opacity:1}}
</style>
</head>
<body>
<div class="card">
  <div class="eye">
    <svg viewBox="0 0 24 24">
      <path d="M 17.94 17.94 A 10.07 10.07 0 0 1 12 20 c-7 0-11-8-11-8 a 18.45 18.45 0 0 1 5.06-5.94 M 9.9 4.24 A 9.12 9.12 0 0 1 12 4 c 7 0 11 8 11 8 a 18.5 18.5 0 0 1-2.16 3.19 m-6.72-1.07 a 3 3 0 1 1-4.24-4.24"/>
      <line x1="1" y1="1" x2="23" y2="23"/>
    </svg>
  </div>
  <h1>Redirecting to OnlyFans…</h1>
  <p>Please hold on while we prepare your link.</p>
  <div class="spin-row">
    <span class="spinner"></span>
    <span class="dots"><span></span><span></span><span></span></span>
  </div>
</div>
<script>
  setTimeout(function(){
    try { location.replace(${JSON.stringify(nextUrl)}); }
    catch(e) { location.href = ${JSON.stringify(nextUrl)}; }
  }, 1500);
</script>
</body>
</html>`;
}

function visitHTML(nextUrl) {
  // 18+ content warning page with a big white "Open" pill. Tap → /go
  // → 302 to onlyfans.com. The OF URL is never present in this HTML,
  // so crawlers reading the page see only "/go" as the destination.
  return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>18+ Content Warning</title>
<meta name="theme-color" content="#000">
<meta name="robots" content="noindex">
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
       background:#000;color:#fff;min-height:100vh;
       display:flex;align-items:center;justify-content:center;padding:24px}
  .card{border:1px solid rgba(255,255,255,.14);border-radius:20px;
        padding:38px 26px 28px;text-align:center;max-width:380px;width:100%;
        background:rgba(255,255,255,.02)}
  .eye{width:60px;height:60px;display:inline-flex;
       align-items:center;justify-content:center;margin-bottom:16px}
  .eye svg{width:48px;height:48px;stroke:#fff;fill:none;
           stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
  h1{font-size:22px;font-weight:700;margin-bottom:10px;letter-spacing:.2px}
  p{font-size:14px;color:rgba(255,255,255,.7);margin-bottom:28px;line-height:1.5}
  a.open-btn{display:block;width:100%;padding:16px 20px;
             background:#fff;color:#000;border-radius:999px;
             font-size:16px;font-weight:700;text-decoration:none;
             letter-spacing:.3px;
             transition:transform .15s ease,opacity .15s ease}
  a.open-btn:active{transform:scale(.98);opacity:.92}
</style>
</head>
<body>
<div class="card">
  <div class="eye">
    <svg viewBox="0 0 24 24">
      <path d="M 17.94 17.94 A 10.07 10.07 0 0 1 12 20 c-7 0-11-8-11-8 a 18.45 18.45 0 0 1 5.06-5.94 M 9.9 4.24 A 9.12 9.12 0 0 1 12 4 c 7 0 11 8 11 8 a 18.5 18.5 0 0 1-2.16 3.19 m-6.72-1.07 a 3 3 0 1 1-4.24-4.24"/>
      <line x1="1" y1="1" x2="23" y2="23"/>
    </svg>
  </div>
  <h1>18+ Content Warning</h1>
  <p>This link may contain graphic or adult content.</p>
  <a class="open-btn" href="${escapeAttr(nextUrl)}" rel="noopener">Open</a>
</div>
</body>
</html>`;
}

// ─── Per-model landing page (what humans see) ─────────────────────────────
// 2026-05-13 redesign per user feedback:
//   • Hero photo removed entirely. Only the OF card matters visually.
//   • OF card moved to TOP, large, with heartbeat animation pulling
//     the eye toward the call-to-action.
//   • Real OnlyFans-style logo SVG in the top-left corner (replaces
//     the old stylized 𝕆 letter badge).
//   • Overlay text ("OFF 💦😘" etc.) is now editable per-slug via
//     slugConfig.of_overlay — falls back to "OFF 💦😘" if unset.
//   • Display name + bio sit below the card, X + IG buttons at the
//     bottom, in that order.
function landingHTML(model, incomingUtm) {
  const utmQS = buildUtmQueryString(incomingUtm, model.slug);
  const ofUrl = appendQS(model.ofLink, utmQS);
  const xUrl  = model.xLink ? appendQS(model.xLink, utmQS) : '';
  const igUrl = model.igLink ? appendQS(model.igLink, utmQS) : '';

  const display    = escapeHTML(model.display || 'Page');
  const bio        = escapeHTML(model.bio     || '');
  const ofPhoto    = model.ofPhotoUrl || '';
  const ofOverlay  = escapeHTML(model.ofOverlay || 'OFF 💦😘');
  const brandName  = escapeHTML(model.brandName || 'Personal');

  // Per-page favicon: a colored circle with the display-name's first
  // letter. Encoded as a data: URI so the browser doesn't need a
  // separate request. CRITICAL: encodeURIComponent the WHOLE SVG —
  // the unencoded " inside SVG attributes would otherwise break out
  // of href="..." and leak the SVG markup into the body (which the
  // user saw on 2026-05-13 as "K"> K"> at the top of the page).
  const initialChar = ((model.display || 'P').trim()[0] || 'P').toUpperCase();
  const faviconSvg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><circle cx="32" cy="32" r="32" fill="#00aff0"/><text x="32" y="44" font-family="Helvetica,Arial,sans-serif" font-size="36" font-weight="700" fill="white" text-anchor="middle">${initialChar}</text></svg>`;
  const faviconUri = `data:image/svg+xml,${encodeURIComponent(faviconSvg)}`;

  return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>${display}</title>
<link rel="icon" type="image/svg+xml" href="${faviconUri}">
<link rel="apple-touch-icon" href="${faviconUri}">
<meta name="theme-color" content="#00aff0">
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
       background:#000;color:#fff;min-height:100vh}
  .wrap{max-width:520px;margin:0 auto;padding:24px 16px 40px;}
  .ident{text-align:center;padding:20px 12px 4px;position:relative}
  .display{font-size:28px;font-weight:700;color:#fff;margin-bottom:4px}
  .bio{color:#ccc;font-size:15px;margin:8px 0 20px;padding:0 8px}
  .stack{display:flex;flex-direction:column;gap:14px;padding:0 6px;margin-top:14px}
  a.btn{display:flex;align-items:center;justify-content:center;gap:10px;
        text-decoration:none;color:#fff;border-radius:16px;
        padding:18px 16px;font-weight:600;font-size:16px;
        transition:transform .15s ease,opacity .15s ease}
  a.btn:hover{transform:scale(1.02);opacity:.95}
  a.x{background:#000;border:1px solid #2a2a2a}
  a.ig{background:linear-gradient(135deg,#f09433 0%,#e6683c 25%,
       #dc2743 50%,#cc2366 75%,#bc1888 100%)}
  .brand-icon{width:24px;height:24px;display:inline-flex;align-items:center;
              justify-content:center;flex:0 0 auto}
  .brand-icon svg{width:100%;height:100%}
  /* OF card — TOP of the page, heartbeat animation, real OF logo. */
  @keyframes heartbeat {
    0%, 30%, 70%, 100% { transform: scale(1); }
    15%, 45%           { transform: scale(1.035); }
  }
  a.of-card{display:block;position:relative;border-radius:22px;
            overflow:hidden;text-decoration:none;
            /* Warm magenta/pink-violet halo — feminine + intense
               without being a flashlight. Slightly larger spread
               than the prior blue glow so it stands out without
               screaming. */
            box-shadow:0 16px 70px 4px rgba(225, 70, 175, .55);
            animation: heartbeat 1.8s ease-in-out infinite;
            transform-origin: center center}
  a.of-card:hover{animation-play-state:paused;transform:scale(1.04)}
  a.of-card .of-img{width:100%;aspect-ratio:4/5;object-fit:cover;
                    display:block}
  a.of-card .of-logo{position:absolute;top:14px;left:14px;
                     width:44px;height:44px;
                     background:#00aff0;border-radius:50%;
                     display:flex;align-items:center;justify-content:center;
                     box-shadow:0 2px 14px rgba(0,175,240,.65)}
  a.of-card .of-logo svg{width:30px;height:30px}
  a.of-card .of-overlay{position:absolute;left:0;right:0;bottom:0;
                        padding:24px 16px 26px;
                        background:linear-gradient(to bottom,
                          rgba(0,0,0,0),rgba(0,0,0,.85));
                        color:#fff;text-align:center;
                        font-size:26px;font-weight:800;
                        letter-spacing:.5px;
                        text-shadow:0 2px 10px rgba(0,0,0,.7)}
  .row{display:flex;align-items:center;gap:14px}
  .row .label{flex:1;text-align:left}
  .foot{text-align:center;color:#666;font-size:11px;margin-top:30px}
  a.brand-foot{display:inline-block;margin-top:6px;
               color:#888;text-decoration:none;
               font-size:12px;font-weight:600;letter-spacing:.4px;
               transition:color .15s ease}
  a.brand-foot:hover{color:#ccc}
  /* Tooltip pointing at the three-dots menu — shown ONLY when JS
     detects an in-app webview (Instagram/FB/TikTok/etc). Real browsers
     skip it because their menus aren't ⋯-shaped. */
  #ext-tip{display:none;position:fixed;top:14px;right:14px;z-index:9998;
           max-width:220px;padding:10px 14px;
           background:#fff;color:#111;border-radius:14px;
           font-size:13px;font-weight:600;line-height:1.35;
           box-shadow:0 6px 28px rgba(0,0,0,.55);text-align:center}
  #ext-tip:after{content:"";position:absolute;top:-9px;right:18px;
                 width:0;height:0;border:9px solid transparent;
                 border-top:none;border-bottom-color:#fff}
  /* 18+ interstitial — shown ONLY when JS detects an in-app webview
     AND the user taps the OF card. Real-browser users skip it entirely
     and go straight through the /r → /v → /go chain. Webview users
     see this overlay first so they can choose how to escape (pink
     button = x-safari://r, ⋯ menu = reopen landing page in OS browser). */
  #ext-overlay{display:none;position:fixed;inset:0;z-index:10000;
               background:rgba(0,0,0,.92);backdrop-filter:blur(8px);
               -webkit-backdrop-filter:blur(8px);
               flex-direction:column;align-items:center;justify-content:center;
               padding:32px 28px;color:#fff;text-align:center}
  #ext-overlay.show{display:flex}
  #ext-overlay .x{position:absolute;top:22px;left:22px;
                  width:38px;height:38px;border-radius:50%;
                  background:rgba(255,255,255,.1);
                  display:flex;align-items:center;justify-content:center;
                  font-size:22px;cursor:pointer;color:#fff}
  #ext-overlay .ext-eye{width:72px;height:72px;margin-bottom:32px;opacity:.95}
  #ext-overlay h2{font-size:24px;font-weight:800;margin-bottom:20px;
                  letter-spacing:.3px}
  #ext-overlay p{font-size:15px;color:#ccc;margin-bottom:26px;line-height:1.5;
                 max-width:340px}
  #ext-overlay .ext-instr{font-size:14px;color:#aaa;margin:0 0 36px;
                          padding:0 16px;line-height:1.5;max-width:340px}
  #ext-overlay .ext-instr b{color:#fff;font-weight:700}
  #ext-overlay #ext-go{appearance:none;border:none;cursor:pointer;
                       display:inline-block;text-decoration:none;
                       padding:18px 34px;border-radius:16px;
                       background:linear-gradient(135deg,#e8458f,#9c27b0);
                       color:#fff;font-size:17px;font-weight:700;
                       letter-spacing:.3px;
                       box-shadow:0 10px 36px rgba(225,70,175,.55);
                       transition:transform .15s ease}
  #ext-overlay #ext-go:active{transform:scale(.97)}
</style>
</head>
<body>
<div id="ext-tip">Tap <b>⋯</b> → <b>Open in external browser</b></div>
<div id="ext-overlay" role="dialog" aria-modal="true">
  <span class="x" onclick="document.getElementById('ext-overlay').classList.remove('show')">×</span>
  <svg class="ext-eye" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
    <path d="M 17.94 17.94 A 10.07 10.07 0 0 1 12 20 c-7 0-11-8-11-8 a 18.45 18.45 0 0 1 5.06-5.94 M 9.9 4.24 A 9.12 9.12 0 0 1 12 4 c 7 0 11 8 11 8 a 18.5 18.5 0 0 1-2.16 3.19 m-6.72-1.07 a 3 3 0 1 1-4.24-4.24"/>
    <line x1="1" y1="1" x2="23" y2="23"/>
  </svg>
  <h2>18+ Content Warning</h2>
  <p>This link may contain graphic or adult content.</p>
  <div class="ext-instr">Tap the <b>⋯</b> in the top right, then choose <b>Open in external browser</b></div>
  <a id="ext-go" href="#" target="_blank" rel="noopener">Open in external browser</a>
  <div id="ext-hold-hint" style="display:none;margin-top:14px;font-size:13px;color:rgba(255,255,255,.78);max-width:300px;line-height:1.5">☝️ <b>Hold</b> the pink button for a moment, then tap <b>Open</b></div>
</div>
<div class="wrap">
  ${ofUrl ? `
    <a class="of-card" href="/r?${utmQS}" data-rotate-utm="of" data-base-href="/r" rel="noopener">
      ${ofPhoto ? `<img class="of-img" src="${escapeAttr(ofPhoto)}" alt="">` : ''}
      <span class="of-logo">
        <!-- White padlock on the light-blue circle backdrop set by
             .of-logo (background:#00aff0). Generic lock mark — implies
             "exclusive/locked content" without using any brand trademark. -->
        <svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
          <!-- Shackle (arc above the body) -->
          <path d="M 8 11 V 8 a 4 4 0 0 1 8 0 v 3" fill="none" stroke="#fff" stroke-width="2.2" stroke-linecap="round"/>
          <!-- Lock body (rounded rect) -->
          <rect x="6" y="11" width="12" height="9" rx="1.8" fill="#fff"/>
          <!-- Keyhole (cut-out colored same as backdrop) -->
          <circle cx="12" cy="15" r="1.2" fill="#00aff0"/>
          <rect x="11.4" y="15.4" width="1.2" height="3" rx="0.6" fill="#00aff0"/>
        </svg>
      </span>
      <div class="of-overlay">${ofOverlay}</div>
    </a>` : ''}
  <div class="ident">
    <div class="display">${display}</div>
    ${bio ? `<div class="bio">${bio}</div>` : ''}
    <div class="stack">
      ${xUrl  ? `<a class="btn x"  href="${escapeAttr(xUrl)}"  data-rotate-utm="x"  data-base-href="${escapeAttr(model.xLink || '')}"  target="_blank" rel="noopener"><span class="row"><span class="brand-icon"><svg viewBox="0 0 24 24" fill="#fff"><path d="M18.244 2.25h3.308l-7.227 8.26 8.502 11.24H16.17l-5.214-6.817L4.99 21.75H1.68l7.73-8.835L1.254 2.25H8.08l4.713 6.231zm-1.161 17.52h1.833L7.084 4.126H5.117z"/></svg></span><span class="label">My Secret X</span></span></a>` : ''}
      ${igUrl ? `<a class="btn ig" href="${escapeAttr(igUrl)}" data-rotate-utm="ig" data-base-href="${escapeAttr(model.igLink || '')}" target="_blank" rel="noopener"><span class="row"><span class="brand-icon"><svg viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2"><rect x="2" y="2" width="20" height="20" rx="5"/><circle cx="12" cy="12" r="4"/><circle cx="17.5" cy="6.5" r="1.2" fill="#fff" stroke="none"/></svg></span><span class="label">My Private Instagram</span></span></a>` : ''}
    </div>
    <div class="foot">© ${new Date().getFullYear()} · ${display} · All rights reserved
      <br><a class="brand-foot" href="/">✨ ${brandName}</a></div>
  </div>
</div>
<script>
// ─── UTM rotation (2026-05-18) ───────────────────────────────────────
// Bake-time UTMs (rendered into hrefs above) are only the no-JS fallback.
// With JS, every page load AND every click rewrites a[data-rotate-utm]
// hrefs with a freshly-picked random set, so:
//   • Cloudflare edge cache returning stale HTML → fixed (JS rewrites
//     on load before user sees anything).
//   • Browser bfcache replaying the same DOM on Back/Forward → fixed
//     (capture-phase click listener rewrites just before navigation).
//   • Multiple clicks on the same page → each click gets a distinct
//     UTM set propagated through /r → /v → /go.
// Pools mirror the server-side buildUtmQueryString in this file.
(function(){
  var SOURCES = ['ig','instagram','tt','tiktok','x','twitter','rd','reddit'];
  var MEDIUMS = ['social','referral','organic'];
  var CONTENTS = ['link_in_bio','bio_link','profile_link','profile',
                  'story_link','story_swipe','dm_link','link_sticker',
                  'pinned_post','highlight'];
  var SLUG = ${JSON.stringify(model.slug || 'organic')};
  function pick(arr){ return arr[Math.floor(Math.random()*arr.length)]; }
  function freshQs(){
    return 'utm_source=' + encodeURIComponent(pick(SOURCES))
      + '&utm_medium=' + encodeURIComponent(pick(MEDIUMS))
      + '&utm_campaign=' + encodeURIComponent(SLUG)
      + '&utm_content=' + encodeURIComponent(pick(CONTENTS));
  }
  function rewriteHref(a){
    var base = a.getAttribute('data-base-href');
    if (!base) return;
    var sep = base.indexOf('?') >= 0 ? '&' : '?';
    a.setAttribute('href', base + sep + freshQs());
  }
  // 1) Initial rewrite (handles Cloudflare cache / bfcache that may serve
  //    stale baked-in UTMs)
  document.querySelectorAll('a[data-rotate-utm]').forEach(rewriteHref);
  // 2) Per-click rewrite (capture phase = runs before any other handler
  //    and before browser navigation, so the new href is what actually
  //    gets navigated to). Delegates from document so it survives any
  //    later DOM mutations.
  document.addEventListener('click', function(e){
    if (e.target && e.target.closest) {
      var a = e.target.closest('a[data-rotate-utm]');
      if (a) rewriteHref(a);
    }
  }, true);
})();
</script>
<script>
(function(){
  // Two distinct flows depending on the user's browser context:
  //   • Real browser (Safari/Chrome): OF card → /r → /v → /go chain
  //     plays out naturally. No overlay, no escape attempt.
  //   • In-app webview (IG/FB/TikTok/etc): OF card tap is intercepted
  //     and the 18+ overlay shows instead. From the overlay, user can
  //     (a) tap pink button → x-safari://r or intent://r → OS prompts
  //     "Leave [App]? Open Safari" → chain plays out externally; or
  //     (b) tap ⋯ → "Open in external browser" → landing page reopens
  //     in OS browser → user taps OF card again → falls into the
  //     real-browser flow.
  var ua = navigator.userAgent || '';
  // Explicit in-app tokens — covers most major apps.
  var inApp = /Instagram|FBAN|FBAV|FB_IAB|FB4A|FBIOS|MessengerForiOS|TikTok|musical_ly|Bytedance|Twitter|TwitterAndroid|Snapchat|MicroMessenger|WeChat|Pinterest|YJApp|KAKAOTALK|NAVER/i.test(ua)
              || /Line\\//i.test(ua)
              || /\\bFacebook\\b/i.test(ua);
  // Heuristic fallback: a mobile UA that is missing every real-browser
  // signature is almost certainly a webview (FB, Messenger, etc. don't
  // always include their FBAN/FBAV/FB_IAB tokens — e.g. FB on iOS
  // sometimes ships only a Mobile/15E148 marker). Better to over-show
  // the overlay than miss a webview tap.
  if (!inApp) {
    var isIOSua = /iPhone|iPad|iPod/i.test(ua);
    var isAndroidUa = /Android/i.test(ua);
    if (isIOSua) {
      var hasRealBrowserTok = /Safari\\//i.test(ua) || /CriOS\\//i.test(ua) ||
                              /FxiOS\\//i.test(ua) || /EdgiOS\\//i.test(ua);
      if (!hasRealBrowserTok) inApp = true;
    } else if (isAndroidUa) {
      // ";wv)" is the standard Android WebView marker, "; wv)" with space too.
      if (/;\\s*wv\\)/i.test(ua)) inApp = true;
    }
  }
  if (!inApp) return;  // real browser — let OF card navigate naturally

  var tip = document.getElementById('ext-tip');
  if (tip) tip.style.display = 'block';

  var ofCard = document.querySelector('a.of-card');
  var overlay = document.getElementById('ext-overlay');
  var goBtn = document.getElementById('ext-go');
  if (ofCard && overlay) {
    ofCard.addEventListener('click', function(e){
      e.preventDefault();
      overlay.classList.add('show');
    }, false);
  }
  if (goBtn && ofCard) {
    // Pink button escape — replicates Bouncy.ai's mechanism, reverse-
    // engineered from their public bouncy-meta-escape.js (2026-05-14).
    //
    //   • iOS + Instagram webview: fire private scheme
    //       instagram://extbrowser/?url=<encoded /r>
    //     IG app routes it to Safari. Single tap, no hold needed.
    //   • iOS + Threads webview: fire private scheme
    //       barcelona://extbrowser/?url=<encoded /r>
    //     Same mechanism; "Barcelona" is Threads' internal name.
    //   • iOS + Facebook (or any other iOS webview): two-step —
    //     (1) try googlechromes://<host>/r (single tap if Chrome
    //         is installed → Chrome opens it),
    //     (2) after 200ms if still on the page, transform the button
    //         into an <a href="https://<host>/r"> with click
    //         preventDefault'd and show a "Hold then tap Open" hint.
    //         User long-presses → iOS native long-press menu fires
    //         at ~500ms → tap "Open" → "Leave Facebook?" prompt →
    //         Safari opens.
    //     FB on iOS has no scheme that works without hold —
    //     confirmed by Bouncy's source ("no known scheme works").
    //   • Android (any webview): intent:// on anchor href. Works on
    //     stock Android, Xiaomi/MIUI, OPPO/ColorOS, Huawei, Samsung —
    //     every Chromium-based WebView honors intent URIs.
    var rawHref = ofCard.getAttribute('href') || '/r';
    var href = new URL(rawHref, location.href).href;
    var isIOS = /iPhone|iPad|iPod/i.test(ua);
    var isAndroid = /Android/i.test(ua);
    if (isIOS) {
      // Detect the Meta sub-platform. Threads check BEFORE Instagram
      // because Threads' UA includes both Barcelona and Instagram tokens.
      var iosPlatform = /Barcelona/i.test(ua) ? 'threads'
                      : /FBAN|FBAV|FB_IAB|FB4A|FBIOS/i.test(ua) ? 'facebook'
                      : /Instagram/i.test(ua) ? 'instagram'
                      : 'other';
      if (iosPlatform === 'instagram' || iosPlatform === 'threads') {
        var metaScheme = iosPlatform === 'instagram'
          ? 'instagram://extbrowser/?url='
          : 'barcelona://extbrowser/?url=';
        goBtn.setAttribute('href', '#');
        goBtn.addEventListener('click', function(e){
          e.preventDefault();
          // location.replace — does NOT add an entry to history,
          // matches Bouncy's call.
          try { window.location.replace(metaScheme + encodeURIComponent(href)); }
          catch (_e) { window.location.href = metaScheme + encodeURIComponent(href); }
        }, false);
      } else {
        // Facebook or unknown iOS webview — Bouncy's exact 3-scheme
        // fallback chain. Whichever one this device's WKWebView
        // honors first wins. From adult-bounce-inline.js lines 479-486:
        //   T=0     : googlechrome://<host>/...   (Chrome, if installed)
        //   T=500ms : x-safari-https://<host>/... (Safari scheme)
        //   T=1000ms: window.open(url, '_blank')  (last-ditch popup)
        //   T=1500ms: transform button to long-press anchor as final
        //             fallback (user holds → iOS native menu → Safari).
        goBtn.setAttribute('href', '#');
        var transformed = false;
        goBtn.addEventListener('click', function(e){
          if (transformed) {
            // Already in long-press mode — block tap, force hold.
            e.preventDefault();
            return;
          }
          e.preventDefault();
          // T=0: googlechrome:// (single colon variant — what Bouncy uses).
          try {
            window.location.href = 'googlechrome://' + href.replace(/^https?:\\/\\//, '');
          } catch (_e) {}
          // T=500ms: x-safari- + full URL (prepended, NOT x-safari-https://).
          setTimeout(function(){
            if (document.hidden) return;
            try { window.location.href = 'x-safari-' + href; } catch (_e) {}
          }, 500);
          // T=1000ms: window.open as fallback popup.
          setTimeout(function(){
            if (document.hidden) return;
            try { window.open(href, '_blank'); } catch (_e) {}
          }, 1000);
          // T=1500ms: still here? transform to long-press anchor.
          setTimeout(function(){
            if (document.hidden) return;
            transformed = true;
            goBtn.setAttribute('href', href);
            goBtn.setAttribute('target', '_blank');
            var hint = document.getElementById('ext-hold-hint');
            if (hint) hint.style.display = 'block';
          }, 1500);
        }, false);
      }
    } else if (isAndroid) {
      var stripped = href.replace(/^https?:\\/\\//, '');
      goBtn.href = 'intent://' + stripped + '#Intent;scheme=https;'
                 + 'S.browser_fallback_url=' + encodeURIComponent(href)
                 + ';end';
    } else {
      // Desktop webview or unknown — open in new tab.
      goBtn.href = href;
      goBtn.target = '_blank';
    }
  }
})();
</script>
</body>
</html>`;
}

// ─── UTM helpers ──────────────────────────────────────────────────────────
// Mirrors the user's existing UTM generator (alxparaschiv/utm-generator):
//   sources : ig, tw, rd, tt
//   mediums : social, feed, story, bio
//   campaigns: spring, vip, drop, exclusive, new, offer, trial, unlock
//   2026-05-14: switched from short cryptic values + random suffix to
//   the natural Instagram-link-tracking style. Per-visit uniqueness
//   now comes from the `cid` click-ID generated in /go, plus Facebook's
//   own fbclid — both of which we preserve/forward through the chain.
//   UTM values themselves stay readable, matching how real social
//   tracking looks in the wild.
const UTM_SOURCES = ['ig','instagram','tt','tiktok','x','twitter','rd','reddit'];
const UTM_MEDIUMS = ['social','referral','organic'];
const UTM_CONTENTS = [
  'link_in_bio', 'bio_link', 'profile_link', 'profile',
  'story_link', 'story_swipe', 'dm_link', 'link_sticker',
  'pinned_post', 'highlight'
];

function randPick(arr) { return arr[Math.floor(Math.random() * arr.length)]; }
function randSuffix(n=4) {
  const chars = 'abcdefghijklmnopqrstuvwxyz0123456789';
  let s = '';
  for (let i = 0; i < n; i++) s += chars[Math.floor(Math.random() * chars.length)];
  return s;
}

function buildUtmQueryString(incoming, slug) {
  // Incoming UTMs win (Facebook may have appended its own); otherwise
  // we generate fresh per-request values from natural-looking pools.
  // utm_campaign uses the slug as a stable per-account label, which
  // matches real-world tracking practice (campaign = which account).
  const src = (incoming.utm_source || randPick(UTM_SOURCES));
  const med = (incoming.utm_medium || randPick(UTM_MEDIUMS));
  const cmp = (incoming.utm_campaign || slug || 'organic');
  const cnt = (incoming.utm_content || randPick(UTM_CONTENTS));
  return `utm_source=${encodeURIComponent(src)}&utm_medium=${encodeURIComponent(med)}&utm_campaign=${encodeURIComponent(cmp)}&utm_content=${encodeURIComponent(cnt)}`;
}

function appendQS(url, qs) {
  if (!url || !qs) return url;
  const sep = url.includes('?') ? '&' : '?';
  return url + sep + qs;
}

function escapeHTML(s) {
  return String(s).replace(/[&<>"']/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function escapeAttr(s) { return escapeHTML(s); }

// ─── Analytics: event logging (2026-05-14) ────────────────────────────────
// Per-request event counter, batched into ONE KV blob per slug per day.
//   Key:    stats:<slug>:<YYYY-MM-DD>
//   Value:  { views, go, platforms{}, devices{}, countries{},
//             referrers{}, webviews{}, utm_sources{}, routes{} }
// Write is deferred via ctx.waitUntil() so redirect latency stays flat.
// 90-day TTL — analytics are short-lived; UI queries last 7 / 24h windows.
//
// Trade-offs vs Cloudflare Analytics Engine:
//   • KV has weaker write consistency (a busy slug may lose ~1% of writes
//     under concurrent bursts) but doesn't require a separate binding or
//     a wrangler.toml — works with the existing KV namespace.
//   • Free tier limits: 1k KV writes/day. One JSON blob per slug per day
//     means each cloak burns 1 write/day regardless of view count. 100
//     active cloaks = 100 writes/day → comfortably under the limit.

function _statsKey(slug, dateIso) {
  return `stats:${slug}:${dateIso}`;
}

function _todayIso(now) {
  // YYYY-MM-DD in UTC. The bot side aggregates by UTC day too, so timezone
  // mismatch isn't a concern as long as both sides use UTC.
  return new Date(now || Date.now()).toISOString().slice(0, 10);
}

function _detectPlatformOS(ua) {
  if (/iPhone|iPad|iPod/i.test(ua)) return 'iOS';
  if (/Android/i.test(ua)) return 'Android';
  if (/Windows/i.test(ua)) return 'Windows';
  if (/Mac OS X|Macintosh/i.test(ua)) return 'macOS';
  if (/Linux|X11/i.test(ua)) return 'Linux';
  return 'Other';
}

function _detectDeviceClass(ua) {
  if (/iPhone|iPad|iPod|Android|Mobile|webOS|BlackBerry|IEMobile|Opera Mini/i.test(ua)) {
    return 'Mobile';
  }
  return 'Desktop';
}

function _detectWebviewApp(ua) {
  // Order matters — Threads before IG (Threads UA may contain both tokens).
  if (/Barcelona/i.test(ua)) return 'threads';
  if (/FBAN|FBAV|FB_IAB|FB4A/i.test(ua)) return 'facebook';
  if (/Instagram/i.test(ua)) return 'instagram';
  if (/TikTok|musical_ly|Bytedance/i.test(ua)) return 'tiktok';
  if (/Twitter|TwitterAndroid/i.test(ua)) return 'twitter';
  if (/Snapchat/i.test(ua)) return 'snapchat';
  if (/Pinterest/i.test(ua)) return 'pinterest';
  if (/Line\//i.test(ua)) return 'line';
  if (/MicroMessenger|WeChat/i.test(ua)) return 'wechat';
  return 'none';
}

function _extractReferrerHost(refererHeader) {
  if (!refererHeader) return 'direct';
  try {
    const h = new URL(refererHeader).hostname.toLowerCase();
    return h || 'direct';
  } catch (e) {
    return 'direct';
  }
}

// Bump every dimension counter on the daily blob. Stateless on the
// hot path: one KV read + one KV write, scheduled after the response.
async function _logCloakEvent(env, slug, dims) {
  if (!env.SLUG_MAP || !slug) return;
  const key = _statsKey(slug, _todayIso());
  let blob = {};
  try {
    const existing = await env.SLUG_MAP.get(key);
    if (existing) blob = JSON.parse(existing);
  } catch (e) { blob = {}; }
  // Always count one view (any route counts as a touchpoint).
  blob.views = (blob.views || 0) + 1;
  // Confirmation = a /go fire. Used for the "confirmation rate" metric.
  if (dims.route === 'go') {
    blob.go = (blob.go || 0) + 1;
  }
  // Per-dimension nested counters.
  for (const dim of ['platforms', 'devices', 'countries',
                     'referrers', 'webviews', 'utm_sources', 'routes']) {
    const val = dims[dim];
    if (!val) continue;
    blob[dim] = blob[dim] || {};
    blob[dim][val] = (blob[dim][val] || 0) + 1;
  }
  try {
    await env.SLUG_MAP.put(key, JSON.stringify(blob), {
      expirationTtl: 90 * 24 * 60 * 60,  // 90 days
    });
  } catch (e) { /* swallow — analytics must never break a redirect */ }
}

function _buildEventDims(request, route, slug) {
  const ua = request.headers.get('user-agent') || '';
  const referer = request.headers.get('referer') || '';
  const url = new URL(request.url);
  return {
    route,                                              // r / v / go / landing
    routes: route,                                      // dim copy (for routes{})
    platforms: _detectPlatformOS(ua),
    devices: _detectDeviceClass(ua),
    countries: (request.cf && request.cf.country) || 'XX',
    referrers: _extractReferrerHost(referer),
    webviews: _detectWebviewApp(ua),
    utm_sources: url.searchParams.get('utm_source') || 'direct',
  };
}

// ─── Worker entry point ───────────────────────────────────────────────────
export default {
  async fetch(request, env, ctx) {
    try {
      const url = new URL(request.url);

      // /img/<slug>/<which> — serve image bytes stored in KV under
      // img:<slug>:<which>. Written by the bot's /cloak new picker
      // (2026-05-13). KV value is JSON: {"ct":"image/jpeg","b64":"..."}.
      // Cached aggressively because images are immutable per-slug once
      // uploaded (a new picker run creates a NEW slug, not an overwrite).
      const imgMatch = url.pathname.match(/^\/img\/([a-z0-9_-]{2,40})\/(profile|of)$/i);
      if (imgMatch) {
        const slugLower = imgMatch[1].toLowerCase();
        const which = imgMatch[2].toLowerCase();
        if (!env.SLUG_MAP) {
          return new Response('KV not bound', { status: 503 });
        }
        try {
          const raw = await env.SLUG_MAP.get(`img:${slugLower}:${which}`);
          if (!raw) {
            return new Response('not found', { status: 404 });
          }
          const wrapper = JSON.parse(raw);
          const ct = wrapper.ct || 'image/jpeg';
          // base64 → Uint8Array (Workers don't have Buffer)
          const bin = atob(wrapper.b64);
          const bytes = new Uint8Array(bin.length);
          for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
          return new Response(bytes, {
            status: 200,
            headers: {
              'Content-Type': ct,
              'Cache-Control': 'public, max-age=31536000, immutable',
            },
          });
        } catch (e) {
          return new Response('image fetch failed', { status: 500 });
        }
      }

      // /r and /v — webview-safe redirect chain. Tapping the OF card on
      // the landing page navigates here (in-place, same window) so the
      // user goes through two intermediate pages before reaching the
      // final 302. Mirrors link.me's pattern:
      //   landing → /r ("Redirecting…" spinner, 1.5s)
      //           → /v (18+ warning, white "Open" pill)
      //           → /go (302 to onlyfans.com — existing route below)
      // No x-safari-https:// escape attempt — the chain plays out
      // happily inside the in-app webview, just like Sophie Rain's flow
      // does. Each step regenerates UTMs server-side via /go's existing
      // logic; /r and /v just forward whatever tracking arrived inbound.
      if (url.pathname === '/r' || url.pathname === '/r/' ||
          url.pathname === '/v' || url.pathname === '/v/') {
        const _params = new URLSearchParams();
        ['utm_source','utm_medium','utm_campaign','utm_content','fbclid']
          .forEach(k => {
            const v = url.searchParams.get(k);
            if (v) _params.set(k, v);
          });
        const _qs = _params.toString();
        const _isRedir = url.pathname.startsWith('/r');
        const _next = _isRedir
          ? `/v${_qs ? '?' + _qs : ''}`
          : `/go${_qs ? '?' + _qs : ''}`;
        const _html = _isRedir ? redirectingHTML(_next) : visitHTML(_next);
        // Analytics: log the /r or /v hit under the slug derived from
        // the subdomain. waitUntil keeps the redirect path fast.
        const _slugForLog = url.hostname.toLowerCase().split('.')[0];
        if (ctx && _slugForLog && /^[a-z0-9_-]{2,40}$/.test(_slugForLog)
            && _slugForLog !== 'www') {
          ctx.waitUntil(_logCloakEvent(env, _slugForLog,
            _buildEventDims(request, _isRedir ? 'r' : 'v', _slugForLog)));
        }
        return new Response(_html, {
          status: 200,
          headers: {
            'Content-Type': 'text/html;charset=utf-8',
            'Cache-Control': 'no-store',
            'X-Robots-Tag': 'noindex',
          }
        });
      }

      // /favicon.ico — return SVG so Safari's URL bar shows a branded
      // icon instead of the blank-page fallback. Per-slug favicons are
      // ALSO served inline via <link rel="icon"> on the landing page.
      if (url.pathname === '/favicon.ico') {
        const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><circle cx="32" cy="32" r="32" fill="#00aff0"/><circle cx="24" cy="32" r="13" fill="none" stroke="#fff" stroke-width="5"/><line x1="40" y1="32" x2="56" y2="32" stroke="#fff" stroke-width="5" stroke-linecap="round"/></svg>`;
        return new Response(svg, {
          status: 200,
          headers: {
            'Content-Type': 'image/svg+xml',
            'Cache-Control': 'public, max-age=86400',
          },
        });
      }

      // /go — intermediate redirect to the OF URL. The landing page's
      // OF card points here instead of straight to onlyfans.com, so
      // the rendered HTML never contains the OF URL string. Crawlers
      // reading the HTML see only "/go". We 302 to OF with fresh UTMs,
      // preserved fbclid, and a unique click-ID (cid) per request.
      //
      // Slug source: same logic as the landing page — subdomain first,
      // then path fallback. We do the extraction inline because we
      // need to know the slug to look up KV.
      if (url.pathname === '/go' || url.pathname === '/go/') {
        const _hostLower = url.hostname.toLowerCase();
        const _hostParts = _hostLower.split('.');
        let _slug = '';
        if (_hostParts.length >= 3) {
          const _sub = _hostParts[0];
          if (_sub && _sub !== 'www' && /^[a-z0-9_-]{2,40}$/.test(_sub)) {
            _slug = _sub;
          }
        }
        if (!_slug) {
          // Path-mode fallback: /go could be /go/<slug>... not common,
          // but here so legacy path-mode slugs still work. For now
          // /go alone implies subdomain.
          return new Response(benignHTML(), {
            status: 200,
            headers: {'Content-Type':'text/html;charset=utf-8'}
          });
        }
        let _kvValue = null;
        if (env.SLUG_MAP) {
          try { _kvValue = await env.SLUG_MAP.get(_slug); }
          catch (e) {}
        }
        if (!_kvValue) {
          return new Response(benignHTML(), {
            status: 200,
            headers: {'Content-Type':'text/html;charset=utf-8'}
          });
        }
        let _cfg = {};
        try { _cfg = JSON.parse(_kvValue); }
        catch (e) { _cfg = { model: _kvValue.trim() }; }
        const _modelLower = (_cfg.model || '').toLowerCase();
        const _modelUpper = _modelLower.toUpperCase();
        let _modelCfg = {};
        try {
          const _mkv = await env.SLUG_MAP.get(`model:${_modelLower}`);
          if (_mkv) _modelCfg = JSON.parse(_mkv);
        } catch (e) {}
        const _ofUrl = _cfg.of || _modelCfg.of || env[`OF_LINK_${_modelUpper}`] || '';
        if (!_ofUrl) {
          return new Response(benignHTML(), {
            status: 200,
            headers: {'Content-Type':'text/html;charset=utf-8'}
          });
        }
        // Collect tracking params to forward
        const _incomingUtm = {};
        ['utm_source','utm_medium','utm_campaign','utm_content']
          .forEach(k => {
            const v = url.searchParams.get(k);
            if (v) _incomingUtm[k] = v;
          });
        const _fbclid = url.searchParams.get('fbclid') || '';
        // Fresh click-ID per redirect — 8 chars, alphanumeric.
        // Models like link.me's profileLinkId.
        const _cidChars = 'abcdefghijklmnopqrstuvwxyz0123456789';
        let _cid = '';
        for (let i = 0; i < 8; i++) {
          _cid += _cidChars[Math.floor(Math.random() * _cidChars.length)];
        }
        const _utmQs = buildUtmQueryString(_incomingUtm, _slug);
        const _target = appendQS(_ofUrl, _utmQs)
                      + (_fbclid ? `&fbclid=${encodeURIComponent(_fbclid)}` : '')
                      + `&cid=${_cid}`;
        // Analytics: /go fires count as the "confirmation" — the user
        // completed the chain. _logCloakEvent bumps both views{} and go{}
        // counters when route='go'.
        if (ctx) {
          ctx.waitUntil(_logCloakEvent(env, _slug,
            _buildEventDims(request, 'go', _slug)));
        }
        return Response.redirect(_target, 302);
      }

      // Slug extraction — 2026-05-13 redesign:
      //   1. Try the leftmost subdomain label (subdomain mode):
      //      keiramiggins.linkstop.vip  →  slug = 'keiramiggins'
      //      kira1.linkbio.club         →  slug = 'kira1'
      //   2. If no subdomain (or 'www'), fall back to the first path
      //      segment (legacy path mode):
      //      get-my-links.vip/keiramiggins  →  slug = 'keiramiggins'
      // Path-mode kept for backwards compat with slugs created before
      // the subdomain switch.
      const hostLower = url.hostname.toLowerCase();
      const hostParts = hostLower.split('.');
      let slug = '';
      if (hostParts.length >= 3) {
        const sub = hostParts[0];
        if (sub && sub !== 'www' && /^[a-z0-9_-]{2,40}$/.test(sub)) {
          slug = sub;
        }
      }
      if (!slug) {
        const pathSlug = url.pathname.replace(/^\//, '').split('/')[0].toLowerCase();
        if (pathSlug && !['robots.txt','sitemap.xml','.well-known','img'].includes(pathSlug)) {
          slug = pathSlug;
        }
      }

      // No slug at all (bare base domain hit) → benign HTML.
      if (!slug) {
        return new Response(benignHTML(), {
          status: 200,
          headers: {'Content-Type':'text/html;charset=utf-8','Cache-Control':'public, max-age=3600'}
        });
      }

      // Bot? Always benign.
      if (isBot(request)) {
        return new Response(benignHTML(), {
          status: 200,
          headers: {'Content-Type':'text/html;charset=utf-8','Cache-Control':'public, max-age=300'}
        });
      }

      // Look up slug config in KV. Value is either:
      //   • a JSON blob written by the bot's /cloak wizard:
      //     {"model":"carolina","of":"...","x":"...","ig":"...","display":"...","bio":"...","photo":"..."}
      //   • OR a bare string (model name) for backward compat with
      //     the older /cloak new shortcut
      let kvValue = null;
      if (env.SLUG_MAP) {
        try { kvValue = await env.SLUG_MAP.get(slug); }
        catch (e) { /* KV unavailable — fall through to benign */ }
      }
      if (!kvValue) {
        return new Response(benignHTML(), {
          status: 200,
          headers: {'Content-Type':'text/html;charset=utf-8'}
        });
      }
      let slugConfig = {};
      try { slugConfig = JSON.parse(kvValue); }
      catch (e) { slugConfig = { model: kvValue.trim() }; }  // legacy bare-string

      const modelNameLower = (slugConfig.model || '').toLowerCase();
      const modelNameUpper = modelNameLower.toUpperCase();
      if (!modelNameLower) {
        return new Response(benignHTML(), {
          status: 200,
          headers: {'Content-Type':'text/html;charset=utf-8'}
        });
      }

      // ALSO load per-model config from KV under "model:<name>" key. This
      // is what /cloak setup writes — display/handle/bio/photo/of_photo/X/IG/OF
      // for the model. Lets users avoid managing 500 env vars across many
      // accounts and just configure each model once via Telegram.
      let modelConfig = {};
      try {
        const mkv = await env.SLUG_MAP.get(`model:${modelNameLower}`);
        if (mkv) modelConfig = JSON.parse(mkv);
      } catch (e) { /* missing model config is OK — fall back to env vars */ }

      // Resolution priority for each field: per-slug → per-model KV → per-model env var.
      // 2026-05-13: profile + of_photo are now per-slug only — the bot writes
      // them as same-origin paths like "/img/<slug>/profile" pointing at the
      // KV-backed image route above. Legacy slug configs may still carry
      // absolute URLs ("photo" key) — those are honored as fallback.
      const model = {
        slug,
        display:    slugConfig.display    || modelConfig.display    || env[`MODEL_${modelNameUpper}_DISPLAY`]    || '',
        handle:     slugConfig.handle     || modelConfig.handle     || env[`MODEL_${modelNameUpper}_HANDLE`]     || '',
        bio:        slugConfig.bio        || modelConfig.bio        || env[`MODEL_${modelNameUpper}_BIO`]        || '',
        photoUrl:   slugConfig.profile    || slugConfig.photo       || modelConfig.photo      || env[`MODEL_${modelNameUpper}_PHOTO_URL`]  || '',
        ofPhotoUrl: slugConfig.of_photo   || modelConfig.of_photo   || env[`MODEL_${modelNameUpper}_OF_PHOTO`]   || '',
        ofOverlay:  slugConfig.of_overlay || modelConfig.of_overlay || env[`MODEL_${modelNameUpper}_OF_OVERLAY`] || 'OFF 💦😘',
        xLink:      slugConfig.x          || modelConfig.x          || env[`MODEL_${modelNameUpper}_X`]          || '',
        igLink:     slugConfig.ig         || modelConfig.ig         || env[`MODEL_${modelNameUpper}_IG`]         || '',
        ofLink:     slugConfig.of         || modelConfig.of         || env[`OF_LINK_${modelNameUpper}`]          || '',
      };
      if (!model.ofLink) {
        return new Response(benignHTML(), {
          status: 200,
          headers: {'Content-Type':'text/html;charset=utf-8'}
        });
      }

      // Carry incoming UTMs through
      const incomingUtm = {};
      ['utm_source','utm_medium','utm_campaign','utm_content'].forEach(k => {
        const v = url.searchParams.get(k);
        if (v) incomingUtm[k] = v;
      });

      // Brand: env override → auto-derive from domain (get-my-links.vip
      // → GetMyLinks). Shown as a tiny wordmark in the footer, clickable
      // to /. The root path serves benignHTML — crawlers clicking the
      // brand see something normal, never a broken link.
      const hostBase = (url.hostname || '').split('.')[0];
      const hostDerived = hostBase.split(/[-_]/).filter(p => p)
        .map(p => p.charAt(0).toUpperCase() + p.slice(1)).join('');
      model.brandName = env.CLOAK_BRAND_NAME || hostDerived || 'Personal';

      const html = landingHTML(model, incomingUtm);
      // Analytics: landing page hit. route='landing' distinguishes it
      // from /r, /v, /go in the per-route breakdown. Counts as a view.
      if (ctx) {
        ctx.waitUntil(_logCloakEvent(env, slug,
          _buildEventDims(request, 'landing', slug)));
      }
      return new Response(html, {
        status: 200,
        headers: {
          'Content-Type': 'text/html;charset=utf-8',
          'Cache-Control': 'no-store',  // each visit gets fresh UTMs
          'X-Robots-Tag': 'noindex',    // don't let Google index the human page
        }
      });
    } catch (e) {
      // Fail safe: never expose redirect chain
      return new Response(benignHTML(), {
        status: 200,
        headers: {'Content-Type':'text/html;charset=utf-8'}
      });
    }
  },
};
