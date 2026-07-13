"""
Cloack-Priv-Prox — standalone utility bot.

Four commands:
  /cloak          — cloaking link manager (Cloudflare KV)
  /privacy        — privacy policy generator (telegra.ph + rentry.co)
  /blob           — FB account blob → Cookie-Editor JSON
  /bg_generator   — solid-color profile/background PNG (jittered)

Required env vars (Railway):
  TELEGRAM_BOT_TOKEN          — bot token from BotFather
  TELEGRAM_CHAT_ID            — your user ID (default admin)
  (optional) TELEGRAM_ADMIN_USER_IDS — extra admin user IDs (comma-sep)

For /cloak:
  CLOAK_CF_ACCOUNT_ID         — Cloudflare account ID
  CLOAK_CF_API_TOKEN          — token with Workers KV + Workers Scripts edit
  CLOAK_CF_KV_NAMESPACE_ID    — KV namespace ID
  CLOAK_DOMAIN_1/2/3          — one base domain per env var (legacy), OR
  CLOAK_BASE_DOMAINS          — comma-sep base domains in one var
  OF_LINK_<NAME>              — per-model OF URL (one env var per model)
  (niches are hardcoded in cloak.py — see NICHES list)

For /privacy AI suggestions + /cloak AI suggestions:
  OPENAI_API_KEY              — OpenAI key (gpt-4o-mini, ~$0.001 per batch)

For /blob (FB cookie blob decoder): no env vars needed.
For /bg_generator: no env vars needed.
"""

import os
import logging
import asyncio

from telegram import Update, BotCommand
from telegram.ext import (
    Application, CommandHandler, ContextTypes, MessageHandler,
    CallbackQueryHandler, filters, ApplicationHandlerStop,
)

import cloak
import privacy
import cookies
import bg
import proxy
import rambler
import sms_verified
import setup_pipeline
import geelark_open
import ig_setup
import drive_image_picker
import rental
import account_pack
import batch_verify
import geelark_image_wizard
import artistic_bg_gen
import banner_gen
import portrait_gen
import nsfw_banner
import bikini_gen
import bio_gen
import bio_gen_v2
import looksmax
import blocked_words
import password_gen


logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ─── Admin gate ───────────────────────────────────────────────────────

def _is_admin(user_id):
    if user_id is None:
        return True
    admin_ids = set()
    extra = (os.getenv('TELEGRAM_ADMIN_USER_IDS') or '').strip()
    if extra:
        for x in extra.split(','):
            try:
                admin_ids.add(int(x.strip()))
            except (ValueError, AttributeError):
                pass
    chat_id = (os.getenv('TELEGRAM_CHAT_ID') or '').strip()
    if chat_id:
        try:
            admin_ids.add(int(chat_id))
        except (ValueError, AttributeError):
            pass
    if not admin_ids:
        return True  # open mode (no whitelist set)
    try:
        return int(user_id) in admin_ids
    except (ValueError, TypeError):
        return False


async def _admin_gate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return
    if _is_admin(user.id):
        return
    try:
        if update.message:
            await update.message.reply_text("⛔ Unauthorized — this bot is private.")
        elif update.callback_query:
            await update.callback_query.answer("⛔ Unauthorized", show_alert=True)
    except Exception:
        pass
    logger.warning(f"[admin gate] blocked user_id={user.id} username={user.username}")
    raise ApplicationHandlerStop


# ─── Central text router (dispatches to whichever module is awaiting input) ──

