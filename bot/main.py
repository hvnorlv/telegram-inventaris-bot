"""
Entrypoint for the Inventory Telegram Bot.

Notes:
- The script intentionally keeps startup synchronous-friendly by using a small
  helper (_init_event_loop_and_run) to run required async setup steps during
  initialization. The actual bot polling uses application.run_polling() (blocking).
- GoogleSheetsManager instance (if available) is stored in application.bot_data["sheets_manager"].
- PermissionManager (optional) is stored in application.bot_data["permission_manager"].
"""
from __future__ import annotations
import asyncio
import importlib
import logging
import os
import sys
import traceback
import time
from typing import Optional, Any

from telegram import __version__ as ptb_version, Update
from telegram.error import NetworkError
from telegram.ext import (
    Application,
    ContextTypes,
    MessageHandler,
    CommandHandler,
    filters,
)

from config import BOT_TOKEN, SPREADSHEET_ID, GOOGLE_CREDENTIALS_PATH, LOG_LEVEL
from google_sheets import GoogleSheetsManager

# Optional permission manager
try:
    from permissions import PermissionManager  # type: ignore
except Exception:
    PermissionManager = None  # type: ignore

# Handlers package (expects register_all + optional setup_services)
import handlers  # type: ignore

# ---------- logging ----------
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
logger = logging.getLogger("inventory_bot")

# Globals used during init
_gsm: Optional[GoogleSheetsManager] = None
_pm: Optional[Any] = None


async def on_error(update: Update | None, context: ContextTypes.DEFAULT_TYPE):
    """
    Central error handler for application.add_error_handler.
    Logs full exception stacktrace and tries to serialize offending Update.
    """
    try:
        logger.error("Unhandled exception in handler", exc_info=context.error)

        if update is not None:
            try:
                ud = update.to_dict()
                logger.error("Offending Update (to_dict): %s", ud)
            except Exception:
                # fallback minimal info
                try:
                    logger.error(
                        "Offending Update: chat=%s user=%s message=%s",
                        getattr(update.effective_chat, "id", None),
                        getattr(update.effective_user, "id", None),
                        getattr(update.message, "text", None) if getattr(update, "message", None) else None,
                    )
                except Exception:
                    logger.exception("Failed to serialize Update object for diagnostics")
        else:
            logger.error("No Update provided (likely polling/transport error). Context.error: %s", repr(context.error))

        # if httpx present, detect httpx errors
        try:
            import httpx  # type: ignore
            import httpcore  # type: ignore
            import anyio  # type: ignore

            if isinstance(context.error, Exception) and isinstance(context.error, httpx.HTTPError):
                logger.error("Detected httpx HTTPError: %s", context.error)
            # detect httpcore/httpx/anyio related exceptions more broadly
            if isinstance(context.error, Exception) and (
                isinstance(context.error, getattr(httpcore, "_exceptions", httpcore).__class__)
            ):
                logger.error("Detected httpcore-related exception: %s", context.error)
        except Exception:
            # libraries not present or introspection failed; ignore
            pass

    except Exception:
        logger.critical("Exception in on_error handler: %s", traceback.format_exc())


def _init_event_loop_and_run(coro):
    """
    Ensure event loop present and run the coroutine to completion synchronously.
    This is used during startup to call async connect/load functions before run_polling.
    """
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # rare: fallback to asyncio.run
            return asyncio.run(coro)
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


def init_sheets_manager() -> Optional[GoogleSheetsManager]:
    """
    Instantiate GoogleSheetsManager and attempt connect (sync wrapper).
    Returns the manager if connected or None if unavailable.
    """
    global _gsm
    try:
        # prefer explicit constructor names (GoogleSheetsManager supports several aliases)
        try:
            _gsm = GoogleSheetsManager(spreadsheet_id=SPREADSHEET_ID, credentials_path=GOOGLE_CREDENTIALS_PATH)
        except TypeError:
            # older kwargs fallback
            _gsm = GoogleSheetsManager(spreadsheet_id=SPREADSHEET_ID, SERVICE_ACCOUNT_FILE=GOOGLE_CREDENTIALS_PATH)
    except Exception as e:
        logger.exception("Failed to instantiate GoogleSheetsManager: %s", e)
        _gsm = None
        return None

    # Warn early if credentials file path likely wrong
    try:
        if GOOGLE_CREDENTIALS_PATH and not os.path.exists(GOOGLE_CREDENTIALS_PATH):
            logger.warning("Google credentials file not found at configured path: %s. This may cause connect failures.", GOOGLE_CREDENTIALS_PATH)
    except Exception:
        pass

    try:
        ok = _init_event_loop_and_run(_gsm.async_connect())
        if ok:
            logger.info("GoogleSheetsManager connected to spreadsheet: %s", SPREADSHEET_ID)
        else:
            logger.warning("GoogleSheetsManager.connect() returned False — Sheets may be unavailable (check credentials / gspread).")
    except Exception:
        logger.exception("Exception while connecting GoogleSheetsManager:")
    return _gsm


