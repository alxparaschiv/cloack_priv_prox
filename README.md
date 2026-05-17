# Cloack-Priv-Prox

Standalone Telegram utility bot with 4 commands:

- **🔗 `/cloak`** — Cloaking link manager (Cloudflare KV)
- **📜 `/privacy`** — Privacy policy generator (telegra.ph + rentry.co)
- **🍪 `/blob`** — FB account blob → Cookie-Editor JSON decoder
- **🎨 `/bg_generator`** — Solid-color profile/background PNG generator

Extracted from a larger reel-bot stack for use as a standalone utility (no Drive, no FB API, no Instagram, no scheduling — just the 4 utilities).

## Quick start (Railway)

1. Fork/clone this repo into your GitHub
2. Create a new Railway service → "Deploy from GitHub repo" → pick this repo
3. Set the env vars below (start with REQUIRED, add optionals as needed)
4. `/start` the bot from your Telegram main account once so it knows your chat ID

## Environment variables

### REQUIRED (always)

| Variable | What |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | Your Telegram user ID (use @userinfobot to find it). Becomes default admin and notification target. |

### OPTIONAL — admin whitelist

| Variable | What |
|---|---|
| `TELEGRAM_ADMIN_USER_IDS` | Comma-separated extra admin user IDs. If both this and `TELEGRAM_CHAT_ID` are unset, bot runs in OPEN mode (anyone can use it). |

### For `/cloak` (cloaking link manager)

| Variable | What |
|---|---|
| `CLOAK_CF_ACCOUNT_ID` | Cloudflare account ID (Workers → right sidebar) |
| `CLOAK_CF_API_TOKEN` | Token with both `Workers KV:Edit` AND `Workers Scripts:Edit` permissions |
| `CLOAK_CF_KV_NAMESPACE_ID` | KV namespace ID (create one in CF dashboard, copy the ID) |
| `CLOAK_BASE_DOMAINS` | Comma-separated base domains, e.g. `mydomain1.link,mydomain2.link`. Each new slug is round-robin-assigned to one of these. |
| `MODELS` | Comma-separated model names, e.g. `Caro,Kira,Lina` |
| `NICHES` | Comma-separated niche slugs, e.g. `goth,police,gamer,nun,cyberpunk` |

You also need to deploy the included `cloudflare_worker.js` to a Cloudflare Worker bound to your base domains (one-time setup, not done by the bot — see "Cloudflare Worker setup" below).

### For AI suggestions (in `/cloak` AND `/privacy`)

| Variable | What |
|---|---|
| `OPENAI_API_KEY` | OpenAI key (gpt-4o-mini, ~$0.001 per batch of 8 suggestions). If unset, falls back to local pools. |

### For `/blob` and `/bg_generator`

No env vars needed.

## Cloudflare Worker setup (one-time, for `/cloak`)

1. Create a Cloudflare Worker named anything (e.g. `cloak`)
2. Paste the entire contents of `cloudflare_worker.js` from this repo
3. Bind the KV namespace as `SLUG_MAP` in the Worker settings (using the same namespace ID you put in `CLOAK_CF_KV_NAMESPACE_ID`)
4. Add Worker routes for each base domain: `<yourdomain>/*` → this Worker
5. Set per-model env vars on the Worker itself:
   - `MODEL_CAROLINA_DISPLAY = "Caro"`
   - `MODEL_CAROLINA_HANDLE = "@vampychyuwu"`
   - `MODEL_CAROLINA_BIO = "My links and more"`
   - `MODEL_CAROLINA_PHOTO_URL = "https://drive.google.com/uc?id=..."`
   - `MODEL_CAROLINA_X = "https://x.com/handle"`
   - `MODEL_CAROLINA_IG = "https://instagram.com/handle"`
   - `OF_LINK_CAROLINA = "https://onlyfans.com/handle"`
   - (repeat for each model — uppercase the model name)

The Worker reads slug → model from KV, then resolves the model's display/photo/bio/links from its env vars. Per-slug overrides (overlay, display, bio, of) come from the KV value the bot writes.

## Local development

```bash
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN=...
export TELEGRAM_CHAT_ID=...
python bot.py
```

## Command reference

### `/cloak`

Shows menu: New / List / Delete.

**New** — 8-step wizard:
1. Pick model (from `MODELS` env var)
2. Pick niche (from `NICHES` env var)
3. Number (auto-suggested next free, or custom)
4. Slug (auto-suggested from model+niche+number, or AI-suggested batch via OpenAI, or custom)
5. OF URL (paste full URL)
6. Overlay text (1-4 words, AI-suggested, or `default` for `OFF 💦😘`, or `skip`)
7. Display name (1-3 words, AI-suggested, or `skip`)
8. Bio (5-12 words, AI-suggested, or `skip`)

Writes to Cloudflare KV with key=slug, value=JSON config. Bot replies with the live URL.

**List** — queries Cloudflare KV, shows all slugs (first 50).
**Delete** — pick a slug, removes it from KV.

### `/privacy`

Pick provider (Telegra.ph or Rentry.co) → type app name → bot creates the randomized privacy policy page on the chosen host, returns the public URL.

Content is randomized at 3 levels:
1. Structure jitter — 14 optional sections, ~30% drop each, shuffled order
2. Phrasing jitter — every section header + paragraph picked from 3-5 variants
3. Anti-fingerprint pass — 28 synonym substitutions (collect→gather/obtain/etc) + ~15% sentence dropout

Across 50 generations for the same app name → 50 distinct outputs.

### `/blob`

Paste a FB account blob (or attach as `.txt` document) → bot parses it (extracts cookies, email, profile_id, user-agent), sanitizes cookies for Chrome's Cookie-Editor extension, returns a `.json` file you can import directly.

Smart parser handles multiple seller-formats: rigid `email:pass:email:emailpass:url:dob:ua:cookies_b64` or modern variants. Only requirement: a valid base64 cookie blob somewhere in the input.

### `/bg_generator`

Generates a 1080×1080 solid-color PNG from a 10-color palette. Each generation is jittered ±12 RGB per channel so two pulls of the same palette pick are never pixel-identical (anti-image-hash clustering — defeats Meta's ability to correlate accounts by profile-picture binary).

Buttons: 🎲 Generate another (random) / 🎨 Pick specific color.

## Cost notes

- `/cloak` AI suggestions: ~$0.0002 per wizard run (4 suggestion batches × $0.00005)
- `/privacy` AI suggestions: same model, similar cost
- All other commands: $0 (no API calls)

Even at 1000 cloak creations/month → ~$0.20/month OpenAI.

## License

Private — not for public distribution.
