# handlers/admin.py
"""
Admin handlers.
- /admin : show admin menu (only for users with admin permission)
- Provide callbacks to list active loans, manage a transaction (approve/deny), and a danger purge (confirmable).
- Exposes:
    - register_admin_handlers()
    - handle_admin_message(update, context) -> bool  (for interactive text routing)
"""

from __future__ import annotations
import logging
from typing import Optional, Dict, Any

from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CommandHandler, CallbackQueryHandler, ContextTypes

from handlers.common import send_md, _save_step, _get_step, _clear_steps, escape_md
from permissions import PermissionManager
from config import PEMINJAMAN_SHEET, INVENTARIS_SHEET, USERS_SHEET

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

# small keys used in chat_data storage for interactive admin flows
_KEY_ADMIN_STEP = "admin_step"
_KEY_ADMIN_DATA = "admin_data"
_KEY_ADMIN_OWNER = "admin_owner"

# admin interaction step values
_STEP_AWAIT_CONFIRM = "await_confirm"
_STEP_AWAIT_TEXT = "await_text"

# ------------------------------------------------------------------
# Registration
# ------------------------------------------------------------------
def register_admin_handlers():
    return [
        CommandHandler("admin", admin_command),
        CallbackQueryHandler(handle_admin_callback, pattern=r"^admin:"),
    ]

# ------------------------------------------------------------------
# Command: /admin
# ------------------------------------------------------------------
async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Show admin menu. Only visible/usable by admin users.
    """
    user = update.effective_user
    pm: PermissionManager = context.application.bot_data.get("permission_manager")
    if not pm:
        await send_md(update, "🔒 PermissionManager tidak terkonfigurasi (admin tidak tersedia).")
        return

    try:
        if not await pm.async_has_permission(user.id, "admin"):
            await send_md(update, "🔒 Kamu bukan admin. Akses ditolak.")
            return
    except Exception:
        # if permission manager fails, deny access for safety
        await send_md(update, "🔒 Gagal memeriksa izin admin. Akses ditolak.")
        return

    kb = [
        [InlineKeyboardButton("📄 Lihat Peminjaman Aktif", callback_data="admin:list_active")],
        [InlineKeyboardButton("🛠️ Manage Transaksi (manual)", callback_data="admin:manage")],
        [InlineKeyboardButton("🧨 Danger Purge (hapus data demo)", callback_data="admin:danger_confirm")],
    ]
    await send_md(update, "⚙️ *Menu Admin* — Pilih aksi:", reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")

# ------------------------------------------------------------------
# Callback router for admin buttons
# ------------------------------------------------------------------
async def handle_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    if not cq:
        return
    await cq.answer()
    user = update.effective_user
    pm: PermissionManager = context.application.bot_data.get("permission_manager")
    if not pm:
        await cq.edit_message_text("🔒 PermissionManager tidak tersedia.")
        return
    try:
        if not await pm.async_has_permission(user.id, "admin"):
            await cq.edit_message_text("🔒 Kamu bukan admin.")
            return
    except Exception:
        await cq.edit_message_text("🔒 Gagal memverifikasi izin admin.")
        return

    data = cq.data or ""

    try:
        if data == "admin:list_active":
            await _admin_list_active(cq, context)
            return

        if data == "admin:manage":
            # ask admin to input TXN id via text
            _save_step(context, user.id, _KEY_ADMIN_STEP, _STEP_AWAIT_TEXT)
            _save_step(context, user.id, _KEY_ADMIN_DATA, {"action": "manage_txn"})
            try:
                await cq.edit_message_text("Ketik `ID Transaksi` yang ingin kamu manage (approve/deny):", parse_mode="Markdown")
            except Exception:
                await send_md(cq, "Ketik ID Transaksi yang ingin kamu manage:")
            return

        if data == "admin:danger_confirm":
            _save_step(context, user.id, _KEY_ADMIN_STEP, _STEP_AWAIT_CONFIRM)
            _save_step(context, user.id, _KEY_ADMIN_OWNER, user.id)
            try:
                await cq.edit_message_text("⚠️ *DANGER* — ketik `I CONFIRM` untuk menghapus semua data demo (inventory & peminjaman). Aksi ini tidak dapat dibatalkan.", parse_mode="Markdown")
            except Exception:
                await send_md(cq, "Ketik `I CONFIRM` untuk menghapus semua data demo.")
            return

        # approve/deny callbacks created by manage txn flow
        if data.startswith("admin:approve:") or data.startswith("admin:deny:"):
            parts = data.split(":")
            if len(parts) >= 3:
                action = parts[1]
                txn = parts[2]
                if action == "approve":
                    await _admin_set_tx_status(cq, context, txn, "Disetujui")
                else:
                    await _admin_set_tx_status(cq, context, txn, "Ditolak")
            return

    except Exception as e:
        logger.exception("Error in admin callback: %s", e)
        try:
            await cq.edit_message_text("Terjadi kesalahan saat memproses aksi admin.")
        except Exception:
            pass

# ------------------------------------------------------------------
# Admin interactive message router (for text-based inputs)
# This is called from handlers/__init__.py router: handle_admin_message(update, context)
# ------------------------------------------------------------------
async def handle_admin_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Handle follow-up text for admin interactive flows.
    Returns True if message was consumed by admin flow.
    """
    if not update.message or not update.message.text:
        return False
    user = update.effective_user
    pm: PermissionManager = context.application.bot_data.get("permission_manager")
    if not pm:
        return False
    try:
        if not await pm.async_has_permission(user.id, "admin"):
            return False
    except Exception:
        return False

    step = _get_step(context, user.id, _KEY_ADMIN_STEP)
    if not step:
        return False

    text = update.message.text.strip()
    # confirm purge
    if step == _STEP_AWAIT_CONFIRM:
        # expect exact phrase
        if text.upper() == "I CONFIRM" or text.upper().strip() == "CONFIRM":
            await _perform_danger_purge(update, context, user.id)
        else:
            await send_md(update, "Konfirmasi tidak cocok. Aksi dibatalkan.")
        _clear_steps(context, user.id)
        return True

    # awaiting a txn id to manage
    if step == _STEP_AWAIT_TEXT:
        data = _get_step(context, user.id, _KEY_ADMIN_DATA) or {}
        action = data.get("action")
        if action == "manage_txn":
            txn = text.strip()
            await _admin_manage_txn_by_id(update, context, txn)
            _clear_steps(context, user.id)
            return True

    # default: clear and continue
    _clear_steps(context, user.id)
    return False