def init_permission_manager(sheets_manager: Optional[GoogleSheetsManager] = None):
    """
    Instantiate and (optionally) load PermissionManager data from sheet.
    Supports both sync and async loading methods defined by PermissionManager.
    """
    global _pm
    if PermissionManager is None:
        logger.info("PermissionManager not present — skipping permission setup.")
        return None

    try:
        _pm = PermissionManager()
        # prefer async loader if present
        if sheets_manager is not None and hasattr(_pm, "async_load_from_sheet"):
            try:
                logger.info("PermissionManager: loading roles from sheet (async).")
                _init_event_loop_and_run(_pm.async_load_from_sheet(sheets_manager))
            except Exception:
                logger.exception("PermissionManager.async_load_from_sheet failed at startup")
        else:
            # try legacy sync loaders
            if hasattr(_pm, "load_from_sheet"):
                try:
                    _pm.load_from_sheet(sheets_manager)
                except Exception:
                    logger.exception("PermissionManager.load_from_sheet failed at startup")
            elif hasattr(_pm, "load"):
                try:
                    _pm.load()
                except Exception:
                    logger.exception("PermissionManager.load failed at startup")
        logger.info("PermissionManager initialized.")
    except Exception:
        logger.exception("Failed to initialize PermissionManager.")
        _pm = None
    return _pm


