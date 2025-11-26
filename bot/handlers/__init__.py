# handlers/__init__.py
"""
Central registration and service setup for handler modules.

This version is tolerant to different register_*_handlers signatures:
- register_xxx(application)  -> new style (we call with application)
- register_xxx()             -> old style (we call without args). If it returns a list/tuple
                               of handlers, we will add them to application.
- If register_xxx(application) raises TypeError (doesn't accept arg), we will fallback
  to calling register_xxx() and treating return value (if any) as handlers list.
"""
from __future__ import annotations
import logging
import inspect
from typing import Optional, Any, Iterable

from telegram.ext import Application

logger = logging.getLogger("handlers")
logger.addHandler(logging.NullHandler())

# Defensive imports of feature modules
try:
    from .add import register_add_handlers, handle_add_message  # type: ignore
except Exception:
    register_add_handlers = None  # type: ignore
    handle_add_message = None  # type: ignore

try:
    from .borrow import register_borrow_handlers, handle_borrow_message  # type: ignore
except Exception:
    register_borrow_handlers = None  # type: ignore
    handle_borrow_message = None  # type: ignore

try:
    from .inventory import register_inventory_handlers, handle_inventory_message  # type: ignore
except Exception:
    register_inventory_handlers = None  # type: ignore
    handle_inventory_message = None  # type: ignore

try:
    from .misc import register_misc_handlers, handle_misc_message  # type: ignore
except Exception:
    register_misc_handlers = None  # type: ignore
    handle_misc_message = None  # type: ignore

try:
    from .admin import register_admin_handlers, handle_admin_message  # type: ignore
except Exception:
    register_admin_handlers = None  # type: ignore
    handle_admin_message = None  # type: ignore


def _is_iterable_handlers(obj: Any) -> bool:
    """Rudimentary check whether object looks like an iterable of handlers."""
    if obj is None:
        return False
    if isinstance(obj, (list, tuple)):
        return True
    # other iterables (generator, etc.)
    try:
        iter(obj)
        return True
    except Exception:
        return False


def _add_handlers_from_iter(application: Application, iterable: Iterable):
    """Try to add each element of iterable to application as a handler."""
    try:
        for h in iterable:
            application.add_handler(h)
        return True
    except Exception:
        logger.exception("Failed to add handlers returned by register function.")
        return False


def _call_register(func, application: Application, name: str):
    """
    Call a register function in a backward/forward-compatible way.

    Strategy:
    1. If func is None -> skip.
    2. Try to call func(application). If it works:
         - If it returns an iterable, attempt to add returned handlers.
         - Otherwise assume func registered handlers itself.
    3. If calling func(application) raises TypeError (e.g. func takes 0 args),
       try calling func() and treat returned value (if any) as an iterable of handlers.
    4. Log extensively for easier debugging.
    """
    if func is None:
        logger.debug("%s not present; skipping.", name)
        return

    try:
        sig = inspect.signature(func)
        params = len(sig.parameters)
    except Exception:
        params = 1  # fallback: assume it accepts application

    # Prefer to call with application when possible, but be tolerant.
    tried_with_application = False
    try:
        # If signature shows it can accept at least 1 param, try calling with application
        if params >= 1:
            tried_with_application = True
            result = func(application)
            logger.debug("Called %s(application).", name)
            # If result looks like iterable handlers, add them
            if _is_iterable_handlers(result):
                _add_handlers_from_iter(application, result)
                logger.debug("Added returned handlers from %s(application).", name)
            return
        else:
            # Signature suggests zero params; call with no args below
            result = func()
            logger.debug("Called %s() (old-style).", name)
            if _is_iterable_handlers(result):
                _add_handlers_from_iter(application, result)
                logger.debug("Added returned handlers from %s().", name)
            return
    except TypeError as te:
        # Common case: func defined without parameters but someone tried to call with application
        logger.debug("TypeError calling %s with application: %s", name, te)
        if tried_with_application:
            # fallback: try calling without args
            try:
                result = func()
                logger.debug("Fallback: called %s() after TypeError.", name)
                if _is_iterable_handlers(result):
                    _add_handlers_from_iter(application, result)
                    logger.debug("Added returned handlers from fallback %s().", name)
                return
            except Exception as e:
                logger.exception("Fallback call %s() also failed: %s", name, e)
                return
        else:
            # we didn't try application; just log and give up
            logger.exception("Unexpected TypeError when calling %s: %s", name, te)
            return
    except Exception as e:
        # Some other exception during call; log with stacktrace
        logger.exception("Error while calling %s: %s", name, e)
        # If we attempted calling with application and it failed with non-TypeError,
        # try fallback to no-arg call as a last resort.
        if not tried_with_application:
            return
        try:
            result = func()
            logger.debug("Fallback: called %s() after error.", name)
            if _is_iterable_handlers(result):
                _add_handlers_from_iter(application, result)
                logger.debug("Added returned handlers from fallback %s().", name)
        except Exception:
            logger.debug("Fallback %s() failed as well; aborting.", name)


def register_all(application: Application):
    """
    Register all feature handlers into the provided Application.
    This attempts to be compatible with modules that expect either:
      - register_xxx(application)
      - register_xxx() returning a list of handlers (old style)
    """
    logger.info("Registering handler modules...")

    _call_register(register_add_handlers, application, "register_add_handlers")
    _call_register(register_borrow_handlers, application, "register_borrow_handlers")
    _call_register(register_inventory_handlers, application, "register_inventory_handlers")
    _call_register(register_misc_handlers, application, "register_misc_handlers")
    _call_register(register_admin_handlers, application, "register_admin_handlers")

    logger.info("Handler registration complete.")


async def setup_services(application: Application, sheets_manager: Optional[Any] = None, permission_manager: Optional[Any] = None):
    """
    Attach shared services to application.bot_data for handlers to use.
    This remains async for compatibility; it only performs assignments.
    """
    bot_data = application.bot_data
    if sheets_manager is not None:
        bot_data["sheets_manager"] = sheets_manager
        logger.debug("Attached sheets_manager to bot_data.")
    if permission_manager is not None:
        bot_data["permission_manager"] = permission_manager
        logger.debug("Attached permission_manager to bot_data.")
    bot_data.setdefault("logger", logger)
    logger.info("Services attached to application.bot_data.")


__all__ = [
    "register_all",
    "setup_services",
    "register_add_handlers",
    "register_borrow_handlers",
    "register_inventory_handlers",
    "register_misc_handlers",
    "register_admin_handlers",
    "handle_add_message",
    "handle_borrow_message",
    "handle_inventory_message",
    "handle_misc_message",
    "handle_admin_message",
]
