# permissions.py
"""
PermissionManager — improved/fault-tolerant version.

Changes vs original:
- Accepts various header names for User ID and Roles when loading from sheet.
- Normalizes roles to lowercase when storing in-memory.
- is_admin/has_permission work case-insensitively.
- More robust parsing of Roles column (comma/semicolon separated).
- Added async helpers (async_has_permission, async_is_admin) and a sync
  wrapper load_from_sheet for compatibility with various startup flows.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Dict, Set, Optional, Iterable, Any, List

from config import USERS_SHEET

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


# Default mapping role -> permissions
DEFAULT_ROLE_PERMISSIONS = {
    "admin": {
        "can_add_items",
        "can_approve",
        "can_return",
        "can_manage_users",
        "can_view_all_requests",
    },
    "owner": {
        "can_approve_own",
    },
    "user": set(),
}

# Flexible header candidates
_USER_ID_KEYS = ["User ID", "user id", "user_id", "userid", "UserID", "ID", "Id"]
_ROLE_KEYS = ["Roles", "Role", "roles", "role", "Role(s)", "Rol"]

# Column headers for USERS_SHEET if using sheet persistence (preferred canonical)
USERS_SHEET_HEADERS = ["User ID", "Roles", "Notes"]


class PermissionManager:
    def __init__(self, initial_admins: Optional[Iterable[int]] = None):
        self._roles_by_user: Dict[int, Set[str]] = {}
        if initial_admins:
            for uid in initial_admins:
                self._roles_by_user[int(uid)] = {"admin"}
        self.role_permissions = {r: set(perms) for r, perms in DEFAULT_ROLE_PERMISSIONS.items()}

    # ----------------
    # Normalization helpers
    # ----------------
    @staticmethod
    def _normalize_role(r: str) -> str:
        return str(r).strip().lower()

    @staticmethod
    def _parse_roles_cell(raw: Any) -> List[str]:
        """Parse a Roles cell into list of normalized role strings."""
        if raw is None:
            return []
        if isinstance(raw, (list, tuple)):
            lst = [str(x).strip() for x in raw if x is not None and str(x).strip() != ""]
        else:
            s = str(raw).strip()
            # split by comma or semicolon
            parts = [p.strip() for p in s.replace(";", ",").split(",") if p.strip() != ""]
            lst = parts
        return [PermissionManager._normalize_role(p) for p in lst]

    # ----------------
    # Sync API
    # ----------------
    def add_user_role(self, user_id: int, role: str) -> None:
        user_id = int(user_id)
        role_norm = self._normalize_role(role)
        if user_id not in self._roles_by_user:
            self._roles_by_user[user_id] = set()
        self._roles_by_user[user_id].add(role_norm)
        logger.info("PermissionManager: added role %s to %s", role_norm, user_id)

    def remove_user_role(self, user_id: int, role: str) -> None:
        user_id = int(user_id)
        role_norm = self._normalize_role(role)
        if user_id in self._roles_by_user and role_norm in self._roles_by_user[user_id]:
            self._roles_by_user[user_id].remove(role_norm)
            if not self._roles_by_user[user_id]:
                self._roles_by_user.pop(user_id, None)
            logger.info("PermissionManager: removed role %s from %s", role_norm, user_id)

    def set_user_roles(self, user_id: int, roles: Iterable[str]) -> None:
        self._roles_by_user[int(user_id)] = set(self._normalize_role(r) for r in roles)

    def get_user_roles(self, user_id: int) -> Set[str]:
        return set(self._roles_by_user.get(int(user_id), set()))

    def is_admin(self, user_id: int) -> bool:
        return "admin" in self._roles_by_user.get(int(user_id), set())

    def has_permission(self, user_id: int, perm: str) -> bool:
        user_id = int(user_id)
        perm = str(perm)
        roles = self._roles_by_user.get(user_id, set())
        if not roles:
            return False
        if "admin" in roles:
            return True
        for r in roles:
            perms = self.role_permissions.get(r, set())
            if perm in perms:
                return True
        return False

    # ----------------
    # Async compatibility helpers
    # ----------------
    async def async_has_permission(self, user_id: int, perm: str) -> bool:
        """
        Async wrapper for has_permission. Runs sync logic in executor to avoid
        blocking event loop if implementation becomes heavier later.
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: self.has_permission(user_id, perm))

    async def async_is_admin(self, user_id: int) -> bool:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: self.is_admin(user_id))

    # ----------------
    # Persistence helpers (async)
    # ----------------
    async def async_load_from_sheet(self, sheets_manager, sheet_name: str = USERS_SHEET) -> None:
        """
        Load users and roles from USERS_SHEET into memory.

        Flexible: accepts column names like 'User ID' or 'UserID', and 'Roles' or 'Role'.
        Normalizes roles to lowercase.
        """
        try:
            # Ensure at least canonical headers exist (it will add if missing)
            await sheets_manager.async_ensure_headers(sheet_name, USERS_SHEET_HEADERS)
            records = await sheets_manager.async_get_all_records(sheet_name)
            self._roles_by_user.clear()
            for r in records:
                try:
                    # find user id using candidate keys
                    raw_id = None
                    for k in _USER_ID_KEYS:
                        if k in r and r.get(k) not in (None, ""):
                            raw_id = r.get(k)
                            break
                    # fallback: try first key in dict (case-insensitive)
                    if raw_id is None:
                        for kname, v in r.items():
                            if isinstance(kname, str) and kname.strip().lower() in [kk.lower() for kk in _USER_ID_KEYS]:
                                raw_id = v
                                break
                    if not raw_id:
                        continue
                    uid = int(str(raw_id).strip())

                    # find roles cell using candidate keys
                    raw_roles = None
                    for k in _ROLE_KEYS:
                        if k in r and r.get(k) not in (None, ""):
                            raw_roles = r.get(k)
                            break
                    if raw_roles is None:
                        # case-insensitive fallback
                        for kname, v in r.items():
                            if isinstance(kname, str) and kname.strip().lower() in [kk.lower() for kk in _ROLE_KEYS]:
                                raw_roles = v
                                break

                    role_list = self._parse_roles_cell(raw_roles)
                    if role_list:
                        self._roles_by_user[uid] = set(role_list)
                except Exception:
                    logger.exception("PermissionManager: skipping bad user row: %s", r)
            logger.info("PermissionManager: loaded %d users from sheet %s", len(self._roles_by_user), sheet_name)
        except Exception as e:
            logger.exception("PermissionManager.async_load_from_sheet failed: %s", e)
            raise

    async def async_save_user(self, sheets_manager, user_id: int, sheet_name: str = USERS_SHEET) -> bool:
        try:
            headers = await sheets_manager.async_ensure_headers(sheet_name, USERS_SHEET_HEADERS)
            # find existing by user id (string)
            row_idx = await sheets_manager.async_find_row_by_value(sheet_name, "User ID", str(user_id), headers_map=headers)
            roles_str = ",".join(sorted(self._roles_by_user.get(int(user_id), set())))
            notes = ""
            if row_idx:
                roles_col = headers.get("Roles") or headers.get("Role")
                if roles_col:
                    ok = await sheets_manager.async_update_cell(sheet_name, row_idx, roles_col, roles_str)
                    return ok
                else:
                    max_col = max(headers.values())
                    row = [""] * max_col
                    row[headers["User ID"] - 1] = str(user_id)
                    row[headers["Roles"] - 1] = roles_str
                    row[headers["Notes"] - 1] = notes
                    return await sheets_manager.async_append_row(sheet_name, row)
            else:
                max_col = max(headers.values())
                row = [""] * max_col
                row[headers["User ID"] - 1] = str(user_id)
                row[headers["Roles"] - 1] = roles_str
                row[headers["Notes"] - 1] = notes
                return await sheets_manager.async_append_row(sheet_name, row)
        except Exception as e:
            logger.exception("PermissionManager.async_save_user failed: %s", e)
            return False

    async def async_save_all(self, sheets_manager, sheet_name: str = USERS_SHEET) -> bool:
        try:
            headers = await sheets_manager.async_ensure_headers(sheet_name, USERS_SHEET_HEADERS)
            ws = await sheets_manager.async_get_worksheet_by_name(sheet_name)
            # Attempt to clear existing rows (non-critical if fails)
            try:
                total_rows = len(ws.get_all_values())
                if total_rows >= 2:
                    for r in range(total_rows, 1, -1):
                        try:
                            ws.delete_rows(r)
                        except Exception:
                            try:
                                ws.delete_row(r)
                            except Exception:
                                logger.debug("Failed deleting row %s (maybe already deleted)", r)
            except Exception:
                logger.exception("Could not clear rows before save_all (continuing to append)")

            for uid, roles in self._roles_by_user.items():
                row = [""] * max(headers.values())
                row[headers["User ID"] - 1] = str(uid)
                row[headers["Roles"] - 1] = ",".join(sorted(roles))
                row[headers["Notes"] - 1] = ""
                await sheets_manager.async_append_row(sheet_name, row)
            return True
        except Exception as e:
            logger.exception("PermissionManager.async_save_all failed: %s", e)
            return False

    # ----------------
    # Sync compatibility wrappers
    # ----------------
    def load_from_sheet(self, sheets_manager, sheet_name: str = USERS_SHEET) -> None:
        """
        Sync wrapper for loading from sheet. This simply runs the async loader.

        NOTE: This uses asyncio.run and therefore must not be called from within an
        already-running event loop. Prefer async_load_from_sheet when possible.
        """
        try:
            asyncio.run(self.async_load_from_sheet(sheets_manager, sheet_name))
        except RuntimeError:
            # If event loop is running in caller, fallback to scheduling in new loop via thread
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(self.async_load_from_sheet(sheets_manager, sheet_name))
            finally:
                loop.close()

    # ----------------
    def seed_admins(self, user_ids: Iterable[int]):
        for uid in user_ids:
            self.add_user_role(int(uid), "admin")
        logger.info("PermissionManager: seeded admins %s", list(user_ids))

    def dump_state(self) -> Dict[str, Any]:
        return {str(k): sorted(list(v)) for k, v in self._roles_by_user.items()}