async def global_message_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Route plain text messages to handler-specific text processors for interactive flows.
    Checks user_data flags saved by handlers to decide who should receive the message.
    """
    text = update.message.text if update.message else ""
    logger.debug("Router: incoming text (first 200 chars): %s", (text[:200] if text else "<no-text>"))

    # Borrow flow
    try:
        if context.user_data.get("borrow_step") or context.user_data.get("borrow_flow") or context.user_data.get("borrow_choice_name"):
            mod = importlib.import_module("handlers.borrow")
            fn = getattr(mod, "handle_borrow_message", None)
            if fn:
                handled = await fn(update, context)
                if handled:
                    logger.debug("Router: borrow handler consumed message.")
                    return
    except Exception:
        logger.exception("Router: error invoking borrow handler")

    # Add flow
    try:
        if context.user_data.get("add_flow"):
            mod = importlib.import_module("handlers.add")
            fn = getattr(mod, "handle_add_message", None)
            if fn:
                handled = await fn(update, context)
                if handled:
                    logger.debug("Router: add handler consumed message.")
                    return
    except Exception:
        logger.exception("Router: error invoking add handler")

    # Inventory related flows
    try:
        if context.user_data.get("awaiting_reduce") or context.user_data.get("awaiting_inv_custom_search") or context.user_data.get("inv_last_mode"):
            mod = importlib.import_module("handlers.inventory")
            fn = getattr(mod, "handle_inventory_message", None)
            if fn:
                handled = await fn(update, context)
                if handled:
                    logger.debug("Router: inventory handler consumed message.")
                    return
    except Exception:
        logger.exception("Router: error invoking inventory handler")

    # misc
    try:
        mod = importlib.import_module("handlers.misc")
        fn_misc = getattr(mod, "handle_misc_message", None)
        if fn_misc:
            handled = await fn_misc(update, context)
            if handled:
                logger.debug("Router: misc handler consumed message.")
                return
        fn_cancel = getattr(mod, "handle_cancel_flow", None)
        if fn_cancel:
            handled = await fn_cancel(update, context)
            if handled:
                logger.debug("Router: misc cancel consumed message.")
                return
    except Exception:
        logger.debug("Router: handlers.misc not available or failed.")

    # admin
    try:
        mod = importlib.import_module("handlers.admin")
        fn_admin = getattr(mod, "handle_admin_message", None)
        if fn_admin:
            handled = await fn_admin(update, context)
            if handled:
                logger.debug("Router: admin handler consumed message.")
                return
    except Exception:
        logger.debug("Router: handlers.admin not available or failed.")

    logger.debug("Router: message not consumed by any handler.")


def _log_registered_handlers(application: Application):
    try:
        for grp, handlers_list in application.handlers.items():
            for h in handlers_list:
                logger.info("Registered handler group=%s handler=%s", grp, getattr(h, "__class__", type(h)).__name__)
    except Exception:
        logger.exception("Failed to enumerate registered handlers.")


# Admin command /reload_users — uses permission manager if present
async def _reload_users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    app = context.application
    pm_instance = app.bot_data.get("permission_manager")
    sheets = app.bot_data.get("sheets_manager")

    if pm_instance is None:
        await app.bot.send_message(chat_id=update.effective_chat.id, text="❌ PermissionManager tidak tersedia.")
        return

    # permission check (support async & sync API)
    try:
        allowed = False
        if hasattr(pm_instance, "async_has_permission"):
            try:
                allowed = await pm_instance.async_has_permission(user.id, "can_manage_users") or await pm_instance.async_has_permission(user.id, "admin")
            except Exception:
                logger.debug("async_has_permission check failed during /reload_users", exc_info=True)
        elif hasattr(pm_instance, "has_permission"):
            try:
                allowed = pm_instance.has_permission(user.id, "can_manage_users") or getattr(pm_instance, "is_admin", lambda *_: False)(user.id)
            except Exception:
                logger.debug("has_permission check failed during /reload_users", exc_info=True)

        if not allowed:
            await app.bot.send_message(chat_id=update.effective_chat.id, text="🚫 Hanya admin yang dapat memuat ulang users.")
            return
    except Exception:
        await app.bot.send_message(chat_id=update.effective_chat.id, text="🚫 Gagal memverifikasi permission — dibatalkan.")
        return

    if not sheets:
        await app.bot.send_message(chat_id=update.effective_chat.id, text="❌ Google Sheets tidak tersedia. Tidak dapat memuat users.")
        return

    await app.bot.send_message(chat_id=update.effective_chat.id, text="🔄 Memuat ulang users/roles dari Google Sheets...")
    try:
        if hasattr(pm_instance, "async_load_from_sheet"):
            await pm_instance.async_load_from_sheet(sheets)
        elif hasattr(pm_instance, "load_from_sheet"):
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, lambda: pm_instance.load_from_sheet(sheets))
        elif hasattr(pm_instance, "load"):
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, pm_instance.load)

        try:
            state = pm_instance.dump_state()
        except Exception:
            state = "(dump_state unavailable)"

        await app.bot.send_message(chat_id=update.effective_chat.id, text="✅ Selesai. Current state:\n" + str(state))
        logger.info("PermissionManager reloaded via /reload_users; state=%s", state)
    except Exception:
        logger.exception("Failed to reload users via /reload_users")
        await app.bot.send_message(chat_id=update.effective_chat.id, text="❌ Gagal memuat users. Periksa log.")


def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN not configured. Set environment variable or config.py.")
        sys.exit(1)

    logger.info("python-telegram-bot version: %s", ptb_version)
    logger.info("Starting Inventory Bot...")

    # Build application
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_error_handler(on_error)

    # enable httpx/httpcore/anyio debug logging if LOG_LEVEL is DEBUG
    try:
        if LOG_LEVEL and LOG_LEVEL.strip().upper() == "DEBUG":
            try:
                import httpx  # type: ignore
                import httpcore  # type: ignore
                import anyio  # type: ignore

                logging.getLogger("httpx").setLevel(logging.DEBUG)
                logging.getLogger("httpcore").setLevel(logging.DEBUG)
                logging.getLogger("anyio").setLevel(logging.DEBUG)
                logger.debug("Enabled debug logging for httpx/httpcore/anyio")
            except Exception:
                logger.debug("httpx/httpcore/anyio not present; skipping http debug logging")
    except Exception:
        logger.debug("Failed to configure http debug logging")

    # lightweight debug text logger (group 0)
    async def _dbg_text_logger(update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            txt = update.message.text if update.message else "<no-text>"
            logger.info("DBG_TEXT_RECEIVED chat=%s user=%s text=%s",
                        getattr(update.effective_chat, "id", None),
                        getattr(update.effective_user, "id", None),
                        (txt[:200] if isinstance(txt, str) else str(txt)))
        except Exception:
            logger.exception("Debug text logger failed")

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _dbg_text_logger), group=0)

    # Initialize services before registering handlers
    gsm = init_sheets_manager()
    pm = init_permission_manager(gsm)

    if gsm:
        application.bot_data["sheets_manager"] = gsm
    if pm:
        application.bot_data["permission_manager"] = pm
    application.bot_data["logger"] = logger

    # -------------------------------
    # Register handlers (explicit)
    # -------------------------------
    # Prefer explicit registration so patterns and order are clear for debugging.
    try:
        # If handlers.register_all exists, prefer it (keeps backward compat)
        if hasattr(handlers, "register_all"):
            logger.info("Using handlers.register_all to register modules.")
            try:
                handlers.register_all(application)
            except Exception:
                logger.exception("handlers.register_all failed; falling back to explicit registration.")
                raise
        else:
            # explicit registrations for modules we expect — adjust if you have more modules
            mods_expected = [
                ("handlers.add", "register_add_handlers"),
                ("handlers.borrow", "register_borrow_handlers"),
                ("handlers.inventory", "register_inventory_handlers"),
                ("handlers.misc", "register_handlers"),
                ("handlers.admin", "register_handlers"),
            ]
            for modpath, regname in mods_expected:
                try:
                    mod = importlib.import_module(modpath)
                    reg = getattr(mod, regname, None)
                    if callable(reg):
                        reg(application)
                        logger.info("Registered handlers from %s via %s", modpath, regname)
                    else:
                        # try alternate generic name
                        alt = getattr(mod, "register_handlers", None)
                        if callable(alt):
                            alt(application)
                            logger.info("Registered handlers from %s via register_handlers", modpath)
                        else:
                            logger.debug("Module %s has no %s or register_handlers; skipping.", modpath, regname)
                except ModuleNotFoundError:
                    logger.debug("Module %s not present; skipping.", modpath)
                except Exception:
                    logger.exception("Failed registering handlers from module %s", modpath)
    except Exception:
        logger.exception("Failed to register handlers via handlers.register_all fallback logic")

    # Admin reload command
    application.add_handler(CommandHandler("reload_users", _reload_users_cmd))

    # Global router as fallback (run after group 0)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, global_message_router), group=1)

    _log_registered_handlers(application)

    # call handlers.setup_services if present (sync or async)
    setup = getattr(handlers, "setup_services", None)
    if setup:
        try:
            if asyncio.iscoroutinefunction(setup):
                _init_event_loop_and_run(setup(application, sheets_manager=gsm, permission_manager=pm))
            else:
                setup(application, sheets_manager=gsm, permission_manager=pm)
        except Exception:
            logger.exception("handlers.setup_services failed")

    # Run polling with resilience to NetworkError / httpx / anyio connection issues
    try:
        # ensure event loop exists (some environments may not have one)
        try:
            _ = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        logger.info("Starting polling (blocking)...")

        backoff = 1.0
        max_backoff = 120.0
        while True:
            try:
                application.run_polling()
                logger.info("Application.run_polling() exited normally.")
                break
            except KeyboardInterrupt:
                logger.info("KeyboardInterrupt received; shutting down.")
                break
            except NetworkError as net_err:
                logger.exception("NetworkError during polling: %s", net_err)
                try:
                    import httpx  # type: ignore
                    logger.debug("httpx version: %s", getattr(httpx, "__version__", "<unknown>"))
                except Exception:
                    pass
                logger.info("Sleeping %.1fs before retrying polling...", backoff)
                time.sleep(backoff)
                backoff = min(max_backoff, backoff * 2)
                continue
            except Exception as e:
                # handle common httpx/httpcore/anyio socket-related exceptions explicitly
                handled = False
                try:
                    import httpx  # type: ignore
                    import httpcore  # type: ignore
                    import anyio  # type: ignore

                    if isinstance(e, httpx.HTTPError):
                        logger.exception("httpx.HTTPError during polling: %s", e)
                        handled = True
                    # httpcore low-level exceptions (map_exceptions wrappers often re-raise as httpcore._exceptions)
                    # We attempt to catch common socket/read errors and anyio BrokenResourceError.
                    try:
                        from httpcore._exceptions import ReadError, ConnectError  # type: ignore
                        if isinstance(e, (ReadError, ConnectError)):
                            logger.exception("httpcore read/connect error: %s", e)
                            handled = True
                    except Exception:
                        # not available or import failed — skip
                        pass

                    try:
                        from anyio import exceptions as anyio_exceptions  # type: ignore
                        if isinstance(e, getattr(anyio_exceptions, "BrokenResourceError", Exception)):
                            logger.exception("anyio BrokenResourceError: %s", e)
                            handled = True
                    except Exception:
                        pass
                except Exception:
                    # httpx/httpcore/anyio not installed or introspection failed; fall back to generic handling
                    pass

                # If exception looks like a network/connection break, retry with backoff.
                if handled or isinstance(e, (OSError, ConnectionError)):
                    logger.exception("Network/transport-related exception caught; will retry after backoff: %s", e)
                    wait = min(max_backoff, backoff)
                    logger.info("Sleeping %.1fs before retrying polling...", wait)
                    time.sleep(wait)
                    backoff = min(max_backoff, backoff * 2)
                    continue

                # otherwise log and short-sleep then retry to avoid tight crash loop
                logger.exception("Unexpected exception in polling loop; sleeping 5s and retrying")
                time.sleep(5)
                continue

    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received; exiting.")
    except Exception:
        logger.exception("Exception while running polling (outer): %s", traceback.format_exc())
    finally:
        logger.info("Bot stopped.")


if __name__ == "__main__":
    main()
