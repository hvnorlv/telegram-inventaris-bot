# handlers/common.py
"""
Common utilities & helpers used across handlers.

Rewritten to be robust against:
 - missing bot instance (fallback to BOT_TOKEN)
 - malformed Markdown entities (BadRequest: Can't parse entities)
 - edit_message_text failures (fallback to send_message)
Provides:
 - send_md: safe send/edit message with fallbacks
 - escape_md: minimal Markdown escaping
 - clear_steps: clear interactive flow flags from user_data
 - build_witel_kb & resolve_witel_token: helper for Witel selection
 - require_registration: ensure user registered (Witel + Divisi) or instruct to /start
"""
from __future__ import annotations
import logging
import time
from typing import Any, Dict, List, Optional, Tuple, Union
from urllib.parse import quote_plus, unquote_plus

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    CallbackQuery,
    Message,
    Bot as TgBot,
)
from telegram.ext import ContextTypes

# telegram-specific exceptions
try:
    from telegram.error import BadRequest
except Exception:
    BadRequest = Exception  # fallback, still will be caught

# Try import config for BOT_TOKEN fallback
try:
    from config import BOT_TOKEN
except Exception:
    BOT_TOKEN = None

logger = logging.getLogger("handlers.common")
logger.addHandler(logging.NullHandler())


# =====================================================================
# send_md — universal safe message sender / editor with BadRequest fallback
# =====================================================================
async def send_md(
    target: Union[Update, CallbackQuery, Message, ContextTypes.DEFAULT_TYPE, Any],
    text: str,
    *,

    reply_markup: Optional[InlineKeyboardMarkup] = None,
    parse_mode: Optional[str] = "Markdown",
    disable_web_page_preview: bool = True,
) -> None:
    """
    Safely send or edit a Markdown message.
    - Tries to edit message for CallbackQuery targets first.
    - If Telegram complains about entities (BadRequest: Can't parse entities),
      retries sending as plain text (no parse_mode).
    - Attempts multiple strategies to resolve bot and chat_id; falls back to constructing
      a temporary Bot using BOT_TOKEN if available.
    """
    try:
        cq = target if isinstance(target, CallbackQuery) else None
        upd = target if isinstance(target, Update) else None
        msg = target if isinstance(target, Message) else None
        ctx_like = None

        # detect context-like objects (Context or Application)
        try:
            if hasattr(target, "application") or hasattr(target, "bot") or hasattr(target, "effective_chat"):
                ctx_like = target
        except Exception:
            ctx_like = None

        # Try to edit message (CallbackQuery only)
        if cq and getattr(cq, "message", None):
            try:
                await cq.edit_message_text(text, reply_markup=reply_markup, parse_mode=parse_mode, disable_web_page_preview=disable_web_page_preview)
                return
            except BadRequest as bre:
                # If bad request due to entities, fallback to sending plain message
                msg_str = str(bre)
                if any(tok in msg_str for tok in ("Can't parse entities", "can't find end of the entity", "Unescaped")):
                    logger.debug("send_md: edit_message_text entity error (%s) — will fallback to send_message plain text", msg_str)
                    # fallthrough to send_message logic (with parse_mode=None on retry)
                else:
                    logger.debug("send_md: edit_message_text BadRequest (%s) — will fallback to send_message", msg_str)
            except Exception as e:
                logger.debug("send_md: edit_message_text failed (%s) — will fallback to send_message", e)

        # Determine chat_id
        chat_id = None
        try:
            if upd and getattr(upd, "effective_chat", None):
                chat_id = upd.effective_chat.id
            elif cq and getattr(cq, "message", None):
                chat_id = cq.message.chat.id
            elif msg and getattr(msg, "chat", None):
                chat_id = msg.chat.id
            elif ctx_like and getattr(ctx_like, "effective_chat", None):
                chat_id = ctx_like.effective_chat.id
        except Exception:
            chat_id = None

        # Determine bot instance
        bot = None
        try:
            if cq and getattr(cq, "bot", None):
                bot = cq.bot
            elif upd:
                bot = getattr(upd, "bot", None) or (getattr(upd, "application", None) and getattr(upd.application, "bot", None))
            elif msg and getattr(msg, "bot", None):
                bot = msg.bot
            elif ctx_like:
                bot = getattr(ctx_like, "bot", None) or (getattr(ctx_like, "application", None) and getattr(ctx_like.application, "bot", None))
            if bot is None and hasattr(target, "application") and getattr(target.application, "bot", None):
                bot = target.application.bot
        except Exception:
            bot = None

        # Ensure not using Bot class type (some code path passes class rather than instance)
        try:
            import telegram

            if bot is not None and isinstance(bot, type) and bot is telegram.Bot:
                logger.debug("send_md: resolved bot is Bot class (not instance) — ignoring.")
                bot = None
        except Exception:
            pass

        # Fallback: create temporary bot instance if possible
        if bot is None and BOT_TOKEN and chat_id is not None:
            try:
                bot = TgBot(BOT_TOKEN)
                logger.debug("send_md: created fallback Bot instance for chat_id=%s", chat_id)
            except Exception as e:
                logger.warning("send_md: failed to create fallback Bot: %s", e)
                bot = None

        # Abort if still missing
        if bot is None or chat_id is None:
            if bot is None and chat_id is None:
                logger.warning("send_md: bot instance not found and chat_id not determined; target=%s", type(target))
            elif bot is None:
                logger.warning("send_md: bot instance not found; chat_id=%s target=%s", chat_id, type(target))
            else:
                logger.warning("send_md: chat_id not determined; bot=%s target=%s", type(bot), type(target))
            return

        # Try to send with requested parse_mode, but handle entity parse errors
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
                disable_web_page_preview=disable_web_page_preview,
            )
            return
        except BadRequest as bre:
            msg_str = str(bre)
            # If entity parse error, retry without parse_mode (plain text)
            if any(tok in msg_str for tok in ("Can't parse entities", "can't find end of the entity", "Unescaped")):
                logger.warning("send_md: BadRequest from send_message (entities). Retrying as plain text — error: %s", msg_str)
                try:
                    await bot.send_message(
                        chat_id=chat_id,
                        text=text,
                        reply_markup=reply_markup,
                        parse_mode=None,
                        disable_web_page_preview=disable_web_page_preview,
                    )
                    return
                except Exception as e2:
                    logger.exception("send_md: retry plain-text send_message also failed: %s", e2)
                    return
            else:
                # other BadRequest -> log and return
                logger.exception("send_md: BadRequest on send_message: %s", msg_str)
                return
        except Exception as e:
            # other exceptions (network, auth, etc.)
            logger.exception("send_md: unexpected error on send_message: %s", e)
            return

    except Exception as e:
        logger.exception("send_md: unexpected error while sending message: %s", e)


