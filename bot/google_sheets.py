# google_sheets.py
"""
GoogleSheetsManager (robust, async-friendly)

- Defensive gspread imports so bot keeps running in environments without gspread.
- Blocking gspread calls are wrapped via asyncio.to_thread for async usage.
- _get_worksheet_by_name tries safer lookup (case-insensitive), refreshes worksheets list,
  and only attempts add_worksheet when the sheet truly does not exist. Handles race conditions.
- Provides get_unique_column_values helper used by Witel keyboard builder.
- Transaction helpers included: async_get_txn_by_id, async_get_user_borrows (approved-only),
  async_return_txn, async_update_inventory_on_return, async_update_row, async_set_cell.
"""
from __future__ import annotations
import asyncio
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("google_sheets")
logger.addHandler(logging.NullHandler())

# Defensive imports
GS_IMPORT_ERROR: Optional[BaseException] = None
try:
    import gspread  # type: ignore
    from google.oauth2.service_account import Credentials  # type: ignore

    # defensive exceptions import (names vary by gspread versions)
    try:
        import gspread.exceptions as _g_excs  # type: ignore
        APIError = getattr(_g_excs, "APIError", Exception)
        WorksheetNotFound = getattr(_g_excs, "WorksheetNotFound", Exception)
    except Exception:
        APIError = WorksheetNotFound = Exception

    GS_AVAILABLE = True
except Exception as e:
    GS_AVAILABLE = False
    GS_IMPORT_ERROR = e
    gspread = None  # type: ignore
    Credentials = None  # type: ignore
    APIError = WorksheetNotFound = Exception


# -------------------------
# Small utility used internally
# -------------------------
def safe_cast_int(v: Any, default: int = 0) -> int:
    try:
        return int(float(str(v).strip()))
    except Exception:
        return default


# -------------------------
# Noop Manager
# -------------------------
class NoopSheetsManager:
    """No-op manager used when gspread not available or connection fails."""

    def __init__(self, *args, **kwargs):
        self._connected = False

    # basic lifecycle
    async def async_connect(self) -> bool:
        logger.warning("NoopSheetsManager: Google Sheets unavailable.")
        self._connected = False
        return False

    async def async_is_connected(self) -> bool:
        return False

    # worksheet helpers
    async def async_get_worksheet_by_name(self, *args, **kwargs):
        return None

    async def async_ensure_headers(self, sheet_name: str, headers: List[str]) -> Dict[str, int]:
        logger.debug("NoopSheetsManager: ensure_headers(%s)", sheet_name)
        return {}

    async def async_get_all_records(self, sheet_name: str) -> List[Dict[str, Any]]:
        return []

    async def async_get_all_rows(self, sheet_name: str) -> List[Dict[str, Any]]:
        return []

    async def async_append_row(self, sheet_name: str, row: List[Any]) -> bool:
        return False

    async def async_update_cell(self, sheet_name: str, row: int, col: int, value: Any) -> bool:
        return False

    async def async_set_cell(self, sheet_name: str, row: int, col_or_name: Any, value: Any) -> bool:
        return False

    async def async_update_row(self, sheet_name: str, row: int, data: Dict[str, Any]) -> bool:
        return False

    async def async_get_cell_value(self, sheet_name: str, row: int, col: int) -> str:
        return ""

    async def async_find_row_by_value(
        self, sheet_name: str, column_name: str, value: str, headers_map: Optional[Dict[str, int]] = None
    ) -> Optional[int]:
        return None

    async def async_increment_cell(self, sheet_name: str, row: int, col: int, delta: int, max_retries: int = 3, retry_delay: float = 0.4) -> Tuple[bool, Optional[str]]:
        return False, None

    async def async_write_log(self, user: str, action: str, item: str = "", note: str = "", sheet_name: str = "Log") -> bool:
        return False

    async def async_clear_sheet(self, sheet_name: str) -> bool:
        return False

    async def async_get_unique_column_values(self, sheet_name: str, column_name_or_index) -> List[str]:
        return []

    # transaction helpers
    async def async_get_txn_by_id(self, txn_id: str) -> Optional[Dict[str, Any]]:
        return None

    async def async_get_user_borrows(self, user_id: int) -> List[Dict[str, Any]]:
        return []

    async def async_return_txn(self, txn_id: str, qty: int, returned_by: Optional[int] = None) -> bool:
        return False

    async def async_update_inventory_on_return(self, txn_id: str, qty: int) -> bool:
        return False