async def _text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Routes text messages to the right module based on which flag is set."""
    text = (update.message.text or '')
    if context.user_data.get('expecting_blob_input'):
        await cookies.blob_text_received(update, context)
        return
    if context.user_data.get('expecting_cloak_input'):
        await cloak.cloak_text_received(update, context, text)
        return
    if context.user_data.get('expecting_privacy_app_name'):
        await privacy.privacy_text_received(update, context)
        return
    if context.user_data.get('expecting_proxy_count'):
        await proxy.proxy_text_received(update, context)
        return
    if context.user_data.get("expecting_rambler_creds"):
        await rambler.rambler_text_received(update, context)
        return
    if context.user_data.get("expecting_rambler_microsoft_creds"):
        await rambler.rambler_microsoft_text_received(update, context)
        return
    if context.user_data.get("expecting_sms_phone"):
        await sms_verified.sms_text_received(update, context)
        return
    if context.user_data.get('expecting_bg_batch_count'):
        await bg.bg_batch_count_text_received(update, context)
        return
    if context.user_data.get('batch_verify'):
        await batch_verify.batch_verify_text_received(update, context)
        return
    if context.user_data.get('expecting_acctpack_count'):
        await account_pack.account_pack_count_text_received(update, context)
        return
    if context.user_data.get('expecting_geelark_name'):
        await geelark_open.geelark_text_received(update, context)
        return
    if context.user_data.get('expecting_geelark_fb_name'):
        await geelark_open.geelark_fb_text_received(update, context)
        return
    if context.user_data.get('expecting_geelark_stop_name'):
        await geelark_open.geelark_stop_text_received(update, context)
        return
    if context.user_data.get('expecting_imgwiz_phone_name'):
        if await geelark_image_wizard.imgwiz_text_received(update, context):
            return
    if context.user_data.get('ig_setup_state'):
        await ig_setup.ig_setup_text_received(update, context)
        return

    # /meta_dev_setup wizard: chat-scoped state (not user_data — see setup_pipeline._state)
    if await setup_pipeline.setup_full_text_received(update, context):
        return
    # No active flow — silently ignore (or could echo a help hint)


async def _document_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('expecting_blob_input'):
        await cookies.blob_document_received(update, context)
        return
    # /meta_dev_setup step 1 also accepts a .txt document upload
    if await setup_pipeline.setup_full_document_received(update, context):
        return
    # /portrait_gen accepts an image sent as a file (uncompressed)
    if await portrait_gen.portrait_photo_received(update, context):
        return


async def _photo_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Photo dispatcher — routes to whichever wizard is awaiting one."""
    if await ig_setup.ig_setup_photo_received(update, context):
        return
    if await banner_gen.banner_photo_received(update, context):
        return
    if await portrait_gen.portrait_photo_received(update, context):
        return
    if await nsfw_banner.nsfw_photo_received(update, context):
        return
    if await looksmax.looksmax_photo_received(update, context):
        return


# ─── /start /info — grouped command guide ─────────────────────────────

COMMANDS_TEXT = (
    "⚡ <b>Commands</b>\n\n"

    "👤 <b>Account creation</b>\n"
    "/meta_dev_setup — Full Meta-Dev account setup (stages 0-12)\n"
    "/ig_setup_private — IG wizard: login + bio + link + pic + Private toggle\n\n"

    "📱 <b>GeeLark phones</b>\n"
    "/geelark_profile_ig_open — Mirror GoLogin → GeeLark + install IG (+ MANUAL image selection)\n"
    "/geelark_profile_ig_auto — Same as above but AUTO 1 normal + 1 artistic bg per profile\n"
    "/geelark_profile_fb_open — Mirror GoLogin → GeeLark + install Facebook\n"
    "/geelark_stop_phone — Batch-stop GeeLark phones once setup is done\n\n"

    "🔐 <b>Verification &amp; SMS</b>\n"
    "/account_pack — Full account package (name+gender+dob+password+rambler+FB number) → Sheet + .zip\n"
    "/batch_sms — Paste many phone numbers, then get all their SMS codes at once\n"
    "/batch_rambler — Paste many email:password lines, get all their Rambler codes at once\n"
    "/password — Strong AI passwords for new accounts ([count], e.g. /password 10)\n"
    "/rambler — Latest FB/IG code from a Rambler inbox\n"
    "/rambler_microsoft — Latest Microsoft code from a Rambler inbox\n"
    "/sms — Latest SMS code from a TextVerified rental\n"
    "/rental_instagram — Rent a fresh 7-day Instagram SMS number\n"
    "/rental_facebook — Rent a fresh 7-day Facebook SMS number\n\n"

    "🎨 <b>Content tools</b>\n"
    "/bg_generator — Solid / gradient / artistic abstract PNG (single OR batch → Drive)\n"
    "/artistic_bg — AI artistic backgrounds from Drive refs — batch → Drive folder link\n"
    "/banner_gen — Upload a model photo → 21:9 cinematic banner (nano-banana-pro)\n"
    "/portrait_gen — Send an image → crop to 3:4 + AI upscale to 2K\n"
    "/nsfw_banner — Model photo → OF-style lingerie banner, soft expression (nano-banana-pro)\n"
    "/bikini_gen — Pick a model → batch of bikini IG-story images → Drive link (no upload)\n"
    "/bio_gen — AI bios: niche → model → 8 with refresh (gpt-4o-mini)\n"
    "/bio_gen_v2 — Girlfriend-brand bios: warmer, flirty, bonding tone\n"
    "/looksmax [model] [hair=blonde] — Send a model photo → 3 glow-up styles (natural/goth/max): paler porcelain skin, bigger rounder eyes, fuller lips, glam → Drive folder link\n"
    "/blocklist — IG blocked-words list with anti-cluster guard (anchors ai/slop/fake/fakeprofile)\n\n"

    "🔗 <b>Cloak &amp; privacy</b>\n"
    "/cloak — Cloaking link manager (Cloudflare)\n"
    "/privacy — Privacy policy generator (Telegra.ph)\n\n"

    "🧪 <b>Proxies &amp; account data</b>\n"
    "/blob — FB account blob → Cookie-Editor JSON\n"
    "/proxy — Batch validate proxies + create GoLogin profiles\n"
    "/proxy_status — Last /proxy batch result\n\n"

    "ℹ️ <b>Help</b>\n"
    "/start — Show this menu\n"
    "/info — Same as /start (full guide)\n"
)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 <b>acc-setup-bot</b> — account-farming control plane.\n\n"
        + COMMANDS_TEXT,
        parse_mode='HTML')


