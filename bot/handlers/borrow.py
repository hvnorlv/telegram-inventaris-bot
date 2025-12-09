# handlers/borrow.py
"""
Handler untuk command /pinjam — ajukan permintaan peminjaman barang.

Tampilan daftar & detail barang sudah disesuaikan:
- Daftar barang & label hanya menampilkan: Nama | Merek (jika ada) | Witel | <angka tersedia>
- Detail item (view) hanya menampilkan: Nama, Merek (jika ada), Witel, Jumlah tersedia (angka)
- Serial number dan Divisi tidak ditampilkan di label / detail (tetap disimpan di sheet).
- Flow pending / konfirmasi tetap seperti sebelumnya.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List, Tuple

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery
from telegram.ext import (
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from handlers.common import send_md, escape_md, require_registration
from utils import sanitize_input, safe_int, make_txn_id
from config import (
    INVENTARIS_SHEET,
    PEMINJAMAN_SHEET,
    ITEM_CATEGORIES,
    QUANTITY_OPTIONS,
    ITEMS_PER_PAGE,
    MAX_BORROW_DAYS,
)

logger = logging.getLogger("handlers.borrow")
logger.addHandler(logging.NullHandler())

# callback prefixes
_PREFIX_CAT = "brw_cat:"
_PREFIX_ITEM = "brw_item:"  # callback_data will carry row index
_PREFIX_QTY = "brw_qty:"
_PREFIX_CANCEL = "brw_cancel:"
_PREFIX_VIEW = "cari_view:"
_PREFIX_CONFIRM = "brw_confirm:"
_PREFIX_CUSTOM_CONFIRM = "brw_custom_confirm:"
_PREFIX_SUBMIT = "brw_submit:"

# user_data keys
_KEY_STEP = "borrow_step"
_KEY_CATEGORY = "borrow_category"
_KEY_CHOICE = "borrow_choice"
_KEY_CHOICE_ROW = "borrow_choice_row"
_KEY_QTY = "borrow_qty"
_KEY_PENDING = "borrow_pending"
_KEY_CUSTOM_NAME = "borrow_custom_name"
_KEY_CUSTOM_SERIAL = "borrow_custom_serial"

# steps
_STEP_CATEGORY = "category_select"
_STEP_CHOOSE_ITEM = "choose_item"
_STEP_CONFIRM = "confirm_item"
_STEP_QTY = "quantity"
_STEP_QTY_CUSTOM = "quantity_custom"
_STEP_DEADLINE = "deadline"
_STEP_CUSTOM_NAME = "custom_name"
_STEP_CUSTOM_SERIAL = "custom_serial"
_STEP_CUSTOM_QTY = "custom_qty"
_STEP_CUSTOM_DEADLINE = "custom_deadline"

# retry helper
async def retry_async(
    fn,
    *args,
    retries: int = 3,
    delay: float = 0.8,
    backoff: float = 2.0,
    allowed_exceptions: tuple = (Exception,),
    **kwargs,
):
    last_exc = None
    d = delay
    for attempt in range(retries):
        try:
            return await fn(*args, **kwargs)
        except allowed_exceptions as e:
            last_exc = e
            if attempt + 1 >= retries:
                break
            logger.debug(
                "retry_async: %s attempt %d/%d — retrying after %.2fs",
                type(e).__name__,
                attempt + 1,
                retries,
                d,
            )
            try:
                await asyncio.sleep(d)
            except Exception:
                break
            d *= backoff
    raise last_exc


# sheet headers helpers (same as before)
async def _ensure_inventaris_headers(sheets) -> Dict[str, int]:
    return await sheets.async_ensure_headers(
        INVENTARIS_SHEET,
        [
            "Item ID",
            "Nama Barang",
            "Kategori",
            "Witel",
            "Divisi",
            "Total Qty",
            "Tersedia",
            "Status",
            "Pemilik ID",
            "Pemilik Nama",
            "Keterangan",
            "Keterangan 1",
            "Keterangan 2",
            "Keterangan 3",
            "Serial Number",
        ],
    )


async def _ensure_peminjaman_headers(sheets) -> Dict[str, int]:
    return await sheets.async_ensure_headers(
        PEMINJAMAN_SHEET,
        [
            "ID Transaksi",
            "Item ID",
            "Nama Barang",
            "Peminjam ID",
            "Peminjam Nama",
            "Qty Dipinjam",
            "Tanggal Pinjam",
            "Deadline",
            "Status Peminjaman",
            "Keterangan",
            "Reminder Sent",
            "ReturnRequestedAt",
            "ReturnRequestedBy",
            "ReturnConfirmedAt",
            "ReturnConfirmedBy",
            "ReturnNote",
        ],
    )


async def _ensure_users_headers(sheets) -> Dict[str, int]:
    return await sheets.async_ensure_headers(
        "Users",
        ["User ID", "Nama", "Role", "Witel", "Divisi"],
    )


async def _get_user_profile_witel_divisi(
    sheets,
    user_id: int,
) -> Tuple[str, str]:
    try:
        uh = await _ensure_users_headers(sheets)
        row_idx = await sheets.async_find_row_by_value(
            "Users",
            "User ID",
            str(user_id),
            headers_map=uh,
        )
        if not row_idx:
            return "", ""
        w = await sheets.async_get_cell_value("Users", row_idx, uh.get("Witel"))
        d = await sheets.async_get_cell_value("Users", row_idx, uh.get("Divisi"))
        return str(w or "").strip(), str(d or "").strip()
    except Exception:
        return "", ""


async def _find_item_row_by_id(
    sheets,
    item_id: str,
    headers_map: Optional[Dict[str, int]] = None,
) -> Optional[int]:
    if not item_id:
        return None
    headers_map = headers_map or await _ensure_inventaris_headers(sheets)
    if hasattr(sheets, "async_find_row_by_value"):
        try:
            row = await sheets.async_find_row_by_value(
                INVENTARIS_SHEET,
                "Item ID",
                item_id,
                headers_map=headers_map,
            )
            if row:
                return row
        except Exception:
            logger.debug("_find_item_row_by_id: helper failed; falling back", exc_info=True)
    try:
        recs = await sheets.async_get_all_records(INVENTARIS_SHEET)
    except Exception:
        logger.exception("_find_item_row_by_id: fallback scan failed")
        return None
    target = str(item_id).strip()
    for idx, r in enumerate(recs):
        try:
            if str(r.get("Item ID", "")).strip() == target:
                return idx + 2
        except Exception:
            continue
    return None


async def _find_item_row_by_name(
    sheets,
    name: str,
    headers_map: Optional[Dict[str, int]] = None,
) -> Optional[int]:
    headers_map = headers_map or await _ensure_inventaris_headers(sheets)

    if hasattr(sheets, "async_find_row_by_value"):
        try:
            row = await sheets.async_find_row_by_value(
                INVENTARIS_SHEET,
                "Nama Barang",
                name,
                headers_map=headers_map,
            )
            if row:
                return row
        except Exception:
            logger.debug("_find_item_row_by_name: async_find_row_by_value failed; falling back", exc_info=True)

    try:
        recs = await sheets.async_get_all_records(INVENTARIS_SHEET)
    except Exception:
        logger.exception("_find_item_row_by_name: fallback scan failed")
        return None

    target = str(name).strip().lower()
    best_row: Optional[int] = None

    for idx, r in enumerate(recs):
        status = str(r.get("Status", "") or "").strip().lower()
        if status == "removed":
            continue

        nm = str(r.get("Nama Barang", "")).strip()
        if not nm:
            continue
        nm_lc = nm.lower()
        if nm_lc == target:
            return idx + 2
        if target and target in nm_lc and best_row is None:
            best_row = idx + 2

    return best_row


async def _get_owner_for_item(
    sheets,
    row_idx: int,
    headers_map: Optional[Dict[str, int]] = None,
):
    headers_map = headers_map or await _ensure_inventaris_headers(sheets)
    col = headers_map.get("Pemilik ID")
    col_name = headers_map.get("Pemilik Nama")
    if not col:
        return None, None

    raw = await sheets.async_get_cell_value(INVENTARIS_SHEET, row_idx, col)
    raw_name = None
    if col_name:
        try:
            raw_name = await sheets.async_get_cell_value(
                INVENTARIS_SHEET,
                row_idx,
                col_name,
            )
        except Exception:
            raw_name = None
    try:
        return int(str(raw).strip()), raw_name
    except Exception:
        return None, raw_name


async def _get_available_stock(
    sheets,
    row_idx: int,
    inv_headers: Dict[str, int],
) -> int:
    if not row_idx or row_idx < 2:
        return 0
    try:
        st_col = inv_headers.get("Status")
        if st_col:
            st = await sheets.async_get_cell_value(
                INVENTARIS_SHEET,
                row_idx,
                st_col,
            )
            st_norm = str(st or "").strip().lower()
            if st_norm == "removed":
                return 0
    except Exception:
        pass

    ters_col = inv_headers.get("Tersedia")
    total_col = inv_headers.get("Total Qty")

    ters_val: Optional[int] = None
    total_val: Optional[int] = None

    if ters_col:
        try:
            v = await sheets.async_get_cell_value(
                INVENTARIS_SHEET,
                row_idx,
                ters_col,
            )
            ters_val = safe_int(v, -999_999)
        except Exception:
            ters_val = -999_999

    if total_col:
        try:
            v = await sheets.async_get_cell_value(
                INVENTARIS_SHEET,
                row_idx,
                total_col,
            )
            total_val = safe_int(v, 0)
        except Exception:
            total_val = 0

    if ters_val is not None and ters_val >= 0:
        return ters_val

    if total_val is not None:
        return max(0, total_val)

    return 0


def _parse_deadline_input(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    s = text.strip()
    if s.isdigit():
        days = int(s)
        if days <= 0:
            return None
        if MAX_BORROW_DAYS and days > MAX_BORROW_DAYS:
            return None
        return (datetime.now().date() + timedelta(days=days)).isoformat()
    try:
        dt = datetime.strptime(s, "%Y-%m-%d").date()
        if dt < datetime.now().date():
            return None
        if MAX_BORROW_DAYS and (dt - datetime.now().date()).days > MAX_BORROW_DAYS:
            return None
        return dt.isoformat()
    except Exception:
        return None


def _friendly_status_label(raw_status: Optional[str]) -> str:
    if not raw_status:
        return "—"
    s = str(raw_status).strip()
    sl = s.lower()
    if "partially" in sl:
        return "Sebagian Dikembalikan"
    if sl in ("disetujui", "dipinjam", "approved", "borrowed"):
        return "Dipinjam / Disetujui"
    return s


# build pending confirmation text (unchanged)
def _build_confirmation_text_and_kb_from_pending(pending: Dict[str, Any]) -> Tuple[str, InlineKeyboardMarkup]:
    meta = pending.get("meta", {})
    lines = []
    lines.append("📋 *Konfirmasi Permintaan Peminjaman*")
    lines.append("")
    lines.append(f"Barang: *{escape_md(str(meta.get('item_name') or '-'))}*")
    if meta.get("item_id"):
        lines.append(f"Item ID: `{escape_md(str(meta.get('item_id')) )}`")
    if meta.get("qty") is not None:
        lines.append(f"Qty: *{meta.get('qty')}*")
    if meta.get("deadline"):
        lines.append(f"Deadline: `{escape_md(str(meta.get('deadline')) )}`")
    if meta.get("witel"):
        lines.append(f"Witel: {escape_md(str(meta.get('witel')))}")
    # divisi intentionally left out of confirmation display as it may be internal
    if meta.get("note"):
        lines.append(f"Keterangan: {escape_md(str(meta.get('note')))}")

    lines.append("")
    lines.append("Tekan ✅ untuk *Konfirmasi & Kirim ke Pemilik*, atau ❌ untuk membatalkan.")

    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Konfirmasi & Kirim ke Pemilik", callback_data=f"{_PREFIX_SUBMIT}confirm")],
            [InlineKeyboardButton("❌ Batal", callback_data=f"{_PREFIX_SUBMIT}cancel")],
        ]
    )
    return ("\n".join(lines), kb)


# submit pending callback (confirm/cancel)
async def brw_submit_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    await cq.answer()

    if not await require_registration(cq, context):
        return

    data = cq.data or ""
    parts = data.split(":", 1)
    token = parts[1].strip() if len(parts) > 1 else ""
    pending = context.user_data.get(_KEY_PENDING)
    if not pending:
        await send_md(cq, "⚠️ Tidak ada permintaan peminjaman yang menunggu konfirmasi. Mulai ulang dengan /pinjam.")
        return

    sheets = context.application.bot_data.get("sheets_manager")
    if not sheets:
        await send_md(cq, "❌ Layanan Google Sheets belum tersedia.")
        return

    user = cq.from_user
    if token == "cancel":
        context.user_data.pop(_KEY_PENDING, None)
        await send_md(cq, "✅ Permintaan dibatalkan.")
        return

    if token == "confirm":
        try:
            pem_map = await _ensure_peminjaman_headers(sheets)
            row_template = pending.get("row") or []
            max_col = max(pem_map.values()) if pem_map else (len(row_template) or 10)
            row = list(row_template) + [""] * max(0, max_col - len(row_template))
            ok = await sheets.async_append_row(PEMINJAMAN_SHEET, row)
            if not ok:
                await send_md(cq, "❌ Gagal membuat permintaan pada sheet. Coba lagi nanti.")
                return

            try:
                await sheets.async_write_log(
                    str(user.id),
                    "AjukanPinjam",
                    pending.get("meta", {}).get("item_name", ""),
                    f"tx={pending.get('meta', {}).get('txn','')} qty={pending.get('meta',{}).get('qty','')}"
                )
            except Exception:
                pass

            try:
                await _notify_owner_for_request(
                    context,
                    sheets,
                    pending.get("meta", {}).get("item_name", ""),
                    pending.get("meta", {}).get("txn", ""),
                    pending.get("meta", {}).get("qty", 0),
                    borrower_id=user.id,
                    borrower_name=user.first_name or user.full_name or "",
                    witel=pending.get("meta", {}).get("witel", ""),
                    divisi=pending.get("meta", {}).get("divisi", ""),
                    item_id=pending.get("meta", {}).get("item_id", ""),
                )
            except Exception:
                logger.exception("Failed notify owner after append", exc_info=True)

            await send_md(cq, f"✅ Permintaan peminjaman (TXN `{pending.get('meta',{}).get('txn','')}`) telah dikirim dan pemilik diberitahu.", parse_mode="Markdown")
        except Exception:
            logger.exception("brw_submit_cb: finalize failed", exc_info=True)
            await send_md(cq, "❌ Terjadi kesalahan saat memproses permintaan. Coba lagi.")
        finally:
            context.user_data.pop(_KEY_PENDING, None)
        return

    await send_md(cq, "Aksi tidak dikenali.")


# /pinjam command (quick + interactive)
async def pinjam_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(
        "borrow: /pinjam called by user=%s args=%s",
        getattr(update.effective_user, "id", None),
        context.args,
    )

    user = update.effective_user
    if not user:
        await send_md(update, "⚠️ Tidak dapat mengenali pengguna.")
        return

    if not await require_registration(update, context):
        return

    sheets = context.application.bot_data.get("sheets_manager")

    # Quick-mode handling (including custom quick)
    if context.args and len(context.args) >= 2:
        args = context.args[:]
        first = args[0].strip().lower()

        # quick custom: /pinjam custom <Nama Item> <qty> [deadline]
        if first == "custom":
            if len(args) < 3:
                await send_md(
                    update,
                    "Gunakan: `/pinjam custom <Nama Item> <qty> [deadline]`",
                    parse_mode="Markdown",
                )
                return

            maybe_deadline = args[-1]
            maybe_qty = safe_int(args[-2], -1) if len(args) >= 3 else -1

            if _parse_deadline_input(maybe_deadline) and maybe_qty > 0:
                name = sanitize_input(" ".join(args[1:-2]))
                qty = maybe_qty
                deadline_raw = maybe_deadline
            else:
                name = sanitize_input(" ".join(args[1:-1]))
                qty = safe_int(args[-1], -1)
                deadline_raw = None

            if qty <= 0:
                await send_md(update, "⚠️ Quantity harus angka lebih dari 0.")
                return

            if not sheets:
                await send_md(update, "❌ Layanan Google Sheets belum tersedia.")
                return

            txn = make_txn_id("TXN")
            today = datetime.now().strftime("%Y-%m-%d")
            pem_map = await _ensure_peminjaman_headers(sheets)
            max_col = max(pem_map.values()) if pem_map else 10
            row = [""] * max_col
            row[pem_map["ID Transaksi"] - 1] = txn
            row[pem_map["Item ID"] - 1] = ""  # custom
            row[pem_map["Nama Barang"] - 1] = f"CUSTOM: {name}"
            row[pem_map["Peminjam ID"] - 1] = str(user.id)
            row[pem_map["Peminjam Nama"] - 1] = (
                user.first_name or user.full_name or ""
            )
            row[pem_map["Qty Dipinjam"] - 1] = str(qty)
            row[pem_map["Tanggal Pinjam"] - 1] = today
            row[pem_map["Deadline"] - 1] = (
                _parse_deadline_input(deadline_raw) if deadline_raw else ""
            )
            row[pem_map["Status Peminjaman"] - 1] = "Menunggu Persetujuan"
            row[pem_map["Keterangan"] - 1] = (
                f"CustomRequest by {user.first_name or user.id}"
            )

            context.user_data[_KEY_PENDING] = {
                "row": row,
                "meta": {
                    "txn": txn,
                    "item_name": f"CUSTOM: {name}",
                    "item_id": "",
                    "qty": qty,
                    "deadline": _parse_deadline_input(deadline_raw) if deadline_raw else "",
                    "witel": "",
                    "divisi": "",
                    "note": f"CustomRequest by {user.first_name or user.id}",
                },
            }
            text, kb = _build_confirmation_text_and_kb_from_pending(context.user_data[_KEY_PENDING])
            await send_md(update, text, parse_mode="Markdown", reply_markup=kb)
            return

        # standard quick-mode parse
        deadline_raw = None
        qty = None
        if len(args) >= 3:
            if _parse_deadline_input(args[-1]) and safe_int(args[-2], -1) > 0:
                deadline_raw = args[-1]
                qty = safe_int(args[-2], -1)
                name = sanitize_input(" ".join(args[:-2]))
            else:
                qty = safe_int(args[-1], -1)
                name = sanitize_input(" ".join(args[:-1]))
        else:
            qty = safe_int(args[-1], -1)
            name = sanitize_input(" ".join(args[:-1]))

        if qty <= 0:
            await send_md(update, "⚠️ Quantity harus angka lebih dari 0.")
            return

        if not sheets:
            await send_md(update, "❌ Layanan Google Sheets belum tersedia.")
            return

        inv_headers = await _ensure_inventaris_headers(sheets)
        row_idx = await _find_item_row_by_name(sheets, name, headers_map=inv_headers)
        if not row_idx:
            await send_md(
                update,
                f"❌ Barang *{escape_md(name)}* tidak ditemukan di inventaris.",
                parse_mode="Markdown",
            )
            return

        recs = await sheets.async_get_all_records(INVENTARIS_SHEET)
        rec = recs[row_idx - 2] if 0 <= (row_idx - 2) < len(recs) else {}
        item_id = str(rec.get("Item ID") or "").strip()
        owner_id, _ = await _get_owner_for_item(sheets, row_idx, inv_headers)
        if owner_id and owner_id == user.id:
            await send_md(
                update,
                "🚫 Kamu tercatat sebagai pemilik barang ini — tidak dapat meminjam barang milik sendiri.",
            )
            return

        available = await _get_available_stock(sheets, row_idx, inv_headers)
        if available <= 0:
            await send_md(
                update,
                f"⚠️ Stok untuk *{escape_md(name)}* saat ini: {available}. Tidak dapat meminjam sekarang.",
                parse_mode="Markdown",
            )
            return
        if qty > available:
            await send_md(
                update,
                f"⚠️ Stok tidak cukup — tersedia: *{available}* unit untuk *{escape_md(name)}*.",
                parse_mode="Markdown",
            )
            return

        witel, divisi = await _get_user_profile_witel_divisi(sheets, user.id)
        deadline_iso = _parse_deadline_input(deadline_raw) if deadline_raw else ""
        txn = make_txn_id("TXN")
        today = datetime.now().strftime("%Y-%m-%d")
        pem_map = await _ensure_peminjaman_headers(sheets)
        max_col = max(pem_map.values()) if pem_map else 10
        row = [""] * max_col
        row[pem_map["ID Transaksi"] - 1] = txn
        row[pem_map["Item ID"] - 1] = item_id
        row[pem_map["Nama Barang"] - 1] = name
        row[pem_map["Peminjam ID"] - 1] = str(user.id)
        row[pem_map["Peminjam Nama"] - 1] = (
            user.first_name or user.full_name or ""
        )
        row[pem_map["Qty Dipinjam"] - 1] = str(qty)
        row[pem_map["Tanggal Pinjam"] - 1] = today
        row[pem_map["Deadline"] - 1] = deadline_iso or ""
        row[pem_map["Status Peminjaman"] - 1] = "Menunggu Persetujuan"
        row[pem_map["Keterangan"] - 1] = (
            f"Request by {user.first_name or user.id} | Witel:{witel} | Divisi:{divisi}"
        )

        context.user_data[_KEY_PENDING] = {
            "row": row,
            "meta": {
                "txn": txn,
                "item_name": name,
                "item_id": item_id,
                "qty": qty,
                "deadline": deadline_iso or "",
                "witel": witel,
                "divisi": divisi,
                "note": row[pem_map["Keterangan"] - 1],
            },
        }
        text, kb = _build_confirmation_text_and_kb_from_pending(context.user_data[_KEY_PENDING])
        await send_md(update, text, parse_mode="Markdown", reply_markup=kb)
        return

    # Interactive mode (category selection)
    for k in (
        _KEY_STEP,
        _KEY_CATEGORY,
        _KEY_CHOICE,
        _KEY_CHOICE_ROW,
        _KEY_QTY,
        _KEY_CUSTOM_NAME,
        _KEY_CUSTOM_SERIAL,
    ):
        context.user_data.pop(k, None)

    context.user_data[_KEY_STEP] = _STEP_CATEGORY

    kb = [
        [InlineKeyboardButton(cat, callback_data=f"{_PREFIX_CAT}{cat}")]
        for cat in ITEM_CATEGORIES
    ]
    if "Custom" not in ITEM_CATEGORIES:
        kb.append(
            [
                InlineKeyboardButton(
                    "Custom (ajukan barang baru)",
                    callback_data=f"{_PREFIX_CAT}Custom",
                )
            ]
        )
    kb.append([InlineKeyboardButton("Batal", callback_data=f"{_PREFIX_CANCEL}back")])
    await send_md(
        update,
        "📂 Pilih kategori barang:",
        reply_markup=InlineKeyboardMarkup(kb),
    )


# cari command (search)
async def cari_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_registration(update, context):
        return

    sheets = context.application.bot_data.get("sheets_manager")
    if not sheets:
        await send_md(update, "❌ Layanan Google Sheets belum tersedia.")
        return

    args = context.args or []
    if not args:
        await send_md(
            update,
            "🔎 Gunakan: `/cari <kata-kunci>` atau `/cari <kategori> <kata-kunci>`",
            parse_mode="Markdown",
        )
        return

    first = args[0].strip()
    category_filter = None
    keyword = " ".join(args).strip()
    for cat in ITEM_CATEGORIES:
        if first.lower() == str(cat).strip().lower():
            category_filter = cat
            keyword = " ".join(args[1:]).strip() if len(args) > 1 else ""
            break

    try:
        recs = await retry_async(
            sheets.async_get_all_records,
            INVENTARIS_SHEET,
            retries=3,
            delay=0.5,
        )
    except Exception:
        await send_md(
            update,
            "❌ Gagal mengambil data inventaris untuk pencarian.",
        )
        return

    matches: List[Tuple[int, Dict[str, Any]]] = []
    kw = keyword.lower().strip()
    for idx, r in enumerate(recs):
        try:
            status = str(r.get("Status", "") or "").strip().lower()
            if status == "removed":
                continue
            # compute available using Tersedia or Total Qty
            available = safe_int(r.get("Tersedia") or r.get("Total Qty") or 0, 0)
            # SKIP items with zero or negative available stock
            if available <= 0:
                continue
            if category_filter:
                if str(r.get("Kategori", "")).strip().lower() != str(category_filter).strip().lower():
                    continue
            name = str(r.get("Nama Barang", "") or "")
            # compact hay: include name + merk + keterangan1 (no serial)
            k1 = str(r.get("Keterangan 1", "") or "")
            brand = str(r.get("Merek", "") or "")
            hay = " ".join([name, k1, brand]).lower()
            if not kw or kw in hay:
                matches.append((idx + 2, r))
        except Exception:
            continue

    if not matches:
        if category_filter and kw:
            await send_md(
                update,
                f"🔎 Tidak ditemukan item kategori *{escape_md(category_filter)}* dengan kata kunci `{escape_md(keyword)}`.",
                parse_mode="Markdown",
            )
        elif category_filter:
            await send_md(
                update,
                f"🔎 Tidak ditemukan item tersedia pada kategori *{escape_md(category_filter)}*.",
                parse_mode="Markdown",
            )
        else:
            await send_md(
                update,
                f"🔎 Tidak ditemukan item untuk kata kunci `{escape_md(keyword)}`.",
                parse_mode="Markdown",
            )
        return

    max_results = 30
    kb = []
    for i, (rownum, rec) in enumerate(matches[:max_results]):
        name = rec.get("Nama Barang") or "-"
        # build compact specs (Merek | Keterangan 1) but exclude Serial
        specs_parts = []
        for k in ("Merek", "Keterangan 1", "Keterangan 2", "Keterangan"):
            v = rec.get(k)
            if v:
                specs_parts.append(str(v))
        specs_txt = " | ".join(specs_parts) if specs_parts else ""
        available = safe_int(rec.get("Tersedia") or rec.get("Total Qty") or 0, 0)
        # label: "Nama — specs — Witel — <available as number>"
        parts = [str(name)]
        if specs_txt:
            parts.append(specs_txt)
        witel = rec.get("Witel") or rec.get("Witel Sekarang") or ""
        if witel:
            parts.append(str(witel))
        parts.append(str(available))
        label = " — ".join(parts)
        if len(label) > 64:
            label = label[:61] + "..."
        # callback will carry row index so detail -> borrow is accurate
        cb_val = str(rownum)
        kb.append([InlineKeyboardButton(label, callback_data=f"{_PREFIX_VIEW}{cb_val}")])

    if len(matches) > max_results:
        kb.append([InlineKeyboardButton(f"Menampilkan {max_results} dari {len(matches)} hasil — refine pencarian", callback_data="cari_refine")])
    kb.append([InlineKeyboardButton("Tutup", callback_data=f"{_PREFIX_CANCEL}close")])
    await send_md(
        update,
        (
            f"🔎 Hasil pencarian untuk `{escape_md(keyword)}` — total *{len(matches)}* (menampilkan {min(len(matches), max_results)})."
        ),
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown",
    )


# detail view for cari (modified: only show name, brand, witel, available)
async def cari_view_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    await cq.answer()

    if not await require_registration(cq, context):
        return

    data = cq.data or ""
    payload = data.split(":", 1)[1].strip() if ":" in data else data
    sheets = context.application.bot_data.get("sheets_manager")
    if not sheets:
        await send_md(cq, "❌ Layanan Google Sheets belum tersedia.")
        return

    inv_headers = await _ensure_inventaris_headers(sheets)

    # payload is expected to be a row index (we set that in cari_command)
    row_idx = None
    try:
        row_idx = int(payload)
    except Exception:
        row_idx = None

    if not row_idx:
        # fallback: try find by id/name
        row_idx = await _find_item_row_by_id(sheets, payload, headers_map=inv_headers)
        if not row_idx:
            row_idx = await _find_item_row_by_name(sheets, payload, headers_map=inv_headers)

    if not row_idx:
        await send_md(cq, f"❌ Barang *{escape_md(payload)}* tidak ditemukan.", parse_mode="Markdown")
        return

    try:
        recs = await sheets.async_get_all_records(INVENTARIS_SHEET)
        rec = recs[row_idx - 2] if 0 <= (row_idx - 2) < len(recs) else {}
    except Exception:
        rec = {}

    # Build detailed text similarly to inventory view
    name = rec.get("Nama Barang") or "-"
    item_id = rec.get("Item ID") or "-"
    available = await _get_available_stock(sheets, row_idx, inv_headers)
    kategori = rec.get("Kategori") or "-"
    # prefer 'Witel Asal' or 'Witel'
    sumber_witel = rec.get("Witel Asal") or rec.get("Witel") or ""
    sumber_div = rec.get("Divisi Asal") or rec.get("Divisi") or ""
    current_witel = rec.get("Witel Sekarang") or rec.get("Witel") or ""
    current_div = rec.get("Divisi Sekarang") or rec.get("Divisi") or ""
    pemilik_nama = rec.get("Pemilik Nama") or "-"
    ket = rec.get("Keterangan") or "-"
    k1 = rec.get("Keterangan 1") or "-"
    k2 = rec.get("Keterangan 2") or "-"
    k3 = rec.get("Keterangan 3") or "-"
    serial = rec.get("Serial Number") or "-"
    merek = rec.get("Merek") or "-"

    teks_lines = [
        "📄 *Detail Barang*",
        "",
        f"Nama: *{escape_md(str(name))}*",
        f"Item ID: `{escape_md(str(item_id))}`",
    ]
    if kategori:
        teks_lines.append(f"Kategori: {escape_md(str(kategori))}")
    # specs block
    specs = []
    if merek and merek != "-":
        specs.append(escape_md(str(merek)))
    if k1 and k1 != "-":
        specs.append(escape_md(str(k1)))
    if k2 and k2 != "-":
        specs.append(escape_md(str(k2)))
    if serial and serial != "-":
        specs.append(escape_md(str(serial)))
    if specs:
        teks_lines.append("Specs: `" + " | ".join(specs) + "`")

    teks_lines.append(f"Tersedia: *{escape_md(str(available))}*")
    if sumber_witel or sumber_div:
        teks_lines.append(f"Sumber Barang: {escape_md(str(sumber_witel or '-'))} — {escape_md(str(sumber_div or '-'))}")
    teks_lines.append(f"Witel Sekarang: {escape_md(str(current_witel or '-'))} — Divisi Sekarang: {escape_md(str(current_div or '-'))}")
    teks_lines.append(f"Pemilik: {escape_md(str(pemilik_nama))}")
    if ket and ket != "-":
        teks_lines.append(f"Keterangan: {escape_md(str(ket))}")

    teks = "\n".join(teks_lines)

    kb = [
        [InlineKeyboardButton("📥 Pinjam barang ini", callback_data=f"{_PREFIX_ITEM}{row_idx}")],
        [InlineKeyboardButton("🔙 Kembali", callback_data="cari_refine")],
        [InlineKeyboardButton("Tutup", callback_data=f"{_PREFIX_CANCEL}close")],
    ]

    try:
        await cq.edit_message_text(teks, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))
    except Exception:
        await send_md(cq, teks, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))


# borrow_category_cb: list items but show only name, brand, witel, available(number)
async def borrow_category_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    await cq.answer()

    if not await require_registration(cq, context):
        return

    data = cq.data or ""
    if ":" in data:
        cat = data.split(":", 1)[1].strip()
    else:
        cat = data

    context.user_data[_KEY_CATEGORY] = cat
    context.user_data[_KEY_STEP] = _STEP_CHOOSE_ITEM

    sheets = context.application.bot_data.get("sheets_manager")
    if not sheets:
        await send_md(cq, "❌ Layanan Google Sheets belum tersedia.")
        return

    try:
        rows = await sheets.async_get_all_records(INVENTARIS_SHEET)
    except Exception:
        await send_md(cq, "❌ Gagal mengambil data inventaris.")
        return

    matched = [
        (idx + 2, r)
        for idx, r in enumerate(rows)
        if str(r.get("Kategori", "")).strip().lower() == str(cat).strip().lower()
    ]

    if matched:
        kb = []
        per_page = int(ITEMS_PER_PAGE or 8)
        count_shown = 0

        # ensure headers once so _get_available_stock can use them
        try:
            inv_headers = await _ensure_inventaris_headers(sheets)
        except Exception:
            inv_headers = None

        for rownum, rec in matched:
            if count_shown >= per_page:
                break

            # use central helper to compute availability (respects Status/Tersedia/Total Qty)
            try:
                available = await _get_available_stock(sheets, rownum, inv_headers) if inv_headers else safe_int(rec.get("Tersedia") or rec.get("Total Qty") or 0, 0)
            except Exception:
                available = safe_int(rec.get("Tersedia") or rec.get("Total Qty") or 0, 0)

            # skip items with zero/negative available
            if available <= 0:
                continue

            name = rec.get("Nama Barang") or "-"
            brand = rec.get("Keterangan 1") or rec.get("Keterangan") or ""
            witel = rec.get("Witel") or ""

            # label: Name [• Brand] [• Witel] — <available number>
            parts = [str(name)]
            if brand:
                parts.append(str(brand))
            if witel:
                parts.append(str(witel))
            label = " • ".join(parts) + f" — {available}"
            if len(label) > 64:
                label = label[:61] + "..."
            kb.append([InlineKeyboardButton(label, callback_data=f"{_PREFIX_ITEM}{rownum}")])
            count_shown += 1

        if str(cat).strip().lower() == "custom":
            kb.append([InlineKeyboardButton("➕ Ajukan barang baru (Custom)", callback_data="brw_custom:start")])

        if not kb:
            try:
                await cq.edit_message_text(f"ℹ️ Kategori *{escape_md(cat)}* — tidak ada item tersedia saat ini.", parse_mode="Markdown")
            except Exception:
                await send_md(cq, f"ℹ️ Kategori {cat} — tidak ada item tersedia saat ini.")
            return

        kb.append([InlineKeyboardButton("🔙 Batal", callback_data=f"{_PREFIX_CANCEL}back")])
        try:
            await cq.edit_message_text(f"📂 Kategori: *{escape_md(cat)}* — pilih item:", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))
        except Exception:
            await send_md(cq, f"📂 Pilih item di kategori {cat}:", reply_markup=InlineKeyboardMarkup(kb))
        return

    # category = custom but no matched items in inventory
    if str(cat).strip().lower() == "custom":
        kb = [
            [InlineKeyboardButton("➕ Ajukan barang baru (Custom)", callback_data="brw_custom:start")],
            [InlineKeyboardButton("🔙 Batal", callback_data=f"{_PREFIX_CANCEL}back")],
        ]
        try:
            await cq.edit_message_text(f"ℹ️ Kategori *{escape_md(cat)}* — belum ada item di inventaris. Kamu bisa mengajukan barang baru:", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))
        except Exception:
            await send_md(cq, f"ℹ️ Kategori {cat} — belum ada item di inventaris.", reply_markup=InlineKeyboardMarkup(kb))
        return

    try:
        await cq.edit_message_text(f"ℹ️ Kategori *{escape_md(cat)}* — tidak ada item di inventaris.", parse_mode="Markdown")
    except Exception:
        await send_md(cq, f"ℹ️ Kategori {cat} — tidak ada item di inventaris.")
    return



# borrow_item_cb: when user chooses an item -> show simplified detail and ask to confirm
async def borrow_item_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    await cq.answer()

    if not await require_registration(cq, context):
        return

    data = cq.data or ""
    payload = data.split(":", 1)[1].strip() if ":" in data else data
    sheets = context.application.bot_data.get("sheets_manager")

    row_idx = None
    inv_headers = None
    if payload.isdigit():
        try:
            row_idx = int(payload)
        except Exception:
            row_idx = None

    if sheets:
        inv_headers = await _ensure_inventaris_headers(sheets)
        if not row_idx:
            row_idx = await _find_item_row_by_id(sheets, payload, headers_map=inv_headers)
        if not row_idx:
            row_idx = await _find_item_row_by_name(sheets, payload, headers_map=inv_headers)

    if not row_idx:
        await send_md(cq, f"❌ Barang *{escape_md(payload)}* tidak ditemukan di inventaris.", parse_mode="Markdown")
        return

    try:
        recs = await sheets.async_get_all_records(INVENTARIS_SHEET)
        rec = recs[row_idx - 2] if 0 <= (row_idx - 2) < len(recs) else {}
    except Exception:
        rec = {}

    item_id = str(rec.get("Item ID") or "").strip()
    item_name = rec.get("Nama Barang") or "-"
    context.user_data[_KEY_CHOICE] = item_name
    context.user_data[_KEY_CHOICE_ROW] = row_idx
    context.user_data[_KEY_STEP] = _STEP_CONFIRM

    available: Optional[int] = None
    brand: Optional[str] = None
    witel = ""

    if sheets:
        try:
            available = await _get_available_stock(sheets, row_idx, inv_headers)
            brand = rec.get("Keterangan 1") or rec.get("Keterangan") or ""
            witel = rec.get("Witel", "") or ""
        except Exception:
            available = None

    if available is not None and available <= 0:
        await send_md(cq, f"⚠️ Stok untuk *{escape_md(item_name)}* saat ini: *0*. Tidak dapat meminjam sekarang.", parse_mode="Markdown")
        context.user_data.pop(_KEY_CHOICE, None)
        context.user_data.pop(_KEY_CHOICE_ROW, None)
        context.user_data.pop(_KEY_STEP, None)
        return

    header_lines = ["📋 *Konfirmasi Item*", "", f"Barang: *{escape_md(item_name)}*"]
    if brand:
        header_lines.append(f"Merek: {escape_md(str(brand))}")
    if witel:
        header_lines.append(f"Witel: {escape_md(str(witel))}")
    if available is not None:
        header_lines.append(f"Tersedia: *{available}*")
    header_lines.append("")
    header_lines.append("Apakah ini barang yang ingin kamu pinjam?")

    header = "\n".join(header_lines)

    kb = [
        [InlineKeyboardButton("✅ Ya, benar — Lanjut ke Qty", callback_data=f"{_PREFIX_CONFIRM}yes")],
        [InlineKeyboardButton("❌ Batal", callback_data=f"{_PREFIX_CANCEL}back")],
    ]
    try:
        await cq.edit_message_text(header, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))
    except Exception:
        await send_md(cq, header, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))


# brw_confirm_item_cb -> qty selection (unchanged logic)
async def brw_confirm_item_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    await cq.answer()

    if not await require_registration(cq, context):
        return

    data = (cq.data or "").split(":", 1)
    if len(data) < 2:
        await send_md(cq, "Aksi konfirmasi tidak dikenali.")
        return
    _, token = data
    token = token.strip().lower()

    item_name = context.user_data.get(_KEY_CHOICE)
    if not item_name:
        row_idx = context.user_data.get(_KEY_CHOICE_ROW)
        sheets = context.application.bot_data.get("sheets_manager")
        if row_idx and sheets:
            try:
                recs = await sheets.async_get_all_records(INVENTARIS_SHEET)
                rec = recs[row_idx - 2] if 0 <= (row_idx - 2) < len(recs) else {}
                item_name = rec.get("Nama Barang")
                context.user_data[_KEY_CHOICE] = item_name
            except Exception:
                item_name = None

    if not item_name:
        await send_md(cq, "❌ Tidak ada item yang dipilih. Mulai ulang dengan /pinjam.")
        return

    if token == "yes":
        context.user_data[_KEY_STEP] = _STEP_QTY
        kb = [[InlineKeyboardButton(str(q), callback_data=f"{_PREFIX_QTY}{q}")] for q in QUANTITY_OPTIONS[:6]]
        kb.append([InlineKeyboardButton("Custom", callback_data=f"{_PREFIX_QTY}custom")])
        kb.append([InlineKeyboardButton("🔙 Batal", callback_data=f"{_PREFIX_CANCEL}back")])
        header = f"Barang: *{escape_md(str(item_name))}*\n\nPilih quantity yang ingin dipinjam atau ketik Custom:"
        try:
            await cq.edit_message_text(header, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))
        except Exception:
            await send_md(cq, header, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))
        return

    await send_md(cq, "Aksi konfirmasi tidak dikenali.")


# borrow_qty_cb -> handle qty selection
async def borrow_qty_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    await cq.answer()

    if not await require_registration(cq, context):
        return

    data = cq.data or ""
    token = data.split(":", 1)[1].strip() if ":" in data else data

    if token == "custom":
        context.user_data[_KEY_STEP] = _STEP_QTY_CUSTOM
        try:
            await cq.edit_message_text("💬 Ketik jumlah yang ingin dipinjam (angka):")
        except Exception:
            await send_md(cq, "💬 Ketik jumlah yang ingin dipinjam (angka):")
        return

    qty = safe_int(token, 0)
    if qty <= 0:
        try:
            await cq.edit_message_text("⚠️ Quantity tidak valid.")
        except Exception:
            await send_md(cq, "⚠️ Quantity tidak valid.")
        return

    context.user_data[_KEY_QTY] = qty
    context.user_data[_KEY_STEP] = _STEP_DEADLINE
    await send_md(cq, "📅 Masukkan deadline (YYYY-MM-DD) atau jumlah hari (mis. `7`):")


# Cancel callback
async def borrow_cancel_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    await cq.answer()

    if not await require_registration(cq, context):
        return

    for k in (
        _KEY_STEP,
        _KEY_CATEGORY,
        _KEY_CHOICE,
        _KEY_CHOICE_ROW,
        _KEY_QTY,
        _KEY_CUSTOM_NAME,
        _KEY_CUSTOM_SERIAL,
        _KEY_PENDING,
    ):
        context.user_data.pop(k, None)

    # coba hapus pesan UI agar 'chat' hilang — fallback ke edit text bila gagal
    try:
        await cq.message.delete()
    except Exception:
        try:
            await cq.edit_message_text("✅ Proses peminjaman dibatalkan.")
        except Exception:
            pass


# handle text messages during flows (custom + deadline etc.)
async def handle_borrow_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    step = context.user_data.get(_KEY_STEP)
    if not step:
        return False

    text_raw = update.message.text or ""
    text = sanitize_input(text_raw)
    sheets = context.application.bot_data.get("sheets_manager")
    user = update.effective_user

    # Custom flow: name -> serial -> qty -> deadline -> pending
    if step == _STEP_CUSTOM_NAME:
        name = (text_raw or "").strip()
        if not name:
            await send_md(update, "⚠️ Nama barang tidak boleh kosong. Ketik nama barang:")
            return True
        context.user_data[_KEY_CUSTOM_NAME] = name
        context.user_data[_KEY_STEP] = _STEP_CUSTOM_SERIAL
        await send_md(update, "🔢 Jika ada Serial Number / Identifier, ketik sekarang. Jika tidak ada, ketik `-` :", parse_mode="Markdown")
        return True

    if step == _STEP_CUSTOM_SERIAL:
        serial = (text_raw or "").strip()
        if not serial:
            await send_md(update, "⚠️ Isi serial atau ketik `-` jika tidak ada.")
            return True
        context.user_data[_KEY_CUSTOM_SERIAL] = serial if serial != "-" else ""
        context.user_data[_KEY_STEP] = _STEP_CUSTOM_QTY
        await send_md(update, "💬 Masukkan jumlah yang ingin dipinjam (angka):")
        return True

    if step == _STEP_CUSTOM_QTY:
        qty = safe_int(text_raw, -1)
        if qty <= 0:
            await send_md(update, "⚠️ Quantity harus angka > 0. Masukkan jumlah yang ingin dipinjam:")
            return True
        context.user_data[_KEY_QTY] = qty
        context.user_data[_KEY_STEP] = _STEP_CUSTOM_DEADLINE
        await send_md(update, (f"📅 Masukkan deadline (YYYY-MM-DD) atau jumlah hari (mis. `7`) (maks {MAX_BORROW_DAYS} hari):"))
        return True

    if step == _STEP_CUSTOM_DEADLINE:
        dl_iso = _parse_deadline_input(text_raw)
        if not dl_iso:
            await send_md(update, (f"⚠️ Deadline tidak valid atau melebihi batas maksimal ({MAX_BORROW_DAYS} hari). Coba lagi."))
            return True

        name = context.user_data.get(_KEY_CUSTOM_NAME)
        serial = context.user_data.get(_KEY_CUSTOM_SERIAL) or "-"
        qty = context.user_data.get(_KEY_QTY, 1)
        txn = make_txn_id("TXN")
        today = datetime.now().strftime("%Y-%m-%d")

        if not sheets:
            await send_md(update, "❌ Layanan Google Sheets belum tersedia.")
            return True

        pem_map = await _ensure_peminjaman_headers(sheets)
        max_col = max(pem_map.values()) if pem_map else 10
        row = [""] * max_col
        row[pem_map["ID Transaksi"] - 1] = txn
        row[pem_map["Item ID"] - 1] = ""  # custom
        row[pem_map["Nama Barang"] - 1] = f"CUSTOM: {name}"
        row[pem_map["Peminjam ID"] - 1] = str(user.id)
        row[pem_map["Peminjam Nama"] - 1] = user.first_name or user.full_name or ""
        row[pem_map["Qty Dipinjam"] - 1] = str(qty)
        row[pem_map["Tanggal Pinjam"] - 1] = today
        row[pem_map["Deadline"] - 1] = dl_iso
        row[pem_map["Status Peminjaman"] - 1] = "Menunggu Persetujuan"
        note = f"CustomRequest by {user.first_name or user.id}"
        if serial:
            note = f"{note} | Serial:{serial}"
        row[pem_map["Keterangan"] - 1] = note

        context.user_data[_KEY_PENDING] = {
            "row": row,
            "meta": {
                "txn": txn,
                "item_name": f"CUSTOM: {name}",
                "item_id": "",
                "qty": qty,
                "deadline": dl_iso,
                "witel": "",
                "divisi": "",
                "note": note,
            },
        }
        text, kb = _build_confirmation_text_and_kb_from_pending(context.user_data[_KEY_PENDING])
        await send_md(update, text, parse_mode="Markdown", reply_markup=kb)

        for k in (_KEY_STEP, _KEY_CUSTOM_NAME, _KEY_CUSTOM_SERIAL, _KEY_QTY):
            context.user_data.pop(k, None)
        return True

    # Qty custom typed (regular inventory item)
    if step == _STEP_QTY_CUSTOM:
        qty = safe_int(text, 0)
        if qty <= 0:
            await send_md(update, "⚠️ Quantity harus angka > 0.")
            return True
        context.user_data[_KEY_QTY] = qty
        context.user_data[_KEY_STEP] = _STEP_DEADLINE
        await send_md(update, "📅 Masukkan deadline (YYYY-MM-DD) atau jumlah hari (mis. `7`):")
        return True

    # Deadline -> finalize (regular inventory item)
    if step == _STEP_DEADLINE:
        dl_iso = _parse_deadline_input(text)
        if not dl_iso:
            await send_md(update, (f"⚠️ Deadline tidak valid atau melebihi batas maksimal ({MAX_BORROW_DAYS} hari). Coba lagi."))
            return True

        item_display_name = context.user_data.get(_KEY_CHOICE)
        qty = context.user_data.get(_KEY_QTY, 1)
        if not item_display_name:
            await send_md(update, "❌ Item belum dipilih. Mulai ulang dengan /pinjam.")
            for k in (_KEY_STEP, _KEY_CATEGORY, _KEY_CHOICE, _KEY_CHOICE_ROW, _KEY_QTY):
                context.user_data.pop(k, None)
            return True

        if not sheets:
            await send_md(update, "❌ Layanan Google Sheets tidak tersedia.")
            return True

        inv_headers = await _ensure_inventaris_headers(sheets)

        row_idx = context.user_data.get(_KEY_CHOICE_ROW)
        if not row_idx:
            row_idx = await _find_item_row_by_id(sheets, item_display_name, headers_map=inv_headers)
            if not row_idx:
                row_idx = await _find_item_row_by_name(sheets, item_display_name, headers_map=inv_headers)

        if not row_idx:
            await send_md(update, f"❌ Barang *{escape_md(str(item_display_name))}* tidak ditemukan di inventaris.", parse_mode="Markdown")
            for k in (_KEY_STEP, _KEY_CATEGORY, _KEY_CHOICE, _KEY_CHOICE_ROW, _KEY_QTY):
                context.user_data.pop(k, None)
            return True

        owner_id, _ = await _get_owner_for_item(sheets, row_idx, inv_headers)
        if owner_id and user and owner_id == user.id:
            await send_md(update, "🚫 Kamu tercatat sebagai pemilik barang ini — tidak dapat meminjam barang milik sendiri.")
            for k in (_KEY_STEP, _KEY_CATEGORY, _KEY_CHOICE, _KEY_CHOICE_ROW, _KEY_QTY):
                context.user_data.pop(k, None)
            return True

        available = await _get_available_stock(sheets, row_idx, inv_headers)
        if available <= 0:
            await send_md(update, f"⚠️ Stok untuk *{escape_md(str(item_display_name))}* saat ini: {available}. Tidak dapat meminjam sekarang.", parse_mode="Markdown")
            for k in (_KEY_STEP, _KEY_CATEGORY, _KEY_CHOICE, _KEY_CHOICE_ROW, _KEY_QTY):
                context.user_data.pop(k, None)
            return True

        if qty > available:
            await send_md(update, f"⚠️ Stok tidak cukup — tersedia *{available}* unit untuk *{escape_md(str(item_display_name))}*.", parse_mode="Markdown")
            for k in (_KEY_STEP, _KEY_CATEGORY, _KEY_CHOICE, _KEY_CHOICE_ROW, _KEY_QTY):
                context.user_data.pop(k, None)
            return True

        witel, divisi = await _get_user_profile_witel_divisi(sheets, user.id)

        try:
            recs = await sheets.async_get_all_records(INVENTARIS_SHEET)
            rec = recs[row_idx - 2] if 0 <= (row_idx - 2) < len(recs) else {}
        except Exception:
            rec = {}
        item_id = str(rec.get("Item ID") or "").strip()
        name = rec.get("Nama Barang") or str(item_display_name)

        pem_map = await _ensure_peminjaman_headers(sheets)
        max_col = max(pem_map.values()) if pem_map else 10
        txn = make_txn_id("TXN")
        today = datetime.now().strftime("%Y-%m-%d")
        row = [""] * max_col
        row[pem_map["ID Transaksi"] - 1] = txn
        row[pem_map["Item ID"] - 1] = item_id
        row[pem_map["Nama Barang"] - 1] = name
        row[pem_map["Peminjam ID"] - 1] = str(user.id)
        row[pem_map["Peminjam Nama"] - 1] = (user.first_name or user.full_name or "")
        row[pem_map["Qty Dipinjam"] - 1] = str(qty)
        row[pem_map["Tanggal Pinjam"] - 1] = today
        row[pem_map["Deadline"] - 1] = dl_iso
        row[pem_map["Status Peminjaman"] - 1] = "Menunggu Persetujuan"
        row[pem_map["Keterangan"] - 1] = (f"Request by {user.first_name or user.id} | Witel:{witel} | Divisi:{divisi}")

        context.user_data[_KEY_PENDING] = {
            "row": row,
            "meta": {
                "txn": txn,
                "item_name": name,
                "item_id": item_id,
                "qty": qty,
                "deadline": dl_iso,
                "witel": witel,
                "divisi": divisi,
                "note": row[pem_map["Keterangan"] - 1],
            },
        }
        text, kb = _build_confirmation_text_and_kb_from_pending(context.user_data[_KEY_PENDING])
        await send_md(update, text, parse_mode="Markdown", reply_markup=kb)

        for k in (_KEY_STEP, _KEY_CATEGORY, _KEY_CHOICE, _KEY_CHOICE_ROW, _KEY_QTY):
            context.user_data.pop(k, None)
        return True

    return False


# custom confirm callback (unchanged flow, creates pending and ask final confirm)
async def brw_custom_confirm_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    await cq.answer()

    if not await require_registration(cq, context):
        return

    data = (cq.data or "").split(":", 1)
    if len(data) < 2:
        await send_md(cq, "Aksi tidak dikenali.")
        return
    _, token = data
    token = token.strip().lower()

    if token != "custom_yes":
        await send_md(cq, "Aksi tidak dikenali.")
        return

    sheets = context.application.bot_data.get("sheets_manager")
    user = cq.from_user

    if not sheets:
        await send_md(cq, "❌ Layanan Google Sheets belum tersedia.")
        return

    name = context.user_data.get(_KEY_CUSTOM_NAME)
    serial = context.user_data.get(_KEY_CUSTOM_SERIAL) or ""
    qty = context.user_data.get(_KEY_QTY, 0)
    dl_iso = context.user_data.get("borrow_custom_deadline") or ""

    if not name or qty <= 0:
        await send_md(cq, "❌ Data custom tidak lengkap. Mulai ulang dengan /pinjam custom.")
        for k in (_KEY_STEP, _KEY_CUSTOM_NAME, _KEY_CUSTOM_SERIAL, _KEY_QTY, "borrow_custom_deadline"):
            context.user_data.pop(k, None)
        return

    txn = make_txn_id("TXN")
    today = datetime.now().strftime("%Y-%m-%d")
    pem_map = await _ensure_peminjaman_headers(sheets)
    max_col = max(pem_map.values()) if pem_map else 10
    row = [""] * max_col
    row[pem_map["ID Transaksi"] - 1] = txn
    row[pem_map["Item ID"] - 1] = ""  # custom
    row[pem_map["Nama Barang"] - 1] = f"CUSTOM: {name}"
    row[pem_map["Peminjam ID"] - 1] = str(user.id)
    row[pem_map["Peminjam Nama"] - 1] = user.first_name or user.full_name or ""
    row[pem_map["Qty Dipinjam"] - 1] = str(qty)
    row[pem_map["Tanggal Pinjam"] - 1] = today
    row[pem_map["Deadline"] - 1] = dl_iso
    row[pem_map["Status Peminjaman"] - 1] = "Menunggu Persetujuan"
    note = f"CustomRequest by {user.first_name or user.id}"
    if serial:
        note = f"{note} | Serial:{serial}"
    row[pem_map["Keterangan"] - 1] = note

    context.user_data[_KEY_PENDING] = {
        "row": row,
        "meta": {
            "txn": txn,
            "item_name": f"CUSTOM: {name}",
            "item_id": "",
            "qty": qty,
            "deadline": dl_iso,
            "witel": "",
            "divisi": "",
            "note": note,
        },
    }
    text, kb = _build_confirmation_text_and_kb_from_pending(context.user_data[_KEY_PENDING])
    await send_md(cq, text, parse_mode="Markdown", reply_markup=kb)

    for k in (_KEY_STEP, _KEY_CUSTOM_NAME, _KEY_CUSTOM_SERIAL, _KEY_QTY, "borrow_custom_deadline"):
        context.user_data.pop(k, None)


# notify owner helper (unchanged, still includes serial/spec in owner message)
async def _notify_owner_for_request(
    context: ContextTypes.DEFAULT_TYPE,
    sheets,
    item_name: str,
    txn: str,
    qty: int,
    *,
    borrower_id: Optional[int],
    borrower_name: str,
    witel: Optional[str] = None,
    divisi: Optional[str] = None,
    item_id: Optional[str] = None,
) -> bool:
    try:
        inv_headers = await _ensure_inventaris_headers(sheets)
        row_idx = None

        if item_id:
            row_idx = await _find_item_row_by_id(sheets, item_id, headers_map=inv_headers)
        else:
            try:
                row_idx = await sheets.async_find_row_by_value(
                    INVENTARIS_SHEET,
                    "Nama Barang",
                    item_name,
                    headers_map=inv_headers,
                )
            except Exception:
                row_idx = await _find_item_row_by_name(sheets, item_name, headers_map=inv_headers)

        if not row_idx:
            logger.debug("_notify_owner_for_request: owner not found — no row for item %s (item_id=%s)", item_name, item_id)
            return True

        pemilik_col = inv_headers.get("Pemilik ID")
        if not pemilik_col:
            logger.debug("_notify_owner_for_request: Pemilik ID column missing")
            return False

        pemilik_raw = await sheets.async_get_cell_value(INVENTARIS_SHEET, row_idx, pemilik_col)
        try:
            owner_chat_id = int(str(pemilik_raw).strip())
        except Exception:
            logger.debug("_notify_owner_for_request: bad pemilik id: %s", pemilik_raw)
            return False

        recs = await sheets.async_get_all_records(INVENTARIS_SHEET)
        rec = None
        idx0 = row_idx - 2
        if 0 <= idx0 < len(recs):
            rec = recs[idx0]

        specs_parts = []
        if rec:
            for k in ("Keterangan 1", "Keterangan 2", "Serial Number", "Keterangan 3", "Keterangan"):
                v = rec.get(k)
                if v:
                    specs_parts.append(str(v))
        specs_text = " | ".join(specs_parts) if specs_parts else "-"

        text_lines = [
            "📩 *Notifikasi Permintaan Peminjaman*",
            "",
            f"Barang: *{escape_md(item_name)}* (x{qty})",
        ]
        if item_id:
            text_lines.append(f"Item ID: `{escape_md(item_id)}`")
        if borrower_name:
            text_lines.append(f"Peminjam: *{escape_md(borrower_name)}* (`{borrower_id}`)")
        if witel:
            text_lines.append(f"Witel: {escape_md(str(witel))}")
        if divisi:
            text_lines.append(f"Divisi: {escape_md(str(divisi))}")
        text_lines += [
            f"Spec / Serial: `{escape_md(specs_text)}`",
            f"TXN: `{escape_md(txn)}`",
            "",
            "/approve untuk menyetujui atau /reject untuk menolak.",
        ]
        text = "\n".join(text_lines)

        try:
            await context.application.bot.send_message(chat_id=owner_chat_id, text=text, parse_mode="Markdown")
        except Exception as e:
            logger.debug("notify_owner: send_message with parse_mode failed: %s", e)
            try:
                await context.application.bot.send_message(chat_id=owner_chat_id, text=text)
            except Exception as e2:
                logger.exception("notify_owner: final send_message failed: %s", e2)
                return False

        try:
            await sheets.async_write_log(
                str(owner_chat_id),
                "NotifOwnerOnRequest",
                item_name,
                f"tx={txn} borrower={borrower_id} itemid={item_id or ''}",
            )
        except Exception:
            pass

        if borrower_id:
            try:
                await context.application.bot.send_message(
                    chat_id=borrower_id,
                    text=(f"ℹ️ Pemilik barang telah diberitahu tentang permintaan (TXN `{txn}`)."),
                    parse_mode="Markdown",
                )
            except Exception:
                pass

        return True
    except Exception as e:
        logger.exception("Failed notifying owner: %s", e)
        return False


# /mypinjam and TXN handlers (kept consistent)
async def mypinjam_cmd(update: Any, context: ContextTypes.DEFAULT_TYPE):
    user = getattr(update, "effective_user", None)
    if user is None and isinstance(update, CallbackQuery):
        user = update.from_user

    if not user:
        await send_md(update, "⚠️ Tidak dapat mengenali pengguna.")
        return

    if not await require_registration(update, context):
        return

    sheets = context.application.bot_data.get("sheets_manager")
    if not sheets:
        await send_md(update, "❌ Layanan Google Sheets belum tersedia.")
        return

    try:
        pem_recs = await sheets.async_get_all_records(PEMINJAMAN_SHEET)
    except Exception as e:
        logger.exception("Failed fetching peminjaman for /mypinjam: %s", e)
        await send_md(update, "❌ Gagal mengambil data peminjaman.")
        return

    pairs = []
    for idx, rec in enumerate(pem_recs):
        try:
            pid = rec.get("Peminjam ID") or ""
            if str(pid).strip() == str(user.id):
                status = (rec.get("Status Peminjaman") or "").strip().lower()
                if any(
                    k in status
                    for k in ("disetujui", "dipinjam", "approved", "borrowed", "partially")
                ):
                    pairs.append((idx + 2, rec))
        except Exception:
            continue

    if not pairs:
        await send_md(update, "📭 Kamu tidak memiliki peminjaman aktif atau riwayat kosong.")
        return

    kb = []
    for i, (rownum, rec) in enumerate(pairs[:12]):
        tx = rec.get("ID Transaksi") or f"row{rownum}"
        name = rec.get("Nama Barang") or ""
        status_raw = rec.get("Status Peminjaman") or ""
        status_label = _friendly_status_label(status_raw)
        qty = rec.get("Qty Dipinjam") or ""
        label = f"{tx} — {name} ({status_label}) x{qty}"
        if len(label) > 64:
            label = label[:61] + "..."
        kb.append([InlineKeyboardButton(label, callback_data=f"mytxn_view:{rownum}")])
    kb.append([InlineKeyboardButton("Refresh", callback_data="mytxn_refresh")])
    await send_md(update, f"📋 Peminjaman milik kamu — total *{len(pairs)}*.", reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    context.user_data["mytxn_pairs"] = pairs


# TXN view & actions (unchanged)
async def mytxn_view_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    await cq.answer()

    if not await require_registration(cq, context):
        return

    data = (cq.data or "").split(":", 1)
    if len(data) < 2:
        return
    row_idx = int(data[1])

    sheets = context.application.bot_data.get("sheets_manager")
    if not sheets:
        await send_md(cq, "❌ Layanan Google Sheets belum tersedia.")
        return

    try:
        rows = await sheets.async_get_all_records(PEMINJAMAN_SHEET)
    except Exception:
        await send_md(cq, "❌ Gagal mengambil data peminjaman.")
        return

    idx0 = row_idx - 2
    if idx0 < 0 or idx0 >= len(rows):
        await send_md(cq, "TXN tidak ditemukan.")
        return

    rec = rows[idx0]
    tx = rec.get("ID Transaksi", "")
    name = rec.get("Nama Barang", "")
    qty = rec.get("Qty Dipinjam", "")
    status = rec.get("Status Peminjaman", "")
    dl = rec.get("Deadline", "")
    peminjam = rec.get("Peminjam Nama", "")
    friendly_status = _friendly_status_label(status)

    teks = (
        f"📄 TXN: `{escape_md(str(tx))}`\n"
        f"Barang: *{escape_md(str(name))}*\n"
        f"Qty: {escape_md(str(qty))}\n"
        f"Peminjam: {escape_md(str(peminjam))}\n"
        f"Status: *{escape_md(str(friendly_status))}*\n"
        f"Deadline: `{escape_md(str(dl))}`\n"
    )

    kb = []
    status_norm = str(status or "").strip().lower()
    if status_norm.startswith("menunggu") or "pending" in status_norm:
        kb.append([InlineKeyboardButton("❌ Cancel Request", callback_data=f"mytxn_cancel:{row_idx}")])
    if status_norm in ("disetujui", "dipinjam", "approved", "borrowed") or ("partially" in status_norm):
        kb.append([InlineKeyboardButton("↩️ Request Return (instruksi)", callback_data=f"mytxn_return:{row_idx}")])
    kb.append([InlineKeyboardButton("Kembali", callback_data="mytxn_refresh")])
    await send_md(cq, teks, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")


async def mytxn_refresh_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    await cq.answer()
    await mypinjam_cmd(cq, context)


async def mytxn_cancel_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    await cq.answer()

    if not await require_registration(cq, context):
        return

    data = (cq.data or "").split(":", 1)
    if len(data) < 2:
        return
    row_idx = int(data[1])

    sheets = context.application.bot_data.get("sheets_manager")
    if not sheets:
        await send_md(cq, "❌ Layanan belum tersedia.")
        return

    pem_headers = await _ensure_peminjaman_headers(sheets)
    try:
        raw = await sheets.async_get_cell_value(PEMINJAMAN_SHEET, row_idx, pem_headers["Peminjam ID"])
    except Exception:
        raw = None

    try:
        pid = int(str(raw).strip()) if raw not in (None, "") else None
    except Exception:
        pid = None

    if pid != cq.from_user.id:
        await send_md(cq, "🚫 Hanya peminjam yang dapat membatalkan request ini.")
        return

    try:
        await sheets.async_update_cell(PEMINJAMAN_SHEET, row_idx, pem_headers["Status Peminjaman"], "Dibatalkan")
        await sheets.async_write_log(str(cq.from_user.id), "CancelRequest", "", f"row={row_idx}")
        await send_md(cq, f"✅ Permintaan pada baris `{row_idx}` dibatalkan.", parse_mode="Markdown")
    except Exception as e:
        logger.exception("Failed cancelling txn: %s", e)
        await send_md(cq, "❌ Gagal membatalkan. Coba lagi.")


async def mytxn_return_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    await cq.answer()

    if not await require_registration(cq, context):
        return

    data = (cq.data or "").split(":", 1)
    if len(data) < 2:
        return
    row_idx = int(data[1])

    sheets = context.application.bot_data.get("sheets_manager")
    if not sheets:
        await send_md(cq, "❌ Layanan belum tersedia.")
        return

    pem_headers = await _ensure_peminjaman_headers(sheets)
    try:
        txn = await sheets.async_get_cell_value(PEMINJAMAN_SHEET, row_idx, pem_headers["ID Transaksi"])
    except Exception:
        txn = None

    if not txn:
        await send_md(cq, "❌ TXN tidak ditemukan.")
        return

    await send_md(cq, "Untuk mengajukan pengembalian, silakan jalankan perintah:\n" f"`/kembali {escape_md(str(txn))}`", parse_mode="Markdown")


# register handlers
def register_borrow_handlers(application=None):
    handlers = [
        CommandHandler("pinjam", pinjam_command),
        CommandHandler("mypinjam", mypinjam_cmd),
        CommandHandler("cari", cari_command),

        CallbackQueryHandler(cari_view_cb, pattern=rf"^{_PREFIX_VIEW}"),

        CallbackQueryHandler(borrow_category_cb, pattern=rf"^{_PREFIX_CAT}"),
        CallbackQueryHandler(borrow_item_cb, pattern=rf"^{_PREFIX_ITEM}"),
        CallbackQueryHandler(brw_confirm_item_cb, pattern=rf"^{_PREFIX_CONFIRM}"),
        CallbackQueryHandler(borrow_qty_cb, pattern=rf"^{_PREFIX_QTY}"),
        CallbackQueryHandler(borrow_cancel_cb, pattern=rf"^{_PREFIX_CANCEL}"),

        CallbackQueryHandler(brw_submit_cb, pattern=rf"^{_PREFIX_SUBMIT}"),

        CallbackQueryHandler(brw_custom_confirm_cb, pattern=rf"^{_PREFIX_CUSTOM_CONFIRM}custom_yes$"),

        CallbackQueryHandler(mytxn_view_cb, pattern=r"^mytxn_view:"),
        CallbackQueryHandler(mytxn_refresh_cb, pattern=r"^mytxn_refresh$"),
        CallbackQueryHandler(mytxn_cancel_cb, pattern=r"^mytxn_cancel:"),
        CallbackQueryHandler(mytxn_return_cb, pattern=r"^mytxn_return:"),

        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_borrow_message),
    ]

    if application:
        for h in handlers:
            application.add_handler(h)
        return None
    return handlers


__all__ = [
    "pinjam_command",
    "handle_borrow_message",
    "register_borrow_handlers",
]