# -------------------------
# Real GoogleSheetsManager
# -------------------------
if GS_AVAILABLE:

    class GoogleSheetsManager:
        """
        GoogleSheetsManager using gspread. Blocking calls are wrapped for async usage.
        """

        def __init__(
            self,
            spreadsheet_id: Optional[str] = None,
            credentials_path: Optional[str] = None,
            creds_json: Optional[dict] = None,
            scopes: Optional[List[str]] = None,
            **kwargs,
        ):
            self.spreadsheet_id = spreadsheet_id
            # allow multiple kwarg names for historical compatibility
            self.credentials_path = credentials_path or kwargs.get("SERVICE_ACCOUNT_FILE") or kwargs.get("credentials_file")
            self.creds_json = creds_json
            self.scopes = scopes or [
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive",
            ]
            self._gc = None
            self._sh = None
            self._worksheet_cache: Dict[str, Any] = {}
            self._connected = False

        # -------------------------
        # Credentials helpers
        # -------------------------
        def _validate_creds_file(self) -> Tuple[bool, str]:
            if self.creds_json:
                return True, "creds_json provided"
            if not self.credentials_path:
                return False, "credentials_path not set"
            path = os.path.expanduser(self.credentials_path)
            if not os.path.exists(path):
                return False, f"credentials file not found at: {path}"
            if not os.path.isfile(path):
                return False, f"credentials path is not a file: {path}"
            return True, f"credentials file exists: {path}"

        def _build_credentials(self):
            if Credentials is None:
                raise RuntimeError("google-auth not available")
            if self.creds_json:
                return Credentials.from_service_account_info(self.creds_json, scopes=self.scopes)
            ok, msg = self._validate_creds_file()
            if not ok:
                raise RuntimeError(f"Credentials validation failed: {msg}")
            return Credentials.from_service_account_file(self.credentials_path, scopes=self.scopes)

        # -------------------------
        # Connect / state
        # -------------------------
        def connect(self) -> bool:
            try:
                creds = self._build_credentials()
                self._gc = gspread.authorize(creds)
                if not self.spreadsheet_id:
                    raise RuntimeError("spreadsheet_id not provided")
                self._sh = self._gc.open_by_key(self.spreadsheet_id)
                self._worksheet_cache = {}
                self._connected = True
                logger.info("GoogleSheetsManager connected to spreadsheet %s", self.spreadsheet_id)
                return True
            except Exception as e:
                logger.exception("GoogleSheetsManager.connect failed: %s", e)
                self._connected = False
                return False

        def is_connected(self) -> bool:
            if self._connected and self._sh:
                return True
            return self.connect()

        # -------------------------
        # Worksheet lookup (defensive)
        # -------------------------
        def _get_worksheet_by_name(self, sheet_name: str):
            """
            Return a worksheet object for sheet_name.

            Behavior:
            - If sheet_name empty -> return first worksheet.
            - Try direct lookup self._sh.worksheet(name)
            - If not found, refresh worksheets() and try case-insensitive match.
            - Only call add_worksheet when truly missing; handle add_worksheet races.
            """
            if not self.is_connected():
                raise RuntimeError("Not connected to Google Sheets")

            key = (sheet_name or "").strip()
            # cache lookup
            if key and key in self._worksheet_cache:
                return self._worksheet_cache[key]
            if not key and "" in self._worksheet_cache:
                return self._worksheet_cache[""]

            # Try direct lookup
            try:
                if key:
                    ws = self._sh.worksheet(sheet_name)
                else:
                    ws = self._sh.get_worksheet(0)
                cache_key = key or ""
                self._worksheet_cache[cache_key] = ws
                return ws
            except Exception as e:
                logger.debug("_get_worksheet_by_name: direct lookup failed for '%s': %s", sheet_name, e)
                # refresh worksheets and try case-insensitive match
                try:
                    sheets_list = self._sh.worksheets()
                    for s in sheets_list:
                        try:
                            title = getattr(s, "title", "")
                        except Exception:
                            title = ""
                        if title and title.strip().lower() == str(sheet_name).strip().lower():
                            cache_key = (sheet_name or "").strip() or ""
                            self._worksheet_cache[cache_key] = s
                            return s
                    if not sheet_name and sheets_list:
                        s0 = sheets_list[0]
                        self._worksheet_cache[""] = s0
                        return s0
                except Exception as e2:
                    logger.debug("_get_worksheet_by_name: worksheets() refresh failed: %s", e2)

                # Not found: attempt to create worksheet (only if a non-empty name requested)
                if not sheet_name:
                    raise e
                try:
                    ws = self._sh.add_worksheet(title=sheet_name, rows="200", cols="20")
                    cache_key = sheet_name.strip()
                    self._worksheet_cache[cache_key] = ws
                    return ws
                except Exception as add_exc:
                    logger.warning(
                        "_get_worksheet_by_name: add_worksheet failed for '%s': %s (attempting recovery)", sheet_name, add_exc
                    )
                    try:
                        sheets_list2 = self._sh.worksheets()
                        for s in sheets_list2:
                            try:
                                title = getattr(s, "title", "")
                            except Exception:
                                title = ""
                            if title and title.strip().lower() == str(sheet_name).strip().lower():
                                cache_key = sheet_name.strip()
                                self._worksheet_cache[cache_key] = s
                                return s
                    except Exception as e3:
                        logger.debug("_get_worksheet_by_name: final refresh failed: %s", e3)
                    raise add_exc

        # -------------------------
        # Headers & simple table helpers (blocking)
        # -------------------------
        def ensure_headers(self, sheet_name: str, headers: List[str]) -> Dict[str, int]:
            """
            Ensure first row contains given headers. Returns mapping {header: col_index}.
            """
            ws = self._get_worksheet_by_name(sheet_name)
            try:
                first_row = ws.row_values(1)
            except Exception:
                first_row = []

            existing = [str(h).strip() for h in first_row] if first_row else []

            # If all required present (case-sensitive exact), return mapping
            if existing and all(any(str(h) == e for e in existing) for h in headers):
                return {name: idx + 1 for idx, name in enumerate(existing)}

            new_header = list(existing) if existing else []
            changed = False
            for h in headers:
                if not any(str(h).strip().lower() == (e or "").strip().lower() for e in new_header):
                    new_header.append(h)
                    changed = True

            if changed:
                try:
                    ws.update("A1", [new_header], value_input_option="RAW")
                except Exception:
                    logger.exception("ensure_headers: failed to write header row to sheet %s", sheet_name)

            return {name: idx + 1 for idx, name in enumerate(new_header)}

        def get_all_records(self, sheet_name: str) -> List[Dict[str, Any]]:
            ws = self._get_worksheet_by_name(sheet_name)
            try:
                return ws.get_all_records()
            except Exception as e:
                logger.exception("get_all_records failed for sheet %s: %s", sheet_name, e)
                return []

        # alias keeping "rows" naming used by handlers
        def get_all_rows(self, sheet_name: str) -> List[Dict[str, Any]]:
            return self.get_all_records(sheet_name)

        def append_row(self, sheet_name: str, row: List[Any]) -> bool:
            ws = self._get_worksheet_by_name(sheet_name)
            try:
                ws.append_row(row, value_input_option="RAW")
                return True
            except Exception as e:
                logger.exception("append_row failed for sheet %s: %s", sheet_name, e)
                return False

        def update_cell(self, sheet_name: str, row: int, col: int, value: Any) -> bool:
            ws = self._get_worksheet_by_name(sheet_name)
            try:
                ws.update_cell(row, col, value)
                return True
            except Exception as e:
                logger.exception("update_cell failed for %s r=%s c=%s : %s", sheet_name, row, col, e)
                return False

        def set_cell(self, sheet_name: str, row: int, col_or_name: Any, value: Any) -> bool:
            """
            Set cell using either numeric column index or column name (string).
            If col_or_name is a string, resolve header to index (case-insensitive).
            """
            ws = self._get_worksheet_by_name(sheet_name)
            try:
                if isinstance(col_or_name, int):
                    ws.update_cell(row, col_or_name, value)
                    return True
                # try resolve header name
                headers = ws.row_values(1)
                for idx, h in enumerate(headers, start=1):
                    if str(h).strip().lower() == str(col_or_name).strip().lower():
                        ws.update_cell(row, idx, value)
                        return True
                # fallback: header not found
                logger.debug("set_cell: header not found: %s", col_or_name)
                return False
            except Exception as e:
                logger.exception("set_cell failed for %s r=%s c=%s : %s", sheet_name, row, col_or_name, e)
                return False

        def update_row(self, sheet_name: str, row: int, data: Dict[str, Any]) -> bool:
            """
            Update multiple columns in a row using column names as keys in data.
            Resolves headers (case-insensitive) and writes each provided field.
            """
            ws = self._get_worksheet_by_name(sheet_name)
            try:
                headers = ws.row_values(1)
            except Exception:
                headers = []
            # mapping lowercase header -> (idx, original)
            header_map = {}
            for idx, h in enumerate(headers, start=1):
                header_map[str(h).strip().lower()] = (idx, h)
            ok = True
            for key, val in data.items():
                try:
                    if isinstance(key, int):
                        ok = ok and ws.update_cell(row, key, val)
                    else:
                        k = str(key).strip().lower()
                        if k in header_map:
                            col_idx = header_map[k][0]
                            ws.update_cell(row, col_idx, val)
                        else:
                            # not found - append new header and write
                            headers.append(str(key))
                            ws.update("A1", [headers], value_input_option="RAW")
                            ws.update_cell(row, len(headers), val)
                except Exception:
                    logger.exception("update_row: failed writing %s=%s", key, val)
                    ok = False
            return ok

        def get_cell_value(self, sheet_name: str, row: int, col: int) -> str:
            ws = self._get_worksheet_by_name(sheet_name)
            try:
                v = ws.cell(row, col).value
                return "" if v is None else str(v)
            except Exception as e:
                logger.debug("get_cell_value failed for %s r=%s c=%s: %s", sheet_name, row, col, e)
                return ""

        def find_row_by_value(self, sheet_name: str, column_name: str, value: str, headers_map: Optional[Dict[str, int]] = None) -> Optional[int]:
            headers_map = headers_map or self.ensure_headers(sheet_name, [column_name])
            col_idx = headers_map.get(column_name)
            if not col_idx:
                for k, v in headers_map.items():
                    if k.strip().lower() == column_name.strip().lower():
                        col_idx = v
                        break
            if not col_idx:
                return None
            ws = self._get_worksheet_by_name(sheet_name)
            try:
                values = ws.col_values(col_idx)
                for idx, v in enumerate(values, start=1):
                    try:
                        if str(v).strip() == str(value).strip():
                            return idx
                    except Exception:
                        continue
            except Exception as e:
                logger.debug("find_row_by_value failed for %s col=%s: %s", sheet_name, column_name, e)
            return None

        def increment_cell(self, sheet_name: str, row: int, col: int, delta: int, max_retries: int = 3, retry_delay: float = 0.4) -> Tuple[bool, Optional[str]]:
            ws = self._get_worksheet_by_name(sheet_name)
            attempt = 0
            last_exc = None
            while attempt < max_retries:
                attempt += 1
                try:
                    cell = ws.cell(row, col)
                    cur = cell.value or "0"
                    try:
                        cur_num = int(float(str(cur).strip()))
                    except Exception:
                        cur_num = 0
                    new_val = cur_num + int(delta)
                    ws.update_cell(row, col, str(new_val))
                    return True, str(new_val)
                except Exception as e:
                    last_exc = e
                    time.sleep(retry_delay * attempt)
            logger.exception("increment_cell failed after %s attempts: %s", max_retries, last_exc)
            return False, None

        def write_log(self, user: str, action: str, item: str = "", note: str = "", sheet_name: str = "Log") -> bool:
            try:
                ws = self._get_worksheet_by_name(sheet_name)
                ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
                ws.append_row([ts, str(user), str(action), str(item), str(note)], value_input_option="RAW")
                return True
            except Exception as e:
                logger.exception("write_log failed: %s", e)
                return False

        def clear_sheet(self, sheet_name: str) -> bool:
            ws = self._get_worksheet_by_name(sheet_name)
            try:
                ws.clear()
                return True
            except Exception as e:
                logger.exception("clear_sheet failed for %s: %s", sheet_name, e)
                return False

        # -------------------------
        # Utility: unique values in a column (blocking)
        # -------------------------
        def get_unique_column_values(self, sheet_name: str, column_name_or_index) -> List[str]:
            try:
                records = self.get_all_records(sheet_name)
                if not records:
                    ws = self._get_worksheet_by_name(sheet_name)
                    try:
                        vals = ws.col_values(1)
                        uniq = []
                        seen = set()
                        for v in vals:
                            s = str(v).strip()
                            if s and s not in seen:
                                seen.add(s)
                                uniq.append(s)
                        return uniq
                    except Exception:
                        return []

                chosen_key = None
                if isinstance(column_name_or_index, int):
                    first = records[0]
                    if isinstance(first, dict):
                        keys = list(first.keys())
                        idx = column_name_or_index - 1
                        if 0 <= idx < len(keys):
                            chosen_key = keys[idx]
                else:
                    for k in (records[0].keys() if records else []):
                        if str(k).strip().lower() == str(column_name_or_index).strip().lower():
                            chosen_key = k
                            break
                    if not chosen_key:
                        for k in (records[0].keys() if records else []):
                            if str(column_name_or_index).strip().lower() in str(k).strip().lower():
                                chosen_key = k
                                break

                uniq = []
                seen = set()
                if chosen_key:
                    for r in records:
                        try:
                            v = r.get(chosen_key)
                            if v is None:
                                continue
                            s = str(v).strip()
                            if s and s not in seen:
                                seen.add(s)
                                uniq.append(s)
                        except Exception:
                            continue
                    return uniq

                for k in (records[0].keys() if records else []):
                    tmp = []
                    seen_tmp = set()
                    for r in records:
                        v = r.get(k)
                        if v is None:
                            continue
                        s = str(v).strip()
                        if s and s not in seen_tmp:
                            seen_tmp.add(s)
                            tmp.append(s)
                    if tmp:
                        return tmp

                return []
            except Exception as e:
                logger.exception("get_unique_column_values failed for %s col=%s: %s", sheet_name, column_name_or_index, e)
                return []

        # -------------------------
        # Transaction helpers (blocking)
        # -------------------------
        def get_txn_by_id(self, txn_id: str) -> Optional[Dict[str, Any]]:
            """Search PEMINJAMAN sheet for a transaction row matching common ID keys. Returns row dict or None."""
            try:
                # try several common names
                candidates = ["Peminjaman", "peminjaman", "Borrow", "borrow"]
                for sname in candidates:
                    try:
                        records = self.get_all_rows(sname)
                        if not records:
                            continue
                    except Exception:
                        continue
                    for rec in records:
                        # check common id keys
                        for k in rec.keys():
                            if str(k).strip().lower() in ("id transaksi", "id_transaksi", "id", "transaction id", "txn", "txnid"):
                                if str(rec.get(k)).strip() == str(txn_id).strip():
                                    return rec
                # last resort: scan any record field for value match
                for sname in candidates:
                    try:
                        records = self.get_all_rows(sname)
                        if not records:
                            continue
                    except Exception:
                        continue
                    for rec in records:
                        for v in rec.values():
                            if str(v).strip() == str(txn_id).strip():
                                return rec
            except Exception:
                logger.exception("get_txn_by_id failed", exc_info=True)
            return None

        def get_user_borrows(self, user_id: int) -> List[Dict[str, Any]]:
            """
            Return list of borrow-like dicts for given user_id.

            IMPORTANT: This version returns ONLY borrows whose status indicates "approved"/"disetujui"
            (case-insensitive) and where remaining > 0.
            Each dict minimally: txn_id, item_name, qty_borrowed, qty_returned (if available), remaining, status
            """
            out: List[Dict[str, Any]] = []
            try:
                # search common sheet names
                candidates = ["Peminjaman", "peminjaman", "Borrow", "borrow"]
                rows = []
                for sname in candidates:
                    try:
                        rr = self.get_all_rows(sname)
                        if rr:
                            rows = rr
                            break
                    except Exception:
                        continue
                if not rows:
                    return out

                for r in rows:
                    # find peminjam id
                    possible_peminjam = None
                    for k in r.keys():
                        if str(k).strip().lower() in ("peminjam id", "peminjamid", "peminjam_id", "peminjam", "borrower id", "userid"):
                            possible_peminjam = r.get(k)
                            break
                    try:
                        owner_numeric = int(str(possible_peminjam).strip()) if possible_peminjam not in (None, "") else None
                    except Exception:
                        owner_numeric = None
                    if owner_numeric != user_id:
                        continue

                    # get status
                    status = ""
                    for k in r.keys():
                        if str(k).strip().lower() in ("status peminjaman", "status", "status_peminjaman"):
                            status = str(r.get(k) or "").strip().lower()
                            break

                    # only approved/disetujui
                    if not (("disetujui" in status) or ("approved" in status)):
                        continue

                    qb = 0
                    qr = 0
                    for k in r.keys():
                        kl = str(k).strip().lower()
                        if kl in ("qty dipinjam", "qty_dipinjam", "qty", "quantity", "jumlah"):
                            qb = safe_cast_int(r.get(k), 0)
                        if kl in ("qty dikembalikan", "qty_returned", "qty_dikembalikan", "qty_return", "qtyreturned"):
                            qr = safe_cast_int(r.get(k), 0)
                    remaining = max(0, qb - qr)
                    if remaining <= 0:
                        continue

                    txn_key = None
                    for k in r.keys():
                        if str(k).strip().lower() in ("id transaksi", "id_transaksi", "id", "txn", "txnid"):
                            txn_key = r.get(k)
                            break
                    item_name = None
                    for k in r.keys():
                        if str(k).strip().lower() in ("nama barang", "nama", "item", "item_name"):
                            item_name = r.get(k)
                            break

                    out.append({"txn_id": txn_key, "item_name": item_name or "Barang", "qty_borrowed": qb, "qty_returned": qr, "remaining": remaining, "status": status})
            except Exception:
                logger.exception("get_user_borrows failed", exc_info=True)
            return out

        def return_txn(self, txn_id: str, qty: int, returned_by: Optional[int] = None) -> bool:
            """
            Atomic-ish operation:
            - find txn row index
            - compute new qty_returned (clamped)
            - write qty_returned (if column exists) or set status if fully returned
            - increment inventory available (if inventory item can be located)
            - write log
            Returns True on success (best-effort), False otherwise.
            """
            try:
                # Try common sheet names and headers
                pem_sheet_names = ["Peminjaman", "peminjaman", "Borrow", "borrow"]
                found = None
                for sname in pem_sheet_names:
                    try:
                        records = self.get_all_rows(sname)
                        if records:
                            found = (sname, records)
                            break
                    except Exception:
                        continue
                if not found:
                    # no transactions sheet found
                    return False
                sname, records = found
                # find index (1-based)
                row_idx = None
                headers = self.ensure_headers(sname, [])  # get headers current mapping
                # search by common id fields
                for i, rec in enumerate(records, start=2):
                    for k in rec.keys():
                        if str(k).strip().lower() in ("id transaksi", "id_transaksi", "id", "txn", "txnid"):
                            if str(rec.get(k)).strip() == str(txn_id).strip():
                                row_idx = i
                                break
                    if row_idx:
                        break
                if not row_idx:
                    # fallback: scan values for txn anywhere
                    for i, rec in enumerate(records, start=2):
                        for v in rec.values():
                            if str(v).strip() == str(txn_id).strip():
                                row_idx = i
                                break
                        if row_idx:
                            break
                if not row_idx:
                    return False

                # Compute current qb and qr
                qb = 0
                qr = 0
                headers_map = self.ensure_headers(sname, [])  # dict header->idx
                # try to read cell values directly using header mapping if we know header names
                try:
                    # attempt to find column indices for common fields
                    col_qb = None
                    col_qr = None
                    for hname in ("Qty Dipinjam", "Qty_Dipinjam", "Qty", "quantity", "Jumlah"):
                        for k, v in headers_map.items():
                            if k.strip().lower() == str(hname).strip().lower():
                                col_qb = v
                                break
                        if col_qb:
                            break
                    for hname in ("Qty Dikembalikan", "Qty_Returned", "qty_returned", "QtyReturned"):
                        for k, v in headers_map.items():
                            if k.strip().lower() == str(hname).strip().lower():
                                col_qr = v
                                break
                        if col_qr:
                            break
                    if col_qb:
                        qb = safe_cast_int(self.get_cell_value(sname, row_idx, col_qb), 0)
                    if col_qr:
                        qr = safe_cast_int(self.get_cell_value(sname, row_idx, col_qr), 0)
                except Exception:
                    pass
                # fallback to records entry
                if qb == 0 or qr == 0:
                    rec = records[row_idx - 2] if 0 <= (row_idx - 2) < len(records) else {}
                    for k, v in rec.items():
                        kl = str(k).strip().lower()
                        if kl in ("qty dipinjam", "qty_dipinjam", "qty", "quantity", "jumlah"):
                            qb = safe_cast_int(v, qb)
                        if kl in ("qty dikembalikan", "qty_returned", "qty_dikembalikan", "qty_return", "qtyreturned"):
                            qr = safe_cast_int(v, qr)
                new_qr = min(qb, qr + qty)

                # write back: prefer to update explicit "Qty Dikembalikan" column if exists otherwise update status
                written = False
                # find return column
                ret_col = None
                for k, v in headers_map.items():
                    if str(k).strip().lower() in ("qty dikembalikan", "qty_returned", "qty_dikembalikan", "qtyreturned"):
                        ret_col = v
                        break
                if ret_col:
                    self.update_cell(sname, row_idx, ret_col, str(new_qr))
                    written = True
                else:
                    # if fully returned, set status column
                    for k, v in headers_map.items():
                        if str(k).strip().lower() in ("status peminjaman", "status", "status_peminjaman"):
                            if new_qr >= qb:
                                self.update_cell(sname, row_idx, v, "Dikembalikan")
                                written = True
                            break

                # update inventory: try to find Nama Barang from record then update "Tersedia"
                nama = None
                # try headers
                for k, v in headers_map.items():
                    if str(k).strip().lower() in ("nama barang", "nama", "item", "item_name"):
                        try:
                            nama = self.get_cell_value(sname, row_idx, v)
                        except Exception:
                            nama = None
                        break
                if not nama:
                    rec = records[row_idx - 2] if 0 <= (row_idx - 2) < len(records) else {}
                    for k, v in rec.items():
                        if str(k).strip().lower() in ("nama barang", "nama", "item", "item_name"):
                            nama = v
                            break

                inv_sheet_names = ["Inventaris", "inventaris", "Inventory", "inventory"]
                for inv_name in inv_sheet_names:
                    try:
                        inv_headers = self.ensure_headers(inv_name, [])
                        # find row by nama
                        if nama:
                            inv_row = self.find_row_by_value(inv_name, "Nama Barang", str(nama), headers_map=inv_headers)
                            if inv_row:
                                # find Tersedia column index
                                tersedia_col = None
                                for hk, hv in inv_headers.items():
                                    if str(hk).strip().lower() in ("tersedia", "available", "qty available"):
                                        tersedia_col = hv
                                        break
                                if tersedia_col:
                                    self.increment_cell(inv_name, inv_row, tersedia_col, qty)
                                    break
                    except Exception:
                        continue

                # write log
                try:
                    self.write_log(str(returned_by or ""), "Return", str(txn_id), f"qty={qty}")
                except Exception:
                    pass

                return True
            except Exception:
                logger.exception("return_txn failed", exc_info=True)
                return False

        def update_inventory_on_return(self, txn_id: str, qty: int) -> bool:
            """
            Separate helper that tries to find inventory item related to txn and increments available stock.
            """
            try:
                # try to find txn row to extract Nama Barang
                rec = self.get_txn_by_id(txn_id)
                if not rec:
                    return False
                nama = None
                for k, v in rec.items():
                    if str(k).strip().lower() in ("nama barang", "nama", "item", "item_name"):
                        nama = v
                        break
                if not nama:
                    return False
                inv_sheet_names = ["Inventaris", "inventaris", "Inventory", "inventory"]
                for inv_name in inv_sheet_names:
                    try:
                        inv_headers = self.ensure_headers(inv_name, [])
                        inv_row = self.find_row_by_value(inv_name, "Nama Barang", str(nama), headers_map=inv_headers)
                        if inv_row:
                            tersedia_col = None
                            for hk, hv in inv_headers.items():
                                if str(hk).strip().lower() in ("tersedia", "available"):
                                    tersedia_col = hv
                                    break
                            if tersedia_col:
                                ok, new = self.increment_cell(inv_name, inv_row, tersedia_col, qty)
                                return ok
                    except Exception:
                        continue
                return False
            except Exception:
                logger.exception("update_inventory_on_return failed", exc_info=True)
                return False

        # -------------------------
        # Async wrappers
        # -------------------------
        async def async_connect(self) -> bool:
            return await asyncio.to_thread(self.connect)

        async def async_is_connected(self) -> bool:
            return await asyncio.to_thread(self.is_connected)

        async def async_get_worksheet_by_name(self, sheet_name: str):
            return await asyncio.to_thread(self._get_worksheet_by_name, sheet_name)

        async def async_ensure_headers(self, sheet_name: str, headers: List[str]) -> Dict[str, int]:
            return await asyncio.to_thread(self.ensure_headers, sheet_name, headers)

        async def async_get_all_records(self, sheet_name: str) -> List[Dict[str, Any]]:
            return await asyncio.to_thread(self.get_all_records, sheet_name)

        async def async_get_all_rows(self, sheet_name: str) -> List[Dict[str, Any]]:
            return await asyncio.to_thread(self.get_all_rows, sheet_name)

        async def async_append_row(self, sheet_name: str, row: List[Any]) -> bool:
            return await asyncio.to_thread(self.append_row, sheet_name, row)

        async def async_update_cell(self, sheet_name: str, row: int, col: int, value: Any) -> bool:
            return await asyncio.to_thread(self.update_cell, sheet_name, row, col, value)

        async def async_set_cell(self, sheet_name: str, row: int, col_or_name: Any, value: Any) -> bool:
            return await asyncio.to_thread(self.set_cell, sheet_name, row, col_or_name, value)

        async def async_update_row(self, sheet_name: str, row: int, data: Dict[str, Any]) -> bool:
            return await asyncio.to_thread(self.update_row, sheet_name, row, data)

        async def async_get_cell_value(self, sheet_name: str, row: int, col: int) -> str:
            return await asyncio.to_thread(self.get_cell_value, sheet_name, row, col)

        async def async_find_row_by_value(
            self, sheet_name: str, column_name: str, value: str, headers_map: Optional[Dict[str, int]] = None
        ) -> Optional[int]:
            return await asyncio.to_thread(self.find_row_by_value, sheet_name, column_name, value, headers_map)

        async def async_increment_cell(
            self, sheet_name: str, row: int, col: int, delta: int, max_retries: int = 3, retry_delay: float = 0.4
        ) -> Tuple[bool, Optional[str]]:
            return await asyncio.to_thread(self.increment_cell, sheet_name, row, col, delta, max_retries, retry_delay)

        async def async_write_log(self, user: str, action: str, item: str = "", note: str = "", sheet_name: str = "Log") -> bool:
            return await asyncio.to_thread(self.write_log, user, action, item, note, sheet_name)

        async def async_clear_sheet(self, sheet_name: str) -> bool:
            return await asyncio.to_thread(self.clear_sheet, sheet_name)

        async def async_get_unique_column_values(self, sheet_name: str, column_name_or_index) -> List[str]:
            return await asyncio.to_thread(self.get_unique_column_values, sheet_name, column_name_or_index)

        # async transaction helpers
        async def async_get_txn_by_id(self, txn_id: str) -> Optional[Dict[str, Any]]:
            return await asyncio.to_thread(self.get_txn_by_id, txn_id)

        async def async_get_user_borrows(self, user_id: int) -> List[Dict[str, Any]]:
            return await asyncio.to_thread(self.get_user_borrows, user_id)

        async def async_return_txn(self, txn_id: str, qty: int, returned_by: Optional[int] = None) -> bool:
            return await asyncio.to_thread(self.return_txn, txn_id, qty, returned_by)

        async def async_update_inventory_on_return(self, txn_id: str, qty: int) -> bool:
            return await asyncio.to_thread(self.update_inventory_on_return, txn_id, qty)