# ------------------------------------------------------------------
# Admin helper implementations
# ------------------------------------------------------------------
async def _admin_list_active(cq, context):
    """
    List active loans (limited). Display basic info.
    """
    sheets = context.application.bot_data.get("sheets_manager")
    try:
        headers = await sheets.async_ensure_headers(PEMINJAMAN_SHEET, ["ID Transaksi", "Nama Barang", "Peminjam ID", "Qty Dipinjam", "Tanggal Pinjam", "Deadline", "Status Peminjaman"])
        rows = await sheets.async_get_all_records(PEMINJAMAN_SHEET)
        lines = []
        count = 0
        for r in rows:
            st = str(r.get("Status Peminjaman", "")).lower()
            if "disetujui" in st or "dipinjam" in st:
                tx = r.get("ID Transaksi") or ""
                name = r.get("Nama Barang") or ""
                pid = r.get("Peminjam ID") or ""
                qty = r.get("Qty Dipinjam") or ""
                dl = r.get("Deadline") or ""
                lines.append(f"- `{tx}` — {name} — peminjam: {pid} — qty: {qty} — dl: {dl}")
                count += 1
                if count >= 30:
                    break
        if not lines:
            await cq.edit_message_text("_Tidak ada peminjaman aktif._")
            return
        await cq.edit_message_text("*Peminjaman Aktif:*\n" + "\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        logger.exception("Failed to list active peminjaman: %s", e)
        try:
            await cq.edit_message_text("Gagal mengambil data peminjaman aktif.")
        except Exception:
            pass

async def _admin_manage_txn_by_id(update, context, txn_id: str):
    """
    Present details for a txn and offer approve/deny buttons.
    """
    sheets = context.application.bot_data.get("sheets_manager")
    try:
        headers = await sheets.async_ensure_headers(PEMINJAMAN_SHEET, ["ID Transaksi", "Nama Barang", "Peminjam ID", "Peminjam Nama", "Qty Dipinjam", "Tanggal Pinjam", "Deadline", "Status Peminjaman", "Keterangan"])
        row_idx = await sheets.async_find_row_by_value(PEMINJAMAN_SHEET, "ID Transaksi", txn_id, headers_map=headers)
        if not row_idx:
            await send_md(update, f"Transaksi `{txn_id}` tidak ditemukan.", parse_mode="Markdown")
            return
        # read row (by reading relevant cells)
        nama = await sheets.async_get_cell_value(PEMINJAMAN_SHEET, row_idx, headers["Nama Barang"])
        pid = await sheets.async_get_cell_value(PEMINJAMAN_SHEET, row_idx, headers["Peminjam ID"])
        pnama = await sheets.async_get_cell_value(PEMINJAMAN_SHEET, row_idx, headers["Peminjam Nama"])
        qty = await sheets.async_get_cell_value(PEMINJAMAN_SHEET, row_idx, headers["Qty Dipinjam"])
        status = await sheets.async_get_cell_value(PEMINJAMAN_SHEET, row_idx, headers["Status Peminjaman"])
        dl = await sheets.async_get_cell_value(PEMINJAMAN_SHEET, row_idx, headers["Deadline"])
        text = (
            f"*Transaksi:* `{txn_id}`\n"
            f"- Nama Barang: `{escape_md(nama)}`\n"
            f"- Peminjam: `{escape_md(pnama or pid)}`\n"
            f"- Qty: {escape_md(qty)}\n"
            f"- Deadline: {escape_md(dl)}\n"
            f"- Status: {escape_md(status)}"
        )
        kb = [
            [InlineKeyboardButton("Setujui ✅", callback_data=f"admin:approve:{txn_id}")],
            [InlineKeyboardButton("Tolak ❌", callback_data=f"admin:deny:{txn_id}")]
        ]
        await send_md(update, text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    except Exception as e:
        logger.exception("Failed manage txn %s: %s", txn_id, e)
        await send_md(update, f"Gagal mengambil detail transaksi `{txn_id}`.", parse_mode="Markdown")

async def _admin_set_tx_status(cq_or_update, context, txn_id: str, status: str):
    """
    Set status for a transaction. If approving, this function DOES NOT automatically
    decrement stock — it only updates Status Peminjaman. The admin can later run /approve command if needed.
    """
    sheets = context.application.bot_data.get("sheets_manager")
    try:
        headers = await sheets.async_ensure_headers(PEMINJAMAN_SHEET, ["ID Transaksi", "Status Peminjaman", "Tanggal Kembali"])
        row_idx = await sheets.async_find_row_by_value(PEMINJAMAN_SHEET, "ID Transaksi", txn_id, headers_map=headers)
        if not row_idx:
            await send_md(cq_or_update, f"Transaksi `{txn_id}` tidak ditemukan.", parse_mode="Markdown")
            return
        await sheets.async_update_cell(PEMINJAMAN_SHEET, row_idx, headers["Status Peminjaman"], status)
        if status.lower().startswith("dikembalikan"):
            # set return timestamp if available
            try:
                await sheets.async_update_cell(PEMINJAMAN_SHEET, row_idx, headers.get("Tanggal Kembali", headers.get("Tanggal Pinjam")), datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            except Exception:
                pass
        await send_md(cq_or_update, f"Status untuk `{txn_id}` diubah menjadi *{status}*.", parse_mode="Markdown")
    except Exception as e:
        logger.exception("Failed set tx status %s -> %s: %s", txn_id, status, e)
        await send_md(cq_or_update, "Gagal memperbarui status transaksi.")

async def _perform_danger_purge(update, context, uid: Optional[int]):
    """
    Danger function to clear inventory & peminjaman sheets.
    Use only after explicit confirmation.
    """
    sheets = context.application.bot_data.get("sheets_manager")
    if not sheets:
        await send_md(update, "Layanan Google Sheets tidak tersedia.")
        return
    try:
        # best-effort: clear worksheets by replacing with headers
        await sheets.async_ensure_headers(INVENTARIS_SHEET, ["Nama Barang", "Kategori", "Witel", "Divisi", "Total Qty", "Tersedia", "Status", "Pemilik ID", "Pemilik Nama", "Keterangan"])
        await sheets.async_ensure_headers(PEMINJAMAN_SHEET, ["ID Transaksi", "Nama Barang", "Peminjam ID", "Peminjam Nama", "Qty Dipinjam", "Tanggal Pinjam", "Deadline", "Status Peminjaman", "Keterangan", "Reminder Sent"])
        # Clear by deleting all rows after header: implementation depends on GoogleSheetsManager; fallback: re-create worksheets if supported.
        try:
            await sheets.async_clear_sheet(INVENTARIS_SHEET)
            await sheets.async_clear_sheet(PEMINJAMAN_SHEET)
            await send_md(update, "🧨 Semua data demo pada inventory & peminjaman telah dihapus.")
            await sheets.async_write_log(str(uid or ""), "DangerPurge", "ALL", "Purge performed by admin")
        except Exception:
            # if clear not available, overwrite by appending nothing (best-effort)
            await send_md(update, "⚠️ Tidak dapat membersihkan sheet secara otomatis (fungsi clear not tersedia). Silakan periksa manual.")
    except Exception as e:
        logger.exception("Failed performing danger purge: %s", e)
        await send_md(update, "Gagal melakukan purge. Periksa logs.")
