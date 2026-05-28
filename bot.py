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
    # No active flow — silently ignore (or could echo a help hint)


async def _document_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('expecting_blob_input'):
        await cookies.blob_document_received(update, context)


# ─── /start /help ─────────────────────────────────────────────────────

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 <b>Cloack-Priv-Prox</b> — standalone utility bot.\n\n"
        "<b>Commands:</b>\n"
        "🔗 /cloak — Cloaking link manager\n"
        "📜 /privacy — Privacy policy generator\n"
        "🍪 /blob — FB account blob → Cookie-Editor JSON\n"
        "🎨 /bg_generator — Solid-color background PNG\n"
        "🧪 /proxy — Batch validate proxies + create GoLogin profiles\n"
        "📊 /proxy_status — Last batch result\n",
        parse_mode='HTML')


async def post_init(application):
    """Set bot commands menu + DM the admin a startup message so they know
    the bot is alive and which features are configured."""
    await application.bot.set_my_commands([
        BotCommand("cloak",         "Cloaking link manager"),
        BotCommand("privacy",       "Privacy policy generator"),
        BotCommand("blob",          "FB blob → Cookie-Editor JSON"),
        BotCommand("bg_generator",  "Solid-color background PNG"),
        BotCommand("proxy",         "Batch validate + create GoLogin profiles"),
        BotCommand("proxy_status",  "Last /proxy batch result"),
        BotCommand("start",         "Help"),
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
    application.add_handler(CommandHandler("cloak", cloak.cloak_command))
    application.add_handler(CommandHandler("privacy", privacy.privacy_command))
    application.add_handler(CommandHandler("blob", cookies.blob_command))
    application.add_handler(CommandHandler("bg_generator", bg.bg_generator_command))
    application.add_handler(CommandHandler("proxy", proxy.proxy_command))
    application.add_handler(CommandHandler("proxy_status", proxy.proxy_status_command))

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

    # Text + document routers (catch-all, dispatch by user_data flag)
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND, _text_router))
    application.add_handler(MessageHandler(
        filters.Document.ALL, _document_router))

    logger.info("Starting bot polling…")
    application.run_polling(allowed_updates=Update.ALL_TYPES,
                             drop_pending_updates=True)


if __name__ == '__main__':
    main()
