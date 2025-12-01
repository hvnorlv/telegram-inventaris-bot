# handlers/borrow.py
"""
Handler untuk command /pinjam — ajukan permintaan peminjaman barang.

Flow (interaktif, UPDATED):
1) /pinjam -> pilih kategori
2) pilih item -> KONFIRMASI item (detail singkat + serial/keterangan) -> pilih quantity (atau ketik custom)
   - jika kategori = "Custom" -> alur custom:
       a) minta nama barang
       b) minta serial number (atau '-' jika tidak ada)
       c) minta quantity
       d) minta deadline
       e) konfirmasi -> buat TXN (Nama akan disimpan apa adanya, ditandai sebagai CUSTOM)
3) masukkan Deadline -> request dibuat
   (Witel & Divisi diambil otomatis dari profil user di sheet Users)

Quick-mode (tetap):
- /pinjam <Nama Item> <qty> [deadline]
- /pinjam custom <Nama Item> <qty> [deadline]  (buat request custom tanpa alur interaktif)

Pencarian:
- /cari <keyword> atau /cari <kategori> <keyword>
  -> hanya menampilkan hasil & DETAIL item (tanpa alur pinjam)
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

# ---------------------------------------------------------------------------
# Callback prefixes
# ---------------------------------------------------------------------------
_PREFIX_CAT = "brw_cat:"
_PREFIX_ITEM = "brw_item:"  # untuk alur /pinjam (item dari inventory)
_PREFIX_QTY = "brw_qty:"
_PREFIX_CANCEL = "brw_cancel:"
_PREFIX_VIEW = "cari_view:"  # detail-only
_PREFIX_CONFIRM = "brw_confirm:"  # konfirmasi item
_PREFIX_CUSTOM_CONFIRM = "brw_custom_confirm:"  # custom confirm callback

# user_data keys / steps
_KEY_STEP = "borrow_step"
_KEY_CATEGORY = "borrow_category"
_KEY_CHOICE = "borrow_choice_name"
_KEY_QTY = "borrow_qty"

# custom flow keys
_KEY_CUSTOM_NAME = "borrow_custom_name"
_KEY_CUSTOM_SERIAL = "borrow_custom_serial"

_STEP_CATEGORY = "category_select"
_STEP_CHOOSE_ITEM = "choose_item"
_STEP_CONFIRM = "confirm_item"
_STEP_QTY = "quantity"
_STEP_QTY_CUSTOM = "quantity_custom"
_STEP_DEADLINE = "deadline"

# custom-specific steps
_STEP_CUSTOM_NAME = "custom_name"
_STEP_CUSTOM_SERIAL = "custom_serial"
_STEP_CUSTOM_QTY = "custom_qty"
_STEP_CUSTOM_DEADLINE = "custom_deadline"
_STEP_CUSTOM_CONFIRM = "custom_confirm"


# ---------------------------------------------------------------------------
# retry helper
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# sheet helpers
# ---------------------------------------------------------------------------
async def _ensure_inventaris_headers(sheets) -> Dict[str, int]:
    # pastikan "Serial Number" ada
    return await sheets.async_ensure_headers(
        INVENTARIS_SHEET,
        [
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
    # dipakai untuk baca profil user
    return await sheets.async_ensure_headers(
        "Users",
        ["User ID", "Nama", "Role", "Witel", "Divisi"],
    )


async def _get_user_profile_witel_divisi(
    sheets,
    user_id: int,
) -> Tuple[str, str]:
    """Ambil (Witel, Divisi) dari sheet Users berdasarkan user_id."""
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


async def _find_item_row_by_name(
    sheets,
    name: str,
    headers_map: Optional[Dict[str, int]] = None,
) -> Optional[int]:
    """
    Cari baris item di INVENTARIS berdasarkan Nama Barang.

    Urutan:
      1) exact match via async_find_row_by_value (kalau ada)
      2) fallback scan:
           - skip Status=Removed
           - cari exact match (case-insensitive)
           - kalau tidak ada, ambil baris pertama yang *mengandung* nama tsb
    """
    headers_map = headers_map or await _ensure_inventaris_headers(sheets)

    # 1) helper bawaan (kalau ada)
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
            logger.debug(
                "_find_item_row_by_name: async_find_row_by_value failed; falling back",
                exc_info=True,
            )

    # 2) fallback scan manual
    try:
        recs = await sheets.async_get_all_records(INVENTARIS_SHEET)
    except Exception:
        logger.exception("_find_item_row_by_name: fallback scan failed")
        return None

    target = str(name).strip().lower()
    best_row: Optional[int] = None

    for idx, r in enumerate(recs):
        # skip removed
        status = str(r.get("Status", "") or "").strip().lower()
        if status == "removed":
            continue

        nm = str(r.get("Nama Barang", "")).strip()
        if not nm:
            continue
        nm_lc = nm.lower()

        # exact (ignore case)
        if nm_lc == target:
            return idx + 2

        # candidate: mengandung kata tsb
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
    """
    Baca stok tersedia dari baris INVENTARIS:

    - Jika Status = Removed -> stok 0.
    - Kalau kolom 'Tersedia' ada & berisi angka >=0 -> pakai itu.
    - Jika 'Tersedia' kosong / tidak valid, tapi 'Total Qty' ada -> pakai 'Total Qty'.
    """
    if not row_idx or row_idx < 2:
        return 0

    # cek status
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

    # baca Tersedia
    if ters_col:
        try:
            v = await sheets.async_get_cell_value(
                INVENTARIS_SHEET,
                row_idx,
                ters_col,
            )
            # sentinel negatif menandakan "tidak valid"
            ters_val = safe_int(v, -999_999)
        except Exception:
            ters_val = -999_999

    # baca Total Qty
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

    # angka = jumlah hari
    if s.isdigit():
        days = int(s)
        if days <= 0:
            return None
        if MAX_BORROW_DAYS and days > MAX_BORROW_DAYS:
            return None
        return (datetime.now().date() + timedelta(days=days)).isoformat()

    # format tanggal YYYY-MM-DD
    try:
        dt = datetime.strptime(s, "%Y-%m-%d").date()
        if dt < datetime.now().date():
            return None
        if MAX_BORROW_DAYS and (dt - datetime.now().date()).days > MAX_BORROW_DAYS:
            return None
        return dt.isoformat()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# friendly label
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# /pinjam command
# ---------------------------------------------------------------------------
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

    # wajib sudah registrasi (/start)
    if not await require_registration(update, context):
        return

    sheets = context.application.bot_data.get("sheets_manager")

    # Quick-mode: /pinjam <Nama Item> <qty> [deadline]
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

            # heuristik: if last token parsable as deadline & qty valid -> treat as deadline
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

            ok = await sheets.async_append_row(PEMINJAMAN_SHEET, row)
            if ok:
                try:
                    await sheets.async_write_log(
                        str(user.id),
                        "AjukanPinjamCustom",
                        name,
                        f"tx={txn} qty={qty} dl={deadline_raw or ''}",
                    )
                except Exception:
                    pass
                await send_md(
                    update,
                    f"✅ Permintaan CUSTOM peminjaman dibuat (TXN `{txn}`). Pemilik akan diberitahu.",
                    parse_mode="Markdown",
                )
            else:
                await send_md(
                    update,
                    "❌ Gagal membuat permintaan peminjaman custom. Coba lagi nanti.",
                )
            return

        # standard quick-mode
        deadline_raw = None
        qty = None
        if len(args) >= 3:
            # last might be deadline or qty
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

        # cek pemilik: tidak boleh meminjam barang sendiri
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

        # ambil profil user
        witel, divisi = await _get_user_profile_witel_divisi(sheets, user.id)
        deadline_iso = _parse_deadline_input(deadline_raw) if deadline_raw else ""
        txn = make_txn_id("TXN")
        today = datetime.now().strftime("%Y-%m-%d")
        pem_map = await _ensure_peminjaman_headers(sheets)
        max_col = max(pem_map.values()) if pem_map else 10
        row = [""] * max_col
        row[pem_map["ID Transaksi"] - 1] = txn
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
            f"Request by {user.first_name or user.id} | "
            f"Witel:{witel} | Divisi:{divisi}"
        )

        ok = await sheets.async_append_row(PEMINJAMAN_SHEET, row)
        if ok:
            try:
                await sheets.async_write_log(
                    str(user.id),
                    "AjukanPinjam",
                    name,
                    f"tx={txn} qty={qty} dl={deadline_iso or ''}",
                )
            except Exception:
                pass
            await _notify_owner_for_request(
                context,
                sheets,
                name,
                txn,
                qty,
                borrower_id=user.id,
                borrower_name=user.first_name or user.full_name or "",
                witel=witel,
                divisi=divisi,
            )
            await send_md(
                update,
                f"✅ Permintaan peminjaman dibuat (TXN `{txn}`). Pemilik akan diberitahu.",
                parse_mode="Markdown",
            )
        else:
            await send_md(
                update,
                "❌ Gagal membuat permintaan peminjaman. Coba lagi nanti.",
            )
        return

    # Interactive mode: start with category selection
    for k in (
        _KEY_STEP,
        _KEY_CATEGORY,
        _KEY_CHOICE,
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
    # include 'Custom' option if not present in ITEM_CATEGORIES
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


# ---------------------------------------------------------------------------
# /cari command (detail-only)
# ---------------------------------------------------------------------------
async def cari_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # wajib sudah registrasi
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

    # check category-first
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
            available = safe_int(
                r.get("Tersedia") or r.get("Total Qty") or 0,
                0,
            )
            if available < 0:
                continue
            if category_filter:
                if (
                    str(r.get("Kategori", "")).strip().lower()
                    != str(category_filter).strip().lower()
                ):
                    continue
            name = str(r.get("Nama Barang", "") or "")
            k1 = str(r.get("Keterangan 1", "") or "")
            k2 = str(r.get("Keterangan 2", "") or "")
            k3 = str(
                r.get("Serial Number", "") or r.get("Keterangan 3", "") or ""
            )
            hay = " ".join([name, k1, k2, k3]).lower()
            if not kw or kw in hay:
                matches.append((idx + 2, r))
        except Exception:
            continue

    if not matches:
        if category_filter and kw:
            await send_md(
                update,
                f"🔎 Tidak ditemukan item kategori *{escape_md(category_filter)}* "
                f"dengan kata kunci `{escape_md(keyword)}`.",
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
        specs = []
        for k in (
            "Keterangan 1",
            "Keterangan 2",
            "Serial Number",
            "Keterangan 3",
            "Keterangan",
        ):
            v = rec.get(k)
            if v:
                specs.append(str(v))
        specs_txt = " | ".join(specs) if specs else ""
        available = safe_int(
            rec.get("Tersedia") or rec.get("Total Qty") or 0,
            0,
        )
        label = f"{name}"
        if specs_txt:
            label = f"{label} — {specs_txt}"
        label = f"{label} (Avail:{available})"
        if len(label) > 64:
            label = label[:61] + "..."
        # DETAIL-ONLY (bukan _PREFIX_ITEM)
        kb.append(
            [InlineKeyboardButton(label, callback_data=f"{_PREFIX_VIEW}{name}")]
        )

    if len(matches) > max_results:
        kb.append(
            [
                InlineKeyboardButton(
                    f"Menampilkan {max_results} dari {len(matches)} hasil — refine pencarian",
                    callback_data="cari_refine",
                )
            ]
        )
    kb.append([InlineKeyboardButton("Batal", callback_data=f"{_PREFIX_CANCEL}back")])
    await send_md(
        update,
        (
            f"🔎 Hasil pencarian untuk `{escape_md(keyword)}` — total *{len(matches)}* "
            f"(menampilkan {min(len(matches), max_results)})."
        ),
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown",
    )


# ---------------------------------------------------------------------------
# DETAIL view untuk /cari (tanpa alur pinjam)
# ---------------------------------------------------------------------------
async def cari_view_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    await cq.answer()

    if not await require_registration(cq, context):
        return

    data = cq.data or ""
    item_name = data.split(":", 1)[1].strip() if ":" in data else data

    sheets = context.application.bot_data.get("sheets_manager")
    if not sheets:
        await send_md(cq, "❌ Layanan Google Sheets belum tersedia.")
        return

    inv_headers = await _ensure_inventaris_headers(sheets)
    row_idx = await _find_item_row_by_name(
        sheets,
        item_name,
        headers_map=inv_headers,
    )
    if not row_idx:
        await send_md(
            cq,
            f"❌ Barang *{escape_md(item_name)}* tidak ditemukan.",
            parse_mode="Markdown",
        )
        return

    # baca record + detail
    try:
        recs = await sheets.async_get_all_records(INVENTARIS_SHEET)
        rec = recs[row_idx - 2] if 0 <= (row_idx - 2) < len(recs) else {}
    except Exception:
        rec = {}

    available = await _get_available_stock(sheets, row_idx, inv_headers)
    kategori = rec.get("Kategori") or "-"
    witel = rec.get("Witel") or "-"
    divisi = rec.get("Divisi") or "-"
    pemilik_nama = rec.get("Pemilik Nama") or "-"
    pemilik_id = rec.get("Pemilik ID") or "-"
    ket = rec.get("Keterangan") or "-"
    k1 = rec.get("Keterangan 1") or "-"
    k2 = rec.get("Keterangan 2") or "-"
    k3 = rec.get("Keterangan 3") or "-"
    serial = rec.get("Serial Number") or "-"

    teks = (
        f"📄 *Detail Barang*\n"
        f"Nama: *{escape_md(item_name)}*\n"
        f"Kategori: {escape_md(str(kategori))}\n"
        f"Tersedia: *{available}*\n"
        f"Witel: {escape_md(str(witel))}\n"
        f"Divisi: {escape_md(str(divisi))}\n"
        f"Pemilik: {escape_md(str(pemilik_nama))} (`{escape_md(str(pemilik_id))}`)\n"
        f"Serial Number: `{escape_md(str(serial))}`\n"
        f"Keterangan: {escape_md(str(ket))}\n"
        f"Keterangan 1: {escape_md(str(k1))}\n"
        f"Keterangan 2: {escape_md(str(k2))}\n"
        f"Keterangan 3: {escape_md(str(k3))}\n"
    )

    kb = [[InlineKeyboardButton("🔙 Kembali", callback_data="cari_refine")]]
    try:
        await cq.edit_message_text(
            teks,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb),
        )
    except Exception:
        await send_md(
            cq,
            teks,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb),
        )


# ---------------------------------------------------------------------------
# Category callback
# ---------------------------------------------------------------------------
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

    # Jika category == "Custom", mulai custom flow
    if str(cat).strip().lower() == "custom":
        context.user_data[_KEY_STEP] = _STEP_CUSTOM_NAME
        await send_md(
            cq,
            "✏️ Kamu memilih *Custom* — ketik *Nama Barang* yang ingin diajukan:",
            parse_mode="Markdown",
        )
        return

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
        r
        for r in rows
        if str(r.get("Kategori", "")).strip().lower()
        == cat.strip().lower()
    ]
    if not matched:
        try:
            await cq.edit_message_text(
                f"ℹ️ Kategori *{escape_md(cat)}* — tidak ada item di inventaris.",
                parse_mode="Markdown",
            )
        except Exception:
            await send_md(
                cq,
                f"ℹ️ Kategori {cat} — tidak ada item di inventaris.",
            )
        return

    kb = []
    per_page = int(ITEMS_PER_PAGE or 8)
    for rec in matched[:per_page]:
        name = rec.get("Nama Barang") or "-"
        specs_parts = []
        for k in (
            "Keterangan 1",
            "Keterangan 2",
            "Serial Number",
            "Keterangan 3",
            "Keterangan",
        ):
            v = rec.get(k)
            if v:
                specs_parts.append(str(v))
        specs = " | ".join(specs_parts)
        available = safe_int(
            rec.get("Tersedia") or rec.get("Total Qty") or 0,
            0,
        )
        label = (
            f"{name} — {specs} (Tersedia: {available})"
            if specs
            else f"{name} (Tersedia: {available})"
        )
        if len(label) > 64:
            label = label[:61] + "..."
        kb.append(
            [InlineKeyboardButton(label, callback_data=f"{_PREFIX_ITEM}{name}")]
        )

    kb.append(
        [InlineKeyboardButton("🔙 Batal", callback_data=f"{_PREFIX_CANCEL}back")]
    )
    try:
        await cq.edit_message_text(
            f"📂 Kategori: *{escape_md(cat)}* — pilih item:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb),
        )
    except Exception:
        await send_md(
            cq,
            f"📂 Pilih item di kategori {cat}:",
            reply_markup=InlineKeyboardMarkup(kb),
        )


# ---------------------------------------------------------------------------
# Item selected -> SHOW KONFIRMASI item (dengan serial & keterangan)
# ---------------------------------------------------------------------------
async def borrow_item_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    await cq.answer()

    if not await require_registration(cq, context):
        return

    data = cq.data or ""
    if ":" in data:
        item_name = data.split(":", 1)[1].strip()
    else:
        item_name = data

    context.user_data[_KEY_CHOICE] = item_name
    context.user_data[_KEY_STEP] = _STEP_CONFIRM

    # read availability + specs (best-effort)
    sheets = context.application.bot_data.get("sheets_manager")
    available: Optional[int] = None
    specs: List[str] = []
    kategori = ""
    owner_name = ""

    if sheets:
        try:
            headers = await _ensure_inventaris_headers(sheets)
            row_idx = await _find_item_row_by_name(
                sheets,
                item_name,
                headers_map=headers,
            )
            if row_idx:
                available = await _get_available_stock(sheets, row_idx, headers)

                recs = await sheets.async_get_all_records(INVENTARIS_SHEET)
                idx0 = row_idx - 2
                if 0 <= idx0 < len(recs):
                    rec = recs[idx0]
                    kategori = rec.get("Kategori", "") or ""
                    owner_name = rec.get("Pemilik Nama", "") or ""
                    for k in (
                        "Keterangan 1",
                        "Keterangan 2",
                        "Serial Number",
                        "Keterangan 3",
                        "Keterangan",
                    ):
                        v = rec.get(k)
                        if v:
                            specs.append(str(v))
        except Exception:
            available = None

    # If known and zero, inform user immediately
    if available is not None and available <= 0:
        await send_md(
            cq,
            f"⚠️ Stok untuk *{escape_md(item_name)}* saat ini: *0*. Tidak dapat meminjam sekarang.",
            parse_mode="Markdown",
        )
        context.user_data.pop(_KEY_CHOICE, None)
        context.user_data.pop(_KEY_STEP, None)
        return

    # Build confirmation message
    header_lines = [
        "📋 *Konfirmasi Item*",
        "",
        f"Barang: *{escape_md(item_name)}*",
    ]
    if kategori:
        header_lines.append(f"Kategori: {escape_md(str(kategori))}")
    if owner_name:
        header_lines.append(f"Pemilik: {escape_md(str(owner_name))}")
    if specs:
        header_lines.append(
            f"Spec / Serial: `{escape_md(' | '.join(specs))}`"
        )
    if available is not None:
        header_lines.append(f"Tersedia: *{available}*")

    header_lines.append("")
    header_lines.append("Apakah ini barang yang ingin kamu pinjam?")

    header = "\n".join(header_lines)

    kb = [
        [
            InlineKeyboardButton(
                "✅ Ya, benar — Lanjut ke Qty",
                callback_data=f"{_PREFIX_CONFIRM}yes",
            )
        ],
        [InlineKeyboardButton("❌ Batal", callback_data=f"{_PREFIX_CANCEL}back")],
    ]
    try:
        await cq.edit_message_text(
            header,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb),
        )
    except Exception:
        await send_md(
            cq,
            header,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb),
        )


# ---------------------------------------------------------------------------
# Konfirmasi item callback -> lanjut ke pilih qty
# ---------------------------------------------------------------------------
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
        await send_md(
            cq,
            "❌ Tidak ada item yang dipilih. Mulai ulang dengan /pinjam.",
        )
        return

    if token == "yes":
        context.user_data[_KEY_STEP] = _STEP_QTY
        kb = [
            [
                InlineKeyboardButton(
                    str(q),
                    callback_data=f"{_PREFIX_QTY}{q}",
                )
            ]
            for q in QUANTITY_OPTIONS[:6]
        ]
        kb.append(
            [
                InlineKeyboardButton(
                    "Custom",
                    callback_data=f"{_PREFIX_QTY}custom",
                )
            ]
        )
        kb.append(
            [
                InlineKeyboardButton(
                    "🔙 Batal",
                    callback_data=f"{_PREFIX_CANCEL}back",
                )
            ]
        )
        header = (
            f"Barang: *{escape_md(item_name)}*\n\n"
            "Pilih quantity yang ingin dipinjam atau ketik Custom:"
        )
        try:
            await cq.edit_message_text(
                header,
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(kb),
            )
        except Exception:
            await send_md(
                cq,
                header,
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(kb),
            )
        return

    await send_md(cq, "Aksi konfirmasi tidak dikenali.")


# ---------------------------------------------------------------------------
# Qty chosen -> next Deadline (profil sudah otomatis)
# ---------------------------------------------------------------------------
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
            await cq.edit_message_text(
                "💬 Ketik jumlah yang ingin dipinjam (angka):",
            )
        except Exception:
            await send_md(
                cq,
                "💬 Ketik jumlah yang ingin dipinjam (angka):",
            )
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
    await send_md(
        cq,
        "📅 Masukkan deadline (YYYY-MM-DD) atau jumlah hari (mis. `7`):",
    )


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------
async def borrow_cancel_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    await cq.answer()

    if not await require_registration(cq, context):
        return

    for k in (
        _KEY_STEP,
        _KEY_CATEGORY,
        _KEY_CHOICE,
        _KEY_QTY,
        _KEY_CUSTOM_NAME,
        _KEY_CUSTOM_SERIAL,
    ):
        context.user_data.pop(k, None)

    try:
        await cq.edit_message_text("✅ Proses peminjaman dibatalkan.")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Handle plain-text during interactive flow (termasuk custom flow)
# ---------------------------------------------------------------------------
async def handle_borrow_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    step = context.user_data.get(_KEY_STEP)
    if not step:
        return False

    text_raw = update.message.text or ""
    text = sanitize_input(text_raw)
    sheets = context.application.bot_data.get("sheets_manager")
    user = update.effective_user

    # --- Custom flow: ask for name
    if step == _STEP_CUSTOM_NAME:
        name = (text_raw or "").strip()
        if not name:
            await send_md(
                update,
                "⚠️ Nama barang tidak boleh kosong. Ketik nama barang:",
            )
            return True
        context.user_data[_KEY_CUSTOM_NAME] = name
        context.user_data[_KEY_STEP] = _STEP_CUSTOM_SERIAL
        await send_md(
            update,
            "🔢 Jika ada Serial Number / Identifier, ketik sekarang. "
            "Jika tidak ada, ketik `-` :",
            parse_mode="Markdown",
        )
        return True

    # --- Custom flow: serial
    if step == _STEP_CUSTOM_SERIAL:
        serial = (text_raw or "").strip()
        if not serial:
            await send_md(
                update,
                "⚠️ Isi serial atau ketik `-` jika tidak ada.",
            )
            return True
        context.user_data[_KEY_CUSTOM_SERIAL] = serial if serial != "-" else ""
        context.user_data[_KEY_STEP] = _STEP_CUSTOM_QTY
        await send_md(
            update,
            "💬 Masukkan jumlah yang ingin dipinjam (angka):",
        )
        return True

    # --- Custom flow: qty
    if step == _STEP_CUSTOM_QTY:
        qty = safe_int(text_raw, -1)
        if qty <= 0:
            await send_md(
                update,
                "⚠️ Quantity harus angka > 0. Masukkan jumlah yang ingin dipinjam:",
            )
            return True
        context.user_data[_KEY_QTY] = qty
        context.user_data[_KEY_STEP] = _STEP_CUSTOM_DEADLINE
        await send_md(
            update,
            (
                f"📅 Masukkan deadline (YYYY-MM-DD) atau jumlah hari (mis. `7`) "
                f"(maks {MAX_BORROW_DAYS} hari):"
            ),
        )
        return True

    # --- Custom flow: deadline -> confirm
    if step == _STEP_CUSTOM_DEADLINE:
        dl_iso = _parse_deadline_input(text_raw)
        if not dl_iso:
            await send_md(
                update,
                (
                    f"⚠️ Deadline tidak valid atau melebihi batas maksimal "
                    f"({MAX_BORROW_DAYS} hari). Coba lagi."
                ),
            )
            return True

        context.user_data[_KEY_STEP] = _STEP_CUSTOM_CONFIRM
        context.user_data["borrow_custom_deadline"] = dl_iso

        name = context.user_data.get(_KEY_CUSTOM_NAME)
        serial = context.user_data.get(_KEY_CUSTOM_SERIAL) or "-"
        qty = context.user_data.get(_KEY_QTY, 1)
        teks = (
            "📋 *Konfirmasi Pengajuan CUSTOM*\n\n"
            f"Nama Barang: *{escape_md(str(name))}*\n"
            f"Serial: `{escape_md(str(serial))}`\n"
            f"Qty: *{qty}*\n"
            f"Deadline: `{dl_iso}`\n\n"
            "Apakah data di atas sudah benar?"
        )
        kb = [
            [
                InlineKeyboardButton(
                    "✅ Ya — Ajukan permintaan",
                    callback_data=f"{_PREFIX_CUSTOM_CONFIRM}custom_yes",
                )
            ],
            [InlineKeyboardButton("❌ Batal", callback_data=f"{_PREFIX_CANCEL}back")],
        ]
        await send_md(
            update,
            teks,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return True

    # Qty custom typed (regular inventory item)
    if step == _STEP_QTY_CUSTOM:
        qty = safe_int(text, 0)
        if qty <= 0:
            await send_md(update, "⚠️ Quantity harus angka > 0.")
            return True
        context.user_data[_KEY_QTY] = qty
        context.user_data[_KEY_STEP] = _STEP_DEADLINE
        await send_md(
            update,
            "📅 Masukkan deadline (YYYY-MM-DD) atau jumlah hari (mis. `7`):",
        )
        return True

    # Deadline -> finalize (regular inventory item)
    if step == _STEP_DEADLINE:
        dl_iso = _parse_deadline_input(text)
        if not dl_iso:
            await send_md(
                update,
                (
                    f"⚠️ Deadline tidak valid atau melebihi batas maksimal "
                    f"({MAX_BORROW_DAYS} hari). Coba lagi."
                ),
            )
            return True

        item_name = context.user_data.get(_KEY_CHOICE)
        qty = context.user_data.get(_KEY_QTY, 1)
        if not item_name:
            await send_md(
                update,
                "❌ Item belum dipilih. Mulai ulang dengan /pinjam.",
            )
            for k in (_KEY_STEP, _KEY_CATEGORY, _KEY_CHOICE, _KEY_QTY):
                context.user_data.pop(k, None)
            return True

        if not sheets:
            await send_md(update, "❌ Layanan Google Sheets belum tersedia.")
            return True

        inv_headers = await _ensure_inventaris_headers(sheets)
        row_idx = await _find_item_row_by_name(sheets, item_name, headers_map=inv_headers)
        if not row_idx:
            await send_md(
                update,
                f"❌ Barang *{escape_md(item_name)}* tidak ditemukan di inventaris.",
                parse_mode="Markdown",
            )
            for k in (_KEY_STEP, _KEY_CATEGORY, _KEY_CHOICE, _KEY_QTY):
                context.user_data.pop(k, None)
            return True

        owner_id, _ = await _get_owner_for_item(sheets, row_idx, inv_headers)
        if owner_id and user and owner_id == user.id:
            await send_md(
                update,
                "🚫 Kamu tercatat sebagai pemilik barang ini — tidak dapat meminjam barang milik sendiri.",
            )
            for k in (_KEY_STEP, _KEY_CATEGORY, _KEY_CHOICE, _KEY_QTY):
                context.user_data.pop(k, None)
            return True

        available = await _get_available_stock(sheets, row_idx, inv_headers)
        if available <= 0:
            await send_md(
                update,
                f"⚠️ Stok untuk *{escape_md(item_name)}* saat ini: {available}. Tidak dapat meminjam sekarang.",
                parse_mode="Markdown",
            )
            for k in (_KEY_STEP, _KEY_CATEGORY, _KEY_CHOICE, _KEY_QTY):
                context.user_data.pop(k, None)
            return True

        if qty > available:
            await send_md(
                update,
                f"⚠️ Stok tidak cukup — tersedia *{available}* unit untuk *{escape_md(item_name)}*.",
                parse_mode="Markdown",
            )
            for k in (_KEY_STEP, _KEY_CATEGORY, _KEY_CHOICE, _KEY_QTY):
                context.user_data.pop(k, None)
            return True

        # ambil profil user
        witel, divisi = await _get_user_profile_witel_divisi(sheets, user.id)

        pem_map = await _ensure_peminjaman_headers(sheets)
        max_col = max(pem_map.values()) if pem_map else 10
        txn = make_txn_id("TXN")
        today = datetime.now().strftime("%Y-%m-%d")
        row = [""] * max_col
        row[pem_map["ID Transaksi"] - 1] = txn
        row[pem_map["Nama Barang"] - 1] = item_name
        row[pem_map["Peminjam ID"] - 1] = str(user.id)
        row[pem_map["Peminjam Nama"] - 1] = (
            user.first_name or user.full_name or ""
        )
        row[pem_map["Qty Dipinjam"] - 1] = str(qty)
        row[pem_map["Tanggal Pinjam"] - 1] = today
        row[pem_map["Deadline"] - 1] = dl_iso
        row[pem_map["Status Peminjaman"] - 1] = "Menunggu Persetujuan"
        row[pem_map["Keterangan"] - 1] = (
            f"Request by {user.first_name or user.id} | "
            f"Witel:{witel} | Divisi:{divisi}"
        )

        ok = await sheets.async_append_row(PEMINJAMAN_SHEET, row)
        if ok:
            try:
                await sheets.async_write_log(
                    str(user.id),
                    "AjukanPinjam",
                    item_name,
                    f"tx={txn} qty={qty} dl={dl_iso}",
                )
            except Exception:
                pass
            await _notify_owner_for_request(
                context,
                sheets,
                item_name,
                txn,
                qty,
                borrower_id=user.id,
                borrower_name=user.first_name or user.full_name or "",
                witel=witel,
                divisi=divisi,
            )
            await send_md(
                update,
                (
                    f"✅ Permintaan peminjaman dibuat (TXN `{txn}`). "
                    "Pemilik telah diberitahu."
                ),
                parse_mode="Markdown",
            )
        else:
            await send_md(
                update,
                "❌ Gagal membuat permintaan pinjam. Coba lagi nanti.",
            )

        for k in (_KEY_STEP, _KEY_CATEGORY, _KEY_CHOICE, _KEY_QTY):
            context.user_data.pop(k, None)
        return True

    return False


# ---------------------------------------------------------------------------
# Callback for custom confirm (from button)
# ---------------------------------------------------------------------------
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

    # finalize custom request
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
        await send_md(
            cq,
            "❌ Data custom tidak lengkap. Mulai ulang dengan /pinjam custom atau /pinjam lalu pilih Custom.",
        )
        for k in (
            _KEY_STEP,
            _KEY_CUSTOM_NAME,
            _KEY_CUSTOM_SERIAL,
            _KEY_QTY,
            "borrow_custom_deadline",
        ):
            context.user_data.pop(k, None)
        return

    txn = make_txn_id("TXN")
    today = datetime.now().strftime("%Y-%m-%d")
    pem_map = await _ensure_peminjaman_headers(sheets)
    max_col = max(pem_map.values()) if pem_map else 10
    row = [""] * max_col
    row[pem_map["ID Transaksi"] - 1] = txn
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

    ok = await sheets.async_append_row(PEMINJAMAN_SHEET, row)
    if ok:
        try:
            await sheets.async_write_log(
                str(user.id),
                "AjukanPinjamCustom",
                name,
                f"tx={txn} qty={qty} dl={dl_iso} serial={serial or ''}",
            )
        except Exception:
            pass
        await send_md(
            cq,
            (
                f"✅ Permintaan CUSTOM peminjaman dibuat (TXN `{txn}`). "
                "Pemilik/admin akan diberitahu."
            ),
            parse_mode="Markdown",
        )
    else:
        await send_md(
            cq,
            "❌ Gagal membuat permintaan peminjaman custom. Coba lagi nanti.",
        )

    for k in (
        _KEY_STEP,
        _KEY_CUSTOM_NAME,
        _KEY_CUSTOM_SERIAL,
        _KEY_QTY,
        "borrow_custom_deadline",
    ):
        context.user_data.pop(k, None)


# ---------------------------------------------------------------------------
# Notify owner helper (includes serial/specs in message)
# ---------------------------------------------------------------------------
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
) -> bool:
    try:
        inv_headers = await _ensure_inventaris_headers(sheets)
        row_idx = None

        # hanya non-custom yang dicari owner-nya
        if not str(item_name).upper().startswith("CUSTOM:"):
            try:
                row_idx = await sheets.async_find_row_by_value(
                    INVENTARIS_SHEET,
                    "Nama Barang",
                    item_name,
                    headers_map=inv_headers,
                )
            except Exception:
                row_idx = await _find_item_row_by_name(
                    sheets,
                    item_name,
                    headers_map=inv_headers,
                )

        if not row_idx:
            logger.debug(
                "_notify_owner_for_request: owner not found — no row for item %s",
                item_name,
            )
            # Untuk custom request, bisa notif admin di tempat lain; di sini cukup return True
            return True

        pemilik_col = inv_headers.get("Pemilik ID")
        if not pemilik_col:
            logger.debug(
                "_notify_owner_for_request: Pemilik ID column missing",
            )
            return False

        pemilik_raw = await sheets.async_get_cell_value(
            INVENTARIS_SHEET,
            row_idx,
            pemilik_col,
        )
        try:
            owner_chat_id = int(str(pemilik_raw).strip())
        except Exception:
            logger.debug(
                "_notify_owner_for_request: bad pemilik id: %s",
                pemilik_raw,
            )
            return False

        recs = await sheets.async_get_all_records(INVENTARIS_SHEET)
        rec = None
        idx0 = row_idx - 2
        if 0 <= idx0 < len(recs):
            rec = recs[idx0]

        specs_parts = []
        if rec:
            for k in (
                "Keterangan 1",
                "Keterangan 2",
                "Serial Number",
                "Keterangan 3",
                "Keterangan",
            ):
                v = rec.get(k)
                if v:
                    specs_parts.append(str(v))
        specs_text = " | ".join(specs_parts) if specs_parts else "-"

        text_lines = [
            "📩 *Notifikasi Permintaan Peminjaman*",
            "",
            f"Barang: *{escape_md(item_name)}* (x{qty})",
            f"Peminjam: *{escape_md(borrower_name)}* (`{borrower_id}`)",
        ]
        if witel:
            text_lines.append(f"Witel: {escape_md(str(witel))}")
        if divisi:
            text_lines.append(f"Divisi: {escape_md(str(divisi))}")
        text_lines += [
            f"Spec / Serial: `{escape_md(specs_text)}`",
            f"TXN: `{escape_md(txn)}`",
            "",
            "Silakan cek sheet Peminjaman untuk menyetujui atau menolak permintaan.",
        ]
        text = "\n".join(text_lines)

        try:
            await context.application.bot.send_message(
                chat_id=owner_chat_id,
                text=text,
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.debug(
                "notify_owner: send_message with parse_mode failed: %s",
                e,
            )
            try:
                await context.application.bot.send_message(
                    chat_id=owner_chat_id,
                    text=text,
                )
            except Exception as e2:
                logger.exception(
                    "notify_owner: final send_message failed: %s",
                    e2,
                )
                return False

        try:
            await sheets.async_write_log(
                str(owner_chat_id),
                "NotifOwnerOnRequest",
                item_name,
                f"tx={txn} borrower={borrower_id}",
            )
        except Exception:
            pass

        if borrower_id:
            try:
                await context.application.bot.send_message(
                    chat_id=borrower_id,
                    text=(
                        f"ℹ️ Pemilik barang telah diberitahu tentang permintaan "
                        f"(TXN `{txn}`)."
                    ),
                    parse_mode="Markdown",
                )
            except Exception:
                pass

        return True
    except Exception as e:
        logger.exception("Failed notifying owner: %s", e)
        return False


# ---------------------------------------------------------------------------
# /mypinjam - list peminjaman milik user (only active ones)
# ---------------------------------------------------------------------------
async def mypinjam_cmd(update: Any, context: ContextTypes.DEFAULT_TYPE):
    # Bisa dipanggil dari Command (/mypinjam) atau callback (refresh)
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
                    for k in (
                        "disetujui",
                        "dipinjam",
                        "approved",
                        "borrowed",
                        "partially",
                    )
                ):
                    pairs.append((idx + 2, rec))
        except Exception:
            continue

    if not pairs:
        await send_md(
            update,
            "📭 Kamu tidak memiliki peminjaman aktif atau riwayat kosong.",
        )
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
        kb.append(
            [InlineKeyboardButton(label, callback_data=f"mytxn_view:{rownum}")]
        )
    kb.append([InlineKeyboardButton("Refresh", callback_data="mytxn_refresh")])
    await send_md(
        update,
        f"📋 Peminjaman milik kamu — total *{len(pairs)}*.",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown",
    )
    context.user_data["mytxn_pairs"] = pairs


# ---------------------------------------------------------------------------
# TXN view / cancel / return handlers
# ---------------------------------------------------------------------------
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
        kb.append(
            [
                InlineKeyboardButton(
                    "❌ Cancel Request",
                    callback_data=f"mytxn_cancel:{row_idx}",
                )
            ]
        )
    if status_norm in ("disetujui", "dipinjam", "approved", "borrowed") or (
        "partially" in status_norm
    ):
        kb.append(
            [
                InlineKeyboardButton(
                    "↩️ Request Return (instruksi)",
                    callback_data=f"mytxn_return:{row_idx}",
                )
            ]
        )
    kb.append([InlineKeyboardButton("Kembali", callback_data="mytxn_refresh")])
    await send_md(
        cq,
        teks,
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown",
    )


async def mytxn_refresh_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    await cq.answer()

    # mypinjam_cmd sudah handle require_registration
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
        raw = await sheets.async_get_cell_value(
            PEMINJAMAN_SHEET,
            row_idx,
            pem_headers["Peminjam ID"],
        )
    except Exception:
        raw = None

    try:
        pid = int(str(raw).strip()) if raw not in (None, "") else None
    except Exception:
        pid = None

    if pid != cq.from_user.id:
        await send_md(
            cq,
            "🚫 Hanya peminjam yang dapat membatalkan request ini.",
        )
        return

    try:
        await sheets.async_update_cell(
            PEMINJAMAN_SHEET,
            row_idx,
            pem_headers["Status Peminjaman"],
            "Dibatalkan",
        )
        await sheets.async_write_log(
            str(cq.from_user.id),
            "CancelRequest",
            "",
            f"row={row_idx}",
        )
        await send_md(
            cq,
            f"✅ Permintaan pada baris `{row_idx}` dibatalkan.",
            parse_mode="Markdown",
        )
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
        txn = await sheets.async_get_cell_value(
            PEMINJAMAN_SHEET,
            row_idx,
            pem_headers["ID Transaksi"],
        )
    except Exception:
        txn = None

    if not txn:
        await send_md(cq, "❌ TXN tidak ditemukan.")
        return

    await send_md(
        cq,
        "Untuk mengajukan pengembalian, silakan jalankan perintah:\n"
        f"`/kembali {escape_md(str(txn))}`",
        parse_mode="Markdown",
    )


# ---------------------------------------------------------------------------
# register handlers
# ---------------------------------------------------------------------------
def register_borrow_handlers(application=None):
    handlers = [
        CommandHandler("pinjam", pinjam_command),
        CommandHandler("mypinjam", mypinjam_cmd),
        CommandHandler("cari", cari_command),

        # DETAIL view untuk /cari (tanpa alur pinjam)
        CallbackQueryHandler(cari_view_cb, pattern=rf"^{_PREFIX_VIEW}"),

        # Alur /pinjam
        CallbackQueryHandler(borrow_category_cb, pattern=rf"^{_PREFIX_CAT}"),
        CallbackQueryHandler(borrow_item_cb, pattern=rf"^{_PREFIX_ITEM}"),
        CallbackQueryHandler(brw_confirm_item_cb, pattern=rf"^{_PREFIX_CONFIRM}"),
        CallbackQueryHandler(borrow_qty_cb, pattern=rf"^{_PREFIX_QTY}"),
        CallbackQueryHandler(borrow_cancel_cb, pattern=rf"^{_PREFIX_CANCEL}"),

        # custom confirm (button)
        CallbackQueryHandler(
            brw_custom_confirm_cb,
            pattern=rf"^{_PREFIX_CUSTOM_CONFIRM}custom_yes$",
        ),

        # TXN handlers
        CallbackQueryHandler(mytxn_view_cb, pattern=r"^mytxn_view:"),
        CallbackQueryHandler(mytxn_refresh_cb, pattern=r"^mytxn_refresh$"),
        CallbackQueryHandler(mytxn_cancel_cb, pattern=r"^mytxn_cancel:"),
        CallbackQueryHandler(mytxn_return_cb, pattern=r"^mytxn_return:"),

        # Text during flow
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