# =====================================================================
# escape_md — basic Markdown escaping
# =====================================================================
_MD_CHARS = ["*", "_", "`", "[", "]", "(" , ")"]


def escape_md(text: Optional[str]) -> str:
    """Escape special Markdown characters for Markdown v1."""
    if text is None:
        return ""
    s = str(text)
    for ch in _MD_CHARS:
        s = s.replace(ch, "\\" + ch)
    return s


# =====================================================================
# clear_steps — reset user_data flags
# =====================================================================
def clear_steps(context: ContextTypes.DEFAULT_TYPE, user_id: Optional[int] = None) -> None:
    """
    Clear common user_data keys for any ongoing interactive flow.
    Used when /cancel or flow reset occurs.
    """
    try:
        ud = context.user_data
    except Exception:
        return

    if not ud:
        return

    static_keys = [
        "add_flow",
        "borrow_step",
        "borrow_flow",
        "borrow_choice_name",
        "awaiting_reduce",
        "awaiting_inv_custom_search",
        "inv_last_mode",
        "inv_last_pairs",
        "inv_last_filters",
        "myinv_pairs",
        "mytxn_pairs",
        "awaiting_field",
        "awaiting_reduce_confirm",
    ]

    for k in static_keys:
        ud.pop(k, None)

    prefixes = ("add_", "borrow_", "inv_", "awaiting_", "myinv_", "mytxn_")
    for k in list(ud.keys()):
        if any(k.startswith(p) for p in prefixes):
            ud.pop(k, None)


