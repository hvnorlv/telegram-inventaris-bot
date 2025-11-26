# reminder.py
"""
Reminder module: checks Peminjaman sheet and sends reminders.

Features:
- Periodic job (default daily) to:
  - Send "upcoming" reminders REMINDER_DAYS_BEFORE days before deadline.
  - Send "overdue" notifications when deadline passed.
- Mark Peminjaman rows in column "Reminder Sent" to avoid duplicates.
- Provide admin commands to force-run reminder job and view status.
- Uses GoogleSheetsManager async wrappers (async_get_all_records, async_update_cell, async_write_log, etc.)
- Designed for python-telegram-bot v20 (async).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from telegram import Update
from telegram.ext import ContextTypes, CommandHandler

from config import PEMINJAMAN_SHEET, INVENTARIS_SHEET
from handlers.common import send_md, escape_md

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

# reminder configuration (change if needed)
REMINDER_DAYS_BEFORE = 3  # send reminder X days before deadline
REMINDER_JOB_INTERVAL_SECONDS = 60 * 60 * 24  # default run daily
REMINDER_TIME_WINDOW_HOURS = 24  # treat run as per-day window

# commands (admin-only ideally)
CMD_FORCE_RUN = "reminder_forcerun"
CMD_STATUS = "reminder_status"


def register_reminder_handlers():
    """
    Return list of CommandHandler objects to register in application.
    These commands are admin-level and should be permission-checked by caller or handlers.
    """
    return [
        CommandHandler(CMD_FORCE_RUN, reminder_force_run),
        CommandHandler(CMD_STATUS, reminder_status),
    ]


async def schedule_reminder_job(application, interval_seconds: int = REMINDER_JOB_INTERVAL_SECONDS):
    """
    Schedule the periodic reminder job on the application's job_queue.
    Call this during bot startup (after application has job_queue).
    """
    # If a job with same name exists, remove it first
    try:
        # name the job function for identification
        # We'll store job in application.job_queue via run_repeating
        application.job_queue.run_repeating(_reminder_job_wrapper, interval=interval_seconds, first=10, name="reminder_job", data=None)
        logger.info("Scheduled reminder job every %s seconds", interval_seconds)
    except Exception as e:
        logger.exception("Failed scheduling reminder job: %s", e)


async def _reminder_job_wrapper(context: ContextTypes.DEFAULT_TYPE):
    """
    Job wrapper invoked by job queue. Delegates to core check function.
    """
    try:
        await run_reminder_check(context)
    except Exception as e:
        logger.exception("Reminder job failed: %s", e)


async def run_reminder_check(context: ContextTypes.DEFAULT_TYPE) -> Dict[str, Any]:
    """
    Core reminder check logic.
    Returns summary dict for diagnostics.
    """
    bot = context.application.bot
    sheets = context.application.bot_data.get("sheets_manager")
    if not sheets:
        logger.warning("Reminder: sheets_manager not available.")
        return {"ok": False, "reason": "no_sheets"}

    # ensure headers exist & get mapping
    pem_map = await sheets.async_ensure_headers(PEMINJAMAN_SHEET, [
        "ID Transaksi", "Nama Barang", "Peminjam ID", "Peminjam Nama",
        "Qty Dipinjam", "Tanggal Pinjam", "Deadline", "Status Peminjaman", "Keterangan", "Reminder Sent"
    ])
    # read all peminjaman rows
    pem_rows = await sheets.async_get_all_records(PEMINJAMAN_SHEET)
    now_date = datetime.now().date()

    to_send: List[Tuple[int, Dict[str, Any]]] = []  # list of (row_index, record) to operate on
    # We need underlying worksheet for reading raw cell row numbers; async_get_all_records returns dicts with no row index.
    # Strategy: to find row indices, fetch first column (ID Transaksi) column values and map them to row numbers.
    try:
        ws = await sheets.async_get_worksheet_by_name(PEMINJAMAN_SHEET)
        id_col = pem_map.get("ID Transaksi")
        id_vals = ws.col_values(id_col)
        # build map txn -> row index
        txn_to_row = {}
        for idx, v in enumerate(id_vals[1:], start=2):
            if v is not None and str(v).strip() != "":
                txn_to_row[str(v).strip()] = idx
    except Exception:
        txn_to_row = {}

    summary = {"upcoming_sent": 0, "overdue_sent": 0, "errors": 0, "skipped": 0}
    for rec in pem_rows:
        try:
            txn = rec.get("ID Transaksi") or rec.get("id") or ""
            if not txn:
                summary["skipped"] += 1
                continue
            # find row index
            row_idx = txn_to_row.get(str(txn).strip())
            # parse fields
            nama_barang = rec.get("Nama Barang") or ""
            peminjam_id_raw = rec.get("Peminjam ID") or ""
            try:
                peminjam_id = int(str(peminjam_id_raw).strip())
            except Exception:
                peminjam_id = None
            peminjam_nama = rec.get("Peminjam Nama") or ""
            qty = rec.get("Qty Dipinjam") or rec.get("Qty") or ""
            status = (rec.get("Status Peminjaman") or "").strip().lower()
            deadline_raw = (rec.get("Deadline") or "").strip()
            reminder_sent_raw = (rec.get("Reminder Sent") or "").strip()

            # only consider active loans (approved/disetujui/dipinjam) or waiting? We'll send reminders only for approved/dipinjam
            # Common statuses: "Menunggu Persetujuan", "Disetujui", "Dipinjam", "Selesai"
            if not status or status.startswith("menunggu") or status.startswith("batal") or status.startswith("selesai"):
                # skip requests still waiting for approval or finished/cancelled
                summary["skipped"] += 1
                continue

            if not deadline_raw:
                summary["skipped"] += 1
                continue
            # parse ISO date YYYY-MM-DD
            try:
                deadline_date = datetime.strptime(deadline_raw.split()[0], "%Y-%m-%d").date()
            except Exception:
                # if can't parse, skip
                summary["skipped"] += 1
                continue

            days_to_deadline = (deadline_date - now_date).days
            # decide if upcoming reminder or overdue
            send_upcoming = (0 <= days_to_deadline <= REMINDER_DAYS_BEFORE)
            send_overdue = (days_to_deadline < 0)

            # check if reminder already sent in type
            already_sent_upcoming = f"before:{days_to_deadline}" in reminder_sent_raw if reminder_sent_raw else False
            already_sent_overdue = reminder_sent_raw.lower().startswith("overdue") if reminder_sent_raw else False
            # More robust marking: we'll mark with "before:X" or "overdue:YYYY-MM-DD"

            if send_upcoming and not already_sent_upcoming:
                # prepare and send upcoming reminder
                if peminjam_id:
                    try:
                        text = (
                            f"🔔 *Pengingat Peminjaman (Mendekati Deadline)*\n\n"
                            f"Barang: *{escape_md(nama_barang)}*\n"
                            f"Jumlah: `{escape_md(str(qty))}`\n"
                            f"Peminjam: *{escape_md(peminjam_nama)}* (`{peminjam_id}`)\n"
                            f"Deadline: `{deadline_date.isoformat()}` (dalam {days_to_deadline} hari)\n\n"
                            "Mohon persiapkan pengembalian/konfirmasi jika diperlukan."
                        )
                        await bot.send_message(chat_id=peminjam_id, text=text, parse_mode="Markdown")
                    except Exception as e:
                        logger.exception("Failed sending upcoming reminder to peminjam %s: %s", peminjam_id, e)
                        summary["errors"] += 1
                        continue
                # also try to notify owner (if exists in Inventaris)
                await _notify_owner_for_reminder(context, sheets, nama_barang, txn, kind="upcoming", days_left=days_to_deadline)
                # mark Reminder Sent for this row (append or set)
                if row_idx:
                    new_marker = f"before:{days_to_deadline}"
                    await _mark_reminder_sent(sheets, row_idx, pem_map, new_marker)
                summary["upcoming_sent"] += 1
            elif send_overdue and not already_sent_overdue:
                # overdue notification
                if peminjam_id:
                    try:
                        text = (
                            f"⚠️ *Peminjaman Telah Terlambat*\n\n"
                            f"Barang: *{escape_md(nama_barang)}*\n"
                            f"Jumlah: `{escape_md(str(qty))}`\n"
                            f"Peminjam: *{escape_md(peminjam_nama)}* (`{peminjam_id}`)\n"
                            f"Deadline: `{deadline_date.isoformat()}` — sudah lewat {abs(days_to_deadline)} hari.\n\n"
                            "Mohon segera mengembalikan atau hubungi pemilik barang untuk perpanjangan."
                        )
                        await bot.send_message(chat_id=peminjam_id, text=text, parse_mode="Markdown")
                    except Exception as e:
                        logger.exception("Failed sending overdue reminder to peminjam %s: %s", peminjam_id, e)
                        summary["errors"] += 1
                        continue
                await _notify_owner_for_reminder(context, sheets, nama_barang, txn, kind="overdue", days_overdue=abs(days_to_deadline))
                # mark Reminder Sent as overdue
                if row_idx:
                    new_marker = f"overdue:{now_date.isoformat()}"
                    await _mark_reminder_sent(sheets, row_idx, pem_map, new_marker)
                summary["overdue_sent"] += 1
            else:
                # nothing to do or already sent
                summary["skipped"] += 1
                continue
        except Exception as e:
            logger.exception("Error processing peminjaman row: %s", e)
            summary["errors"] += 1
            continue

    # write a summary log
    try:
        await sheets.async_write_log("system", "ReminderRun", "", f"{summary}")
    except Exception:
        logger.debug("Failed writing reminder run log (non-fatal).")
    return summary


async def _mark_reminder_sent(sheets, row_idx: int, pem_map: Dict[str, int], marker: str) -> bool:
    """
    Set the Reminder Sent column for the given row to marker.
    Overwrites previous value (we keep simple behavior).
    """
    try:
        col = pem_map.get("Reminder Sent")
        if not col:
            # ensure header exists and refresh mapping
            pem_map = await sheets.async_ensure_headers(PEMINJAMAN_SHEET, [
                "ID Transaksi", "Nama Barang", "Peminjam ID", "Peminjam Nama",
                "Qty Dipinjam", "Tanggal Pinjam", "Deadline", "Status Peminjaman", "Keterangan", "Reminder Sent"
            ])
            col = pem_map.get("Reminder Sent")
        if not col:
            return False
        ok = await sheets.async_update_cell(PEMINJAMAN_SHEET, row_idx, col, marker)
        return ok
    except Exception as e:
        logger.exception("Failed to mark reminder sent for row %s: %s", row_idx, e)
        return False


async def _notify_owner_for_reminder(context: ContextTypes.DEFAULT_TYPE, sheets, item_name: str, txn: str, *, kind: str = "upcoming", days_left: Optional[int] = None, days_overdue: Optional[int] = None):
    """
    Notify the owner (Pemilik ID in Inventaris) about upcoming/overdue peminjaman.
    """
    try:
        # ensure inventaris headers
        inv_map = await sheets.async_ensure_headers(INVENTARIS_SHEET, [
            "Nama Barang", "Kategori", "Witel", "Divisi", "Total Qty", "Tersedia", "Status", "Pemilik ID", "Pemilik Nama"
        ])
        # find row by Nama Barang
        row_idx = await sheets.async_find_row_by_value(INVENTARIS_SHEET, "Nama Barang", item_name, headers_map=inv_map)
        if not row_idx:
            return False
        pemilik_col = inv_map.get("Pemilik ID")
        pemilik_raw = await sheets.async_get_cell_value(INVENTARIS_SHEET, row_idx, pemilik_col)
        if not pemilik_raw:
            return False
        try:
            owner_chat_id = int(str(pemilik_raw).strip())
        except Exception:
            return False

        if kind == "upcoming":
            text = (
                f"📩 *Notifikasi: Peminjaman Mendekati Deadline*\n\n"
                f"Barang: *{escape_md(item_name)}*\n"
                f"TXN: `{escape_md(txn)}`\n"
                f"Sisa hari: `{days_left}`\n\n"
                "Mohon cek persiapan pengembalian/komunikasi dengan peminjam."
            )
        else:  # overdue
            text = (
                f"⚠️ *Notifikasi: Peminjaman Telah Terlambat*\n\n"
                f"Barang: *{escape_md(item_name)}*\n"
                f"TXN: `{escape_md(txn)}`\n"
                f"Terlambat: `{days_overdue}` hari\n\n"
                "Mohon tindak lanjut untuk pengembalian atau kompensasi."
            )
        try:
            await context.application.bot.send_message(chat_id=owner_chat_id, text=text, parse_mode="Markdown")
            await sheets.async_write_log(str(owner_chat_id), "NotifOwnerReminder", item_name, f"tx={txn} kind={kind}")
            return True
        except Exception as e:
            logger.exception("Failed sending owner reminder to %s: %s", owner_chat_id, e)
            return False
    except Exception as e:
        logger.exception("notify_owner_for_reminder failed: %s", e)
        return False


# -----------------------
# Admin commands
# -----------------------
async def reminder_force_run(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Admin command to force-run reminder job.
    Permission check should be applied by caller or by reading permission manager.
    """
    user = update.effective_user
    sheets = context.application.bot_data.get("sheets_manager")
    if not sheets:
        await send_md(update, "❌ Layanan belum tersedia.")
        return
    await send_md(update, "🔄 Memulai pemeriksaan reminder (paksa)...")
    summary = await run_reminder_check(context)
    await send_md(update, f"✅ Selesai. Ringkasan: `{escape_md(str(summary))}`")