# If gspread not available, export NoopSheetsManager
if not GS_AVAILABLE:
    GoogleSheetsManager = NoopSheetsManager  # type: ignore


# -------------------------
# Diagnostics helper
# -------------------------
def diagnose(spreadsheet_id: Optional[str] = None, credentials_path: Optional[str] = None) -> Dict[str, str]:
    out: Dict[str, str] = {}
    out["GS_AVAILABLE"] = str(GS_AVAILABLE)
    out["GS_IMPORT_ERROR"] = repr(GS_IMPORT_ERROR) if GS_IMPORT_ERROR is not None else "None"
    if credentials_path:
        p = os.path.expanduser(credentials_path)
        out["creds_path"] = p
        out["creds_exists"] = str(os.path.exists(p))
    else:
        out["creds_path"] = "none"
        out["creds_exists"] = "False"
    out["spreadsheet_id"] = str(spreadsheet_id or "none")
    if GS_AVAILABLE:
        gsm = GoogleSheetsManager(spreadsheet_id=spreadsheet_id, credentials_path=credentials_path)
        try:
            ok = gsm.connect()
            out["connect"] = str(ok)
            if not ok:
                out["connect_note"] = "connect failed - check logs"
        except Exception as e:
            out["connect_exception"] = repr(e)
    else:
        out["note"] = "gspread import failed; see GS_IMPORT_ERROR"
    return out
