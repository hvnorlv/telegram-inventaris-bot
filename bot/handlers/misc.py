# handlers/misc.py
from __future__ import annotations
import asyncio
import logging
from typing import Optional, Tuple, Any, Dict, List
from datetime import datetime

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    CallbackQuery,
    Message,
)
from telegram.ext import (
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    Application,
    CallbackQueryHandler,
)

from handlers.common import send_md, escape_md, require_registration
from config import INVENTARIS_SHEET, PEMINJAMAN_SHEET
from utils import sanitize_input, safe_int

logger = logging.getLogger("handlers.misc")
logger.addHandler(logging.NullHandler())

# ------------ user_data keys / steps
_K_START_STEP = "start_step"
_K_START_WITEL = "start_witel"
_STEP_START_WITEL = "start_witel"
_STEP_START_DIVISI = "start_divisi"
_PFX_START_WITEL_PICK = "start_witel_pick:"
_PFX_START_CANCEL = "start_cancel"

_K_KEMBALI_STEP = "kembali_step"
_K_KEMBALI_PAIRS = "kembali_pairs"
_K_KEMBALI_SELECTED = "kembali_selected"
_STEP_KEMBALI_SELECT = "kembali_select"
_STEP_KEMBALI_QTY = "kembali_qty"
_STATUS_PARTIAL = "Partially Returned"

_K_PDW_STEP = "pdw_step"
_K_PDW_TARGET = "pdw_target"
_STEP_PDW_PICK = "pdw_pick"
_STEP_PDW_DIVISI = "pdw_divisi"
_PFX_PDW_PICK = "pdw_pick:"
_PFX_PDW_CANCEL = "pdw_cancel"