async def reminder_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Admin command to show counts of upcoming/overdue requests (quick summary).
    """
    sheets = context.application.bot_data.get("sheets_manager")
    if not sheets:
        await send_md(update, "❌ Layanan belum tersedia.")
        return

    # ensure headers
    pem_map = await sheets.async_ensure_headers(PEMINJAMAN_SHEET, [
        "ID Transaksi", "Nama Barang", "Peminjam ID", "Peminjam Nama",
        "Qty Dipinjam", "Tanggal Pinjam", "Deadline", "Status Peminjaman", "Keterangan", "Reminder Sent"
    ])
    rows = await sheets.async_get_all_records(PEMINJAMAN_SHEET)
    now_date = datetime.now().date()

    upcoming = []
    overdue = []
    for r in rows:
        status = (r.get("Status Peminjaman") or "").strip().lower()
        if not status or status.startswith("menunggu") or status.startswith("batal") or status.startswith("selesai"):
            continue
        deadline_raw = (r.get("Deadline") or "").strip()
        if not deadline_raw:
            continue
        try:
            dl = datetime.strptime(deadline_raw.split()[0], "%Y-%m-%d").date()
        except Exception:
            continue
        days = (dl - now_date).days
        if 0 <= days <= REMINDER_DAYS_BEFORE:
            upcoming.append(r)
        if days < 0:
            overdue.append(r)

    teks = (
        f"📋 *Status Reminder*\n\n"
        f"Upcoming (dalam {REMINDER_DAYS_BEFORE} hari): `{len(upcoming)}`\n"
        f"Overdue: `{len(overdue)}`\n\n"
        "Gunakan `/reminder_forcerun` untuk memaksa cek sekarang."
    )
    await send_md(update, teks)


# End of reminder.py