# =====================================================================
# Witel helpers — keyboard builder and token resolver
# =====================================================================
async def _get_witel_values_from_sheets(sheets, sheet_name: str = "Witel", column_header: str = "Witel") -> List[str]:
    """Fetch unique Witel values from sheets (multiple fallback strategies)."""
    out: List[str] = []
    if sheets is None:
        return out

    # 1) Try async_get_unique_column_values (preferred)
    if hasattr(sheets, "async_get_unique_column_values"):
        try:
            vals = await sheets.async_get_unique_column_values(sheet_name, column_header)
            seen = set()
            for v in vals or []:
                if v:
                    s = str(v).strip()
                    if s and s not in seen:
                        seen.add(s)
                        out.append(s)
            if out:
                return out
        except Exception:
            logger.debug("_get_witel_values_from_sheets: unique column read failed", exc_info=True)

    # 2) Try dedicated sheet (records)
    try:
        if hasattr(sheets, "async_get_all_records"):
            recs = await sheets.async_get_all_records(sheet_name)
            if isinstance(recs, list) and recs:
                seen = set()
                for r in recs:
                    if isinstance(r, dict):
                        val = r.get(column_header) or next(iter(r.values()), None)
                        if val:
                            s = str(val).strip()
                            if s and s not in seen:
                                seen.add(s)
                                out.append(s)
                if out:
                    return out
    except Exception:
        logger.debug("_get_witel_values_from_sheets: reading dedicated sheet failed", exc_info=True)

    # 3) Fallback: read INVENTARIS sheet (default sheet in manager)
    try:
        if hasattr(sheets, "async_get_all_records"):
            inv = await sheets.async_get_all_records()
            seen = set()
            for r in inv:
                val = r.get("Witel") or r.get("witel") or r.get("WITEL")
                if val:
                    s = str(val).strip()
                    if s and s not in seen:
                        seen.add(s)
                        out.append(s)
    except Exception:
        logger.debug("_get_witel_values_from_sheets: fallback INVENTARIS read failed", exc_info=True)

    return out


async def build_witel_kb(
    app_or_context: Any,
    sheets: Optional[Any] = None,
    *,
    sheet_name: str = "Witel",
    column_header: str = "Witel",
    inv_prefix: str = "inv_witel:",
    include_manual: bool = True,
    include_cancel: bool = True,
    cancel_callback_data: str = "inv_main",
    max_buttons: int = 100,
) -> Tuple[InlineKeyboardMarkup, List[str]]:
    """
    Build InlineKeyboardMarkup listing Witel names.

    Returns:
      (markup, values)

    - `app_or_context` can be Application, Context, or similar object with `.bot_data`.
    - `sheets` optional override of sheets manager.
    - `inv_prefix` is prepended to callback_data for each button (e.g. "inv_witel:WitelA").
    - `include_manual` if True adds a "Ketik Witel manual" button (handlers must support it).
       Callers that want to forbid manual entry should pass include_manual=False (e.g. borrow flow).
    - `include_cancel` adds a cancel/back button (callback_data customizable).
    """
    # Get app & bot_data
    app = getattr(app_or_context, "application", app_or_context)
    bot_data = getattr(app, "bot_data", {}) if app else {}

    if sheets is None:
        sheets = bot_data.get("sheets_manager") if bot_data else None

    vals: List[str] = []
    try:
        vals = await _get_witel_values_from_sheets(sheets, sheet_name, column_header)
    except Exception:
        logger.exception("build_witel_kb: failed fetching witel list")

    # normalize and unique
    uniq: List[str] = []
    seen = set()
    for v in vals:
        if v:
            s = str(v).strip()
            if s and s not in seen:
                seen.add(s)
                uniq.append(s)

    vals = uniq[:max_buttons]

    # Build keyboard layout (one button per row), callback_data uses urlencoded token
    kb: List[List[InlineKeyboardButton]] = []
    for v in vals:
        token = quote_plus(v)
        cb = f"{inv_prefix}{token}"
        kb.append([InlineKeyboardButton(v, callback_data=cb)])

    if include_manual:
        # Some flows rely on this; caller may disable it. Text and callback_data conservative.
        kb.append([InlineKeyboardButton("Ketik Witel manual", callback_data="inv_witel_manual")])

    if include_cancel:
        kb.append([InlineKeyboardButton("Kembali", callback_data=cancel_callback_data)])

    markup = InlineKeyboardMarkup(kb)

    # Store token map for resolution (map encoded_token -> value)
    try:
        wmap: Dict[str, Dict[str, Any]] = app.bot_data.get("witel_map", {}) if hasattr(app, "bot_data") else {}
        ts = int(time.time())
        for v in vals:
            token = quote_plus(v)
            wmap[token] = {"value": v, "ts": ts}
        if hasattr(app, "bot_data"):
            app.bot_data["witel_map"] = wmap
    except Exception:
        logger.debug("build_witel_kb: failed caching witel_map", exc_info=True)

    return markup, vals


