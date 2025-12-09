# handlers/inventory.py
"""
Inventory handlers.

Perubahan utama:
- Daftar ringkas (list / pagination / myitems / search results) TIDAK menampilkan Item ID.
  Tombol/label hanya menyajikan: Nama, Specs singkat, Lokasi (Witel/Divisi) dan Qty tersedia.
- Saat tombol list diklik callback carries row index (inv_view_row:<row>) sehingga akurat.
- View detail menampilkan Item ID, Serial, Pemilik, dsb. Dari view detail user dapat menekan
  tombol "Pinjam barang ini" yang memanggil callback `brw_item:{row}` untuk kompatibilitas
  dengan alur pinjam di handlers/borrow.py.
- Semua filter (kategori, witel, search, pagination) diperbarui sesuai format label baru.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional, Tuple, Set

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    CallbackQuery,
)
from telegram.ext import (
    ContextTypes,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

from handlers.common import send_md, escape_md, require_registration
from config import (
    INVENTARIS_SHEET,
    ITEMS_PER_PAGE,
    PRESET_CATEGORIES,
    PEMINJAMAN_SHEET,
)
from utils import safe_int

logger = logging.getLogger("handlers.inventory")
logger.addHandler(logging.NullHandler())


# -------------------------
# retry helper (network resilience)
# -------------------------
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
                "retry_async: caught %s, attempt %d/%d — retrying after %.2fs",
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


# -------------------------
# small helpers
# -------------------------
def _user_from(update_or_cq: Any):
    if isinstance(update_or_cq, CallbackQuery):
        return update_or_cq.from_user
    if hasattr(update_or_cq, "effective_user"):
        return update_or_cq.effective_user
    return None


def _compact_specs_from_rec(rec: Dict[str, Any]) -> str:
    """
    Short specs used in list label.

    Perubahan: jangan sertakan 'Serial Number' di ringkasan daftar.
    Sertakan 'Merek' (jika ada) dan beberapa Keterangan singkat.
    """
    parts = []
    # prefer 'Merek' jika ada, lalu beberapa keterangan lain — TIDAK termasuk Serial Number
    for key in ("Merek", "Keterangan 1", "Keterangan", "Keterangan 2"):
        v = rec.get(key)
        if v:
            s = str(v).strip()
            if s:
                parts.append(s)
    return " | ".join(parts)


def _label_for_button(
    row_num: int,
    rec: Dict[str, Any],
    index_in_page: Optional[int] = None,
) -> str:
    """
    Build short label for page buttons.

    Perubahan:
    - Tidak menampilkan Item ID.
    - Tidak menampilkan Serial Number.
    - Tidak menampilkan Divisi.
    - Menampilkan: "<index>. Nama — <specs> — <Witel sekarang/asal jika ada> — <available>"
      dimana available adalah angka langsung (mis. 6).
    """
    name = str(rec.get("Nama Barang") or rec.get("Nama") or "-").strip()
    specs = _compact_specs_from_rec(rec)
    available = safe_int(rec.get("Tersedia") or rec.get("Total Qty") or 0, 0)
    # tampilkan hanya Witel (prioritaskan Witel Sekarang)
    witel = (rec.get("Witel Sekarang") or rec.get("Witel") or "") or ""
    witel = str(witel).strip()

    pieces: List[str] = []
    if index_in_page is not None:
        pieces.append(f"{index_in_page}.")
    pieces.append(name)
    if specs:
        pieces.append(specs)
    if witel:
        pieces.append(witel)
    pieces.append(str(available))

    label = " — ".join(pieces)
    if len(label) > 64:
        label = label[:61] + "..."
    return label



def _format_item_summary_from_rec(row: int, rec: Dict[str, Any]) -> str:
    """
    Build detailed text for a single item (full view).
    Includes Item ID, serial, owner, sumber & sekarang.
    """
    name = rec.get("Nama Barang", "") or rec.get("Nama", "")
    cat = rec.get("Kategori", "") or "-"
    origin_witel = rec.get("Witel Asal") or rec.get("Witel") or "-"
    origin_div = rec.get("Divisi Asal") or rec.get("Divisi") or "-"
    current_witel = rec.get("Witel Sekarang") or rec.get("Witel") or "-"
    current_div = rec.get("Divisi Sekarang") or rec.get("Divisi") or "-"
    total = rec.get("Total Qty") or "-"
    available = rec.get("Tersedia") or "-"
    status = rec.get("Status") or "-"
    pemilik_nama = rec.get("Pemilik Nama") or "-"
    pemilik_id = rec.get("Pemilik ID") or "-"
    item_id = rec.get("Item ID") or "-"

    k1 = rec.get("Keterangan 1") or rec.get("Keterangan") or "-"
    k2 = rec.get("Keterangan 2") or "-"
    serial = rec.get("Serial Number") or "-"

    parts = [
        f"Row: `{row}`",
        f"Nama: *{escape_md(str(name))}*",
        f"Item ID: `{escape_md(str(item_id))}`",
    ]
    if cat:
        parts.append(f"Kategori: {escape_md(str(cat))}")
    # specs block
    specs = []
    if k1 and k1 != "-":
        specs.append(escape_md(str(k1)))
    if k2 and k2 != "-":
        specs.append(escape_md(str(k2)))
    if serial and serial != "-":
        specs.append(escape_md(str(serial)))
    if specs:
        parts.append("Specs: `" + " | ".join(specs) + "`")

    parts.append(
        f"Qty: {escape_md(str(available))}/{escape_md(str(total))} — Status: {escape_md(str(status))}"
    )
    # <-- label changed here from 'Witel Asal' menjadi 'Sumber Barang'
    parts.append(f"Sumber Barang: {escape_md(str(origin_witel))} — Divisi Asal: {escape_md(str(origin_div))}")
    parts.append(f"Witel Sekarang: {escape_md(str(current_witel))} — Divisi Sekarang: {escape_md(str(current_div))}")
    parts.append(f"Pemilik: {escape_md(str(pemilik_nama))} (`{escape_md(str(pemilik_id))}`)")

    return "\n".join(parts)


def _build_page_keyboard_from_pairs(
    pairs: List[Tuple[int, Dict[str, Any]]],
    page: int,
    items_per_page: int,
) -> InlineKeyboardMarkup:
    total = len(pairs)
    pages = max(1, (total + items_per_page - 1) // items_per_page)
    page = max(1, min(page, pages))
    start = (page - 1) * items_per_page
    end = min(start + items_per_page, total)
    kb: List[List[InlineKeyboardButton]] = []
    for idx in range(start, end):
        rownum, rec = pairs[idx]
        index_label = idx - start + 1
        label = _label_for_button(rownum, rec, index_in_page=index_label)
        kb.append([InlineKeyboardButton(label, callback_data=f"inv_view_row:{rownum}")])

    nav_row: List[InlineKeyboardButton] = []
    if page > 1:
        nav_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"inv_page:{page-1}"))
    nav_row.append(InlineKeyboardButton(f"Halaman {page}/{pages}", callback_data="inv_page:noop"))
    if page < pages:
        nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"inv_page:{page+1}"))
    kb.append(nav_row)

    kb.append([InlineKeyboardButton("Refresh", callback_data=f"inv_page:{page}"),
               InlineKeyboardButton("Kembali ke Menu", callback_data="inv_main")])
    return InlineKeyboardMarkup(kb)


async def _is_user_allowed_modify(
    rec: Dict[str, Any],
    user_id: int,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    """
    Check whether given user may modify (reduce/remove) this inventory row:
    - owner (Pemilik ID)
    - or permission_manager indicates admin/can_manage_users
    """
    try:
        pemilik_raw = rec.get("Pemilik ID") or rec.get("PemilikId") or rec.get("owner") or ""
        if pemilik_raw:
            try:
                if int(str(pemilik_raw).strip()) == int(user_id):
                    return True
            except Exception:
                if str(pemilik_raw).strip() == str(user_id).strip():
                    return True

        pm = context.application.bot_data.get("permission_manager")
        if pm:
            # try async API then sync fallback
            if hasattr(pm, "async_has_permission"):
                try:
                    if await pm.async_has_permission(user_id, "can_manage_users") or await pm.async_has_permission(user_id, "admin"):
                        return True
                except Exception:
                    pass
            if hasattr(pm, "has_permission"):
                try:
                    if pm.has_permission(user_id, "can_manage_users") or getattr(pm, "is_admin", lambda *_: False)(user_id):
                        return True
                except Exception:
                    pass
            if hasattr(pm, "is_admin"):
                try:
                    if pm.is_admin(user_id):
                        return True
                except Exception:
                    pass
    except Exception:
        logger.exception("Error checking permissions for user %s on rec %s", user_id, rec)
    return False


# -------------------------
# Witel helpers
# -------------------------
async def _get_witel_values_from_sheets(sheets) -> List[str]:
    out: List[str] = []
    if not sheets:
        return out
    try:
        # try dedicated Witel sheet first
        try:
            recs = await retry_async(sheets.async_get_all_records, "Witel", retries=2, delay=0.3)
            if recs:
                names: List[str] = []
                for r in recs:
                    if isinstance(r, dict):
                        for key in ("Witel", "Nama Witel", "Daftar Witel"):
                            if key in r and r[key]:
                                names.append(str(r[key]).strip())
                                break
                        else:
                            for v in r.values():
                                if v not in (None, ""):
                                    names.append(str(v).strip())
                                    break
                if names:
                    seen = set()
                    uniq = []
                    for n in names:
                        if n not in seen:
                            seen.add(n)
                            uniq.append(n)
                    return uniq
        except Exception:
            logger.debug("reading dedicated Witel sheet failed; fallback to INVENTARIS_SHEET", exc_info=True)

        inv_recs = await retry_async(sheets.async_get_all_records, INVENTARIS_SHEET, retries=3, delay=0.4)
        added: Set[str] = set()
        out_list: List[str] = []
        for r in inv_recs:
            try:
                v = r.get("Witel") or r.get("WITEL") or r.get("witel") or ""
                s = str(v).strip()
                if s and s not in added:
                    added.add(s)
                    out_list.append(s)
            except Exception:
                continue
        return out_list
    except Exception:
        logger.exception("Failed to read Witel values")
        return []


async def inv_witel_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    await cq.answer()
    if not await require_registration(cq, context):
        return

    sheets = context.application.bot_data.get("sheets_manager")
    witel_list = []
    try:
        witel_list = await _get_witel_values_from_sheets(sheets)
    except Exception as e:
        logger.exception("inv_witel_cb: %s", e)

    if not witel_list:
        kb = [[InlineKeyboardButton("Kembali", callback_data="inv_main")]]
        await send_md(cq, "⚠️ Daftar Witel kosong atau gagal dibaca.", reply_markup=InlineKeyboardMarkup(kb))
        return

    kb: List[List[InlineKeyboardButton]] = []
    for name in witel_list[:40]:
        kb.append([InlineKeyboardButton(name, callback_data=f"inv_witel_filter:{name}")])
    kb.append([InlineKeyboardButton("Kembali", callback_data="inv_main")])
    await send_md(cq, f"📍 Pilih Witel (total {len(witel_list)}):", reply_markup=InlineKeyboardMarkup(kb))


async def inv_witel_filter_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    await cq.answer()
    if not await require_registration(cq, context):
        return
    data = (cq.data or "").split(":", 1)
    if len(data) < 2:
        await send_md(cq, "Pilihan Witel tidak dikenali.")
        return
    witel = data[1].strip()
    if not witel:
        await send_md(cq, "Nama Witel kosong.")
        return
    await _show_available(cq, context, page=1, witel_filter=witel)


# -------------------------
# /inventaris - main menu
# -------------------------
async def inventaris_cmd(update_or_cq: Any, context: ContextTypes.DEFAULT_TYPE):
    if not await require_registration(update_or_cq, context):
        return

    header = (
        "*Menu Inventaris*\n\n"
        "Pilih filter atau kategori:\n"
        "- Barang yang tersedia\n"
        "- Barang sedang dipinjam\n"
        "- Menunggu persetujuan pemilik\n\n"
    )
    kb: List[List[InlineKeyboardButton]] = []
    kb.append(
        [
            InlineKeyboardButton(
                "📦  Barang yang tersedia",
                callback_data="inv_cat:available",
            )
        ]
    )
    kb.append(
        [
            InlineKeyboardButton(
                "🔵  Barang sedang dipinjam",
                callback_data="inv_cat:borrowed",
            )
        ]
    )
    kb.append(
        [
            InlineKeyboardButton(
                "⏳  Menunggu persetujuan pemilik",
                callback_data="inv_cat:pending",
            )
        ]
    )
    kb.append(
        [
            InlineKeyboardButton(
                "🔎 Filter by Witel",
                callback_data="inv_witel",
            )
        ]
    )

    presets: List[str] = []
    for c in PRESET_CATEGORIES:
        if c.lower() == "custom":
            continue
        if c not in presets:
            presets.append(c)
        if len(presets) >= 6:
            break

    for i in range(0, len(presets), 2):
        left = presets[i]
        right = presets[i + 1] if i + 1 < len(presets) else None
        row = [
            InlineKeyboardButton(left, callback_data=f"inv_filter:{left}")
        ]
        if right:
            row.append(
                InlineKeyboardButton(
                    right,
                    callback_data=f"inv_filter:{right}",
                )
            )
        kb.append(row)

    kb.append([InlineKeyboardButton("Custom", callback_data="inv_filter:Custom")])
    # ubah label Batal -> Tutup (menutup penggunaan menu)
    kb.append([InlineKeyboardButton("Tutup", callback_data="inv_cancel")])
    await send_md(
        update_or_cq,
        header,
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown",
    )


# -------------------------
# Category callbacks
# -------------------------
async def inv_category_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    await cq.answer()
    if not await require_registration(cq, context):
        return
    data = (cq.data or "").split(":", 1)
    if len(data) < 2:
        return
    token = data[1]
    if token == "noop":
        return
    if token == "available":
        await _show_available(cq, context, page=1)
        return
    if token == "borrowed":
        await _show_borrowed(cq, context, page=1)
        return
    if token == "pending":
        await _show_pending(cq, context, page=1)
        return
    await send_md(cq, "Pilihan kategori tidak dikenali.")


async def inv_filter_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    await cq.answer()
    if not await require_registration(cq, context):
        return
    data = (cq.data or "").split(":", 1)
    if len(data) < 2:
        return
    token = data[1].strip()
    if token.lower() == "custom":
        context.user_data["awaiting_inv_custom_search"] = True
        await send_md(cq, "Ketik nama barang (atau sebagian nama) untuk mencari custom items:")
        return
    await _show_available(cq, context, page=1, category_filter=token)


# -------------------------
# Show available items
# -------------------------
async def _show_available(
    target: Any,
    context: ContextTypes.DEFAULT_TYPE,
    page: int = 1,
    category_filter: Optional[str] = None,
    witel_filter: Optional[str] = None,
):
    sheets = context.application.bot_data.get("sheets_manager")
    if not sheets:
        await send_md(target, "❌ Layanan Google Sheets belum tersedia.")
        return

    try:
        all_recs = await retry_async(
            sheets.async_get_all_records,
            INVENTARIS_SHEET,
            retries=4,
            delay=0.8,
            backoff=2.0,
        )
    except Exception as e:
        logger.exception("Failed fetching inventory for available: %s", e)
        await send_md(target, "❌ Gagal mengambil data inventaris.")
        return

    pairs: List[Tuple[int, Dict[str, Any]]] = []
    for idx, rec in enumerate(all_recs):
        row_num = idx + 2
        status = str(rec.get("Status", "") or "").strip().lower()
        if status == "removed":
            continue
        available = safe_int(rec.get("Tersedia") or rec.get("Total Qty") or 0, 0)
        if available <= 0:
            continue
        if category_filter:
            if (
                str(rec.get("Kategori", "")).strip().lower()
                != str(category_filter).strip().lower()
            ):
                continue
        if witel_filter:
            try:
                wv_origin = str(rec.get("Witel") or "").strip().lower()
                wv_current = str(rec.get("Witel Sekarang") or "").strip().lower()
            except Exception:
                wv_origin = ""
                wv_current = ""
            combined = f"{wv_origin} {wv_current}".strip()
            if witel_filter.strip().lower() not in combined:
                continue
        pairs.append((row_num, rec))

    # sort alphabetically by Nama Barang
    pairs.sort(key=lambda x: str((x[1].get("Nama Barang") or x[1].get("Nama") or "")).lower())

    if not pairs:
        kb = [
            [InlineKeyboardButton("Kembali ke Menu", callback_data="inv_main")],
            [InlineKeyboardButton("Tutup", callback_data="inv_cancel")],
        ]
        if witel_filter and category_filter:
            await send_md(
                target,
                (
                    "📭 Tidak ada barang tersedia untuk filter kategori "
                    f"*{escape_md(str(category_filter))}* dan Witel "
                    f"*{escape_md(str(witel_filter))}*."
                ),
                reply_markup=InlineKeyboardMarkup(kb),
                parse_mode="Markdown",
            )
        elif witel_filter:
            await send_md(
                target,
                (
                    "📭 Tidak ada barang tersedia untuk Witel "
                    f"*{escape_md(str(witel_filter))}*."
                ),
                reply_markup=InlineKeyboardMarkup(kb),
                parse_mode="Markdown",
            )
        else:
            await send_md(
                target,
                "📭 Tidak ada barang tersedia sesuai filter.",
                reply_markup=InlineKeyboardMarkup(kb),
            )
        return

    items_per_page = int(ITEMS_PER_PAGE or 8)
    markup = _build_page_keyboard_from_pairs(pairs, page, items_per_page)
    filt_parts = []
    if category_filter:
        filt_parts.append(f"Kategori: {category_filter}")
    if witel_filter:
        filt_parts.append(f"Witel: {witel_filter}")
    filt_text = ", ".join(filt_parts) if filt_parts else "semua"
    await send_md(
        target,
        (
            "📚 Barang yang tersedia — total *{total}* item "
            "(filter: {filt})."
        ).format(total=len(pairs), filt=escape_md(str(filt_text))),
        reply_markup=markup,
        parse_mode="Markdown",
    )
    context.user_data["inv_last_pairs"] = pairs
    context.user_data["inv_last_mode"] = "available"
    context.user_data["inv_last_filters"] = {
        "category": category_filter,
        "witel": witel_filter,
    }



# -------------------------
# Show borrowed items (PEMINJAMAN sheet)
# -------------------------
async def _show_borrowed(
    target: Any,
    context: ContextTypes.DEFAULT_TYPE,
    page: int = 1,
):
    sheets = context.application.bot_data.get("sheets_manager")
    if not sheets:
        await send_md(target, "❌ Layanan Google Sheets belum tersedia.")
        return

    try:
        pem_recs = await retry_async(
            sheets.async_get_all_records,
            PEMINJAMAN_SHEET,
            retries=4,
            delay=0.8,
            backoff=2.0,
        )
    except Exception as e:
        logger.exception("Failed fetching peminjaman records: %s", e)
        await send_md(target, "❌ Gagal mengambil data peminjaman.")
        return

    pairs: List[Tuple[int, Dict[str, Any]]] = []
    for idx, rec in enumerate(pem_recs):
        row_num = idx + 2
        status = str(rec.get("Status Peminjaman", "") or "").strip().lower()
        if any(
            k in status for k in ("disetujui", "dipinjam", "approved", "borrowed")
        ):
            display = {
                "Nama Barang": rec.get("Nama Barang", ""),
                "Item ID": rec.get("Item ID") or "",
                "Kategori": "",
                "Witel": "",
                "Divisi": "",
                "Total Qty": "",
                "Tersedia": f"{rec.get('Qty Dipinjam', '')}",
                "Status": status,
                "Pemilik ID": rec.get("Peminjam ID") or "",
                "Pemilik Nama": rec.get("Peminjam Nama") or "",
                "Keterangan": rec.get("Keterangan", ""),
            }
            pairs.append((row_num, display))

    # sort by name
    pairs.sort(key=lambda x: str((x[1].get("Nama Barang") or "")).lower())

    if not pairs:
        kb = [
            [InlineKeyboardButton("Kembali ke Menu", callback_data="inv_main")],
            [InlineKeyboardButton("Tutup", callback_data="inv_cancel")],
        ]
        await send_md(
            target,
            "📭 Tidak ada peminjaman yang sedang dipinjam saat ini.",
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return

    items_per_page = int(ITEMS_PER_PAGE or 8)
    markup = _build_page_keyboard_from_pairs(pairs, page, items_per_page)
    await send_md(
        target,
        f"🔁 Barang yang sedang dipinjam — total *{len(pairs)}*.",
        reply_markup=markup,
        parse_mode="Markdown",
    )
    context.user_data["inv_last_pairs"] = pairs
    context.user_data["inv_last_mode"] = "borrowed"



# -------------------------
# Show pending approvals
# -------------------------
async def _show_pending(
    target: Any,
    context: ContextTypes.DEFAULT_TYPE,
    page: int = 1,
):
    sheets = context.application.bot_data.get("sheets_manager")
    if not sheets:
        await send_md(target, "❌ Layanan Google Sheets belum tersedia.")
        return

    try:
        pem_recs = await retry_async(
            sheets.async_get_all_records,
            PEMINJAMAN_SHEET,
            retries=4,
            delay=0.8,
            backoff=2.0,
        )
    except Exception as e:
        logger.exception("Failed fetching peminjaman records: %s", e)
        await send_md(target, "❌ Gagal mengambil data peminjaman.")
        return

    pending_keywords = {
        "menunggu",
        "pending",
        "waiting",
        "waiting_owner",
        "waiting owner",
        "menunggu persetujuan",
        "menunggu persetujuan pemilik",
        "menunggu pemilik",
        "awaiting_owner",
        "awaiting approval",
    }

    pairs: List[Tuple[int, Dict[str, Any]]] = []
    for idx, rec in enumerate(pem_recs):
        row_num = idx + 2
        raw_status = str(rec.get("Status Peminjaman", "") or "").strip().lower()
        is_pending = any(k in raw_status for k in pending_keywords)
        if is_pending:
            display = {
                "Nama Barang": rec.get("Nama Barang", ""),
                "Item ID": rec.get("Item ID") or "",
                "Kategori": "",
                "Witel": "",
                "Divisi": "",
                "Total Qty": "",
                "Tersedia": f"{rec.get('Qty Dipinjam', '')}",
                "Status": raw_status,
                "Pemilik ID": rec.get("Peminjam ID") or "",
                "Pemilik Nama": rec.get("Peminjam Nama") or "",
                "Keterangan": rec.get("Keterangan", ""),
            }
            pairs.append((row_num, display))

    # sort by name
    pairs.sort(key=lambda x: str((x[1].get("Nama Barang") or "")).lower())

    if not pairs:
        kb = [
            [InlineKeyboardButton("Kembali ke Menu", callback_data="inv_main")],
            [InlineKeyboardButton("Tutup", callback_data="inv_cancel")],
        ]
        await send_md(
            target,
            "📭 Tidak ada permintaan pinjam yang menunggu persetujuan.",
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return

    items_per_page = int(ITEMS_PER_PAGE or 8)
    markup = _build_page_keyboard_from_pairs(pairs, page, items_per_page)
    await send_md(
        target,
        f"⏳ Menunggu persetujuan — total *{len(pairs)}*.",
        reply_markup=markup,
        parse_mode="Markdown",
    )
    context.user_data["inv_last_pairs"] = pairs
    context.user_data["inv_last_mode"] = "pending"



# -------------------------
# Pagination callback
# -------------------------
async def inv_page_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    await cq.answer()
    if not await require_registration(cq, context):
        return
    data = cq.data or ""
    parts = data.split(":", 1)
    if len(parts) < 2:
        return
    token = parts[1]
    if token == "noop":
        return
    try:
        page = int(token)
    except Exception:
        page = 1

    filters = context.user_data.get("inv_last_filters", {})
    category_filter = filters.get("category")
    witel_filter = filters.get("witel")

    pairs = context.user_data.get("inv_last_pairs")
    if not pairs:
        mode = context.user_data.get("inv_last_mode", "available")
        if mode == "available":
            await _show_available(cq, context, page=page, category_filter=category_filter, witel_filter=witel_filter)
            return
        if mode == "borrowed":
            await _show_borrowed(cq, context, page=page)
            return
        if mode == "pending":
            await _show_pending(cq, context, page=page)
            return

    items_per_page = int(ITEMS_PER_PAGE or 8)
    markup = _build_page_keyboard_from_pairs(pairs, page, items_per_page)
    await send_md(cq, f"📚 Inventaris — halaman {page}", reply_markup=markup)


# -------------------------
# Sync owner location into INVENTARIS (Witel Sekarang / Divisi Sekarang)
# -------------------------
async def _sync_owner_location_to_inventaris(sheets, sheet_row: int, owner_profile: Tuple[str, str]) -> None:
    try:
        if not sheets or sheet_row <= 0:
            return
        headers = await retry_async(
            sheets.async_ensure_headers,
            INVENTARIS_SHEET,
            [
                "Nama Barang",
                "Kategori",
                "Witel",
                "Divisi",
                "Witel Asal",
                "Divisi Asal",
                "Witel Sekarang",
                "Divisi Sekarang",
                "Total Qty",
                "Tersedia",
                "Status",
                "Pemilik ID",
                "Pemilik Nama",
                "Keterangan 1",
                "Keterangan 2",
                "Serial Number",
                "Item ID",
            ],
            retries=3,
            delay=0.3,
        )
        col_wsekarang = headers.get("Witel Sekarang")
        col_dsekarang = headers.get("Divisi Sekarang")
        new_witel = (owner_profile[0] or "").strip()
        new_div = (owner_profile[1] or "").strip()

        try:
            if col_wsekarang:
                cur = str(await sheets.async_get_cell_value(INVENTARIS_SHEET, sheet_row, col_wsekarang) or "").strip()
                if new_witel and cur != new_witel:
                    await retry_async(sheets.async_update_cell, INVENTARIS_SHEET, sheet_row, col_wsekarang, new_witel, retries=2, delay=0.2)
            if col_dsekarang:
                cur = str(await sheets.async_get_cell_value(INVENTARIS_SHEET, sheet_row, col_dsekarang) or "").strip()
                if new_div and cur != new_div:
                    await retry_async(sheets.async_update_cell, INVENTARIS_SHEET, sheet_row, col_dsekarang, new_div, retries=2, delay=0.2)
        except Exception:
            logger.exception("_sync_owner_location_to_inventaris: failed to update", exc_info=True)

        try:
            note = f"sync_owner_location row={sheet_row} -> now Witel='{new_witel}' Divisi='{new_div}'"
            await retry_async(sheets.async_write_log, "system", "SyncOwnerLocation", f"row{sheet_row}", note, retries=2, delay=0.2)
        except Exception:
            pass
    except Exception:
        logger.exception("_sync_owner_location_to_inventaris: unexpected", exc_info=True)


# -------------------------
# View item detail by sheet row (inv_view_row:<row>)
# -------------------------
async def inv_view_row_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    await cq.answer()
    if not await require_registration(cq, context):
        return

    data = cq.data or ""
    parts = data.split(":", 1)
    if len(parts) < 2:
        await send_md(cq, "Pilihan tidak dikenali.")
        return
    _, row_token = parts
    sheet_row = safe_int(row_token, default=0)
    if sheet_row <= 0:
        await send_md(cq, "Nomor baris tidak valid.")
        return

    sheets = context.application.bot_data.get("sheets_manager")
    if not sheets:
        await send_md(cq, "❌ Layanan Google Sheets belum tersedia.")
        return

    try:
        inv_records = await retry_async(sheets.async_get_all_records, INVENTARIS_SHEET, retries=4, delay=0.8, backoff=2.0)
    except Exception as e:
        logger.exception("Failed fetching inventory records for view: %s", e)
        await send_md(cq, "❌ Gagal mengambil data inventaris.")
        return

    idx0 = sheet_row - 2
    if 0 <= idx0 < len(inv_records):
        rec = inv_records[idx0]

        # attempt to sync owner's current location into Witel Sekarang / Divisi Sekarang
        owner_raw = rec.get("Pemilik ID") or rec.get("PemilikId") or rec.get("owner") or ""
        owner_current = ("", "")
        if owner_raw:
            try:
                owner_id = int(str(owner_raw).strip())
            except Exception:
                owner_id = None
            if owner_id:
                try:
                    from handlers.add import _get_user_profile_witel_divisi as _get_profile  # type: ignore
                    owner_current = await _get_profile(sheets, owner_id)
                except Exception:
                    owner_current = ("", "")
        try:
            await _sync_owner_location_to_inventaris(sheets, sheet_row, owner_current)
            if owner_current and (owner_current[0] or owner_current[1]):
                if owner_current[0]:
                    rec["Witel Sekarang"] = owner_current[0]
                if owner_current[1]:
                    rec["Divisi Sekarang"] = owner_current[1]
        except Exception:
            logger.debug("inv_view_row_cb: sync attempt failed", exc_info=True)

        text = _format_item_summary_from_rec(sheet_row, rec)
        kb = [
            [InlineKeyboardButton("📥 Pinjam barang ini", callback_data=f"brw_item:{sheet_row}")],
            [InlineKeyboardButton("🔽 Kurangi Qty", callback_data=f"inv_reduce_row:{sheet_row}")],
            [InlineKeyboardButton("🗑️ Hapus Item", callback_data=f"inv_remove_row:{sheet_row}")],
            [InlineKeyboardButton("Kembali ke daftar", callback_data="inv_page:1")],
            [InlineKeyboardButton("Kembali ke Menu", callback_data="inv_main")],
        ]
        await send_md(cq, text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
        return

    # fallback: maybe user clicked a PEMINJAMAN row index — try to read peminjaman sheet
    try:
        pem_records = await retry_async(sheets.async_get_all_records, PEMINJAMAN_SHEET, retries=4, delay=0.8, backoff=2.0)
    except Exception:
        pem_records = []

    idx0p = sheet_row - 2
    if 0 <= idx0p < len(pem_records):
        rec = pem_records[idx0p]
        item_id = rec.get("Item ID") or "-"
        text = (
            f"TXN: `{escape_md(str(rec.get('ID Transaksi', '')) )}`\n"
            f"Nama: *{escape_md(str(rec.get('Nama Barang', '')) )}* [{escape_md(str(item_id))}]\n"
            f"Qty: {escape_md(str(rec.get('Qty Dipinjam', '')) )}\n"
            f"Peminjam: {escape_md(str(rec.get('Peminjam Nama', '')) )}\n"
            f"Status: {escape_md(str(rec.get('Status Peminjaman', '')) )}"
        )
        await send_md(cq, text, parse_mode="Markdown")
        return

    await send_md(cq, "Item/TXN tidak ditemukan di sheet yang tersedia.")


# -------------------------
# Reduce qty flow (inv_reduce_row:<row>)
# -------------------------
async def inv_reduce_row_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    await cq.answer()
    if not await require_registration(cq, context):
        return
    parts = (cq.data or "").split(":", 1)
    if len(parts) < 2:
        await send_md(cq, "Pilihan tidak dikenali.")
        return
    sheet_row = safe_int(parts[1], default=0)
    if sheet_row <= 0:
        await send_md(cq, "Nomor baris tidak valid.")
        return

    sheets = context.application.bot_data.get("sheets_manager")
    if not sheets:
        await send_md(cq, "❌ Layanan Google Sheets belum tersedia.")
        return

    try:
        recs = await retry_async(sheets.async_get_all_records, INVENTARIS_SHEET, retries=4, delay=0.8, backoff=2.0)
    except Exception as e:
        logger.exception("Failed fetching inventory for reduce: %s", e)
        await send_md(cq, "❌ Gagal mengambil data inventaris.")
        return

    idx0 = sheet_row - 2
    if idx0 < 0 or idx0 >= len(recs):
        await send_md(cq, "Item tidak ditemukan.")
        return

    rec = recs[idx0]
    allowed = await _is_user_allowed_modify(rec, cq.from_user.id, context)
    if not allowed:
        await send_md(cq, "🚫 Hanya pemilik barang atau admin yang dapat mengurangi quantity item ini.")
        return

    context.user_data["awaiting_reduce"] = {"sheet_row": sheet_row, "user_id": cq.from_user.id}
    await send_md(cq, "Masukkan jumlah yang ingin dikurangi (angka). Ketik `0` untuk membatalkan.", parse_mode="Markdown")


# -------------------------
# Text handler: custom search, reduce flows
# -------------------------
async def text_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # custom search (after pressing Custom in menu)
    if context.user_data.get("awaiting_inv_custom_search"):
        if not await require_registration(update, context):
            context.user_data.pop("awaiting_inv_custom_search", None)
            return
        query = (update.message.text or "").strip()
        context.user_data.pop("awaiting_inv_custom_search", None)
        sheets = context.application.bot_data.get("sheets_manager")
        if not sheets:
            await send_md(update, "❌ Layanan Google Sheets belum tersedia.")
            return
        try:
            recs = await retry_async(sheets.async_get_all_records, INVENTARIS_SHEET, retries=4, delay=0.8, backoff=2.0)
        except Exception:
            await send_md(update, "❌ Gagal mengambil data inventaris.")
            return
        pairs: List[Tuple[int, Dict[str, Any]]] = []
        for idx, rec in enumerate(recs):
            name = str(rec.get("Nama Barang", "") or "")
            item_id = str(rec.get("Item ID") or "")
            brand = str(rec.get("Merek") or rec.get("Keterangan 1") or "")
            hay = " ".join([name, item_id, brand]).lower()
            if query.lower() in hay:
                pairs.append((idx + 2, rec))
        if not pairs:
            await send_md(update, f"🔎 Tidak ditemukan barang yang cocok untuk `{escape_md(query)}`", parse_mode="Markdown")
            return
        context.user_data["inv_last_pairs"] = pairs
        items_per_page = int(ITEMS_PER_PAGE or 8)
        markup = _build_page_keyboard_from_pairs(pairs, 1, items_per_page)
        await send_md(update, f"🔎 Hasil pencarian untuk `{escape_md(query)}` — total *{len(pairs)}*.", reply_markup=markup, parse_mode="Markdown")
        return

    # reduce flow finish: user types a number after being prompted
    if context.user_data.get("awaiting_reduce"):
        payload = context.user_data.pop("awaiting_reduce")
        sheet_row = int(payload.get("sheet_row", 0))
        user_id = payload.get("user_id")
        if update.effective_user and update.effective_user.id != int(user_id):
            await send_md(update, "Hanya pengguna yang memulai aksi yang boleh menyelesaikannya.")
            return

        q = safe_int((update.message.text or "").strip(), default=-1)
        if q < 0:
            await send_md(update, "Masukkan angka valid (mis. `2`). Aksi dibatalkan.")
            return
        if q == 0:
            await send_md(update, "Aksi pengurangan dibatalkan.")
            return

        sheets = context.application.bot_data.get("sheets_manager")
        if not sheets:
            await send_md(update, "❌ Layanan Google Sheets belum tersedia — tidak dapat mengubah data.")
            return

        try:
            recs = await retry_async(sheets.async_get_all_records, INVENTARIS_SHEET, retries=4, delay=0.8, backoff=2.0)
        except Exception as e:
            logger.exception("Failed fetching inventory for reduce: %s", e)
            await send_md(update, "❌ Gagal mengambil data inventaris.")
            return

        idx0 = sheet_row - 2
        if idx0 < 0 or idx0 >= len(recs):
            await send_md(update, "Item tidak ditemukan (baris out of range).")
            return

        rec = recs[idx0]
        allowed = await _is_user_allowed_modify(rec, update.effective_user.id, context)
        if not allowed:
            await send_md(update, "🚫 Hanya pemilik barang atau admin yang dapat mengurangi quantity item ini.")
            return

        try:
            headers = await retry_async(
                sheets.async_ensure_headers,
                INVENTARIS_SHEET,
                ["Tersedia", "Total Qty"],
                retries=3,
            )
        except Exception:
            headers = {}

        col_available = headers.get("Tersedia")
        if col_available:
            try:
                cur_val = await retry_async(sheets.async_get_cell_value, INVENTARIS_SHEET, sheet_row, col_available, retries=3)
                cur_num = safe_int(cur_val, 0)
            except Exception:
                cur_num = safe_int(rec.get("Tersedia") or rec.get("Total Qty") or 0, 0)
        else:
            cur_num = safe_int(rec.get("Tersedia") or rec.get("Total Qty") or 0, 0)

        new_available = max(0, cur_num - q)
        ok1 = True
        try:
            if col_available:
                ok1 = await retry_async(sheets.async_update_cell, INVENTARIS_SHEET, sheet_row, col_available, str(new_available), retries=3)
        except Exception as e:
            logger.exception("Failed updating Tersedia: %s", e)
            ok1 = False

        if ok1:
            try:
                await retry_async(sheets.async_write_log, update.effective_user.id, "KurangiQty", rec.get("Nama Barang", ""), f"-{q}", retries=3)
            except Exception:
                pass
            await send_md(update, ("✅ Berhasil mengurangi *{name}* sebanyak *{q}*. Tersedia sekarang: *{avail}*.").format(name=escape_md(rec.get("Nama Barang", "")), q=q, avail=new_available), parse_mode="Markdown")
        else:
            await send_md(update, "❌ Gagal memperbarui inventory di Google Sheets.")
        return

    # otherwise do nothing so other handlers may act
    return


# -------------------------
# Remove item (soft-delete) by sheet row
# -------------------------
async def inv_remove_row_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    await cq.answer()
    if not await require_registration(cq, context):
        return
    parts = (cq.data or "").split(":", 1)
    if len(parts) < 2:
        await send_md(cq, "Pilihan tidak dikenali.")
        return
    sheet_row = safe_int(parts[1], default=0)
    if sheet_row <= 0:
        await send_md(cq, "Nomor baris tidak valid.")
        return

    sheets = context.application.bot_data.get("sheets_manager")
    if not sheets:
        await send_md(cq, "❌ Layanan Google Sheets belum tersedia.")
        return

    try:
        recs = await retry_async(sheets.async_get_all_records, INVENTARIS_SHEET, retries=4, delay=0.8, backoff=2.0)
    except Exception as e:
        logger.exception("Failed fetching inventory for removal: %s", e)
        await send_md(cq, "❌ Gagal mengambil data inventaris.")
        return

    idx0 = sheet_row - 2
    if idx0 < 0 or idx0 >= len(recs):
        await send_md(cq, "Item tidak ditemukan.")
        return

    rec = recs[idx0]
    allowed = await _is_user_allowed_modify(rec, cq.from_user.id, context)
    if not allowed:
        await send_md(cq, "🚫 Hanya pemilik barang atau admin yang dapat menghapus item ini.")
        return

    try:
        headers = await retry_async(sheets.async_ensure_headers, INVENTARIS_SHEET, ["Status", "Tersedia"], retries=4)
    except Exception:
        headers = {}

    col_status = headers.get("Status")
    col_available = headers.get("Tersedia")
    ok = True
    try:
        if col_status:
            ok = await retry_async(sheets.async_update_cell, INVENTARIS_SHEET, sheet_row, col_status, "Removed", retries=3)
        if col_available:
            await retry_async(sheets.async_update_cell, INVENTARIS_SHEET, sheet_row, col_available, "0", retries=3)
    except Exception as e:
        logger.exception("Failed updating sheet during remove: %s", e)
        ok = False

    if ok:
        try:
            await retry_async(sheets.async_write_log, cq.from_user.id, "HapusItem", rec.get("Nama Barang", ""), "Soft-removed", retries=3)
        except Exception:
            pass
        await send_md(cq, ("🗑️ Item *{name}* telah dihapus (soft-delete).").format(name=escape_md(rec.get("Nama Barang", ""))), parse_mode="Markdown")
    else:
        await send_md(cq, "❌ Gagal menghapus item di Google Sheets.")


# -------------------------
# /myitems - list & manage items owned by the user
# -------------------------
async def myitems_cmd(update_or_cq: Any, context: ContextTypes.DEFAULT_TYPE):
    if not await require_registration(update_or_cq, context):
        return
    user = _user_from(update_or_cq)
    if not user:
        await send_md(update_or_cq, "⚠️ Tidak dapat mengenali pengguna.")
        return

    sheets = context.application.bot_data.get("sheets_manager")
    if not sheets:
        await send_md(update_or_cq, "❌ Layanan Google Sheets belum tersedia.")
        return

    try:
        recs = await retry_async(sheets.async_get_all_records, INVENTARIS_SHEET, retries=4, delay=0.6, backoff=2.0)
    except Exception as e:
        logger.exception("Failed fetching inventory for /myitems: %s", e)
        await send_md(update_or_cq, "❌ Gagal mengambil data inventaris.")
        return

    pairs: List[Tuple[int, Dict[str, Any]]] = []
    for idx, rec in enumerate(recs):
        try:
            pemilik_raw = rec.get("Pemilik ID") or rec.get("PemilikId") or rec.get("owner") or ""
            if pemilik_raw and str(pemilik_raw).strip() != "":
                if int(str(pemilik_raw).strip()) == int(user.id):
                    pairs.append((idx + 2, rec))
        except Exception:
            continue
    # sort by name
    pairs.sort(key=lambda x: str((x[1].get("Nama Barang") or x[1].get("Nama") or "")).lower())  

    if not pairs:
        await send_md(update_or_cq, "📭 Kamu belum menambahkan barang apapun ke inventaris.")
        return

    items_per_page = int(ITEMS_PER_PAGE or 8)
    kb: List[List[InlineKeyboardButton]] = []
    for i, (rownum, rec) in enumerate(pairs[:items_per_page]):
        label = _label_for_button(rownum, rec, index_in_page=i + 1)
        kb.append([InlineKeyboardButton(label, callback_data=f"myinv_view:{rownum}")])
    kb.append([InlineKeyboardButton("Refresh", callback_data="myinv_refresh")])
    kb.append([InlineKeyboardButton("Kembali", callback_data="inv_main")])
    await send_md(update_or_cq, f"📦 Barang milik kamu — total *{len(pairs)}*.", reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    context.user_data["myinv_pairs"] = pairs


async def myinv_view_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    await cq.answer()
    if not await require_registration(cq, context):
        return
    data = (cq.data or "").split(":", 1)
    if len(data) < 2:
        await send_md(cq, "Pilihan tidak dikenali.")
        return
    try:
        row_idx = int(data[1])
    except Exception:
        await send_md(cq, "Row tidak valid.")
        return

    sheets = context.application.bot_data.get("sheets_manager")
    if not sheets:
        await send_md(cq, "❌ Layanan Google Sheets belum tersedia.")
        return

    try:
        recs = await retry_async(sheets.async_get_all_records, INVENTARIS_SHEET, retries=4, delay=0.6, backoff=2.0)
    except Exception:
        await send_md(cq, "❌ Gagal mengambil data inventaris.")
        return

    idx0 = row_idx - 2
    if idx0 < 0 or idx0 >= len(recs):
        await send_md(cq, "Item tidak ditemukan.")
        return

    rec = recs[idx0]
    allowed = await _is_user_allowed_modify(rec, cq.from_user.id, context)
    teks = _format_item_summary_from_rec(row_idx, rec)
    kb: List[List[InlineKeyboardButton]] = []
    if allowed:
        kb.append([InlineKeyboardButton("🔽 Reduce Qty", callback_data=f"myinv_reduce:{row_idx}")])
        kb.append([InlineKeyboardButton("🗑️ Remove (soft-delete)", callback_data=f"myinv_remove:{row_idx}")])
    kb.append([InlineKeyboardButton("🔁 Refresh", callback_data=f"myinv_view:{row_idx}")])
    kb.append([InlineKeyboardButton("Kembali", callback_data="myinv_refresh")])
    await send_md(cq, teks, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")


async def myinv_refresh_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    await cq.answer()
    await myitems_cmd(cq, context)


async def myinv_reduce_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    await cq.answer()
    if not await require_registration(cq, context):
        return
    data = (cq.data or "").split(":", 1)
    if len(data) < 2:
        return
    try:
        row_idx = int(data[1])
    except Exception:
        await send_md(cq, "Row tidak valid.")
        return
    # reuse reduce handler
    original = cq.data
    try:
        cq.data = f"inv_reduce_row:{row_idx}"
        await inv_reduce_row_cb(update, context)
    except Exception as e:
        logger.exception("myinv_reduce_cb fallback failed: %s", e)
        context.user_data["awaiting_reduce"] = {"sheet_row": row_idx, "user_id": cq.from_user.id}
        await send_md(cq, "Masukkan jumlah yang ingin dikurangi (angka). Ketik `0` untuk membatalkan.", parse_mode="Markdown")
    finally:
        try:
            cq.data = original
        except Exception:
            pass


async def myinv_remove_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    await cq.answer()
    if not await require_registration(cq, context):
        return
    data = (cq.data or "").split(":", 1)
    if len(data) < 2:
        await send_md(cq, "Pilihan tidak dikenali.")
        return
    try:
        row_idx = int(data[1])
    except Exception:
        await send_md(cq, "Row tidak valid.")
        return
    original = cq.data
    try:
        cq.data = f"inv_remove_row:{row_idx}"
        await inv_remove_row_cb(update, context)
    except Exception as e:
        logger.exception("myinv_remove_cb fallback failed: %s", e)
    finally:
        try:
            cq.data = original
        except Exception:
            pass


# -------------------------
# inv_cancel / inv_main callbacks
# -------------------------
async def inv_cancel_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq: CallbackQuery = update.callback_query
    await cq.answer()

    # bersihkan data inventaris terkait di user_data
    for k in ("inv_last_pairs", "inv_last_mode", "inv_last_filters", "myinv_pairs", "inv_last_page"):
        context.user_data.pop(k, None)
    context.user_data.pop("awaiting_inv_custom_search", None)

    # coba hapus pesan agar menu hilang; fallback: kirim/ubah pesan
    try:
        await cq.message.delete()
    except Exception:
        try:
            await cq.edit_message_text("✅ Menu inventaris ditutup. Terima kasih.")
        except Exception:
            try:
                await send_md(cq, "✅ Menu inventaris ditutup. Terima kasih.")
            except Exception:
                pass


async def inv_main_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    await cq.answer()
    try:
        await inventaris_cmd(cq, context)
    except Exception:
        logger.exception("Failed to show main inventaris menu from inv_main_cb")
        await send_md(cq, "Gagal menampilkan menu inventaris. Coba lagi.")


# -------------------------
# Backward-compatible alias
# -------------------------
async def handle_inventory_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await text_message_handler(update, context)


# -------------------------
# Register handlers
# -------------------------
def register_inventory_handlers(application):
    application.add_handler(CommandHandler("inventaris", inventaris_cmd))
    application.add_handler(CommandHandler("myitems", myitems_cmd))

    application.add_handler(CallbackQueryHandler(inv_category_cb, pattern=r"^inv_cat:"))
    application.add_handler(CallbackQueryHandler(inv_filter_cb, pattern=r"^inv_filter:"))
    application.add_handler(CallbackQueryHandler(inv_page_cb, pattern=r"^inv_page:"))
    application.add_handler(CallbackQueryHandler(inv_view_row_cb, pattern=r"^inv_view_row:"))
    application.add_handler(CallbackQueryHandler(inv_reduce_row_cb, pattern=r"^inv_reduce_row:"))
    application.add_handler(CallbackQueryHandler(inv_remove_row_cb, pattern=r"^inv_remove_row:"))
    application.add_handler(CallbackQueryHandler(inv_cancel_cb, pattern=r"^inv_cancel$"))
    application.add_handler(CallbackQueryHandler(inv_main_cb, pattern=r"^inv_main$"))

    # witel handlers
    application.add_handler(CallbackQueryHandler(inv_witel_cb, pattern=r"^inv_witel$"))
    application.add_handler(CallbackQueryHandler(inv_witel_filter_cb, pattern=r"^inv_witel_filter:"))

    # myitems callbacks
    application.add_handler(CallbackQueryHandler(myinv_view_cb, pattern=r"^myinv_view:"))
    application.add_handler(CallbackQueryHandler(myinv_refresh_cb, pattern=r"^myinv_refresh$"))
    application.add_handler(CallbackQueryHandler(myinv_reduce_cb, pattern=r"^myinv_reduce:"))
    application.add_handler(CallbackQueryHandler(myinv_remove_cb, pattern=r"^myinv_remove:"))

    # message handler: non-blocking so other flows can run
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, text_message_handler, block=False)
    )


__all__ = ["register_inventory_handlers", "handle_inventory_message"]