async def info_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Alias for /start so the user can pull up the full guide any time."""
    await update.message.reply_text(COMMANDS_TEXT, parse_mode='HTML')


async def post_init(application):
    """Set bot commands menu + DM the admin a startup message so they know
    the bot is alive and which features are configured."""
    # Telegram renders this in the order we set it; mirror /start's groups so
    # the typeahead picker reads top-to-bottom like the docs.
    await application.bot.set_my_commands([
        # 👤 Account creation
        BotCommand("meta_dev_setup",       "👤 Full Meta-Dev account setup (stages 0-12)"),
        BotCommand("ig_setup_private",     "👤 IG wizard — login + bio + link + pic + Private"),

        # 📱 GeeLark phones
        BotCommand("geelark_profile_ig_open", "📱 Mirror GoLogin → GeeLark + IG (manual image picking)"),
        BotCommand("geelark_profile_ig_auto", "📱 Same — but auto 1 normal + 1 artistic bg per profile"),
        BotCommand("geelark_profile_fb_open", "📱 Mirror GoLogin → GeeLark + install Facebook"),
        BotCommand("geelark_stop_phone",      "📱 Batch-stop GeeLark phones"),

        # 🔐 Verification & SMS
        BotCommand("account_pack",       "🧩 Full account package → Sheet + .zip (single/batch)"),
        BotCommand("batch_sms",          "🔑 Paste phone numbers → all SMS codes at once"),
        BotCommand("batch_rambler",      "📧 Paste email:password → all Rambler codes at once"),
        BotCommand("password",           "🔑 Strong AI passwords for new accounts (batch)"),
        BotCommand("rambler",            "🔐 Latest FB/IG code from a Rambler inbox"),
        BotCommand("rambler_microsoft",  "🔐 Latest Microsoft code from a Rambler inbox"),
        BotCommand("sms",                "🔐 Latest SMS code from a TextVerified rental"),
        BotCommand("rental_instagram",   "🔐 Rent a fresh 7-day Instagram SMS number"),
        BotCommand("rental_facebook",    "🔐 Rent a fresh 7-day Facebook SMS number"),

        # 🎨 Content tools
        BotCommand("bg_generator",  "🎨 Background PNGs — single OR batch → Drive"),
        BotCommand("artistic_bg",   "🎨 AI artistic backgrounds — batch → Drive folder link"),
        BotCommand("banner_gen",    "🎨 Model photo → 21:9 cinematic banner (nano-banana-pro)"),
        BotCommand("portrait_gen",  "🖼 Resize image to 3:4 @ 2K (AI upscale)"),
        BotCommand("nsfw_banner",   "🔥 Model photo → OF-style lingerie banner (nano-banana-pro)"),
        BotCommand("bikini_gen",    "👙 Pick a model → batch bikini IG-story images → Drive"),
        BotCommand("looksmax",      "🧬 Model photo → glow-up styles (paler/eyes/lips/glam) → Drive link"),
        BotCommand("bio_gen",       "🎨 AI bios — niche → model → 8 w/ refresh"),
        BotCommand("bio_gen_v2",    "💞 Girlfriend-brand bios — warmer/flirty/bonding"),
        BotCommand("blocklist",     "🎨 IG blocked-words — anchored + anti-cluster guard"),

        # 🔗 Cloak & privacy
        BotCommand("cloak",    "🔗 Cloaking link manager (Cloudflare)"),
        BotCommand("privacy",  "🔗 Privacy policy generator"),

        # 🧪 Proxies & account data
        BotCommand("blob",          "🧪 FB blob → Cookie-Editor JSON"),
        BotCommand("proxy",         "🧪 Batch validate + create GoLogin profiles"),
        BotCommand("proxy_status",  "🧪 Last /proxy batch result"),

        # ℹ️ Help
        BotCommand("start", "ℹ️ Show grouped command guide"),
        BotCommand("info",  "ℹ️ Same as /start (full guide)"),
    ])
    logger.info("Bot commands menu set")

    chat_id = os.getenv('TELEGRAM_CHAT_ID', '').strip()
    if not chat_id:
        logger.warning("TELEGRAM_CHAT_ID not set — skipping startup DM")
        return
    # Check which feature areas are fully configured. Each tuple = (label,
    # required env vars). Lets the user see at a glance what's ready.
    feature_checks = [
        ("🔗 /cloak",  ["CLOAK_CF_API_TOKEN", "CLOAK_CF_ACCOUNT_ID",
                       "CLOAK_CF_KV_NAMESPACE_ID"]),
        ("📜 /privacy",     ["OPENAI_API_KEY"]),  # AI suggestions; basic mode works without
        ("🍪 /blob",        []),                  # zero env deps
        ("🎨 /bg_generator",[]),                  # zero env deps
        ("🧪 /proxy",       ["GOLOGIN_API_KEY", "IPROYAL_USERNAME", "IPROYAL_PASSWORD",
                            "IPQS_API_KEY", "ABUSEIPDB_API_KEY",
                            "FB_PROXY_TEST_PHONE", "FB_PROXY_TEST_PASSWORD"]),
        ("📬 /rambler",     []),  # zero env deps — user provides creds per-call
        ("🪟 /rambler_microsoft", []),  # zero env deps — user provides creds per-call
        ("🔑 /password",    []),  # zero env deps — local CSPRNG generator
        ("📱 /sms",         ["TEXTVERIFIED_API_KEY"]),
        ("🧩 /account_pack", ["TEXTVERIFIED_API_KEY", "OPENAI_API_KEY"]),
        ("🛠 /meta_dev_setup", ["GOLOGIN_API_KEY", "TEXTVERIFIED_API_KEY",
                                "GOOGLE_TOKEN_PICKLE"]),
        ("📷 /geelark_profile_ig_open", ["GOLOGIN_API_KEY", "GEELARK_API_KEY", "GEELARK_APP_ID"]),
        ("📘 /geelark_profile_fb_open", ["GOLOGIN_API_KEY", "GEELARK_API_KEY", "GEELARK_APP_ID"]),
        ("🎨 /artistic_bg", ["WAVESPEED_API_KEY", "REEL_GOOGLE_TOKEN_PICKLE"]),
        ("🖼 /banner_gen",  ["WAVESPEED_API_KEY"]),
        ("🖼 /portrait_gen", ["WAVESPEED_API_KEY"]),  # crop + AI 2K upscale
        ("🔥 /nsfw_banner", ["WAVESPEED_API_KEY"]),
        ("👙 /bikini_gen", ["WAVESPEED_API_KEY", "REEL_GOOGLE_TOKEN_PICKLE"]),
        ("🤖 /bio_gen",     ["OPENAI_API_KEY"]),
        ("💞 /bio_gen_v2",  ["OPENAI_API_KEY"]),
    ]
    feature_lines = []
    for label, needed in feature_checks:
        missing = [k for k in needed if not os.getenv(k)]
        if needed and missing:
            feature_lines.append(f"  ⚠️ {label} — missing: <code>"
                                 f"{', '.join(missing)}</code>")
        elif not needed:
            feature_lines.append(f"  ✅ {label}")
        else:
            feature_lines.append(f"  ✅ {label}")

    # /proxy-specific flags worth surfacing on boot
    proxy_flags = []
    if os.getenv('FB_PROXY_SKIP_GOOGLE_GATE', '') == '1':
        proxy_flags.append("  • Google gate: <b>skipped</b> "
                           "(<code>FB_PROXY_SKIP_GOOGLE_GATE=1</code>)")
    else:
        proxy_flags.append("  • Google gate: enabled "
                           "(set <code>FB_PROXY_SKIP_GOOGLE_GATE=1</code> to skip)")

    msg = ("🟢 <b>acc-setup-bot is online!</b>\n\n"
           "<b>Features:</b>\n"
           + "\n".join(feature_lines)
           + "\n\n<b>Proxy pipeline flags:</b>\n"
           + "\n".join(proxy_flags)
           + "\n\nSend /start for the full command list.")
    try:
        await application.bot.send_message(chat_id=chat_id, text=msg,
                                           parse_mode='HTML')
        logger.info(f"startup DM sent to {chat_id}")
    except Exception as e:
        logger.error(f"startup DM failed: {e}")
        # Fallback: shorter message in case any of the HTML formatting broke
        try:
            await application.bot.send_message(
                chat_id=chat_id,
                text="🟢 acc-setup-bot is online! Send /start for commands.")
        except Exception as e2:
            logger.error(f"startup DM fallback also failed: {e2}")


def main():
    token = os.getenv('TELEGRAM_BOT_TOKEN')
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN env var not set")

    application = Application.builder().token(token).post_init(post_init).build()

    # Admin gate (runs FIRST, in group -1)
    admin_extra = (os.getenv('TELEGRAM_ADMIN_USER_IDS') or '').strip()
    admin_chat = (os.getenv('TELEGRAM_CHAT_ID') or '').strip()
    if not admin_extra and not admin_chat:
        logger.warning("⚠️ TELEGRAM_ADMIN_USER_IDS + TELEGRAM_CHAT_ID both unset "
                       "— bot runs in OPEN mode (anyone can use it).")
    else:
        logger.info(f"[admin gate] enabled (admin_user_ids={admin_extra or '-'}, "
                    f"chat_id={admin_chat or '-'})")
    application.add_handler(MessageHandler(filters.ALL, _admin_gate), group=-1)
    application.add_handler(CallbackQueryHandler(_admin_gate), group=-1)

    # Commands
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", start_command))
    application.add_handler(CommandHandler("info", info_command))
    application.add_handler(CommandHandler("cloak", cloak.cloak_command))
    application.add_handler(CommandHandler("privacy", privacy.privacy_command))
    application.add_handler(CommandHandler("blob", cookies.blob_command))
    application.add_handler(CommandHandler("bg_generator", bg.bg_generator_command))
    application.add_handler(CommandHandler("proxy", proxy.proxy_command))
    application.add_handler(CommandHandler("proxy_status", proxy.proxy_status_command))
    # /meta_dev_setup is the SINGLE entry point — 3-step wizard then end-to-end pipeline.
    # (The legacy meta_dev.meta_dev_command is no longer registered.)
    application.add_handler(CommandHandler("meta_dev_setup", setup_pipeline.setup_full_command))
    application.add_handler(CommandHandler("rambler", rambler.rambler_command))
    application.add_handler(CommandHandler("rambler_microsoft", rambler.rambler_microsoft_command))
    application.add_handler(CommandHandler("sms", sms_verified.sms_command))
    application.add_handler(CommandHandler("password", password_gen.password_command))
    # IG flow — keep both old + new names registered for in-flight runs.
    # geelark_profile_open is the legacy alias; geelark_profile_ig_open is the
    # user-facing canonical name as of 2026-06-08.
    application.add_handler(CommandHandler("geelark_profile_open", geelark_open.geelark_profile_open_command))
    application.add_handler(CommandHandler("geelark_profile_ig_open", geelark_open.geelark_profile_open_command))
    application.add_handler(CommandHandler("geelark_profile_ig_auto",
                                            geelark_image_wizard.automated_create_command))
    # FB flow — create phone + install Facebook, no images, no mode select
    application.add_handler(CommandHandler("geelark_profile_fb_open", geelark_open.geelark_profile_fb_open_command))
    application.add_handler(CommandHandler("geelark_stop_phone", geelark_open.geelark_stop_phone_command))
    application.add_handler(CommandHandler("ig_setup_private", ig_setup.ig_setup_command))
    application.add_handler(CommandHandler("rental_instagram", rental.rental_instagram_command))
    application.add_handler(CommandHandler("rental_facebook", rental.rental_facebook_command))
    application.add_handler(CommandHandler("account_pack", account_pack.account_pack_command))
    application.add_handler(CommandHandler("batch_sms", batch_verify.batch_sms_command))
    application.add_handler(CommandHandler("batch_rambler", batch_verify.batch_rambler_command))
    application.add_handler(CommandHandler("artistic_bg", artistic_bg_gen.artistic_bg_command))
    application.add_handler(CommandHandler("banner_gen", banner_gen.banner_gen_command))
    application.add_handler(CommandHandler("portrait_gen", portrait_gen.portrait_gen_command))
    application.add_handler(CommandHandler("nsfw_banner", nsfw_banner.nsfw_banner_command))
    application.add_handler(CommandHandler("bikini_gen", bikini_gen.bikini_gen_command))
    application.add_handler(CommandHandler("bio_gen", bio_gen.bio_gen_command))
    application.add_handler(CommandHandler("bio_gen_v2", bio_gen_v2.bio_gen_v2_command))
    application.add_handler(CommandHandler("looksmax", looksmax.looksmax_command))
    application.add_handler(CommandHandler("blocklist", blocked_words.blocklist_command))
    application.add_handler(CommandHandler("cancel", setup_pipeline.cancel_command))

    # Callback handlers — pattern-based
    application.add_handler(CallbackQueryHandler(
        cloak.cloak_callback, pattern=r'^cloak:'))
    application.add_handler(CallbackQueryHandler(
        privacy.privacy_provider_callback, pattern=r'^privacy_provider_'))
    application.add_handler(CallbackQueryHandler(
        cookies.blob_callback, pattern=r'^blob:'))
    application.add_handler(CallbackQueryHandler(
        bg.bg_callback, pattern=r'^bg_gen:'))
    application.add_handler(CallbackQueryHandler(
        proxy.proxy_callback, pattern=r'^proxy:'))
    application.add_handler(CallbackQueryHandler(
        drive_image_picker.drive_pick_callback, pattern=r'^drive_pick:'))
    application.add_handler(CallbackQueryHandler(
        geelark_image_wizard.imgwiz_callback, pattern=r'^imgwiz:'))
    application.add_handler(CallbackQueryHandler(
        artistic_bg_gen.artbg_callback, pattern=r'^artbg:'))
    application.add_handler(CallbackQueryHandler(
        geelark_open.geelark_done_callback, pattern=r'^geelark_done:'))
    application.add_handler(CallbackQueryHandler(
        geelark_open.geelark_pre_fb_callback, pattern=r'^gp:fb:'))
    application.add_handler(CallbackQueryHandler(
        bio_gen.bio_gen_callback, pattern=r'^biogen:'))
    application.add_handler(CallbackQueryHandler(
        bio_gen_v2.bio_gen_v2_callback, pattern=r'^biogen2:'))
    application.add_handler(CallbackQueryHandler(
        bikini_gen.bikini_callback, pattern=r'^bikini:'))
    application.add_handler(CallbackQueryHandler(
        account_pack.account_pack_callback, pattern=r'^acctpack:'))
    application.add_handler(CallbackQueryHandler(
        batch_verify.batch_verify_callback, pattern=r'^bverify:'))

    # Text + document routers (catch-all, dispatch by user_data flag)
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND, _text_router))
    application.add_handler(MessageHandler(
        filters.Document.ALL, _document_router))
    application.add_handler(MessageHandler(
        filters.PHOTO, _photo_router))

    logger.info("Starting bot polling…")
    application.run_polling(allowed_updates=Update.ALL_TYPES,
                             drop_pending_updates=True)


if __name__ == '__main__':
    main()