async def resolve_witel_token(app_or_context: Any, token: str, max_age_seconds: Optional[int] = 3600) -> Optional[str]:
    """Resolve a token (typically the encoded name) to a stored Witel value using cached mapping.

    - If token not found in cache, returns token.strip() (best-effort decoded).
    - If mapping exists but is older than max_age_seconds, still returns the value but logs expiry.
    """
    if not token:
        return None
    try:
        # token may include prefix like "inv_witel:..." — strip prefix if present
        if ":" in token:
            try:
                token = token.split(":", 1)[1]
            except Exception:
                pass

        decoded = unquote_plus(token)

        app = getattr(app_or_context, "application", app_or_context)
        bot_data = getattr(app, "bot_data", {}) if app else {}
        wmap = bot_data.get("witel_map") or {}

        # first try encoded token key
        ent = wmap.get(token) or wmap.get(quote_plus(decoded))
        if ent:
            ts = ent.get("ts", 0)
            if not max_age_seconds or (int(time.time()) - int(ts) <= max_age_seconds):
                return ent.get("value")
            logger.debug("resolve_witel_token: token '%s' expired (age=%d)", token, int(time.time()) - int(ts))
            return ent.get("value")

    except Exception:
        logger.debug("resolve_witel_token: failed resolving token", exc_info=True)
    # fallback: return decoded token (best-effort)
    return decoded.strip()


# =====================================================================
# Registration check helper
# =====================================================================
async def require_registration(target, context: "ContextTypes.DEFAULT_TYPE") -> bool:
    """
    Pastikan user sudah terdaftar dan memiliki Witel & Divisi.
    - target: Update | CallbackQuery | Message (dipakai untuk mengirim reply jika belum lengkap)
    - context: Context object dari handler
    Returns True jika terdaftar & profil lengkap; False jika tidak (dan sudah mengirim instruksi).
    """
    try:
        # resolve user id
        user = None
        try:
            if hasattr(target, "effective_user"):
                user = target.effective_user
            elif hasattr(target, "from_user"):
                user = target.from_user
            elif hasattr(target, "chat") and getattr(target, "chat", None):
                # message-like fallback
                user = getattr(target, "from_user", None)
        except Exception:
            user = None

        if not user:
            # can't identify user — deny
            await send_md(target, "⚠️ Tidak dapat mengenali pengguna.")
            return False

        sheets = context.application.bot_data.get("sheets_manager")
        if not sheets:
            await send_md(target, "❌ Layanan Google Sheets belum tersedia.")
            return False

        # ensure Users headers (reuse the convention used elsewhere)
        try:
            uh = await sheets.async_ensure_headers("Users", ["User ID", "Nama", "Role", "Witel", "Divisi"])
        except Exception:
            # if ensure headers fails, fall back to telling user to /regis
            await send_md(target, "❌ Gagal memeriksa data pengguna. Coba /regis atau hubungi admin.")
            return False

        uid = str(user.id)
        row_idx = await sheets.async_find_row_by_value("Users", "User ID", uid, headers_map=uh)
        if not row_idx:
            # user not in sheet — ask to /regis
            await send_md(target, "ℹ️ Akunmu belum didaftarkan. Silakan jalankan /regis dan lengkapi Witel & Divisi.", parse_mode="Markdown")
            return False

        cur_witel = str(await sheets.async_get_cell_value("Users", row_idx, uh["Witel"]) or "").strip()
        cur_divisi = str(await sheets.async_get_cell_value("Users", row_idx, uh["Divisi"]) or "").strip()
        if not cur_witel or not cur_divisi:
            await send_md(target, "ℹ️ Profil belum lengkap. Silakan jalankan /regis untuk mengisi *Witel* dan *Divisi*.", parse_mode="Markdown")
            return False

        return True
    except Exception:
        # defensive fallback: if something goes wrong, block action and ask to /regis
        try:
            await send_md(target, "⚠️ Gagal memverifikasi profil. Silakan jalankan /regis atau hubungi admin.")
        except Exception:
            pass
        return False


# =====================================================================
# Exports
# =====================================================================
__all__ = [
    "send_md",
    "escape_md",
    "clear_steps",
    "build_witel_kb",
    "resolve_witel_token",
    "require_registration",
]
