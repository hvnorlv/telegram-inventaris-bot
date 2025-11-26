# handlers/misc.py
from __future__ import annotations
import logging
from typing import Optional, Tuple, Any, Dict, List
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery, Message
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
# start flow
_K_START_STEP = "start_step"
_K_START_WITEL = "start_witel"
_STEP_START_WITEL = "start_witel"
_STEP_START_DIVISI = "start_divisi"
_PFX_START_WITEL_PICK = "start_witel_pick:"
_PFX_START_CANCEL = "start_cancel"

# kembali flow
_K_KEMBALI_STEP = "kembali_step"
_K_KEMBALI_PAIRS = "kembali_pairs"
_K_KEMBALI_SELECTED = "kembali_selected"
_STEP_KEMBALI_SELECT = "kembali_select"
_STEP_KEMBALI_QTY = "kembali_qty"
_STATUS_PARTIAL = "Partially Returned"

# pindah witel flow
_K_PDW_STEP = "pdw_step"
_K_PDW_TARGET = "pdw_target"
_STEP_PDW_PICK = "pdw_pick"
_STEP_PDW_DIVISI = "pdw_divisi"
_PFX_PDW_PICK = "pdw_pick:"
_PFX_PDW_CANCEL = "pdw_cancel"

# ------------ Sheet helpers
async def _ensure_users_headers(sheets) -> Dict[str, int]:
    return await sheets.async_ensure_headers("Users", ["User ID", "Nama", "Role", "Witel", "Divisi"])

async def _ensure_inventaris_headers(sheets) -> Dict[str, int]:
    return await sheets.async_ensure_headers(
        INVENTARIS_SHEET,
        [
            "Nama Barang","Kategori","Witel","Divisi","Total Qty","Tersedia","Status",
            "Pemilik ID","Pemilik Nama","Keterangan","Keterangan 1","Keterangan 2","Keterangan 3","Serial Number",
        ],
    )

async def _ensure_peminjaman_headers(sheets) -> Dict[str, int]:
    headers = [
        "ID Transaksi","Nama Barang","Peminjam ID","Peminjam Nama","Qty Dipinjam",
        "Tanggal Pinjam","Deadline","Status Peminjaman","Keterangan","Reminder Sent",
        "ReturnRequestedAt","ReturnRequestedBy","ReturnConfirmedAt","ReturnConfirmedBy","ReturnNote",
    ]
    return await sheets.async_ensure_headers(PEMINJAMAN_SHEET, headers)

async def _find_peminjaman_row_by_txn(sheets, txn: str) -> Tuple[Optional[int], Dict[str, int]]:
    pem_headers = await _ensure_peminjaman_headers(sheets)
    row_idx = await sheets.async_find_row_by_value(
        PEMINJAMAN_SHEET, "ID Transaksi", txn, headers_map=pem_headers
    )
    return row_idx, pem_headers

async def _archive_txn_row(sheets, row_idx: int, pem_headers: Dict[str, int]) -> bool:
    """Append ringkasan TXN ke 'Peminjaman_Archive', lalu tandai baris asli sebagai Archived."""
    try:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        async def safe_get(colname: str) -> str:
            try:
                v = await sheets.async_get_cell_value(PEMINJAMAN_SHEET, row_idx, pem_headers.get(colname))
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

        await sheets.async_ensure_headers(
            "Peminjaman_Archive",
            ["Archived At","ID Transaksi","Nama Barang","Peminjam ID","Peminjam Nama","Qty Dipinjam","Tanggal Pinjam","Deadline","Status Peminjaman"],
        )
        await sheets.async_append_row("Peminjaman_Archive", [ts, txn, nama, pid, pname, qty, tgl, dl, status])

        try:
            await sheets.async_update_cell(PEMINJAMAN_SHEET, row_idx, pem_headers["ID Transaksi"], f"ARCHIVED_{txn or ''}".strip("_"))
            await sheets.async_update_cell(PEMINJAMAN_SHEET, row_idx, pem_headers["Qty Dipinjam"], "0")
            await sheets.async_update_cell(PEMINJAMAN_SHEET, row_idx, pem_headers["Status Peminjaman"], "Archived")
        except Exception:
            logger.exception("mark archived failed", exc_info=True)

        return True
    except Exception:
        logger.exception("_archive_txn_row failed", exc_info=True)
        return False