# -------------------------
# Retry helper
# -------------------------
async def retry_async(
    fn,
    *args,
    retries: int = 3,
    delay: float = 0.6,
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
                "retry_async: caught %s (attempt %d/%d), retrying after %.2fs",
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

# ------------ Sheet helpers
async def _ensure_users_headers(sheets) -> Dict[str, int]:
    return await sheets.async_ensure_headers(
        "Users", ["User ID", "Nama", "Role", "Witel", "Divisi"]
    )

async def _ensure_inventaris_headers(sheets) -> Dict[str, int]:
    # include Item ID for robust matching
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
    headers = [
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
        "Item ID",
        "Witel",
        "Divisi",
    ]
    return await sheets.async_ensure_headers(PEMINJAMAN_SHEET, headers)

async def _find_peminjaman_row_by_txn(
    sheets, txn: str
) -> Tuple[Optional[int], Dict[str, int]]:
    pem_headers = await _ensure_peminjaman_headers(sheets)
    row_idx = await sheets.async_find_row_by_value(
        PEMINJAMAN_SHEET, "ID Transaksi", txn, headers_map=pem_headers
    )
    return row_idx, pem_headers

async def _archive_txn_row(sheets, row_idx: int, pem_headers: Dict[str, int]) -> bool:
    if not sheets or not row_idx or row_idx < 2:
        return False
    try:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        async def safe_get(colname: str) -> str:
            col_idx = pem_headers.get(colname)
            if not col_idx:
                return ""
            try:
                v = await sheets.async_get_cell_value(PEMINJAMAN_SHEET, row_idx, col_idx)
                return str(v or "")
            except Exception:
                return ""

        txn = await safe_get("ID Transaksi")
        nama = await safe_get("Nama Barang")
        pid = await safe_get("Peminjam ID")
        pname = await safe_get("Peminjam Nama")
        qty = await safe_get("Qty Dipinjam")
        tgl = await safe_get("Tanggal Pinjam")
        dl = await safe_get("Deadline")
        status = await safe_get("Status Peminjaman")

        if not txn:
            logger.warning(f"_archive_txn_row: empty TXN at row {row_idx}, skip")
            return False

        archive_headers = [
            "Archived At",
            "ID Transaksi",
            "Nama Barang",
            "Peminjam ID",
            "Peminjam Nama",
            "Qty Dipinjam",
            "Tanggal Pinjam",
            "Deadline",
            "Status Peminjaman",
        ]
        await sheets.async_ensure_headers("Peminjaman_Archive", archive_headers)
        archive_row = [ts, txn, nama, pid, pname, qty, tgl, dl, status]

        try:
            await sheets.async_append_row("Peminjaman_Archive", archive_row)
        except Exception:
            logger.exception("_archive_txn_row: append to Peminjaman_Archive failed", exc_info=True)
            return False

        try:
            id_col = pem_headers.get("ID Transaksi")
            qty_col = pem_headers.get("Qty Dipinjam")
            st_col = pem_headers.get("Status Peminjaman")

            if id_col:
                new_id = f"ARCHIVED_{txn}".strip("_")
                await sheets.async_update_cell(PEMINJAMAN_SHEET, row_idx, id_col, new_id)
            if qty_col:
                await sheets.async_update_cell(PEMINJAMAN_SHEET, row_idx, qty_col, "0")
            if st_col:
                await sheets.async_update_cell(PEMINJAMAN_SHEET, row_idx, st_col, "Archived")
        except Exception:
            logger.exception("_archive_txn_row: marking original as Archived failed", exc_info=True)
            return True

        return True
    except Exception:
        logger.exception("_archive_txn_row: unexpected error", exc_info=True)
        return False

async def _get_item_specs_for_msg(
    sheets,
    inv_row: int,
    inv_headers: Dict[str, int],
) -> str:
    parts: List[str] = []
    for key in ("Keterangan", "Keterangan 1", "Keterangan 2", "Keterangan 3", "Serial Number"):
        col = inv_headers.get(key)
        if not col:
            continue
        try:
            v = await sheets.async_get_cell_value(INVENTARIS_SHEET, inv_row, col)
        except Exception:
            v = None
        if v not in (None, ""):
            parts.append(str(v))
    return " | ".join(parts)

# ---- Witel utilities (robust read) ----
async def _fetch_witel_values(
    sheets,
    sheet_name: str = "Witel",
    header: Optional[str] = None,
) -> List[str]:
    candidates: List[str] = []
    if header:
        wanted_headers = [header.strip().lower()]
    else:
        wanted_headers = ["witel", "daftar witel"]

    try:
        recs = await sheets.async_get_all_records(sheet_name)
        if recs:
            keys_lower = {k.strip().lower(): k for k in recs[0].keys()}
            match_key = None
            for wh in wanted_headers:
                if wh in keys_lower:
                    match_key = keys_lower[wh]
                    break
            if match_key:
                for r in recs:
                    v = str(r.get(match_key) or "").strip()
                    if v:
                        candidates.append(v)
    except Exception:
        pass

    if not candidates:
        try:
            raw = await sheets.async_get_all_values(sheet_name)
            if raw and len(raw) > 0:
                header_row = raw[0]
                col_idx = None
                if len(header_row) == 1:
                    col_idx = 0
                else:
                    for i, h in enumerate(header_row):
                        if str(h).strip().lower() in wanted_headers:
                            col_idx = i
                            break
                if col_idx is None:
                    col_idx = 0
                for row in raw[1:]:
                    cell = (row[col_idx] if col_idx < len(row) else "") or ""
                    v = str(cell).strip()
                    if v:
                        candidates.append(v)
        except Exception:
            pass

    if not candidates:
        try:
            col = await sheets.async_get_range_values(sheet_name, "A2:A")
            if col:
                for r in col:
                    v = str((r[0] if r else "") or "").strip()
                    if v:
                        candidates.append(v)
        except Exception:
            pass

    out, seen = [], set()
    for v in candidates:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out

def _kb_from_list(
    options: List[str], prefix: str, add_cancel: bool = True
) -> InlineKeyboardMarkup:
    kb = []
    for w in options:
        label = w if len(w) <= 64 else (w[:61] + "...")
        kb.append([InlineKeyboardButton(label, callback_data=f"{prefix}{w}")])
    if add_cancel:
        cancel_cb = _PFX_START_CANCEL if prefix.startswith("start") else _PFX_PDW_CANCEL
        kb.append([InlineKeyboardButton("Batal", callback_data=cancel_cb)])
    return InlineKeyboardMarkup(kb)

# ------------- robust parse for 'Tersedia' values -------------
def _parse_available_field(raw: Any) -> int:
    """
    Tolerant parse of inventory cell like:
      - 5
      - ' 5'
      - 'x5'
      - '5 pcs'
      - 'x 5'
    Returns int >= 0 (0 on failure).
    """
    if raw is None:
        return 0
    s = str(raw).strip()
    if s == "":
        return 0
    # collect digits
    digits = ""
    for ch in s:
        if ch.isdigit():
            digits += ch
    if digits == "":
        return 0
    try:
        return int(digits)
    except Exception:
        return 0

# ============ /regis (replaces the interactive registration flow previously bound to /start) ============
async def regis_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    sheets = context.application.bot_data.get("sheets_manager")

    if not user:
        await send_md(update, "⚠️ Tidak dapat mengenali pengguna.")
        return
    if not sheets:
        await send_md(update, "❌ Layanan Google Sheets belum tersedia.")
        return

    uh = await _ensure_users_headers(sheets)
    uid = str(user.id)
    row_idx = await sheets.async_find_row_by_value("Users", "User ID", uid, headers_map=uh)
    if not row_idx:
        max_col = max(uh.values()) if uh else 5
        data = [""] * max_col
        data[uh["User ID"] - 1] = uid
        data[uh["Nama"] - 1] = user.full_name or user.first_name or ""
        try:
            await sheets.async_append_row("Users", data)
            row_idx = await sheets.async_find_row_by_value("Users", "User ID", uid, headers_map=uh)
        except Exception:
            logger.exception("Failed append Users row", exc_info=True)
            await send_md(update, "❌ Gagal mendaftar. Coba lagi nanti.")
            return

    cur_witel = str(await sheets.async_get_cell_value("Users", row_idx, uh["Witel"]) or "").strip()
    cur_divisi = str(await sheets.async_get_cell_value("Users", row_idx, uh["Divisi"]) or "").strip()

    if cur_witel and cur_divisi:
        await send_md(update, f"👋 Hai *{escape_md(user.first_name or user.full_name or '')}*. Profilmu sudah lengkap.\nKetik /help untuk menu.", parse_mode="Markdown")
        return

    options = await _fetch_witel_values(sheets)
    if not options:
        await send_md(update, "⚠️ Daftar Witel kosong/gagal dibaca dari sheet *Witel*.")
        return
    context.user_data[_K_START_STEP] = _STEP_START_WITEL
    await send_md(update, "📍 Pilih *Witel* kamu:", reply_markup=_kb_from_list(options, _PFX_START_WITEL_PICK), parse_mode="Markdown")

# ============ /start (welcome only) ============
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        await send_md(update, "👋 Hai! Untuk memulai, jalankan perintah /regis untuk mendaftar.")
        return

    teks = (
        f"👋 Hai *{escape_md(user.first_name or user.full_name or '')}*!\n\n"
        "Selamat datang di bot Inventaris — untuk mulai menggunakan fitur lengkap, "
        "silakan lengkapi profilmu terlebih dahulu dengan menjalankan perintah:\n\n"
        "➡️ /regis — Daftar & pilih *Witel* serta isi *Divisi* kamu.\n\n"
        "Setelah itu, coba /help untuk melihat daftar perintah."
    )
    await send_md(update, teks, parse_mode="Markdown")

async def start_witel_pick_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    await cq.answer()
    if context.user_data.get(_K_START_STEP) != _STEP_START_WITEL:
        await send_md(cq, "⚠️ Pilihan ini tidak aktif. Jalankan /regis lagi.")
        return
    witel = (cq.data or "").split(":", 1)[1].strip()
    context.user_data[_K_START_WITEL] = witel
    context.user_data[_K_START_STEP] = _STEP_START_DIVISI
    await send_md(cq, f"📍 Witel: *{escape_md(witel)}*\n\n🏢 Ketik *Divisi* kamu (mis. `NOC` / `FIELD`).", parse_mode="Markdown")

async def start_divisi_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if context.user_data.get(_K_START_STEP) != _STEP_START_DIVISI:
        return False

    divisi = (update.message.text or "").strip()
    if not divisi:
        await send_md(update, "⚠️ Divisi tidak boleh kosong. Coba lagi.")
        return True

    sheets = context.application.bot_data.get("sheets_manager")
    if not sheets:
        await send_md(update, "❌ Layanan Sheets tidak tersedia.")
        return True

    uh = await _ensure_users_headers(sheets)
    uid = str(update.effective_user.id)
    row_idx = await sheets.async_find_row_by_value("Users", "User ID", uid, headers_map=uh)
    if not row_idx:
        await send_md(update, "⚠️ Akunmu belum terdaftar. Jalankan /regis dulu.")
        return True

    witel = context.user_data.get(_K_START_WITEL, "")
    try:
        await sheets.async_update_cell("Users", row_idx, uh["Witel"], witel)
        await sheets.async_update_cell("Users", row_idx, uh["Divisi"], divisi)
        try:
            await sheets.async_write_log(uid, "SetProfile", "", f"Witel={witel} Divisi={divisi}")
        except Exception:
            pass
    except Exception:
        logger.exception("Failed saving profile", exc_info=True)
        await send_md(update, "❌ Gagal menyimpan Witel/Divisi. Coba /regis lagi.")
        return True

    context.user_data.pop(_K_START_STEP, None)
    context.user_data.pop(_K_START_WITEL, None)
    await send_md(update, "✅ Profil tersimpan. Ketik /help untuk menu.")
    return True

async def start_cancel_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    await cq.answer()
    context.user_data.pop(_K_START_STEP, None)
    context.user_data.pop(_K_START_WITEL, None)
    await send_md(cq, "✅ Dibatalkan.")

# ============ Help ringkas ============
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    teks = (
        "📘 *Panduan Lengkap Perintah Bot*\n\n"
        "👤 *Akun & Profil*\n"
        "• /regis — daftar dan isi Witel & Divisi kamu\n"
        "• /pindahwitel — pindah Witel & Divisi (otomatis pindahkan barang yang kamu miliki)\n"
        "• /lokasi <Nama Barang> — cek lokasi dan pemilik suatu barang\n"
        "• /cancel — batalkan semua alur interaktif yang sedang berjalan\n\n"
        "🧰 *Inventaris*\n"
        "• /tambah — tambah barang baru ke inventaris (flow interaktif)\n"
        "• /inventaris — buka menu inventaris (filter kategori, status, witel)\n"
        "• /myitems — tampilkan daftar barang yang kamu miliki\n"
        "• /hapus <Nama Barang> <qty|all> — kurangi atau hapus stok\n\n"
        "🔍 *Pencarian*\n"
        "• /cari <keyword> — cari barang dalam inventaris (berdasarkan nama, kategori, atau serial)\n\n"
        "📦 *Peminjaman*\n"
        "• /pinjam — ajukan peminjaman (flow interaktif)\n"
        "• /pinjam <Nama Item> <qty> [deadline] — ajukan pinjam cepat\n"
        "• /mypinjam — lihat daftar pinjaman kamu yang aktif\n"
        "• /kembali [TXN_ID] — minta atau konfirmasi pengembalian barang\n\n"
        "🛠️ *Owner / Pemilik Barang*\n"
        "• /approve <TXN_ID> — setujui permintaan pinjam barangmu\n"
        "• /reject <TXN_ID> — tolak permintaan pinjam barangmu\n"
        "• /hapus <Nama Barang> <qty|all> — hapus atau kurangi stok\n\n"
        "🧑‍💼 *Admin*\n"
        "• /admin — buka menu admin (list peminjaman aktif, manage transaksi, danger purge)\n\n"
        "ℹ️ *Informasi*\n"
        "• /help — tampilkan daftar perintah lengkap ini\n\n"
        "💡 *Tips:* Pastikan kamu sudah menjalankan /regis dan memilih *Witel* serta *Divisi* sebelum menggunakan fitur lainnya."
    )
    await send_md(update, teks, parse_mode="Markdown")

# ============ Approve / Reject helpers & robust fuzzy resolver ============
async def _find_inventaris_by_item_or_fields(
    sheets,
    item_id: Optional[str],
    nama: str,
    keterangan: Optional[str],
    k1: Optional[str],
    k2: Optional[str],
    witel: Optional[str],
    divisi: Optional[str],
) -> Tuple[Optional[int], Dict[str, int]]:
    """
    Cari row di INVENTARIS:
     - utamakan Item ID bila disediakan
     - fallback: cari berdasarkan Nama Barang + keterangan/serial + witel/divisi
    Return (row_idx, headers)
    """
    inv_headers = await _ensure_inventaris_headers(sheets)
    try:
        recs = await retry_async(sheets.async_get_all_records, INVENTARIS_SHEET, retries=3)
    except Exception:
        logger.exception("_find_inventaris_by_item_or_fields: failed to read inventaris", exc_info=True)
        recs = []

    # 1) by Item ID
    if item_id:
        for idx, r in enumerate(recs):
            try:
                if str(r.get("Item ID") or "").strip() == str(item_id).strip():
                    return idx + 2, inv_headers
            except Exception:
                continue

    # normalize helper
    def _norm(v):
        return "" if v is None else str(v).strip().lower()

    target = {
        "nama": _norm(nama),
        "ket": _norm(keterangan or ""),
        "k1": _norm(k1 or ""),
        "k2": _norm(k2 or ""),
        "witel": _norm(witel or ""),
        "divisi": _norm(divisi or ""),
    }

    for idx, r in enumerate(recs):
        try:
            if _norm(r.get("Nama Barang")) != target["nama"]:
                continue
            # match at least one of keterangan / k1 / k2 / serial (if provided)
            same_k1 = target["k1"] == _norm(r.get("Keterangan 1"))
            same_k2 = target["k2"] == _norm(r.get("Keterangan 2"))
            same_ket = target["ket"] == _norm(r.get("Keterangan"))
            same_serial = target["k1"] == _norm(r.get("Serial Number")) or target["k2"] == _norm(r.get("Serial Number"))
            # if no additional info provided, accept by name+witel/divisi
            if target["k1"] == "" and target["k2"] == "" and target["ket"] == "":
                pass  # allow
            else:
                if not (same_k1 or same_k2 or same_ket or same_serial):
                    continue
            if target["witel"] and _norm(r.get("Witel")) != target["witel"]:
                continue
            if target["divisi"] and _norm(r.get("Divisi")) != target["divisi"]:
                continue
            return idx + 2, inv_headers
        except Exception:
            continue

    return None, inv_headers

async def _resolve_inv_row_with_fallback(sheets, itemid: Optional[str], nama: str) -> Tuple[Optional[int], Dict[str, int]]:
    """
    Robust resolver used by pending-list and other places:
    try in order:
      1) _find_inventaris_by_item_or_fields (if itemid)
      2) exact find_row_by_value Nama Barang
      3) fuzzy scan (contains / keterangan / serial contains)
    """
    inv_headers = await _ensure_inventaris_headers(sheets)

    # 1) try by itemid via helper
    if itemid:
        try:
            r = await _find_inventaris_by_item_or_fields(sheets, itemid, nama, None, None, None, None)
            if r and r[0]:
                return r[0], r[1]
        except Exception:
            logger.debug("resolve_inv: itemid search failed", exc_info=True)

    # 2) exact by name (fast)
    try:
        row = await sheets.async_find_row_by_value(INVENTARIS_SHEET, "Nama Barang", nama, headers_map=inv_headers)
        if row:
            return row, inv_headers
    except Exception:
        pass

    # 3) fuzzy fallback scan
    try:
        recs = await retry_async(sheets.async_get_all_records, INVENTARIS_SHEET, retries=2)
        target = (nama or "").strip().lower()
        best = None
        for idx, r in enumerate(recs):
            try:
                in_name = str(r.get("Nama Barang") or "").strip().lower()
                if not in_name:
                    continue
                if target == in_name or (target and target in in_name):
                    best = idx + 2
                    break
                # search in other descriptive fields
                for fld in ("Keterangan", "Keterangan 1", "Keterangan 2", "Serial Number"):
                    v = str(r.get(fld) or "").strip().lower()
                    if v and target and target in v:
                        best = idx + 2
                        break
                if best:
                    break
            except Exception:
                continue
        if best:
            return best, inv_headers
    except Exception:
        logger.debug("resolve_inv: fuzzy scan failed", exc_info=True)

    return None, inv_headers

# --- Approve command (list + direct) ---
async def approve_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_registration(update, context):
        return

    user = update.effective_user
    sheets = context.application.bot_data.get("sheets_manager")
    if not sheets:
        await send_md(update, "❌ Google Sheets tidak tersedia.")
        return

    # direct /approve <txn>
    if context.args and len(context.args) >= 1:
        txn = sanitize_input(context.args[0])
        await _process_approve_txn(update, context, txn)
        return

    # list pending for which caller is owner
    try:
        pem_recs = await retry_async(sheets.async_get_all_records, PEMINJAMAN_SHEET, retries=3)
    except Exception:
        await send_md(update, "❌ Gagal membaca daftar peminjaman.")
        return

    pending_list: List[Tuple[int, Dict[str, Any]]] = []
    for idx, rec in enumerate(pem_recs):
        try:
            status = str(rec.get("Status Peminjaman") or "").strip().lower()
            if not any(k in status for k in ("menunggu", "pending", "waiting", "awaiting_owner", "awaiting approval")):
                continue

            nama = str(rec.get("Nama Barang") or "").strip()
            itemid = str(rec.get("Item ID") or "").strip() or None

            inv_row, inv_headers = await _resolve_inv_row_with_fallback(sheets, itemid, nama)
            pemilik_raw = None
            if inv_row:
                try:
                    pemilik_raw = await sheets.async_get_cell_value(INVENTARIS_SHEET, inv_row, inv_headers.get("Pemilik ID"))
                except Exception:
                    pemilik_raw = None

            if pemilik_raw and str(pemilik_raw).strip() != "":
                try:
                    if int(str(pemilik_raw).strip()) == int(user.id):
                        pending_list.append((idx + 2, rec))
                except Exception:
                    if str(pemilik_raw).strip() == str(user.id).strip():
                        pending_list.append((idx + 2, rec))
        except Exception:
            continue

    if not pending_list:
        kb = [[InlineKeyboardButton("Kembali", callback_data="inv_main")],
              [InlineKeyboardButton("Tutup", callback_data="ok_close")]]
        await send_md(update, "📭 Tidak ada permintaan pinjam yang menunggu persetujuan untuk barang milikmu.", reply_markup=InlineKeyboardMarkup(kb))
        return

    kb = []
    for row_idx, rec in pending_list[:30]:
        tx = rec.get("ID Transaksi") or f"row{row_idx}"
        name = rec.get("Nama Barang") or ""
        qty = rec.get("Qty Dipinjam") or ""
        label = f"{tx} — {name} (x{qty})"
        if len(label) > 64:
            label = label[:61] + "..."
        kb.append([InlineKeyboardButton(label, callback_data=f"approve_do:{tx}")])

    kb.append([InlineKeyboardButton("Batal", callback_data=f"{_PFX_START_CANCEL}")])
    await send_md(update, f"📋 Permintaan yang menunggu persetujuan untuk barang milikmu — total *{len(pending_list)}*.", reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")

# --- Reject command (mirror approve) ---
async def reject_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_registration(update, context):
        return

    user = update.effective_user
    sheets = context.application.bot_data.get("sheets_manager")
    if not sheets:
        await send_md(update, "❌ Google Sheets tidak tersedia.")
        return

    if context.args and len(context.args) >= 1:
        txn = sanitize_input(context.args[0])
        await _process_reject_txn(update, context, txn)
        return

    try:
        pem_recs = await retry_async(sheets.async_get_all_records, PEMINJAMAN_SHEET, retries=3)
    except Exception:
        await send_md(update, "❌ Gagal membaca daftar peminjaman.")
        return

    pending_list: List[Tuple[int, Dict[str, Any]]] = []
    for idx, rec in enumerate(pem_recs):
        try:
            status = str(rec.get("Status Peminjaman") or "").strip().lower()
            if not any(k in status for k in ("menunggu", "pending", "waiting", "awaiting_owner", "awaiting approval")):
                continue

            nama = str(rec.get("Nama Barang") or "").strip()
            itemid = str(rec.get("Item ID") or "").strip() or None

            inv_row, inv_headers = await _resolve_inv_row_with_fallback(sheets, itemid, nama)
            pemilik_raw = None
            if inv_row:
                try:
                    pemilik_raw = await sheets.async_get_cell_value(INVENTARIS_SHEET, inv_row, inv_headers.get("Pemilik ID"))
                except Exception:
                    pemilik_raw = None

            if pemilik_raw and str(pemilik_raw).strip() != "":
                try:
                    if int(str(pemilik_raw).strip()) == int(user.id):
                        pending_list.append((idx + 2, rec))
                except Exception:
                    if str(pemilik_raw).strip() == str(user.id).strip():
                        pending_list.append((idx + 2, rec))
        except Exception:
            continue

    if not pending_list:
        kb = [[InlineKeyboardButton("Kembali", callback_data="inv_main")],
              [InlineKeyboardButton("Tutup", callback_data="ok_close")]]
        await send_md(update, "📭 Tidak ada permintaan pinjam yang menunggu persetujuan untuk barang milikmu.", reply_markup=InlineKeyboardMarkup(kb))
        return

    kb = []
    for row_idx, rec in pending_list[:30]:
        tx = rec.get("ID Transaksi") or f"row{row_idx}"
        name = rec.get("Nama Barang") or ""
        qty = rec.get("Qty Dipinjam") or ""
        label = f"{tx} — {name} (x{qty})"
        if len(label) > 64:
            label = label[:61] + "..."
        kb.append([InlineKeyboardButton(label, callback_data=f"reject_do:{tx}")])

    kb.append([InlineKeyboardButton("Batal", callback_data=f"{_PFX_START_CANCEL}")])
    await send_md(update, f"📋 Permintaan yang menunggu persetujuan untuk barang milikmu — total *{len(pending_list)}*.", reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")

# --- Callback handlers for approve/reject buttons ---
async def approve_do_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    await cq.answer()

    if not await require_registration(cq, context):
        return

    data = (cq.data or "").split(":", 1)
    if len(data) < 2:
        await send_md(cq, "Aksi tidak dikenali.")
        return
    txn = data[1].strip()
    await _process_approve_txn(cq, context, txn)

async def reject_do_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    await cq.answer()

    if not await require_registration(cq, context):
        return

    data = (cq.data or "").split(":", 1)
    if len(data) < 2:
        await send_md(cq, "Aksi tidak dikenali.")
        return
    txn = data[1].strip()
    await _process_reject_txn(cq, context, txn)

# --- Shared approve/reject processing ---
async def _process_approve_txn(update_or_cq, context, txn: str):
    sheets = context.application.bot_data.get("sheets_manager")
    if not sheets:
        await send_md(update_or_cq, "❌ Google Sheets tidak tersedia.")
        return

    row_idx, pem_headers = await _find_peminjaman_row_by_txn(sheets, txn)
    if not row_idx:
        await send_md(update_or_cq, f"❌ TXN `{escape_md(txn)}` tidak ditemukan.", parse_mode="Markdown")
        return

    status = await sheets.async_get_cell_value(PEMINJAMAN_SHEET, row_idx, pem_headers["Status Peminjaman"])
    st = str(status or "").strip().lower()
    if st.startswith("disetujui") or "dikembalikan" in st or "returned" in st:
        await send_md(update_or_cq, f"⚠️ TXN `{escape_md(txn)}` tidak bisa disetujui (status: `{escape_md(str(status))}`).", parse_mode="Markdown")
        return

    nama = await sheets.async_get_cell_value(PEMINJAMAN_SHEET, row_idx, pem_headers["Nama Barang"])
    qty = safe_int(await sheets.async_get_cell_value(PEMINJAMAN_SHEET, row_idx, pem_headers["Qty Dipinjam"]), 0)
    if qty <= 0:
        await send_md(update_or_cq, "⚠️ Jumlah pinjam tidak valid.")
        return

    try:
        txn_itemid = str(await sheets.async_get_cell_value(PEMINJAMAN_SHEET, row_idx, pem_headers.get("Item ID")) or "").strip()
        if txn_itemid == "":
            txn_itemid = None
    except Exception:
        txn_itemid = None

    try:
        keterangan = await sheets.async_get_cell_value(PEMINJAMAN_SHEET, row_idx, pem_headers.get("Keterangan")) or ""
    except Exception:
        keterangan = ""
    try:
        txn_witel = await sheets.async_get_cell_value(PEMINJAMAN_SHEET, row_idx, pem_headers.get("Witel")) or ""
    except Exception:
        txn_witel = ""
    try:
        txn_divisi = await sheets.async_get_cell_value(PEMINJAMAN_SHEET, row_idx, pem_headers.get("Divisi")) or ""
    except Exception:
        txn_divisi = ""

    inv_row, inv_headers = await _find_inventaris_by_item_or_fields(sheets, txn_itemid, nama, keterangan, None, None, txn_witel, txn_divisi)
    if not inv_row:
        # attempt fuzzy resolve as last resort
        inv_row, inv_headers = await _resolve_inv_row_with_fallback(sheets, txn_itemid, nama)
    if not inv_row:
        await send_md(update_or_cq, f"⚠️ Item *{escape_md(str(nama or ''))}* tidak ditemukan di inventaris (coba pastikan Item ID ada di transaksi).", parse_mode="Markdown")
        return

    # permission check
    caller_id = None
    if isinstance(update_or_cq, CallbackQuery):
        caller_id = update_or_cq.from_user.id
    else:
        caller_id = update_or_cq.effective_user.id if getattr(update_or_cq, "effective_user", None) else None

    pemilik_id = None
    try:
        pemilik_raw = await sheets.async_get_cell_value(INVENTARIS_SHEET, inv_row, inv_headers.get("Pemilik ID"))
        if pemilik_raw not in (None, ""):
            try:
                pemilik_id = int(str(pemilik_raw).strip())
            except Exception:
                pemilik_id = None
    except Exception:
        pemilik_id = None

    allowed = False
    if pemilik_id and caller_id and int(pemilik_id) == int(caller_id):
        allowed = True
    pm = context.application.bot_data.get("permission_manager")
    if not allowed and pm:
        try:
            if hasattr(pm, "async_has_permission"):
                if await pm.async_has_permission(caller_id, "can_approve") or await pm.async_has_permission(caller_id, "admin"):
                    allowed = True
            else:
                if getattr(pm, "has_permission", lambda *_: False)(caller_id, "can_approve") or getattr(pm, "is_admin", lambda *_: False)(caller_id):
                    allowed = True
        except Exception:
            logger.debug("perm check fail /approve", exc_info=True)

    if not allowed:
        await send_md(update_or_cq, "🚫 Hanya pemilik atau admin yang dapat menyetujui.", parse_mode="Markdown")
        return

    # read current available
    tersedia_col = inv_headers.get("Tersedia")
    try:
        raw_ters = await retry_async(sheets.async_get_cell_value, INVENTARIS_SHEET, inv_row, tersedia_col, retries=2)
    except Exception:
        raw_ters = None
    cur_ters = _parse_available_field(raw_ters)

    if qty > cur_ters:
        await send_md(update_or_cq, f"⚠️ Stok tidak cukup (tersedia {cur_ters}, diminta {qty}).", parse_mode="Markdown")
        return

    # decrement stok (try increment_cell, fallback to read/update)
    ok_decr = False
    new_val = None
    try:
        try:
            res = await retry_async(sheets.async_increment_cell, INVENTARIS_SHEET, inv_row, tersedia_col, -qty, retries=3)
            if isinstance(res, tuple) and len(res) >= 1:
                ok_decr = bool(res[0])
                new_val = res[1] if len(res) > 1 else None
            elif isinstance(res, bool):
                ok_decr = res
            else:
                ok_decr = True
        except AttributeError:
            cur_raw = await retry_async(sheets.async_get_cell_value, INVENTARIS_SHEET, inv_row, tersedia_col, retries=2)
            cur_num = _parse_available_field(cur_raw)
            updated_num = max(0, cur_num - qty)
            ok_decr = await retry_async(sheets.async_update_cell, INVENTARIS_SHEET, inv_row, tersedia_col, str(updated_num), retries=3)
            new_val = updated_num
    except Exception:
        logger.exception("approve: decrement failed", exc_info=True)
        ok_decr = False

    if not ok_decr:
        await send_md(update_or_cq, "⚠️ Gagal memperbarui stok (konflik/IO). Coba lagi.", parse_mode="Markdown")
        return

    # update peminjaman status
    try:
        await retry_async(sheets.async_update_cell, PEMINJAMAN_SHEET, row_idx, pem_headers["Status Peminjaman"], "Disetujui", retries=3)
        try:
            await retry_async(sheets.async_write_log, str(caller_id), "Approve", nama, f"tx={txn} qty={qty}", retries=2)
        except Exception:
            pass
    except Exception:
        logger.exception("approve: update txn status failed", exc_info=True)

    itemname = str(await retry_async(sheets.async_get_cell_value, INVENTARIS_SHEET, inv_row, inv_headers.get("Nama Barang"), retries=2) or nama)

    await send_md(update_or_cq, f"✅ TXN `{escape_md(txn)}` disetujui. Stok dikurangi {qty} dari *{escape_md(itemname)}*.", parse_mode="Markdown")

    # notify peminjam
    try:
        peminjam_id_raw = await retry_async(sheets.async_get_cell_value, PEMINJAMAN_SHEET, row_idx, pem_headers["Peminjam ID"], retries=2)
        pid = int(str(peminjam_id_raw).strip())
        try:
            await context.application.bot.send_message(chat_id=pid, text=f"✅ Permintaan `{escape_md(txn)}` disetujui.", parse_mode="Markdown")
        except Exception:
            pass
    except Exception:
        pass

async def _process_reject_txn(update_or_cq, context, txn: str):
    sheets = context.application.bot_data.get("sheets_manager")
    if not sheets:
        await send_md(update_or_cq, "❌ Google Sheets tidak tersedia.")
        return

    row_idx, pem_headers = await _find_peminjaman_row_by_txn(sheets, txn)
    if not row_idx:
        await send_md(update_or_cq, f"❌ TXN `{escape_md(txn)}` tidak ditemukan.", parse_mode="Markdown")
        return

    status = await sheets.async_get_cell_value(PEMINJAMAN_SHEET, row_idx, pem_headers["Status Peminjaman"])
    st = str(status or "").strip().lower()
    if st.startswith("disetujui") or "dikembalikan" in st or "returned" in st:
        await send_md(update_or_cq, f"⚠️ TXN `{escape_md(txn)}` tidak bisa ditolak (status: `{escape_md(str(status))}`).", parse_mode="Markdown")
        return

    nama = await sheets.async_get_cell_value(PEMINJAMAN_SHEET, row_idx, pem_headers["Nama Barang"])

    # permission check
    caller_id = None
    if isinstance(update_or_cq, CallbackQuery):
        caller_id = update_or_cq.from_user.id
    else:
        caller_id = update_or_cq.effective_user.id if getattr(update_or_cq, "effective_user", None) else None

    # resolve inv row robustly
    inv_row, inv_headers = await _resolve_inv_row_with_fallback(sheets, None, nama)
    pemilik_id = None
    try:
        if inv_row:
            pemilik_raw = await sheets.async_get_cell_value(INVENTARIS_SHEET, inv_row, inv_headers.get("Pemilik ID"))
            if pemilik_raw not in (None, ""):
                try:
                    pemilik_id = int(str(pemilik_raw).strip())
                except Exception:
                    pemilik_id = None
    except Exception:
        pemilik_id = None

    allowed = False
    if pemilik_id and caller_id and int(pemilik_id) == int(caller_id):
        allowed = True
    pm = context.application.bot_data.get("permission_manager")
    if not allowed and pm:
        try:
            if hasattr(pm, "async_has_permission"):
                if await pm.async_has_permission(caller_id, "can_approve") or await pm.async_has_permission(caller_id, "admin"):
                    allowed = True
            else:
                if getattr(pm, "has_permission", lambda *_: False)(caller_id, "can_approve") or getattr(pm, "is_admin", lambda *_: False)(caller_id):
                    allowed = True
        except Exception:
            pass

    if not allowed:
        await send_md(update_or_cq, "🚫 Hanya pemilik atau admin yang dapat menolak.", parse_mode="Markdown")
        return

    # mark rejected
    try:
        await retry_async(sheets.async_update_cell, PEMINJAMAN_SHEET, row_idx, pem_headers["Status Peminjaman"], "Ditolak", retries=2)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        note_col = pem_headers.get("Keterangan")
        if note_col:
            old = await retry_async(sheets.async_get_cell_value, PEMINJAMAN_SHEET, row_idx, note_col, retries=2)
            newk = (old or "") + f" | RejectedBy:{caller_id}@{now}"
            await retry_async(sheets.async_update_cell, PEMINJAMAN_SHEET, row_idx, note_col, newk, retries=2)
        try:
            await retry_async(sheets.async_write_log, str(caller_id), "Reject", nama, f"tx={txn}", retries=2)
        except Exception:
            pass
        await send_md(update_or_cq, f"✅ TXN `{escape_md(txn)}` ditolak.", parse_mode="Markdown")

        try:
            peminjam_id_raw = await retry_async(sheets.async_get_cell_value, PEMINJAMAN_SHEET, row_idx, pem_headers["Peminjam ID"], retries=2)
            pid = int(str(peminjam_id_raw).strip()) if peminjam_id_raw not in (None, "") else None
            if pid:
                await context.application.bot.send_message(chat_id=pid, text=f"❌ Permintaan `{escape_md(txn)}` ditolak.", parse_mode="Markdown")
        except Exception:
            pass
    except Exception:
        logger.exception("reject failed", exc_info=True)
        await send_md(update_or_cq, "❌ Gagal menolak permintaan.")

# ============ Kembali (interaktif & quick) ============
async def kembali_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_registration(update, context):
        return

    user = update.effective_user
    sheets = context.application.bot_data.get("sheets_manager")
    pm = context.application.bot_data.get("permission_manager")

    if not sheets:
        await send_md(update, "❌ Google Sheets tidak tersedia.")
        return

    if context.args and len(context.args) >= 1:
        txn = sanitize_input(context.args[0])
        row_idx, pem_headers = await _find_peminjaman_row_by_txn(sheets, txn)
        if not row_idx:
            await send_md(update, f"❌ TXN `{escape_md(txn)}` tidak ditemukan.", parse_mode="Markdown")
            return

        status = await sheets.async_get_cell_value(PEMINJAMAN_SHEET, row_idx, pem_headers["Status Peminjaman"])
        status_norm = str(status).strip().lower()
        if "dikembalikan" in status_norm or "returned" in status_norm:
            await send_md(update, f"⚠️ TXN `{escape_md(txn)}` sudah dikembalikan.", parse_mode="Markdown")
            return
        if status_norm.startswith("returnrequested"):
            await send_md(update, f"ℹ️ Pengembalian untuk TXN `{escape_md(txn)}` sudah diminta.", parse_mode="Markdown")
            return

        peminjam_id_raw = await sheets.async_get_cell_value(PEMINJAMAN_SHEET, row_idx, pem_headers["Peminjam ID"])
        try:
            peminjam_id = int(str(peminjam_id_raw).strip())
        except Exception:
            peminjam_id = None

        caller_id = user.id if user else None
        allowed = peminjam_id and caller_id and int(peminjam_id) == int(caller_id)

        if not allowed and pm:
            try:
                if hasattr(pm, "async_has_permission"):
                    if await pm.async_has_permission(caller_id, "admin"):
                        allowed = True
                else:
                    if getattr(pm, "has_permission", lambda *_: False)(caller_id, "admin"):
                        allowed = True
            except Exception:
                pass

        if not allowed:
            try:
                nama = await sheets.async_get_cell_value(PEMINJAMAN_SHEET, row_idx, pem_headers["Nama Barang"])
                inv_row, inv_headers = await _resolve_inv_row_with_fallback(sheets, None, nama)
                if inv_row:
                    pemilik_raw = await sheets.async_get_cell_value(INVENTARIS_SHEET, inv_row, inv_headers.get("Pemilik ID"))
                    pemilik_id = int(str(pemilik_raw).strip()) if pemilik_raw not in (None, "") else None
                    if pemilik_id and caller_id and int(pemilik_id) == int(caller_id):
                        allowed = True
            except Exception:
                pass

        if not allowed:
            await send_md(update, "🚫 Hanya peminjam, pemilik barang, atau admin yang dapat meminta pengembalian.", parse_mode="Markdown")
            return

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            await retry_async(sheets.async_update_cell, PEMINJAMAN_SHEET, row_idx, pem_headers["Status Peminjaman"], "ReturnRequested", retries=2)
            await retry_async(sheets.async_update_cell, PEMINJAMAN_SHEET, row_idx, pem_headers["ReturnRequestedAt"], now, retries=2)
            await retry_async(sheets.async_update_cell, PEMINJAMAN_SHEET, row_idx, pem_headers["ReturnRequestedBy"], str(caller_id), retries=2)
            await retry_async(sheets.async_write_log, str(caller_id), "ReturnRequested", txn, f"by={caller_id}", retries=2)
        except Exception:
            await send_md(update, "❌ Gagal mencatat permintaan pengembalian.")
            return

        try:
            nama = await retry_async(sheets.async_get_cell_value, PEMINJAMAN_SHEET, row_idx, pem_headers["Nama Barang"], retries=2)
            inv_row, inv_headers = await _resolve_inv_row_with_fallback(sheets, None, nama)
            pemilik_id = None
            specs_text = ""
            if inv_row:
                pemilik_raw = await retry_async(sheets.async_get_cell_value, INVENTARIS_SHEET, inv_row, inv_headers.get("Pemilik ID"), retries=2)
                pemilik_id = int(str(pemilik_raw).strip()) if pemilik_raw not in (None, "") else None
                try:
                    specs_text = await _get_item_specs_for_msg(sheets, inv_row, inv_headers)
                except Exception:
                    specs_text = ""

            detail_line = f"\nDetail: `{escape_md(specs_text)}`" if specs_text else ""
            kb = [
                [
                    InlineKeyboardButton("✅ Konfirmasi Terima", callback_data=f"return_confirm:approve:{txn}"),
                    InlineKeyboardButton("❌ Tolak", callback_data=f"return_confirm:deny:{txn}"),
                ]
            ]
            text = (
                "📩 Permintaan Pengembalian\n\n"
                f"TXN: `{escape_md(txn)}`\n"
                f"Barang: *{escape_md(nama)}*"
                f"{detail_line}\n"
                f"Permintaan oleh: `{escape_md(str(caller_id))}`\n\n"
                "Tekan ✅ jika sudah diterima, atau ❌ untuk menolak."
            )
            sent = False
            if pemilik_id:
                try:
                    await context.application.bot.send_message(chat_id=pemilik_id, text=text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))
                    sent = True
                except Exception:
                    pass
            if not sent:
                await send_md(update, "⚠️ Pemilik/admin tidak dapat dihubungi. Permintaan dicatat.")
            else:
                await send_md(update, f"✅ Permintaan pengembalian untuk `{escape_md(txn)}` dikirim.", parse_mode="Markdown")
        except Exception:
            await send_md(update, "✅ Permintaan pengembalian dicatat, notifikasi ke pemilik gagal.")
        return

    # INTERACTIVE: list user's active loans
    if not user:
        await send_md(update, "⚠️ Tidak dapat mengenali pengguna.")
        return

    try:
        pem_recs = await retry_async(sheets.async_get_all_records, PEMINJAMAN_SHEET, retries=3)
    except Exception:
        await send_md(update, "❌ Gagal mengambil data peminjaman.")
        return

    pairs: List[Tuple[int, Dict[str, Any]]] = []
    for idx, rec in enumerate(pem_recs):
        try:
            if str(rec.get("Peminjam ID") or "").strip() != str(user.id):
                continue
            status = (rec.get("Status Peminjaman") or "").strip().lower()
            if any(k in status for k in ("disetujui", "dipinjam", "approved", "borrowed", _STATUS_PARTIAL.lower())):
                pairs.append((idx + 2, rec))
        except Exception:
            continue

    if not pairs:
        await send_md(update, "📭 Kamu tidak punya peminjaman aktif saat ini.", parse_mode="Markdown")
        return

    kb = []
    for rownum, rec in pairs[:15]:
        tx = rec.get("ID Transaksi") or f"row{rownum}"
        name = rec.get("Nama Barang") or ""
        qty = rec.get("Qty Dipinjam") or ""
        label = f"{tx} — {name} (x{qty})"
        if len(label) > 64:
            label = label[:61] + "..."
        kb.append([InlineKeyboardButton(label, callback_data=f"kembali_select:{rownum}")])
    kb.append([InlineKeyboardButton("Batal", callback_data="kembali_cancel")])

    context.user_data[_K_KEMBALI_PAIRS] = pairs
    context.user_data[_K_KEMBALI_STEP] = _STEP_KEMBALI_SELECT
    await send_md(update, f"📋 Pilih item yang ingin dikembalikan — total *{len(pairs)}*.", reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")

async def kembali_select_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    await cq.answer()
    step = context.user_data.get(_K_KEMBALI_STEP)
    if step != _STEP_KEMBALI_SELECT:
        await send_md(cq, "⚠️ Pilihan ini hanya berlaku saat memilih barang untuk dikembalikan.")
        return

    data = (cq.data or "").split(":", 1)
    if len(data) < 2:
        await send_md(cq, "Pilihan tidak dikenali.")
        return
    try:
        row_idx = int(data[1])
    except Exception:
        await send_md(cq, "TXN tidak valid.")
        return

    pairs: List[Tuple[int, Dict[str, Any]]] = context.user_data.get(_K_KEMBALI_PAIRS) or []
    chosen = None
    for r, rec in pairs:
        if r == row_idx:
            chosen = (r, rec)
            break
    if not chosen:
        await send_md(cq, "Pilihan tidak ditemukan. Coba /kembali lagi.")
        context.user_data.pop(_K_KEMBALI_STEP, None)
        context.user_data.pop(_K_KEMBALI_PAIRS, None)
        return

    context.user_data[_K_KEMBALI_SELECTED] = chosen
    context.user_data[_K_KEMBALI_STEP] = _STEP_KEMBALI_QTY

    rec = chosen[1]
    tx = rec.get("ID Transaksi") or ""
    name = rec.get("Nama Barang") or ""
    qty = rec.get("Qty Dipinjam") or ""
    try:
        await cq.edit_message_text((f"📄 TXN: `{escape_md(str(tx))}`\nBarang: *{escape_md(str(name))}*\nQty dipinjam: *{escape_md(str(qty))}*\n\nMasukkan jumlah yang akan dikembalikan (angka):"), parse_mode="Markdown")
    except Exception:
        await send_md(cq, f"Masukkan jumlah yang akan dikembalikan untuk {name} (maks {qty}):")

async def handle_kembali_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    step = context.user_data.get(_K_KEMBALI_STEP)
    if step != _STEP_KEMBALI_QTY:
        return False

    text = (update.message.text or "").strip()
    qty = safe_int(text, -1)
    if qty <= 0:
        await send_md(update, "⚠️ Masukkan angka > 0 untuk jumlah yang dikembalikan.")
        return True

    chosen = context.user_data.get(_K_KEMBALI_SELECTED)
    if not chosen:
        await send_md(update, "⚠️ Tidak ada item yang dipilih. Mulai ulang dengan /kembali.")
        context.user_data.pop(_K_KEMBALI_STEP, None)
        context.user_data.pop(_K_KEMBALI_PAIRS, None)
        context.user_data.pop(_K_KEMBALI_SELECTED, None)
        return True

    row_idx, rec = chosen
    borrowed_qty = safe_int(rec.get("Qty Dipinjam") or 0, 0)
    if qty > borrowed_qty:
        await send_md(update, f"⚠️ Jumlah pengembalian ({qty}) melebihi jumlah dipinjam ({borrowed_qty}).")
        return True

    sheets = context.application.bot_data.get("sheets_manager")
    if not sheets:
        await send_md(update, "❌ Layanan Google Sheets tidak tersedia.")
        return True

    pem_headers = await _ensure_peminjaman_headers(sheets)
    try:
        txn = await retry_async(sheets.async_get_cell_value, PEMINJAMAN_SHEET, row_idx, pem_headers["ID Transaksi"], retries=2)
    except Exception:
        txn = None

    if not txn:
        await send_md(update, "❌ Gagal menemukan TXN pada sheet. Coba lagi nanti.")
        context.user_data.pop(_K_KEMBALI_STEP, None)
        context.user_data.pop(_K_KEMBALI_PAIRS, None)
        context.user_data.pop(_K_KEMBALI_SELECTED, None)
        return True

    caller_id = update.effective_user.id
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        await retry_async(sheets.async_update_cell, PEMINJAMAN_SHEET, row_idx, pem_headers["Status Peminjaman"], "ReturnRequested", retries=2)
        await retry_async(sheets.async_update_cell, PEMINJAMAN_SHEET, row_idx, pem_headers["ReturnRequestedAt"], now, retries=2)
        await retry_async(sheets.async_update_cell, PEMINJAMAN_SHEET, row_idx, pem_headers["ReturnRequestedBy"], str(caller_id), retries=2)
        rn_col = pem_headers.get("ReturnNote") or pem_headers.get("Keterangan")
        if rn_col:
            old = await retry_async(sheets.async_get_cell_value, PEMINJAMAN_SHEET, row_idx, rn_col, retries=2)
            newv = (old or "") + f" | ReturnRequestedQty:{qty}@{now}"
            await retry_async(sheets.async_update_cell, PEMINJAMAN_SHEET, row_idx, rn_col, newv, retries=2)
        await retry_async(sheets.async_write_log, str(caller_id), "ReturnRequested", txn or "", f"by={caller_id} qty={qty}", retries=2)
    except Exception:
        await send_md(update, "❌ Gagal mencatat pengembalian pada sheet.")
        context.user_data.pop(_K_KEMBALI_STEP, None)
        context.user_data.pop(_K_KEMBALI_PAIRS, None)
        context.user_data.pop(_K_KEMBALI_SELECTED, None)
        return True

    try:
        nama = rec.get("Nama Barang")
        inv_row, inv_headers = await _resolve_inv_row_with_fallback(sheets, None, nama)
        pemilik_id = None
        specs_text = ""
        if inv_row:
            pemilik_raw = await retry_async(sheets.async_get_cell_value, INVENTARIS_SHEET, inv_row, inv_headers.get("Pemilik ID"), retries=2)
            pemilik_id = int(str(pemilik_raw).strip()) if pemilik_raw not in (None, "") else None
            try:
                specs_text = await _get_item_specs_for_msg(sheets, inv_row, inv_headers)
            except Exception:
                specs_text = ""

        detail_line = f"\nDetail: `{escape_md(specs_text)}`" if specs_text else ""
        kb = [
            [
                InlineKeyboardButton("✅ Konfirmasi Terima", callback_data=f"return_confirm:approve:{txn}:{qty}"),
                InlineKeyboardButton("❌ Tolak", callback_data=f"return_confirm:deny:{txn}:{qty}"),
            ]
        ]
        text = (
            "📩 Permintaan Pengembalian\n\n"
            f"TXN: `{escape_md(str(txn))}`\n"
            f"Barang: *{escape_md(str(nama))}*"
            f"{detail_line}\n"
            f"Permintaan oleh: `{escape_md(str(caller_id))}`\n"
            f"Jumlah yang ingin dikembalikan: *{qty}*\n\n"
            "Tekan ✅ jika sudah diterima, atau ❌ untuk menolak."
        )
        sent = False
        if pemilik_id:
            try:
                await context.application.bot.send_message(chat_id=pemilik_id, text=text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))
                sent = True
            except Exception:
                pass
        if not sent:
            await send_md(update, "⚠️ Pemilik/admin tidak dapat dihubungi. Permintaan dicatat.")
        else:
            await send_md(update, f"✅ Permintaan pengembalian untuk `{escape_md(str(txn))}` telah dikirim.", parse_mode="Markdown")
    except Exception:
        await send_md(update, "✅ Permintaan pengembalian dicatat, tetapi pemberitahuan gagal dikirim ke pemilik.")

    context.user_data.pop(_K_KEMBALI_STEP, None)
    context.user_data.pop(_K_KEMBALI_PAIRS, None)
    context.user_data.pop(_K_KEMBALI_SELECTED, None)
    return True

async def return_confirm_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq: CallbackQuery = update.callback_query
    if not cq:
        return
    await cq.answer()

    if not await require_registration(cq, context):
        return

    parts = (cq.data or "").split(":")
    if len(parts) < 3:
        await send_md(cq, "Aksi tidak dikenali.")
        return
    _, action, txn = parts[0:3]
    qty_override = safe_int(parts[3], None) if len(parts) >= 4 else None

    sheets = context.application.bot_data.get("sheets_manager")
    if not sheets:
        await send_md(cq, "❌ Google Sheets tidak tersedia.")
        return

    user = cq.from_user
    caller_id = user.id if user else None

    row_idx, pem_headers = await _find_peminjaman_row_by_txn(sheets, txn)
    if not row_idx:
        await send_md(cq, f"❌ TXN `{escape_md(txn)}` tidak ditemukan.")
        return

    nama = await retry_async(sheets.async_get_cell_value, PEMINJAMAN_SHEET, row_idx, pem_headers["Nama Barang"], retries=2)
    inv_row, inv_headers = await _resolve_inv_row_with_fallback(sheets, None, nama)

    pemilik_id = None
    if inv_row:
        pemilik_raw = await retry_async(sheets.async_get_cell_value, INVENTARIS_SHEET, inv_row, inv_headers.get("Pemilik ID"), retries=2)
        try:
            pemilik_id = int(str(pemilik_raw).strip()) if pemilik_raw not in (None, "") else None
        except Exception:
            pemilik_id = None

    pm_inst = context.application.bot_data.get("permission_manager")
    allowed = pemilik_id and caller_id and int(pemilik_id) == int(caller_id)
    if not allowed and pm_inst:
        try:
            if hasattr(pm_inst, "async_has_permission"):
                if await pm_inst.async_has_permission(caller_id, "admin"):
                    allowed = True
            else:
                if getattr(pm_inst, "has_permission", lambda *_: False)(caller_id, "admin"):
                    allowed = True
        except Exception:
            pass
    if not allowed:
        await send_md(cq, "🚫 Hanya pemilik atau admin yang dapat mengkonfirmasi pengembalian.")
        return

    if action == "approve":
        qty_from_sheet = safe_int(await retry_async(sheets.async_get_cell_value, PEMINJAMAN_SHEET, row_idx, pem_headers["Qty Dipinjam"], retries=2), 0)
        qty = qty_override if qty_override is not None else qty_from_sheet
        if qty <= 0:
            await send_md(cq, "⚠️ Jumlah pinjam tidak valid.")
            return
        if not inv_row:
            await send_md(cq, f"⚠️ Item *{escape_md(nama)}* tidak ditemukan di inventaris.", parse_mode="Markdown")
            return

        tersedia_col = inv_headers.get("Tersedia")
        try:
            res = await retry_async(sheets.async_increment_cell, INVENTARIS_SHEET, inv_row, tersedia_col, qty, retries=3)
            if isinstance(res, tuple):
                ok_inc = bool(res[0])
                new_val = res[1] if len(res) > 1 else None
            elif isinstance(res, bool):
                ok_inc = res
                new_val = None
            else:
                ok_inc = True
                new_val = None
        except AttributeError:
            cur_raw = await retry_async(sheets.async_get_cell_value, INVENTARIS_SHEET, inv_row, tersedia_col, retries=2)
            cur_num = _parse_available_field(cur_raw)
            new_val = cur_num + qty
            ok_inc = await retry_async(sheets.async_update_cell, INVENTARIS_SHEET, inv_row, tersedia_col, str(new_val), retries=2)
        except Exception:
            logger.exception("return_confirm: increment failed", exc_info=True)
            await send_md(cq, "⚠️ Gagal memperbarui stok (konflik). Coba lagi.")
            return

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            if qty < qty_from_sheet:
                remaining = qty_from_sheet - qty
                await retry_async(sheets.async_update_cell, PEMINJAMAN_SHEET, row_idx, pem_headers["Qty Dipinjam"], str(remaining), retries=2)
                await retry_async(sheets.async_update_cell, PEMINJAMAN_SHEET, row_idx, pem_headers["Status Peminjaman"], _STATUS_PARTIAL, retries=2)
                k_col = pem_headers.get("Keterangan")
                if k_col:
                    old = await retry_async(sheets.async_get_cell_value, PEMINJAMAN_SHEET, row_idx, k_col, retries=2)
                    newk = (old or "") + f" | PartialReturnBy:{caller_id}@{now} qty={qty} remaining={remaining}"
                    await retry_async(sheets.async_update_cell, PEMINJAMAN_SHEET, row_idx, k_col, newk, retries=2)
                rca = pem_headers.get("ReturnConfirmedAt")
                rcb = pem_headers.get("ReturnConfirmedBy")
                if rca:
                    await retry_async(sheets.async_update_cell, PEMINJAMAN_SHEET, row_idx, rca, now, retries=2)
                if rcb:
                    await retry_async(sheets.async_update_cell, PEMINJAMAN_SHEET, row_idx, rcb, str(caller_id), retries=2)
            else:
                await retry_async(sheets.async_update_cell, PEMINJAMAN_SHEET, row_idx, pem_headers["Qty Dipinjam"], "0", retries=2)
                await retry_async(sheets.async_update_cell, PEMINJAMAN_SHEET, row_idx, pem_headers["Status Peminjaman"], "Dikembalikan", retries=2)
                await retry_async(sheets.async_update_cell, PEMINJAMAN_SHEET, row_idx, pem_headers["ReturnConfirmedAt"], now, retries=2)
                await retry_async(sheets.async_update_cell, PEMINJAMAN_SHEET, row_idx, pem_headers["ReturnConfirmedBy"], str(caller_id), retries=2)
                k_col = pem_headers.get("Keterangan")
                if k_col:
                    old = await retry_async(sheets.async_get_cell_value, PEMINJAMAN_SHEET, row_idx, k_col, retries=2)
                    newk = (old or "") + f" | ReturnConfirmedBy:{caller_id}@{now} qty={qty}"
                    await retry_async(sheets.async_update_cell, PEMINJAMAN_SHEET, row_idx, k_col, newk, retries=2)
                try:
                    await _archive_txn_row(sheets, row_idx, pem_headers)
                except Exception:
                    logger.debug("archive failed", exc_info=True)
        except Exception:
            await send_md(cq, "❌ Pengembalian tercatat di stok tetapi gagal memperbarui status TXN.")
            return

        try:
            peminjam_id_raw = await retry_async(sheets.async_get_cell_value, PEMINJAMAN_SHEET, row_idx, pem_headers["Peminjam ID"], retries=2)
            borrower_id = int(str(peminjam_id_raw).strip()) if peminjam_id_raw not in (None, "") else None
        except Exception:
            borrower_id = None
        if borrower_id and caller_id and int(borrower_id) != int(caller_id):
            try:
                await context.application.bot.send_message(chat_id=borrower_id, text=(f"✅ Pengembalian TXN `{escape_md(txn)}` dikonfirmasi."), parse_mode="Markdown")
            except Exception:
                pass

        try:
            await cq.edit_message_text((f"✅ Pengembalian `{escape_md(txn)}` dikonfirmasi. Stok sekarang: {new_val}"), parse_mode="Markdown")
        except Exception:
            await send_md(cq, f"✅ Pengembalian `{escape_md(txn)}` dikonfirmasi.")
        return

    if action == "deny":
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            await retry_async(sheets.async_update_cell, PEMINJAMAN_SHEET, row_idx, pem_headers["Status Peminjaman"], "Dipinjam", retries=2)
            rn_col = pem_headers.get("ReturnNote") or pem_headers.get("Keterangan")
            if rn_col:
                old = await retry_async(sheets.async_get_cell_value, PEMINJAMAN_SHEET, row_idx, rn_col, retries=2)
                await retry_async(sheets.async_update_cell, PEMINJAMAN_SHEET, row_idx, rn_col, (old or "") + f" | ReturnDeniedBy:{caller_id}@{now}", retries=2)
        except Exception:
            await send_md(cq, "❌ Gagal memperbarui status setelah penolakan.")
            return
        try:
            peminjam_id_raw = await retry_async(sheets.async_get_cell_value, PEMINJAMAN_SHEET, row_idx, pem_headers["Peminjam ID"], retries=2)
            pid = int(str(peminjam_id_raw).strip()) if peminjam_id_raw not in (None, "") else None
            if pid:
                await context.application.bot.send_message(chat_id=pid, text=(f"❌ Pengembalian TXN `{escape_md(txn)}` ditolak oleh pemilik."), parse_mode="Markdown")
        except Exception:
            pass
        try:
            await cq.edit_message_text((f"❌ Pengembalian `{escape_md(txn)}` ditolak. Pemohon diberi tahu."), parse_mode="Markdown")
        except Exception:
            await send_md(cq, f"❌ Pengembalian `{escape_md(txn)}` ditolak.")
        return

    await send_md(cq, "Aksi tidak dikenali.")

async def kembali_cancel_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    await cq.answer()
    for k in (_K_KEMBALI_STEP, _K_KEMBALI_PAIRS, _K_KEMBALI_SELECTED):
        context.user_data.pop(k, None)
    await send_md(cq, "✅ Dibatalkan.")

# ============ Hapus ============
async def hapus_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_registration(update, context):
        return

    user = update.effective_user
    sheets = context.application.bot_data.get("sheets_manager")
    pm = context.application.bot_data.get("permission_manager")

    if not user:
        await send_md(update, "⚠️ Tidak dapat mengenali pengguna.")
        return
    if not sheets:
        await send_md(update, "❌ Google Sheets tidak tersedia.")
        return
    if not context.args or len(context.args) < 2:
        await send_md(update, ("Gunakan: /hapus <Nama Barang> <qty|all>\nContoh: `/hapus \"Patch Cord\" 2` atau `/hapus \"Router X\" all`"), parse_mode="Markdown")
        return

    *name_parts, last = context.args
    nama = " ".join(name_parts).strip()
    if not nama:
        await send_md(update, "Nama barang kosong.")
        return

    last_tok = str(last).strip().lower()
    is_all = last_tok == "all"
    qty_int = None
    if not is_all:
        qty_int = safe_int(last_tok, default=-1)
        if qty_int <= 0:
            await send_md(update, "Argumen qty tidak valid. Gunakan angka > 0 atau 'all'.")
            return

    inv_headers = await _ensure_inventaris_headers(sheets)
    inv_row = await sheets.async_find_row_by_value(INVENTARIS_SHEET, "Nama Barang", nama, headers_map=inv_headers)
    if not inv_row:
        try:
            recs = await retry_async(sheets.async_get_all_records, INVENTARIS_SHEET, retries=3)
            matched_row = None
            for idx, r in enumerate(recs):
                if str(r.get("Nama Barang") or "").strip().lower() == nama.strip().lower():
                    matched_row = idx + 2
                    break
            inv_row = matched_row
        except Exception:
            inv_row = None

    if not inv_row:
        await send_md(update, f"❌ Item *{escape_md(nama)}* tidak ditemukan.", parse_mode="Markdown")
        return

    caller_id = user.id
    allowed = False
    try:
        pemilik_raw = await retry_async(sheets.async_get_cell_value, INVENTARIS_SHEET, inv_row, inv_headers.get("Pemilik ID"), retries=2)
        pemilik_id = int(str(pemilik_raw).strip()) if pemilik_raw not in (None, "") else None
    except Exception:
        pemilik_id = None

    if pemilik_id and int(pemilik_id) == int(caller_id):
        allowed = True
    if not allowed and pm:
        try:
            if hasattr(pm, "async_has_permission"):
                if await pm.async_has_permission(caller_id, "can_manage_users") or await pm.async_has_permission(caller_id, "admin"):
                    allowed = True
            else:
                if getattr(pm, "has_permission", lambda *_: False)(caller_id, "can_manage_users") or getattr(pm, "is_admin", lambda *_: False)(caller_id):
                    allowed = True
        except Exception:
            pass

    if not allowed:
        await send_md(update, "🚫 Hanya pemilik atau admin yang dapat menghapus/kurangi stok.", parse_mode="Markdown")
        return

    tersedia_col = inv_headers.get("Tersedia")
    total_col = inv_headers.get("Total Qty")
    status_col = inv_headers.get("Status")

    try:
        cur_ters = safe_int(await retry_async(sheets.async_get_cell_value, INVENTARIS_SHEET, inv_row, tersedia_col, retries=2), 0) if tersedia_col else 0
        cur_total = safe_int(await retry_async(sheets.async_get_cell_value, INVENTARIS_SHEET, inv_row, total_col, retries=2), 0) if total_col else 0
    except Exception:
        cur_ters = 0
        cur_total = 0

    if is_all:
        try:
            if status_col:
                await retry_async(sheets.async_update_cell, INVENTARIS_SHEET, inv_row, status_col, "Removed", retries=2)
            if tersedia_col:
                await retry_async(sheets.async_update_cell, INVENTARIS_SHEET, inv_row, tersedia_col, "0", retries=2)
            if total_col:
                await retry_async(sheets.async_update_cell, INVENTARIS_SHEET, inv_row, total_col, "0", retries=2)
            await retry_async(sheets.async_write_log, str(caller_id), "HapusAll", nama, "soft-removed", retries=2)
            await send_md(update, f"🗑️ Item *{escape_md(nama)}* dihapus (soft-remove).", parse_mode="Markdown")
        except Exception:
            await send_md(update, "❌ Gagal menghapus item di Google Sheets.")
        return

    q = int(qty_int)
    if q > cur_ters:
        await send_md(update, f"⚠️ Jumlah yang ingin dihapus ({q}) melebihi stok tersedia ({cur_ters}).", parse_mode="Markdown")
        return

    ok1, new_ters = True, None
    try:
        if tersedia_col:
            res = await retry_async(sheets.async_increment_cell, INVENTARIS_SHEET, inv_row, tersedia_col, -q, retries=3)
            if isinstance(res, tuple):
                ok1 = bool(res[0]); new_ters = res[1] if len(res) > 1 else None
            elif isinstance(res, bool):
                ok1 = res
            else:
                ok1 = True
        else:
            ok1 = False
    except Exception:
        ok1 = False

    ok2, new_total = True, None
    try:
        if total_col:
            res2 = await retry_async(sheets.async_increment_cell, INVENTARIS_SHEET, inv_row, total_col, -q, retries=3)
            if isinstance(res2, tuple):
                ok2 = bool(res2[0]); new_total = res2[1] if len(res2) > 1 else None
            elif isinstance(res2, bool):
                ok2 = res2
            else:
                ok2 = True
        else:
            ok2 = False
    except Exception:
        ok2 = False

    if ok1 and ok2:
        try:
            await retry_async(sheets.async_write_log, str(caller_id), "HapusQty", nama, f"-{q}", retries=2)
        except Exception:
            pass
        await send_md(update, f"✅ Berhasil mengurangi *{escape_md(nama)}* sebanyak *{q}*. Tersisa: *{escape_md(str(new_ters))}*.", parse_mode="Markdown")
    else:
        await send_md(update, "❌ Gagal memperbarui stok di Google Sheets.")

# ============ Cancel ============
async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await send_md(update, "✅ Semua alur interaktif dibatalkan.")

# ============ Pindah Witel (interaktif) ============
async def pindahwitel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_registration(update, context):
        return

    sheets = context.application.bot_data.get("sheets_manager")
    if not sheets:
        await send_md(update, "❌ Layanan Google Sheets tidak tersedia.")
        return
    options = await _fetch_witel_values(sheets)
    if not options:
        await send_md(update, "⚠️ Daftar Witel kosong/gagal dibaca dari sheet *Witel*.")
        return
    context.user_data[_K_PDW_STEP] = _STEP_PDW_PICK
    await send_md(update, "🌍 Pilih *Witel baru* kamu:", reply_markup=_kb_from_list(options, _PFX_PDW_PICK), parse_mode="Markdown")

async def pindahwitel_pick_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    await cq.answer()
    if context.user_data.get(_K_PDW_STEP) != _STEP_PDW_PICK:
        await send_md(cq, "⚠️ Alur tidak aktif. Jalankan /pindahwitel lagi.")
        return

    target = (cq.data or "").split(":", 1)[1].strip()
    context.user_data[_K_PDW_TARGET] = target
    context.user_data[_K_PDW_STEP] = _STEP_PDW_DIVISI
    await send_md(cq, f"🌍 Witel baru: *{escape_md(target)}*\n\n🏢 Sekarang ketik *Divisi baru* kamu:", parse_mode="Markdown")

async def _apply_owner_move_and_history(
    sheets,
    owner_id: str,
    old_witel: str,
    new_witel: str,
    old_divisi: str,
    new_divisi: str,
) -> int:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    inv_h = await _ensure_inventaris_headers(sheets)
    updated = 0

    try:
        recs = await retry_async(sheets.async_get_all_records, INVENTARIS_SHEET, retries=3)
    except Exception:
        recs = []

    await sheets.async_ensure_headers("Lokasi_History", ["Timestamp", "Pemilik ID", "Pemilik Nama", "Nama Barang", "From Witel", "To Witel", "From Divisi", "To Divisi", "Catatan"])
    nama_to_row: Dict[str, int] = {}

    for idx, r in enumerate(recs):
        try:
            owner = str(r.get("Pemilik ID") or "").strip()
            if owner != owner_id:
                continue

            rownum = idx + 2
            nama = str(r.get("Nama Barang") or "")
            pemilik_nama = str(r.get("Pemilik Nama") or "")

            if inv_h.get("Witel"):
                await sheets.async_update_cell(INVENTARIS_SHEET, rownum, inv_h["Witel"], new_witel)
            if inv_h.get("Divisi"):
                await sheets.async_update_cell(INVENTARIS_SHEET, rownum, inv_h["Divisi"], new_divisi)

            await sheets.async_append_row("Lokasi_History", [ts, owner_id, pemilik_nama, nama, old_witel, new_witel, old_divisi, new_divisi, "pindahwitel"])
            updated += 1
            if nama:
                nama_to_row[nama] = rownum
        except Exception:
            continue

    pem_h = await _ensure_peminjaman_headers(sheets)
    try:
        pem_rows = await retry_async(sheets.async_get_all_records, PEMINJAMAN_SHEET, retries=3)
    except Exception:
        pem_rows = []

    active_keywords = ("disetujui", "dipinjam", "approved", "borrowed", "partial")
    for idx, rec in enumerate(pem_rows, start=2):
        try:
            status = str(rec.get("Status Peminjaman") or "").strip().lower()
            if not any(k in status for k in active_keywords):
                continue
            nama = str(rec.get("Nama Barang") or "").strip()
            if not nama:
                continue

            inv_row = nama_to_row.get(nama)
            if not inv_row:
                inv_row = await sheets.async_find_row_by_value(INVENTARIS_SHEET, "Nama Barang", nama, headers_map=inv_h)
            if not inv_row:
                continue

            pemilik_raw = await retry_async(sheets.async_get_cell_value, INVENTARIS_SHEET, inv_row, inv_h.get("Pemilik ID"), retries=2)
            pemilik_id = str(pemilik_raw).strip() if pemilik_raw not in (None, "") else ""
            if pemilik_id != owner_id:
                continue

            k_col = pem_h.get("Keterangan")
            if k_col:
                old = await retry_async(sheets.async_get_cell_value, PEMINJAMAN_SHEET, idx, k_col, retries=2)
                newk = (old or "") + f" | LokasiOwnerPindah:{old_witel}->{new_witel}, {old_divisi}->{new_divisi}@{ts}"
                await retry_async(sheets.async_update_cell, PEMINJAMAN_SHEET, idx, k_col, newk, retries=2)
        except Exception:
            continue

    return updated

async def pindahwitel_cancel_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    await cq.answer()
    context.user_data.pop(_K_PDW_STEP, None)
    context.user_data.pop(_K_PDW_TARGET, None)
    await send_md(cq, "✅ Dibatalkan.")

# ============ Lokasi ============
async def lokasi_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_registration(update, context):
        return

    sheets = context.application.bot_data.get("sheets_manager")
    if not sheets:
        await send_md(update, "❌ Layanan Google Sheets tidak tersedia.")
        return
    if not context.args:
        await send_md(update, "Gunakan: /lokasi <Nama Barang>")
        return
    name = sanitize_input(" ".join(context.args))

    inv_h = await _ensure_inventaris_headers(sheets)
    row = await sheets.async_find_row_by_value(INVENTARIS_SHEET, "Nama Barang", name, headers_map=inv_h)
    if not row:
        await send_md(update, f"❌ Barang *{escape_md(name)}* tidak ditemukan.", parse_mode="Markdown")
        return

    witel = await retry_async(sheets.async_get_cell_value, INVENTARIS_SHEET, row, inv_h["Witel"], retries=2)
    pemilik_nama = await retry_async(sheets.async_get_cell_value, INVENTARIS_SHEET, row, inv_h["Pemilik Nama"], retries=2)
    pemilik_id = await retry_async(sheets.async_get_cell_value, INVENTARIS_SHEET, row, inv_h["Pemilik ID"], retries=2)

    last_move = ""
    try:
        hist = await retry_async(sheets.async_get_all_records, "Lokasi_History", retries=2)
        filt = [r for r in hist if str(r.get("Nama Barang") or "").strip().lower() == name.strip().lower()]
        if filt:
            filt.sort(key=lambda x: str(x.get("Timestamp") or ""))
            last = filt[-1]
            last_move = f"\nRiwayat terakhir: {escape_md(str(last.get('From Witel') or ''))} → {escape_md(str(last.get('To Witel') or ''))} @ {escape_md(str(last.get('Timestamp') or ''))}"
    except Exception:
        pass

    teks = (f"📦 *{escape_md(name)}*\n"
            f"Witel: *{escape_md(str(witel or '-'))}*\n"
            f"Pemilik: {escape_md(str(pemilik_nama or '-'))} (`{escape_md(str(pemilik_id or '-'))}`)"
            f"{last_move}")
    await send_md(update, teks, parse_mode="Markdown")

# ============ Router teks umum ============
async def handle_misc_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not update.message or not update.message.text:
        return False

    txt_raw = update.message.text
    txt = txt_raw.strip().lower()

    if txt in ("cancel", "batal", "/cancel"):
        context.user_data.clear()
        await send_md(update, "✅ Semua alur interaktif dibatalkan.")
        return True

    # allow the registration divisi text to be handled here (for /regis flow)
    if await start_divisi_text(update, context):
        return True

    if context.user_data.get(_K_PDW_STEP) == _STEP_PDW_DIVISI:
        if not await require_registration(update, context):
            context.user_data.pop(_K_PDW_STEP, None)
            context.user_data.pop(_K_PDW_TARGET, None)
            return True

        new_div = (txt_raw or "").strip()
        if not new_div:
            await send_md(update, "⚠️ Divisi tidak boleh kosong. Coba lagi.")
            return True

        sheets = context.application.bot_data.get("sheets_manager")
        if not sheets:
            await send_md(update, "❌ Layanan Google Sheets tidak tersedia.")
            return True

        uh = await _ensure_users_headers(sheets)
        uid = str(update.effective_user.id)
        row_idx = await sheets.async_find_row_by_value("Users", "User ID", uid, headers_map=uh)
        if not row_idx:
            await send_md(update, "⚠️ Akunmu belum terdaftar. Jalankan /regis dulu.")
            context.user_data.pop(_K_PDW_STEP, None)
            context.user_data.pop(_K_PDW_TARGET, None)
            return True

        target_witel = context.user_data.get(_K_PDW_TARGET, "")
        old_witel = str(await retry_async(sheets.async_get_cell_value, "Users", row_idx, uh["Witel"], retries=2) or "").strip()
        old_divisi = str(await retry_async(sheets.async_get_cell_value, "Users", row_idx, uh["Divisi"], retries=2) or "").strip()

        try:
            await retry_async(sheets.async_update_cell, "Users", row_idx, uh["Witel"], target_witel, retries=2)
            await retry_async(sheets.async_update_cell, "Users", row_idx, uh["Divisi"], new_div, retries=2)
        except Exception:
            await send_md(update, "❌ Gagal menyimpan perubahan Witel/Divisi.")
            return True

        try:
            moved = await _apply_owner_move_and_history(sheets, uid, old_witel, target_witel, old_divisi, new_div)
        except Exception:
            moved = 0

        context.user_data.pop(_K_PDW_STEP, None)
        context.user_data.pop(_K_PDW_TARGET, None)

        await send_md(update, (f"✅ Witel & Divisi diperbarui.\n\nWitel: *{escape_md(target_witel)}*\nDivisi: *{escape_md(new_div)}*\nBarang dipindah: *{moved}*"), parse_mode="Markdown")
        return True

    if await handle_kembali_message(update, context):
        return True

    return False

# ============ Register handlers ============
def register_misc_handlers(application: Application):
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("regis", regis_command))

    application.add_handler(CallbackQueryHandler(start_witel_pick_cb, pattern=rf"^{_PFX_START_WITEL_PICK}"))
    application.add_handler(CallbackQueryHandler(start_cancel_cb, pattern=rf"^{_PFX_START_CANCEL}$"))
    application.add_handler(CommandHandler("help", help_command))

    application.add_handler(CommandHandler("approve", approve_command))
    application.add_handler(CommandHandler("reject", reject_command))
    application.add_handler(CallbackQueryHandler(approve_do_cb, pattern=r"^approve_do:"))
    application.add_handler(CallbackQueryHandler(reject_do_cb, pattern=r"^reject_do:"))

    application.add_handler(CommandHandler("kembali", kembali_command))
    application.add_handler(CallbackQueryHandler(kembali_select_cb, pattern=r"^kembali_select:"))
    application.add_handler(CallbackQueryHandler(return_confirm_cb, pattern=r"^return_confirm:"))
    application.add_handler(CallbackQueryHandler(kembali_cancel_cb, pattern=r"^kembali_cancel$"))
    application.add_handler(CommandHandler("hapus", hapus_command))
    application.add_handler(CommandHandler("cancel", cancel_command))

    application.add_handler(CommandHandler("pindahwitel", pindahwitel_cmd))
    application.add_handler(CallbackQueryHandler(pindahwitel_pick_cb, pattern=rf"^{_PFX_PDW_PICK}"))
    application.add_handler(CallbackQueryHandler(pindahwitel_cancel_cb, pattern=rf"^{_PFX_PDW_CANCEL}$"))
    application.add_handler(CommandHandler("lokasi", lokasi_cmd))

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_misc_message))

def register_handlers(application: Application):
    return register_misc_handlers(application)

__all__ = [
    "register_misc_handlers",
    "start_command",
    "regis_command",
    "help_command",
    "approve_command",
    "reject_command",
    "kembali_command",
    "hapus_command",
    "cancel_command",
    "handle_misc_message",
    "return_confirm_cb",
    "pindahwitel_cmd",
    "lokasi_cmd",
]