# ---- Witel utilities (robust read)
async def _fetch_witel_values(
    sheets,
    sheet_name: str = "Witel",
    header: Optional[str] = None,   # None = auto-detect ("Witel" / "Daftar Witel" / any single-col title)
) -> List[str]:
    """
    Return ordered, de-duplicated Witel names from sheet `Witel`.
    Works with:
      - Single-column list: A1 is a title (e.g., "Daftar Witel"), data from A2 down.
      - Table with header row containing either "Witel" or "Daftar Witel" (case-insensitive).
    """
    candidates: List[str] = []
    if header:
        wanted_headers = [header.strip().lower()]
    else:
        wanted_headers = ["witel", "daftar witel"]

    # 1) Try "records" API (headered table)
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

    # 2) Fallback: raw values grid
    if not candidates:
        try:
            raw = await sheets.async_get_all_values(sheet_name)
            if raw and len(raw) > 0:
                header_row = raw[0]
                col_idx = None

                if len(header_row) == 1:
                    # single column sheet: use col A, skip title row
                    col_idx = 0
                else:
                    # find header column by name
                    for i, h in enumerate(header_row):
                        if str(h).strip().lower() in wanted_headers:
                            col_idx = i
                            break

                if col_idx is None:
                    col_idx = 0  # assume column A

                for row in raw[1:]:
                    cell = (row[col_idx] if col_idx < len(row) else "") or ""
                    v = str(cell).strip()
                    if v:
                        candidates.append(v)
        except Exception:
            pass

    # 3) Last-chance: direct range A2:A*
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

    # de-duplicate preserving order
    out, seen = [], set()
    for v in candidates:
        if v not in seen:
            seen.add(v); out.append(v)
    return out

def _kb_from_list(options: List[str], prefix: str, add_cancel: bool = True) -> InlineKeyboardMarkup:
    kb = []
    for w in options:
        label = w if len(w) <= 64 else (w[:61] + "...")
        kb.append([InlineKeyboardButton(label, callback_data=f"{prefix}{w}")])
    if add_cancel:
        cancel_cb = _PFX_START_CANCEL if prefix.startswith("start") else _PFX_PDW_CANCEL
        kb.append([InlineKeyboardButton("Batal", callback_data=cancel_cb)])
    return InlineKeyboardMarkup(kb)

# ============ /start ============
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

async def start_witel_pick_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    await cq.answer()
    if context.user_data.get(_K_START_STEP) != _STEP_START_WITEL:
        await send_md(cq, "⚠️ Pilihan ini tidak aktif. Jalankan /start lagi.")
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
        await send_md(update, "⚠️ Akunmu belum terdaftar. /start lagi.")
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
        await send_md(update, "❌ Gagal menyimpan Witel/Divisi. Coba /start lagi.")
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
        "• /start — daftar dan isi Witel & Divisi kamu\n"
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
        "• /hapus <Nama Barang> <qty|all> — hapus atau kurangi stok barang\n\n"
        "🧑‍💼 *Admin*\n"
        "• /admin — buka menu admin (list peminjaman aktif, manage transaksi, danger purge)\n\n"
        "ℹ️ *Informasi*\n"
        "• /help — tampilkan daftar perintah lengkap ini\n\n"
        "💡 *Tips:* Pastikan kamu sudah menjalankan /start dan memilih *Witel* serta *Divisi* sebelum menggunakan fitur lainnya."
    )
    await send_md(update, teks, parse_mode="Markdown")

# ============ Approve / Reject ============
async def approve_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # require registration
    if not await require_registration(update, context):
        return

    user = update.effective_user
    sheets = context.application.bot_data.get("sheets_manager")
    pm = context.application.bot_data.get("permission_manager")

    if not sheets:
        await send_md(update, "❌ Google Sheets tidak tersedia.")
        return
    if not context.args:
        await send_md(update, "Gunakan: /approve <TXN_ID>")
        return

    txn = sanitize_input(context.args[0])
    row_idx, pem_headers = await _find_peminjaman_row_by_txn(sheets, txn)
    if not row_idx:
        await send_md(update, f"❌ TXN `{escape_md(txn)}` tidak ditemukan.", parse_mode="Markdown")
        return

    status = await sheets.async_get_cell_value(PEMINJAMAN_SHEET, row_idx, pem_headers["Status Peminjaman"])
    st = str(status or "").strip().lower()
    if st.startswith("disetujui") or "dikembalikan" in st or "returned" in st:
        await send_md(update, f"⚠️ TXN `{escape_md(txn)}` tidak bisa disetujui (status: `{escape_md(str(status))}`).", parse_mode="Markdown")
        return

    nama = await sheets.async_get_cell_value(PEMINJAMAN_SHEET, row_idx, pem_headers["Nama Barang"])
    qty = safe_int(await sheets.async_get_cell_value(PEMINJAMAN_SHEET, row_idx, pem_headers["Qty Dipinjam"]), 0)
    if qty <= 0:
        await send_md(update, "⚠️ Jumlah pinjam tidak valid.")
        return

    inv_headers = await _ensure_inventaris_headers(sheets)
    inv_row = await sheets.async_find_row_by_value(INVENTARIS_SHEET, "Nama Barang", nama, headers_map=inv_headers)
    if not inv_row:
        await send_md(update, f"⚠️ Item *{escape_md(nama)}* tidak ditemukan di inventaris.", parse_mode="Markdown")
        return

    caller_id = user.id if user else None
    allowed = False
    try:
        pemilik_raw = await sheets.async_get_cell_value(INVENTARIS_SHEET, inv_row, inv_headers.get("Pemilik ID"))
        pemilik_id = int(str(pemilik_raw).strip()) if pemilik_raw not in (None, "") else None
    except Exception:
        pemilik_id = None
    if pemilik_id and caller_id and int(pemilik_id) == int(caller_id):
        allowed = True
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
        await send_md(update, "🚫 Hanya pemilik atau admin yang dapat menyetujui.", parse_mode="Markdown")
        return

    tersedia_col = inv_headers.get("Tersedia")
    cur_ters_raw = await sheets.async_get_cell_value(INVENTARIS_SHEET, inv_row, tersedia_col)
    cur_ters = safe_int(cur_ters_raw, 0)
    if qty > cur_ters:
        await send_md(update, f"⚠️ Stok tidak cukup (tersedia {cur_ters}).", parse_mode="Markdown")
        return

    ok_decr, new_val = await sheets.async_increment_cell(INVENTARIS_SHEET, inv_row, tersedia_col, -qty, max_retries=4)
    if not ok_decr:
        await send_md(update, "⚠️ Gagal memperbarui stok (konflik). Coba lagi.", parse_mode="Markdown")
        return

    try:
        await sheets.async_update_cell(PEMINJAMAN_SHEET, row_idx, pem_headers["Status Peminjaman"], "Disetujui")
        await sheets.async_write_log(str(caller_id), "Approve", nama, f"tx={txn} qty={qty}")
    except Exception:
        logger.exception("write approval info failed", exc_info=True)

    await send_md(update, f"✅ TXN `{escape_md(txn)}` disetujui. Stok sekarang: {new_val}", parse_mode="Markdown")

    # notify peminjam
    try:
        peminjam_id_raw = await sheets.async_get_cell_value(PEMINJAMAN_SHEET, row_idx, pem_headers["Peminjam ID"])
        pid = int(str(peminjam_id_raw).strip())
        try:
            await context.application.bot.send_message(chat_id=pid, text=f"✅ Permintaan `{escape_md(txn)}` disetujui.", parse_mode="Markdown")
        except Exception:
            pass
    except Exception:
        pass

async def reject_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_registration(update, context):
        return

    user = update.effective_user
    sheets = context.application.bot_data.get("sheets_manager")
    pm = context.application.bot_data.get("permission_manager")

    if not sheets:
        await send_md(update, "❌ Google Sheets tidak tersedia.")
        return
    if not context.args:
        await send_md(update, "Gunakan: /reject <TXN_ID>")
        return

    txn = sanitize_input(context.args[0])
    row_idx, pem_headers = await _find_peminjaman_row_by_txn(sheets, txn)
    if not row_idx:
        await send_md(update, f"❌ TXN `{escape_md(txn)}` tidak ditemukan.", parse_mode="Markdown")
        return

    status = await sheets.async_get_cell_value(PEMINJAMAN_SHEET, row_idx, pem_headers["Status Peminjaman"])
    st = str(status or "").strip().lower()
    if st.startswith("disetujui") or "dikembalikan" in st or "returned" in st:
        await send_md(update, f"⚠️ TXN `{escape_md(txn)}` tidak bisa ditolak.", parse_mode="Markdown")
        return

    nama = await sheets.async_get_cell_value(PEMINJAMAN_SHEET, row_idx, pem_headers["Nama Barang"])

    # permission check (owner/admin)
    caller_id = user.id if user else None
    inv_headers = await _ensure_inventaris_headers(sheets)
    inv_row = await sheets.async_find_row_by_value(INVENTARIS_SHEET, "Nama Barang", nama, headers_map=inv_headers)
    pemilik_id = None
    try:
        if inv_row:
            pemilik_raw = await sheets.async_get_cell_value(INVENTARIS_SHEET, inv_row, inv_headers.get("Pemilik ID"))
            pemilik_id = int(str(pemilik_raw).strip()) if pemilik_raw not in (None, "") else None
    except Exception:
        pemilik_id = None

    allowed = False
    if pemilik_id and caller_id and int(pemilik_id) == int(caller_id):
        allowed = True
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
        await send_md(update, "🚫 Hanya pemilik atau admin yang dapat menolak.", parse_mode="Markdown")
        return

    try:
        await sheets.async_update_cell(PEMINJAMAN_SHEET, row_idx, pem_headers["Status Peminjaman"], "Ditolak")
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        note_col = pem_headers.get("Keterangan")
        if note_col:
            old = await sheets.async_get_cell_value(PEMINJAMAN_SHEET, row_idx, note_col)
            newk = (old or "") + f" | RejectedBy:{caller_id}@{now}"
            await sheets.async_update_cell(PEMINJAMAN_SHEET, row_idx, note_col, newk)
        await sheets.async_write_log(str(caller_id), "Reject", nama, f"tx={txn}")
        await send_md(update, f"✅ TXN `{escape_md(txn)}` ditolak.", parse_mode="Markdown")
        try:
            peminjam_id_raw = await sheets.async_get_cell_value(PEMINJAMAN_SHEET, row_idx, pem_headers["Peminjam ID"])
            pid = int(str(peminjam_id_raw).strip())
            await context.application.bot.send_message(chat_id=pid, text=f"❌ Permintaan `{escape_md(txn)}` ditolak.", parse_mode="Markdown")
        except Exception:
            pass
    except Exception:
        logger.exception("reject failed", exc_info=True)
        await send_md(update, "❌ Gagal menolak permintaan.")

# ============ Kembali (interaktif) ============
async def kembali_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tanpa arg -> interaktif; dengan TXN -> quick-mode set ReturnRequested."""
    if not await require_registration(update, context):
        return

    user = update.effective_user
    sheets = context.application.bot_data.get("sheets_manager")
    pm = context.application.bot_data.get("permission_manager")

    if not sheets:
        await send_md(update, "❌ Google Sheets tidak tersedia.")
        return

    # quick mode
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
        allowed = (peminjam_id and caller_id and int(peminjam_id) == int(caller_id))

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
            # allow owner too
            try:
                nama = await sheets.async_get_cell_value(PEMINJAMAN_SHEET, row_idx, pem_headers["Nama Barang"])
                inv_headers = await _ensure_inventaris_headers(sheets)
                inv_row = await sheets.async_find_row_by_value(INVENTARIS_SHEET, "Nama Barang", nama, headers_map=inv_headers)
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
            await sheets.async_update_cell(PEMINJAMAN_SHEET, row_idx, pem_headers["Status Peminjaman"], "ReturnRequested")
            await sheets.async_update_cell(PEMINJAMAN_SHEET, row_idx, pem_headers["ReturnRequestedAt"], now)
            await sheets.async_update_cell(PEMINJAMAN_SHEET, row_idx, pem_headers["ReturnRequestedBy"], str(caller_id))
            await sheets.async_write_log(str(caller_id), "ReturnRequested", txn, f"by={caller_id}")
        except Exception:
            await send_md(update, "❌ Gagal mencatat permintaan pengembalian.")
            return

        # notify owner/admin
        try:
            nama = await sheets.async_get_cell_value(PEMINJAMAN_SHEET, row_idx, pem_headers["Nama Barang"])
            inv_headers = await _ensure_inventaris_headers(sheets)
            inv_row = await sheets.async_find_row_by_value(INVENTARIS_SHEET, "Nama Barang", nama, headers_map=inv_headers)
            pemilik_id = None
            if inv_row:
                pemilik_raw = await sheets.async_get_cell_value(INVENTARIS_SHEET, inv_row, inv_headers.get("Pemilik ID"))
                try:
                    pemilik_id = int(str(pemilik_raw).strip()) if pemilik_raw not in (None, "") else None
                except Exception:
                    pemilik_id = None

            kb = [[
                InlineKeyboardButton("✅ Konfirmasi Terima", callback_data=f"return_confirm:approve:{txn}"),
                InlineKeyboardButton("❌ Tolak", callback_data=f"return_confirm:deny:{txn}"),
            ]]
            text = (
                f"📩 Permintaan Pengembalian\n\n"
                f"TXN: `{escape_md(txn)}`\n"
                f"Barang: *{escape_md(nama)}*\n"
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

    # interaktif: tampilkan pinjaman aktif milik user
    if not user:
        await send_md(update, "⚠️ Tidak dapat mengenali pengguna.")
        return

    try:
        pem_recs = await sheets.async_get_all_records(PEMINJAMAN_SHEET)
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
        await cq.edit_message_text(
            f"📄 TXN: `{escape_md(str(tx))}`\nBarang: *{escape_md(str(name))}*\nQty dipinjam: *{escape_md(str(qty))}*\n\nMasukkan jumlah yang akan dikembalikan (angka):",
            parse_mode="Markdown",
        )
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
        txn = await sheets.async_get_cell_value(PEMINJAMAN_SHEET, row_idx, pem_headers["ID Transaksi"])
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
        await sheets.async_update_cell(PEMINJAMAN_SHEET, row_idx, pem_headers["Status Peminjaman"], "ReturnRequested")
        await sheets.async_update_cell(PEMINJAMAN_SHEET, row_idx, pem_headers["ReturnRequestedAt"], now)
        await sheets.async_update_cell(PEMINJAMAN_SHEET, row_idx, pem_headers["ReturnRequestedBy"], str(caller_id))
        rn_col = pem_headers.get("ReturnNote") or pem_headers.get("Keterangan")
        if rn_col:
            old = await sheets.async_get_cell_value(PEMINJAMAN_SHEET, row_idx, rn_col)
            newv = (old or "") + f" | ReturnRequestedQty:{qty}@{now}"
            await sheets.async_update_cell(PEMINJAMAN_SHEET, row_idx, rn_col, newv)
        await sheets.async_write_log(str(caller_id), "ReturnRequested", txn or "", f"by={caller_id} qty={qty}")
    except Exception:
        await send_md(update, "❌ Gagal mencatat pengembalian pada sheet.")
        context.user_data.pop(_K_KEMBALI_STEP, None)
        context.user_data.pop(_K_KEMBALI_PAIRS, None)
        context.user_data.pop(_K_KEMBALI_SELECTED, None)
        return True

    # notify owner/admin
    try:
        nama = rec.get("Nama Barang")
        inv_headers = await _ensure_inventaris_headers(sheets)
        inv_row = await sheets.async_find_row_by_value(INVENTARIS_SHEET, "Nama Barang", nama, headers_map=inv_headers)
        pemilik_id = None
        if inv_row:
            pemilik_raw = await sheets.async_get_cell_value(INVENTARIS_SHEET, inv_row, inv_headers.get("Pemilik ID"))
            pemilik_id = int(str(pemilik_raw).strip()) if pemilik_raw not in (None, "") else None

        kb = [[
            InlineKeyboardButton("✅ Konfirmasi Terima", callback_data=f"return_confirm:approve:{txn}:{qty}"),
            InlineKeyboardButton("❌ Tolak", callback_data=f"return_confirm:deny:{txn}:{qty}"),
        ]]
        text = (
            f"📩 Permintaan Pengembalian\n\n"
            f"TXN: `{escape_md(str(txn))}`\n"
            f"Barang: *{escape_md(str(nama))}*\n"
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

    # require registration for the actor (owner/admin)
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

    nama = await sheets.async_get_cell_value(PEMINJAMAN_SHEET, row_idx, pem_headers["Nama Barang"])
    inv_headers = await _ensure_inventaris_headers(sheets)
    inv_row = await sheets.async_find_row_by_value(INVENTARIS_SHEET, "Nama Barang", nama, headers_map=inv_headers)

    pemilik_id = None
    if inv_row:
        pemilik_raw = await sheets.async_get_cell_value(INVENTARIS_SHEET, inv_row, inv_headers.get("Pemilik ID"))
        try:
            pemilik_id = int(str(pemilik_raw).strip()) if pemilik_raw not in (None, "") else None
        except Exception:
            pemilik_id = None

    pm_inst = context.application.bot_data.get("permission_manager")
    allowed = (pemilik_id and caller_id and int(pemilik_id) == int(caller_id))
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
        qty_from_sheet = safe_int(await sheets.async_get_cell_value(PEMINJAMAN_SHEET, row_idx, pem_headers["Qty Dipinjam"]), 0)
        qty = qty_override if qty_override is not None else qty_from_sheet
        if qty <= 0:
            await send_md(cq, "⚠️ Jumlah pinjam tidak valid.")
            return
        if not inv_row:
            await send_md(cq, f"⚠️ Item *{escape_md(nama)}* tidak ditemukan di inventaris.", parse_mode="Markdown")
            return

        tersedia_col = inv_headers.get("Tersedia")
        ok_inc, new_val = await sheets.async_increment_cell(INVENTARIS_SHEET, inv_row, tersedia_col, qty, max_retries=4)
        if not ok_inc:
            await send_md(cq, "⚠️ Gagal memperbarui stok (konflik). Coba lagi.")
            return

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            if qty < qty_from_sheet:
                remaining = qty_from_sheet - qty
                await sheets.async_update_cell(PEMINJAMAN_SHEET, row_idx, pem_headers["Qty Dipinjam"], str(remaining))
                await sheets.async_update_cell(PEMINJAMAN_SHEET, row_idx, pem_headers["Status Peminjaman"], _STATUS_PARTIAL)
                k_col = pem_headers.get("Keterangan")
                if k_col:
                    old = await sheets.async_get_cell_value(PEMINJAMAN_SHEET, row_idx, k_col)
                    newk = (old or "") + f" | PartialReturnBy:{caller_id}@{now} qty={qty} remaining={remaining}"
                    await sheets.async_update_cell(PEMINJAMAN_SHEET, row_idx, k_col, newk)
                rca = pem_headers.get("ReturnConfirmedAt")
                rcb = pem_headers.get("ReturnConfirmedBy")
                if rca:
                    await sheets.async_update_cell(PEMINJAMAN_SHEET, row_idx, rca, now)
                if rcb:
                    await sheets.async_update_cell(PEMINJAMAN_SHEET, row_idx, rcb, str(caller_id))
            else:
                await sheets.async_update_cell(PEMINJAMAN_SHEET, row_idx, pem_headers["Qty Dipinjam"], "0")
                await sheets.async_update_cell(PEMINJAMAN_SHEET, row_idx, pem_headers["Status Peminjaman"], "Dikembalikan")
                await sheets.async_update_cell(PEMINJAMAN_SHEET, row_idx, pem_headers["ReturnConfirmedAt"], now)
                await sheets.async_update_cell(PEMINJAMAN_SHEET, row_idx, pem_headers["ReturnConfirmedBy"], str(caller_id))
                k_col = pem_headers.get("Keterangan")
                if k_col:
                    old = await sheets.async_get_cell_value(PEMINJAMAN_SHEET, row_idx, k_col)
                    newk = (old or "") + f" | ReturnConfirmedBy:{caller_id}@{now} qty={qty}"
                    await sheets.async_update_cell(PEMINJAMAN_SHEET, row_idx, k_col, newk)
                try:
                    await _archive_txn_row(sheets, row_idx, pem_headers)
                except Exception:
                    logger.debug("archive failed", exc_info=True)
        except Exception:
            await send_md(cq, "❌ Pengembalian tercatat di stok tetapi gagal memperbarui status TXN.")
            return

        # notify borrower
        try:
            peminjam_id_raw = await sheets.async_get_cell_value(PEMINJAMAN_SHEET, row_idx, pem_headers["Peminjam ID"])
            borrower_id = int(str(peminjam_id_raw).strip())
        except Exception:
            borrower_id = None
        if borrower_id and caller_id and int(borrower_id) != int(caller_id):
            try:
                await context.application.bot.send_message(chat_id=borrower_id, text=f"✅ Pengembalian TXN `{escape_md(txn)}` dikonfirmasi.", parse_mode="Markdown")
            except Exception:
                pass

        try:
            await cq.edit_message_text(f"✅ Pengembalian `{escape_md(txn)}` dikonfirmasi. Stok sekarang: {new_val}", parse_mode="Markdown")
        except Exception:
            await send_md(cq, f"✅ Pengembalian `{escape_md(txn)}` dikonfirmasi.")
        return

    if action == "deny":
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            await sheets.async_update_cell(PEMINJAMAN_SHEET, row_idx, pem_headers["Status Peminjaman"], "Dipinjam")
            rn_col = pem_headers.get("ReturnNote") or pem_headers.get("Keterangan")
            if rn_col:
                old = await sheets.async_get_cell_value(PEMINJAMAN_SHEET, row_idx, rn_col)
                await sheets.async_update_cell(PEMINJAMAN_SHEET, row_idx, rn_col, (old or "") + f" | ReturnDeniedBy:{caller_id}@{now}")
        except Exception:
            await send_md(cq, "❌ Gagal memperbarui status setelah penolakan.")
            return
        try:
            peminjam_id_raw = await sheets.async_get_cell_value(PEMINJAMAN_SHEET, row_idx, pem_headers["Peminjam ID"])
            pid = int(str(peminjam_id_raw).strip())
            await context.application.bot.send_message(chat_id=pid, text=f"❌ Pengembalian TXN `{escape_md(txn)}` ditolak oleh pemilik.", parse_mode="Markdown")
        except Exception:
            pass
        try:
            await cq.edit_message_text(f"❌ Pengembalian `{escape_md(txn)}` ditolak. Pemohon diberi tahu.", parse_mode="Markdown")
        except Exception:
            await send_md(cq, f"❌ Pengembalian `{escape_md(txn)}` ditolak.")
        return

    await send_md(cq, "Aksi tidak dikenali.")

# cancel tombol kembali
async def kembali_cancel_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    await cq.answer()
    # clear only kembali-related keys
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
        await send_md(update, "Gunakan: /hapus <Nama Barang> <qty|all>\nContoh: `/hapus \"Patch Cord\" 2` atau `/hapus \"Router X\" all`", parse_mode="Markdown")
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
            recs = await sheets.async_get_all_records(INVENTARIS_SHEET)
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
        pemilik_raw = await sheets.async_get_cell_value(INVENTARIS_SHEET, inv_row, inv_headers.get("Pemilik ID"))
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
        cur_ters = safe_int(await sheets.async_get_cell_value(INVENTARIS_SHEET, inv_row, tersedia_col), 0) if tersedia_col else 0
        cur_total = safe_int(await sheets.async_get_cell_value(INVENTARIS_SHEET, inv_row, total_col), 0) if total_col else 0
    except Exception:
        cur_ters = 0
        cur_total = 0

    if is_all:
        try:
            if status_col:
                await sheets.async_update_cell(INVENTARIS_SHEET, inv_row, status_col, "Removed")
            if tersedia_col:
                await sheets.async_update_cell(INVENTARIS_SHEET, inv_row, tersedia_col, "0")
            if total_col:
                await sheets.async_update_cell(INVENTARIS_SHEET, inv_row, total_col, "0")
            await sheets.async_write_log(str(caller_id), "HapusAll", nama, "soft-removed")
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
            ok1, new_ters = await sheets.async_increment_cell(INVENTARIS_SHEET, inv_row, tersedia_col, -q, max_retries=4)
        else:
            ok1 = False
    except Exception:
        ok1 = False

    ok2, new_total = True, None
    try:
        if total_col:
            ok2, new_total = await sheets.async_increment_cell(INVENTARIS_SHEET, inv_row, total_col, -q, max_retries=4)
        else:
            ok2 = False
    except Exception:
        ok2 = False

    if ok1 and ok2:
        try:
            await sheets.async_write_log(str(caller_id), "HapusQty", nama, f"-{q}")
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

    await send_md(
        cq,
        f"🌍 Witel baru: *{escape_md(target)}*\n\n🏢 Sekarang ketik *Divisi baru* kamu:",
        parse_mode="Markdown",
    )

async def _apply_owner_move_and_history(
    sheets,
    owner_id: str,
    old_witel: str,
    new_witel: str,
    old_divisi: str,
    new_divisi: str,
) -> int:
    """
    Pindahkan semua barang milik owner_id ke new_witel & new_divisi
    dan catat ke Lokasi_History.
    Juga catat pemindahan pada Peminjaman aktif yang terkait barang milik owner_id.
    Return jumlah item inventaris yang diupdate.
    """
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    inv_h = await _ensure_inventaris_headers(sheets)
    updated = 0

    try:
        recs = await sheets.async_get_all_records(INVENTARIS_SHEET)
    except Exception:
        recs = []

    # pastikan header Lokasi_History mencakup info Divisi juga
    await sheets.async_ensure_headers(
        "Lokasi_History",
        [
            "Timestamp",
            "Pemilik ID",
            "Pemilik Nama",
            "Nama Barang",
            "From Witel",
            "To Witel",
            "From Divisi",
            "To Divisi",
            "Catatan",
        ],
    )

    # Map nama_barang -> rownum untuk lookup cepat (juga dipakai peminjaman)
    nama_to_row: Dict[str, int] = {}

    for idx, r in enumerate(recs):
        try:
            owner = str(r.get("Pemilik ID") or "").strip()
            if owner != owner_id:
                continue

            rownum = idx + 2
            nama = str(r.get("Nama Barang") or "")
            pemilik_nama = str(r.get("Pemilik Nama") or "")

            # update lokasi di INVENTARIS: Witel & Divisi
            if inv_h.get("Witel"):
                await sheets.async_update_cell(INVENTARIS_SHEET, rownum, inv_h["Witel"], new_witel)
            if inv_h.get("Divisi"):
                await sheets.async_update_cell(INVENTARIS_SHEET, rownum, inv_h["Divisi"], new_divisi)

            # catat histori
            await sheets.async_append_row(
                "Lokasi_History",
                [
                    ts,
                    owner_id,
                    pemilik_nama,
                    nama,
                    old_witel,
                    new_witel,
                    old_divisi,
                    new_divisi,
                    "pindahwitel",
                ],
            )

            updated += 1
            if nama:
                nama_to_row[nama] = rownum
        except Exception:
            continue

    # Update catatan di Peminjaman untuk item milik owner yang sedang aktif
    pem_h = await _ensure_peminjaman_headers(sheets)
    try:
        pem_rows = await sheets.async_get_all_records(PEMINJAMAN_SHEET)
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

            # cek pemilik dari inventaris (agar akurat meski nama sama)
            inv_row = nama_to_row.get(nama)
            if not inv_row:
                # fallback cari cepat
                inv_row = await sheets.async_find_row_by_value(
                    INVENTARIS_SHEET, "Nama Barang", nama, headers_map=inv_h
                )
            if not inv_row:
                continue

            pemilik_raw = await sheets.async_get_cell_value(
                INVENTARIS_SHEET, inv_row, inv_h.get("Pemilik ID")
            )
            pemilik_id = str(pemilik_raw).strip() if pemilik_raw not in (None, "") else ""
            if pemilik_id != owner_id:
                continue

            k_col = pem_h.get("Keterangan")
            if k_col:
                old = await sheets.async_get_cell_value(PEMINJAMAN_SHEET, idx, k_col)
                newk = (old or "") + f" | LokasiOwnerPindah:{old_witel}->{new_witel}, {old_divisi}->{new_divisi}@{ts}"
                await sheets.async_update_cell(PEMINJAMAN_SHEET, idx, k_col, newk)
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

    witel = await sheets.async_get_cell_value(INVENTARIS_SHEET, row, inv_h["Witel"])
    pemilik_nama = await sheets.async_get_cell_value(INVENTARIS_SHEET, row, inv_h["Pemilik Nama"])
    pemilik_id = await sheets.async_get_cell_value(INVENTARIS_SHEET, row, inv_h["Pemilik ID"])

    # last history (optional)
    last_move = ""
    try:
        hist = await sheets.async_get_all_records("Lokasi_History")
        filt = [r for r in hist if str(r.get("Nama Barang") or "").strip().lower() == name.strip().lower()]
        if filt:
            # sort by timestamp string (assumes uniform format)
            filt.sort(key=lambda x: str(x.get("Timestamp") or ""))
            last = filt[-1]
            last_move = f"\nRiwayat terakhir: {escape_md(str(last.get('From Witel') or ''))} → {escape_md(str(last.get('To Witel') or ''))} @ {escape_md(str(last.get('Timestamp') or ''))}"
    except Exception:
        pass

    teks = (
        f"📦 *{escape_md(name)}*\n"
        f"Witel: *{escape_md(str(witel or '-'))}*\n"
        f"Pemilik: {escape_md(str(pemilik_nama or '-'))} (`{escape_md(str(pemilik_id or '-'))}`)"
        f"{last_move}"
    )
    await send_md(update, teks, parse_mode="Markdown")

# ============ Router teks umum ============
async def handle_misc_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not update.message or not update.message.text:
        return False

    txt_raw = update.message.text
    txt = txt_raw.strip().lower()

    # cancel via text (boleh tanpa registrasi)
    if txt in ("cancel", "batal", "/cancel"):
        context.user_data.clear()
        await send_md(update, "✅ Semua alur interaktif dibatalkan.")
        return True

    # start divisi input (bagian dari /start, jangan pakai require_registration)
    if await start_divisi_text(update, context):
        return True

    # pindahwitel: input divisi baru sesudah pilih witel
    if context.user_data.get(_K_PDW_STEP) == _STEP_PDW_DIVISI:
        # user sudah lewat /pindahwitel (sudah ter-cek), tapi kita safety check lagi
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
            await send_md(update, "⚠️ Akunmu belum terdaftar. /start dulu.")
            # clear state
            context.user_data.pop(_K_PDW_STEP, None)
            context.user_data.pop(_K_PDW_TARGET, None)
            return True

        target_witel = context.user_data.get(_K_PDW_TARGET, "")
        old_witel = str(await sheets.async_get_cell_value("Users", row_idx, uh["Witel"]) or "").strip()
        old_divisi = str(await sheets.async_get_cell_value("Users", row_idx, uh["Divisi"]) or "").strip()

        # update Users (Witel & Divisi)
        try:
            await sheets.async_update_cell("Users", row_idx, uh["Witel"], target_witel)
            await sheets.async_update_cell("Users", row_idx, uh["Divisi"], new_div)
        except Exception:
            await send_md(update, "❌ Gagal menyimpan perubahan Witel/Divisi.")
            return True

        # Relokasi semua barang milik user + catat histori + update catatan peminjaman aktif
        try:
            moved = await _apply_owner_move_and_history(
                sheets,
                uid,
                old_witel,
                target_witel,
                old_divisi,
                new_div,
            )
        except Exception:
            moved = 0

        # clear state
        context.user_data.pop(_K_PDW_STEP, None)
        context.user_data.pop(_K_PDW_TARGET, None)

        await send_md(
            update,
            (
                "✅ Witel & Divisi diperbarui.\n\n"
                f"Witel: *{escape_md(target_witel)}*\n"
                f"Divisi: *{escape_md(new_div)}*\n"
                f"Barang dipindah: *{moved}*"
            ),
            parse_mode="Markdown",
        )
        return True


# ============ Register handlers ============
def register_misc_handlers(application: Application):
    # start
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CallbackQueryHandler(start_witel_pick_cb, pattern=rf"^{_PFX_START_WITEL_PICK}"))
    application.add_handler(CallbackQueryHandler(start_cancel_cb, pattern=rf"^{_PFX_START_CANCEL}$"))

    # help
    application.add_handler(CommandHandler("help", help_command))

    # approve / reject / kembali / hapus / cancel
    application.add_handler(CommandHandler("approve", approve_command))
    application.add_handler(CommandHandler("reject", reject_command))
    application.add_handler(CommandHandler("kembali", kembali_command))
    application.add_handler(CallbackQueryHandler(kembali_select_cb, pattern=r"^kembali_select:"))
    application.add_handler(CallbackQueryHandler(return_confirm_cb, pattern=r"^return_confirm:"))
    application.add_handler(CallbackQueryHandler(kembali_cancel_cb, pattern=r"^kembali_cancel$"))
    application.add_handler(CommandHandler("hapus", hapus_command))
    application.add_handler(CommandHandler("cancel", cancel_command))

    # pindah witel + lokasi
    application.add_handler(CommandHandler("pindahwitel", pindahwitel_cmd))
    application.add_handler(CallbackQueryHandler(pindahwitel_pick_cb, pattern=rf"^{_PFX_PDW_PICK}"))
    application.add_handler(CallbackQueryHandler(pindahwitel_cancel_cb, pattern=rf"^{_PFX_PDW_CANCEL}$"))
    application.add_handler(CommandHandler("lokasi", lokasi_cmd))

    # text router (start divisi / pindahwitel divisi / kembali qty / cancel)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_misc_message))

# alias
def register_handlers(application: Application):
    return register_misc_handlers(application)

__all__ = [
    "register_misc_handlers",
    "start_command",
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
