# handlers/add.py
"""
Add handlers for inventory bot.

UPDATE:
- Witel & Divisi barang otomatis diambil dari profil pemilik (sheet "Users").
- Custom & generic flow now ask for an optional Serial Number.
- Serial Number disimpan ke kolom "Serial Number".
- Duplicate detection membandingkan:
  (Nama Barang, Kategori, Witel, Divisi, Pemilik Nama, Keterangan 1, Keterangan 2, Serial Number)
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional, Tuple, List

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    CallbackQuery,
    User,
)
from telegram.ext import (
    ContextTypes,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

from handlers.common import send_md, escape_md, clear_steps
from config import PRESET_CATEGORIES, INVENTARIS_SHEET
from utils import sanitize_input, safe_int, build_name_from_spec

logger = logging.getLogger("handlers.add")
logger.addHandler(logging.NullHandler())

# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------
BRAND_MODELS: Dict[str, List[str]] = {
    "Fiberhome": ["FHS-24", "FHS-48", "FHS-24-POE"],
    "ZTE": ["ZXR10-5928", "ZXA10-SR", "ZXR10-2910"],
    "Huawei": ["S5720-28X-LI-AC", "S5720-28X-SI", "AR1220"],
    "Cisco": ["CBS250-24", "CBS250-48", "C2960-X"],
    "MikroTik": ["CCR1009", "CRS326-24G", "RB4011"],
    "Other": ["Manual"],
}

SFP_RATES = ["1", "10", "25", "40", "100"]
SFP_DISTANCES = ["0.5", "1", "2", "10"]

ATTENUATOR_DB_PRESETS = ["3", "5", "10", "20"]  # dB presets
PC_LENGTH_PRESETS = ["0.5", "1", "2", "3", "5", "10"]  # meter presets


# ---------------------------------------------------------------------------
# Retry helper untuk operasi network/IO
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
                "retry_async: caught %s attempt %d/%d — retrying after %.2fs",
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
# Sheet helpers (Users & Inventaris)
# ---------------------------------------------------------------------------
async def _ensure_users_headers(sheets) -> Dict[str, int]:
    return await sheets.async_ensure_headers(
        "Users",
        ["User ID", "Nama", "Role", "Witel", "Divisi"],
    )


async def _get_user_profile_witel_divisi(
    sheets,
    user_id: int,
) -> Tuple[str, str]:
    """
    Ambil (Witel, Divisi) dari sheet Users berdasarkan user_id;
    mengembalikan ("", "") jika tidak ada.
    """
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


async def _ensure_inventaris_headers(sheets) -> Dict[str, int]:
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


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------
def _start_state(user_id: int) -> Dict[str, Any]:
    return {
        "user_id": user_id,
        "category": None,
        "specs": {},
        "awaiting_field": None,
    }


def _user_from(update_or_cq: Any) -> Optional[User]:
    if isinstance(update_or_cq, CallbackQuery):
        return update_or_cq.from_user
    if hasattr(update_or_cq, "effective_user"):
        return update_or_cq.effective_user
    return None


# ---------------------------------------------------------------------------
# /tambah entry
# ---------------------------------------------------------------------------
async def start_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        await send_md(update, "⚠️ Tidak dapat mengenali pengguna.")
        return

    # require registration (Witel & Divisi must be present)
    from handlers.common import require_registration

    if not await require_registration(update, context):
        return

    context.user_data["add_flow"] = _start_state(user.id)

    # Build category list preserving order; ensure Custom at end
    seen: set[str] = set()
    categories: List[str] = []
    for c in PRESET_CATEGORIES:
        if c not in seen:
            categories.append(c)
            seen.add(c)
    if "Custom" not in seen:
        categories.append("Custom")

    kb = [[InlineKeyboardButton(cat, callback_data=f"add_cat:{cat}")] for cat in categories]
    kb.append([InlineKeyboardButton("Batal", callback_data="add_cancel")])

    await send_md(
        update,
        "📦 Pilih kategori barang yang ingin ditambahkan:",
        reply_markup=InlineKeyboardMarkup(kb),
    )


# ---------------------------------------------------------------------------
# Category callback
# ---------------------------------------------------------------------------
async def category_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    await cq.answer()

    from handlers.common import require_registration

    if not await require_registration(cq, context):
        return

    data = (cq.data or "").split(":", 1)
    if len(data) < 2:
        await send_md(cq, "Pilihan kategori tidak dikenali.")
        return

    _, category = data
    user = cq.from_user
    state = context.user_data.setdefault("add_flow", _start_state(user.id))
    state["category"] = sanitize_input(category)
    state["specs"] = {}
    state["awaiting_field"] = None

    cat_low = (state["category"] or "").lower().strip()

    if cat_low == "patch cord":
        connectors = [
            "LC-LC",
            "LC-SC",
            "LC-FC",
            "LC-MU",
            "SC-SC",
            "SC-FC",
            "FC-FC",
            "MU-MU",
        ]
        kb = [[InlineKeyboardButton(c, callback_data=f"pc_connector:{c}")] for c in connectors]
        kb.append([InlineKeyboardButton("Custom Connector", callback_data="pc_connector:custom")])
        kb.append([InlineKeyboardButton("Batal", callback_data="add_cancel")])
        state["awaiting_field"] = "connector"
        await send_md(
            cq,
            "Patch Cord — pilih connector pair:",
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return

    if cat_low == "sfp":
        kb = [[InlineKeyboardButton(f"{r}G", callback_data=f"sfp_rate:{r}")] for r in SFP_RATES]
        kb.append([InlineKeyboardButton("Manual (ketik)", callback_data="sfp_rate:manual")])
        kb.append([InlineKeyboardButton("Batal", callback_data="add_cancel")])
        state["awaiting_field"] = "sfp_rate_choice"
        await send_md(
            cq,
            "SFP — pilih data rate (Gbps):",
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return

    if cat_low == "attenuator":
        kb = [[InlineKeyboardButton(f"{d}dB", callback_data=f"atten_db:{d}")] for d in ATTENUATOR_DB_PRESETS]
        kb.append([InlineKeyboardButton("Manual (ketik)", callback_data="atten_db:manual")])
        kb.append([InlineKeyboardButton("Batal", callback_data="add_cancel")])
        state["awaiting_field"] = "atten_db_choice"
        await send_md(
            cq,
            "Attenuator — pilih nilai dB:",
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return

    if cat_low == "l2 switch":
        brands = list(BRAND_MODELS.keys())
        kb = [[InlineKeyboardButton(b, callback_data=f"l2_brand:{b}")] for b in brands]
        kb.append([InlineKeyboardButton("Batal", callback_data="add_cancel")])
        state["awaiting_field"] = "l2_brand_choice"
        await send_md(
            cq,
            "L2 Switch — pilih merek/brand:",
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return

    if cat_low == "custom":
        state["awaiting_field"] = "custom_name"
        await send_md(
            cq,
            "Custom — ketik nama barang (contoh: 'Modem XYZ' atau 'UPS 1kVA'):",
        )
        return

    # generic fallback
    state["awaiting_field"] = "generic_specs"
    await send_md(
        cq,
        f"Kategori *{escape_md(category)}* dipilih. "
        f"Ketik spesifikasi / model (atau kosong):",
        parse_mode="Markdown",
    )


# ---------------------------------------------------------------------------
# Patch cord connector callback -> present length presets
# ---------------------------------------------------------------------------
async def pc_connector_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    await cq.answer()

    from handlers.common import require_registration

    if not await require_registration(cq, context):
        return

    data = (cq.data or "").split(":", 1)
    if len(data) < 2:
        await send_md(cq, "Pilihan connector tidak dikenali.")
        return

    _, token = data
    user = cq.from_user
    state = context.user_data.setdefault("add_flow", _start_state(user.id))

    if token == "custom":
        state["awaiting_field"] = "connector_custom"
        await send_md(
            cq,
            "Ketik tipe connector custom (mis. 'MTRJ-LC'):",
        )
        return

    state["specs"]["connector"] = token
    kb = [[InlineKeyboardButton(f"{L}m", callback_data=f"pc_length:{L}")] for L in PC_LENGTH_PRESETS]
    kb.append(
        [InlineKeyboardButton("Manual (ketik panjang)", callback_data="pc_length:manual")]
    )
    kb.append([InlineKeyboardButton("Batal", callback_data="add_cancel")])
    state["awaiting_field"] = "pc_length_choice"

    try:
        await cq.edit_message_text(
            f"Connector dipilih: *{escape_md(token)}*\nPilih panjang (meter):",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb),
        )
    except Exception:
        await send_md(
            cq,
            f"Connector: {token}\nPilih panjang (meter):",
            reply_markup=InlineKeyboardMarkup(kb),
        )


# ---------------------------------------------------------------------------
# Patch cord length callback (preset or manual)
# ---------------------------------------------------------------------------
async def pc_length_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    await cq.answer()

    from handlers.common import require_registration

    if not await require_registration(cq, context):
        return

    data = (cq.data or "").split(":", 1)
    if len(data) < 2:
        await send_md(cq, "Pilihan panjang tidak dikenali.")
        return

    _, token = data
    user = cq.from_user
    state = context.user_data.setdefault("add_flow", _start_state(user.id))

    if token == "manual":
        state["awaiting_field"] = "pc_length_manual"
        await send_md(
            cq,
            "Ketik panjang Patch Cord (meter), contoh: `1` atau `2.5`:",
        )
        return

    try:
        length_val = float(token)
    except Exception:
        await send_md(cq, "Nilai panjang tidak valid.")
        return

    state["specs"]["length_m"] = length_val
    state["awaiting_field"] = "qty"
    await send_md(
        cq,
        f"Panjang dipilih: *{token}m*. Masukkan quantity (angka):",
        parse_mode="Markdown",
    )


# ---------------------------------------------------------------------------
# SFP callbacks: rate -> distance -> serial
# ---------------------------------------------------------------------------
async def sfp_rate_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    await cq.answer()

    from handlers.common import require_registration

    if not await require_registration(cq, context):
        return

    data = (cq.data or "").split(":", 1)
    if len(data) < 2:
        await send_md(cq, "Pilihan data rate tidak dikenali.")
        return

    _, rate_token = data
    rate_token = sanitize_input(rate_token)
    user = cq.from_user
    state = context.user_data.setdefault("add_flow", _start_state(user.id))

    if rate_token.lower() == "manual":
        state["awaiting_field"] = "sfp_rate_manual"
        try:
            await cq.edit_message_text(
                "Ketik data rate (Gbps) untuk SFP, mis. `1`, `10`, `25`:",
            )
        except Exception:
            await send_md(
                cq,
                "Ketik data rate (Gbps) untuk SFP, mis. `1`, `10`, `25`:",
            )
        return

    state["specs"]["rate_g"] = rate_token
    kb = [[InlineKeyboardButton(f"{d}KM", callback_data=f"sfp_dist:{d}")] for d in SFP_DISTANCES]
    kb.append([InlineKeyboardButton("Manual (ketik)", callback_data="sfp_dist:manual")])
    kb.append([InlineKeyboardButton("Batal", callback_data="add_cancel")])
    state["awaiting_field"] = "sfp_dist_choice"

    try:
        await cq.edit_message_text(
            f"Data rate dipilih: *{escape_md(rate_token + 'G')}*\n"
            f"Pilih jarak (KM):",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb),
        )
    except Exception:
        await send_md(
            cq,
            f"Data rate: {rate_token}G\nPilih jarak (KM):",
            reply_markup=InlineKeyboardMarkup(kb),
        )


async def sfp_dist_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    await cq.answer()

    from handlers.common import require_registration

    if not await require_registration(cq, context):
        return

    data = (cq.data or "").split(":", 1)
    if len(data) < 2:
        await send_md(cq, "Pilihan jarak tidak dikenali.")
        return

    _, dist_token = data
    dist_token = sanitize_input(dist_token)
    user = cq.from_user
    state = context.user_data.setdefault("add_flow", _start_state(user.id))

    if dist_token.lower() == "manual":
        state["awaiting_field"] = "sfp_dist_manual"
        try:
            await cq.edit_message_text(
                "Ketik jarak (KM) untuk SFP (contoh: `0.5`, `1`, `10`):",
            )
        except Exception:
            await send_md(
                cq,
                "Ketik jarak (KM) untuk SFP (contoh: `0.5`, `1`, `10`):",
            )
        return

    state["specs"]["distance_km"] = dist_token
    state["awaiting_field"] = "sfp_serial"

    try:
        await cq.edit_message_text(
            f"Jarak dipilih: *{escape_md(dist_token + 'KM')}*.\n\n"
            f"Masukkan serial number SFP (ketik manual):",
            parse_mode="Markdown",
        )
    except Exception:
        await send_md(
            cq,
            f"Jarak: {dist_token}KM\nMasukkan serial number SFP (ketik manual):",
        )


# ---------------------------------------------------------------------------
# Attenuator callback (preset/manual)
# ---------------------------------------------------------------------------
async def atten_db_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    await cq.answer()

    from handlers.common import require_registration

    if not await require_registration(cq, context):
        return

    data = (cq.data or "").split(":", 1)
    if len(data) < 2:
        await send_md(cq, "Pilihan nilai dB tidak dikenali.")
        return

    _, db_token = data
    db_token = sanitize_input(db_token)
    user = cq.from_user
    state = context.user_data.setdefault("add_flow", _start_state(user.id))

    if db_token == "manual":
        state["awaiting_field"] = "atten_db_manual"
        try:
            await cq.edit_message_text(
                "Ketik nilai dB Attenuator (mis. `3`, `5`, `10`):",
            )
        except Exception:
            await send_md(
                cq,
                "Ketik nilai dB Attenuator (mis. `3`, `5`, `10`):",
            )
        return

    state["specs"]["db"] = db_token
    state["awaiting_field"] = "qty"
    await send_md(
        cq,
        f"Nilai dB dipilih: *{db_token}dB*. Masukkan quantity (angka):",
        parse_mode="Markdown",
    )


# ---------------------------------------------------------------------------
# L2 callbacks: brand -> model -> serial
# ---------------------------------------------------------------------------
async def l2_brand_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    await cq.answer()

    from handlers.common import require_registration

    if not await require_registration(cq, context):
        return

    data = (cq.data or "").split(":", 1)
    if len(data) < 2:
        await send_md(cq, "Pilihan brand tidak dikenali.")
        return

    _, brand = data
    brand = sanitize_input(brand)
    user = cq.from_user
    state = context.user_data.setdefault("add_flow", _start_state(user.id))
    state["specs"]["brand"] = brand

    models = BRAND_MODELS.get(brand, ["Manual"])
    kb = [[InlineKeyboardButton(m, callback_data=f"l2_model:{m}")] for m in models]
    kb.append(
        [InlineKeyboardButton("Manual / Lainnya", callback_data="l2_model:Manual")]
    )
    kb.append([InlineKeyboardButton("Batal", callback_data="add_cancel")])
    state["awaiting_field"] = "l2_model_choice"

    try:
        await cq.edit_message_text(
            f"L2 Switch — brand: *{escape_md(brand)}*\nPilih tipe/model:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb),
        )
    except Exception:
        await send_md(
            cq,
            f"Brand: {brand}\nPilih tipe/model:",
            reply_markup=InlineKeyboardMarkup(kb),
        )


async def l2_model_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    await cq.answer()

    from handlers.common import require_registration

    if not await require_registration(cq, context):
        return

    data = (cq.data or "").split(":", 1)
    if len(data) < 2:
        await send_md(cq, "Pilihan model tidak dikenali.")
        return

    _, model_token = data
    model_token = sanitize_input(model_token)
    user = cq.from_user
    state = context.user_data.setdefault("add_flow", _start_state(user.id))

    if model_token.lower() in ("manual", "lainnya", "other"):
        state["awaiting_field"] = "l2_model_manual"
        try:
            await cq.edit_message_text(
                "Ketik tipe/model L2 Switch (contoh: 'CBS250-24' atau 'S5720-28X'):",
            )
        except Exception:
            await send_md(
                cq,
                "Ketik tipe/model L2 Switch (contoh: 'CBS250-24' atau 'S5720-28X'):",
            )
        return

    state["specs"]["model"] = model_token
    state["awaiting_field"] = "l2_serial"
    try:
        await cq.edit_message_text(
            f"Tipe/model dipilih: *{escape_md(model_token)}*.\n\n"
            f"Masukkan serial number (ketik manual):",
            parse_mode="Markdown",
        )
    except Exception:
        await send_md(
            cq,
            f"Tipe/model: {model_token}\nMasukkan serial number (ketik manual):",
        )


# ---------------------------------------------------------------------------
# Quantity choices helper (inline, saat ini belum dipakai langsung)
# ---------------------------------------------------------------------------
def _quantity_kb() -> InlineKeyboardMarkup:
    qs = [1, 2, 3, 5, 10, 20, 50]
    kb = [[InlineKeyboardButton(str(q), callback_data=f"add_qty:{q}")] for q in qs]
    kb.append([InlineKeyboardButton("Ketik manual", callback_data="add_qty:manual")])
    kb.append([InlineKeyboardButton("Batal", callback_data="add_cancel")])
    return InlineKeyboardMarkup(kb)


async def qty_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    await cq.answer()

    from handlers.common import require_registration

    if not await require_registration(cq, context):
        return

    data = (cq.data or "").split(":", 1)
    if len(data) < 2:
        await send_md(cq, "Pilihan quantity tidak dikenali.")
        return

    _, token = data
    user = cq.from_user
    state = context.user_data.setdefault("add_flow", _start_state(user.id))

    if token == "manual":
        state["awaiting_field"] = "qty"
        await send_md(cq, "Ketik jumlah (angka) yang ingin ditambahkan:")
        return

    q = safe_int(token, default=0)
    if q <= 0:
        await send_md(cq, "Pilihan quantity tidak valid.")
        return

    state["specs"]["qty"] = q
    state["awaiting_field"] = None
    await _ask_confirm(cq, context)


# ---------------------------------------------------------------------------
# Text message dispatcher (handles manual inputs)
# ---------------------------------------------------------------------------
async def text_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    text = sanitize_input(update.message.text)
    state = context.user_data.get("add_flow")

    # Jika user tidak sedang dalam add_flow, hanya cek registrasi lalu biarkan handler lain yang urus
    if not state:
        from handlers.common import require_registration

        if not await require_registration(update, context):
            return
        return

    field = state.get("awaiting_field")

    # connector custom -> minta panjang (manual)
    if field == "connector_custom":
        state["specs"]["connector"] = text
        # BUGFIX: sebelumnya "pc_length" (tidak pernah di-handle), sekarang pakai "pc_length_manual"
        state["awaiting_field"] = "pc_length_manual"
        await send_md(
            update,
            "Masukkan panjang (meter), contoh: `1` atau `2.5`:",
        )
        return

    if field in ("pc_length_manual", "pc_length_choice"):
        try:
            length = float(text)
        except Exception:
            await send_md(
                update,
                "Masukkan panjang numeric (contoh `1` atau `2.5`).",
            )
            return
        state["specs"]["length_m"] = length
        state["awaiting_field"] = "qty"
        await send_md(update, "Masukkan quantity (angka):")
        return

    # SFP manual rate
    if field == "sfp_rate_manual":
        q = safe_int(text, default=-1)
        if q <= 0:
            await send_md(
                update,
                "Masukkan data rate numeric (Gbps) yang valid, mis. `1`, `10`, `25`.",
            )
            return
        state["specs"]["rate_g"] = str(q)
        kb = [[InlineKeyboardButton(f"{d}KM", callback_data=f"sfp_dist:{d}")] for d in SFP_DISTANCES]
        kb.append([InlineKeyboardButton("Manual (ketik)", callback_data="sfp_dist:manual")])
        kb.append([InlineKeyboardButton("Batal", callback_data="add_cancel")])
        state["awaiting_field"] = "sfp_dist_choice"
        await send_md(
            update,
            "Pilih jarak (KM) untuk SFP:",
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return

    # SFP manual distance
    if field == "sfp_dist_manual":
        try:
            dval = float(text)
        except Exception:
            await send_md(
                update,
                "Masukkan jarak numeric (KM) yang valid, mis. `0.5`, `1`, `10`.",
            )
            return
        state["specs"]["distance_km"] = str(dval)
        state["awaiting_field"] = "sfp_serial"
        await send_md(update, "Masukkan serial number SFP (ketik '-' jika tidak ada):")
        return

    # SFP serial -> qty=1 -> confirm
    if field == "sfp_serial":
        if text.strip() in ("-", ""):
            state["specs"].pop("serial", None)
        else:
            state["specs"]["serial"] = text
        state["specs"]["qty"] = 1
        state["awaiting_field"] = None
        await _ask_confirm(update, context)
        return

    # Attenuator manual dB entry
    if field == "atten_db_manual":
        try:
            db_val = float(text)
        except Exception:
            await send_md(
                update,
                "Masukkan nilai dB numeric yang valid, mis. `3`, `5`, `10`.",
            )
            return
        state["specs"]["db"] = str(db_val)
        state["awaiting_field"] = "qty"
        await send_md(update, "Masukkan quantity (angka):")
        return

    # L2 model manual
    if field == "l2_model_manual":
        if not text:
            await send_md(update, "Ketik tipe/model L2 Switch yang valid.")
            return
        state["specs"]["model"] = text
        state["awaiting_field"] = "l2_serial"
        await send_md(update, "Masukkan serial number (ketik '-' jika tidak ada):")
        return

    # L2 serial -> qty=1 -> confirm
    if field == "l2_serial":
        if text.strip() in ("-", ""):
            state["specs"].pop("serial", None)
        else:
            state["specs"]["serial"] = text
        state["specs"]["qty"] = 1
        state["awaiting_field"] = None
        await _ask_confirm(update, context)
        return

    # custom name
    if field == "custom_name":
        state["specs"]["name"] = text
        state["awaiting_field"] = "custom_keterangan"
        await send_md(
            update,
            "Opsional: ketik keterangan / detail spesifikasi (akan masuk ke Keterangan 1). "
            "Ketik '-' untuk kosong:",
        )
        return

    if field == "custom_keterangan":
        k = text if text.strip() not in ("", "-") else ""
        if k:
            state["specs"]["keterangan"] = k
        else:
            state["specs"].pop("keterangan", None)
        state["awaiting_field"] = "custom_serial"
        await send_md(
            update,
            "Opsional: masukkan Serial Number (ketik '-' jika tidak ada):",
        )
        return

    if field == "custom_serial":
        if text.strip() in ("-", ""):
            state["specs"].pop("serial", None)
        else:
            state["specs"]["serial"] = text
        state["awaiting_field"] = "qty"
        await send_md(update, "Masukkan quantity (angka):")
        return

    if field == "generic_specs":
        state["specs"]["name"] = text
        state["awaiting_field"] = "generic_serial"
        await send_md(
            update,
            "Opsional: masukkan Serial Number (ketik '-' jika tidak ada):",
        )
        return

    if field == "generic_serial":
        if text.strip() in ("-", ""):
            state["specs"].pop("serial", None)
        else:
            state["specs"]["serial"] = text
        state["awaiting_field"] = "qty"
        await send_md(update, "Masukkan quantity (angka):")
        return

    if field == "qty":
        q = safe_int(text, default=-1)
        if q <= 0:
            await send_md(update, "Masukkan quantity numeric > 0.")
            return
        state["specs"]["qty"] = q
        state["awaiting_field"] = None
        await _ask_confirm(update, context)
        return

    await send_md(
        update,
        "Input tidak dikenali pada tahap ini. Gunakan /tambah untuk memulai ulang.",
    )


# ---------------------------------------------------------------------------
# Build readable name & sheet spec formatting
# ---------------------------------------------------------------------------
def _build_item_name(category: str, specs: Dict[str, Any]) -> str:
    cat = category or "Item"
    cat_low = (cat or "").lower()

    if cat_low == "patch cord":
        connector = specs.get("connector", "")
        length = specs.get("length_m", "")
        length_str = ""
        if length not in ("", None):
            try:
                if float(length).is_integer():
                    length_str = f"{int(float(length))}m"
                else:
                    length_str = f"{length}m"
            except Exception:
                length_str = f"{length}m"
        parts = [cat, connector, length_str]
        return " ".join([p for p in parts if p]).strip()

    if cat_low == "sfp":
        rate = specs.get("rate_g", "")
        dist = specs.get("distance_km", "")
        parts = [cat]
        if rate:
            parts.append(f"{rate}G")
        if dist:
            try:
                if float(dist).is_integer():
                    parts.append(f"{int(float(dist))}KM")
                else:
                    parts.append(f"{dist}KM")
            except Exception:
                parts.append(f"{dist}KM")
        return " ".join(parts).strip()

    if cat_low == "attenuator":
        db = specs.get("db", "")
        return f"{cat} {db}dB".strip() if db != "" else cat

    if cat_low == "l2 switch":
        brand = specs.get("brand") or ""
        model = specs.get("model") or ""
        parts = [cat]
        if brand:
            parts.append(brand)
        if model:
            parts.append(model)
        return " ".join([p for p in parts if p]).strip()

    name = specs.get("name")
    if name:
        return f"{cat} {name}".strip()

    return build_name_from_spec(cat, specs.get("name") or "")


def _sheet_base_name(category: str, specs: Dict[str, Any]) -> str:
    if not category:
        return "Item"
    if category.strip().lower() == "custom":
        return specs.get("name") or "Custom"
    return category


def _format_specs_for_sheet_three(
    category: str,
    specs: Dict[str, Any],
) -> Tuple[str, str, str]:
    cat_low = (category or "").lower()

    if cat_low == "patch cord":
        connector = specs.get("connector")
        length = specs.get("length_m")
        k1 = str(connector) if connector else ""
        k2 = ""
        if length not in (None, ""):
            try:
                if float(length).is_integer():
                    k2 = f"{int(float(length))}m"
                else:
                    k2 = f"{length}m"
            except Exception:
                k2 = f"{length}m"
        return k1 or "", k2 or "", ""

    if cat_low == "sfp":
        rate = specs.get("rate_g")
        dist = specs.get("distance_km")
        serial = specs.get("serial")
        k1 = f"{rate}G" if rate else ""
        k2 = f"{dist}KM" if dist else ""
        k3 = serial or ""
        return k1 or "", k2 or "", k3 or ""

    if cat_low == "attenuator":
        db = specs.get("db")
        if db not in (None, ""):
            try:
                if float(db).is_integer():
                    return f"{int(float(db))}dB", "", ""
                return f"{db}dB", "", ""
            except Exception:
                return f"{db}dB", "", ""
        return "", "", ""

    if cat_low == "l2 switch":
        brand = specs.get("brand") or ""
        model = specs.get("model") or ""
        serial = specs.get("serial") or ""
        return brand or "", model or "", serial or ""

    # custom atau generic: keterangan/name -> k1, k2 kosong, k3 serial
    cust_ket = specs.get("keterangan") or specs.get("name") or ""
    serial = specs.get("serial") or ""
    return cust_ket or "", "", serial or ""


# ---------------------------------------------------------------------------
# Confirm summary & ask
# ---------------------------------------------------------------------------
async def _ask_confirm(target: Any, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get("add_flow", {})
    if not state:
        await send_md(target, "State tidak ditemukan. Mulai ulang dengan /tambah.")
        return

    sheets = context.application.bot_data.get("sheets_manager")
    user = _user_from(target)
    if not sheets or not user:
        await send_md(target, "❌ Layanan tidak tersedia.")
        return

    witel, divisi = await _get_user_profile_witel_divisi(sheets, user.id)
    if not witel or not divisi:
        await send_md(
            target,
            "⚠️ Profil kamu belum lengkap (Witel/Divisi kosong). "
            "Jalankan /start untuk mengatur Witel & Divisi terlebih dahulu.",
        )
        return

    cat = state.get("category", "")
    specs = state.get("specs", {}) or {}
    qty = specs.get("qty", 0)
    item_name = _build_item_name(cat, specs)

    # build spec summary (show also profile's Witel/Divisi)
    spec_lines = []
    for k in (
        "connector",
        "length_m",
        "rate_g",
        "distance_km",
        "db",
        "brand",
        "model",
        "serial",
        "name",
        "keterangan",
    ):
        if k in specs and specs[k] not in (None, ""):
            spec_lines.append(f"{k}: {specs[k]}")
    spec_text = ", ".join(spec_lines) if spec_lines else "-"

    summary = (
        f"*Ringkasan tambah barang:*\n"
        f"Nama: {escape_md(item_name)}\n"
        f"Kategori: {escape_md(cat)}\n"
        f"Spec: `{escape_md(spec_text)}`\n"
        f"Witel: *{escape_md(witel)}* | Divisi: *{escape_md(divisi)}*\n"
        f"Qty: {qty}"
    )

    kb = [
        [InlineKeyboardButton("Konfirmasi Tambah", callback_data="add_confirm")],
        [InlineKeyboardButton("Batal", callback_data="add_cancel")],
    ]
    await send_md(
        target,
        summary,
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown",
    )


# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------
def _normalize(v: Optional[str]) -> str:
    return "" if v is None else str(v).strip().lower()


async def _find_duplicate_row_and_headers(
    sheets,
    nama,
    kategori,
    witel,
    divisi,
    pemilik_nama,
    k1,
    k2,
    k3,
):
    """
    Returns (row_idx, headers) if duplicate found, else (None, headers).
    """
    headers = await retry_async(
        sheets.async_ensure_headers,
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
            "Keterangan 1",
            "Keterangan 2",
            "Serial Number",
        ],
        retries=4,
        delay=0.5,
        backoff=2.0,
        allowed_exceptions=(Exception,),
    )

    try:
        recs = await sheets.async_get_all_records(INVENTARIS_SHEET)
    except Exception:
        logger.debug("_find_duplicate: failed reading all records", exc_info=True)
        recs = []

    target = {
        "nama": _normalize(nama),
        "kategori": _normalize(kategori),
        "witel": _normalize(witel),
        "divisi": _normalize(divisi),
        "pemilik_nama": _normalize(pemilik_nama),
        "k1": _normalize(k1),
        "k2": _normalize(k2),
        "k3": _normalize(k3),
    }

    for idx, r in enumerate(recs):
        try:
            if (
                _normalize(r.get("Nama Barang")) == target["nama"]
                and _normalize(r.get("Kategori")) == target["kategori"]
                and _normalize(r.get("Witel")) == target["witel"]
                and _normalize(r.get("Divisi")) == target["divisi"]
                and _normalize(r.get("Pemilik Nama")) == target["pemilik_nama"]
                and _normalize(r.get("Keterangan 1")) == target["k1"]
                and _normalize(r.get("Keterangan 2")) == target["k2"]
                and _normalize(r.get("Serial Number")) == target["k3"]
            ):
                return idx + 2, headers
        except Exception:
            continue

    return None, headers


# ---------------------------------------------------------------------------
# Finalize add -> write to sheet
# ---------------------------------------------------------------------------
async def confirm_add_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    await cq.answer()

    from handlers.common import require_registration

    if not await require_registration(cq, context):
        return

    await _finalize_add(cq, context)


async def _finalize_add(update_or_cq: Any, context: ContextTypes.DEFAULT_TYPE):
    from handlers.common import require_registration

    # ensure user still registered
    if not await require_registration(update_or_cq, context):
        return

    user = _user_from(update_or_cq)
    if not user:
        await send_md(update_or_cq, "Tidak dapat mengenali pengguna.")
        return

    state = context.user_data.get("add_flow")
    if not state:
        await send_md(update_or_cq, "State tidak ditemukan. Mulai ulang dengan /tambah.")
        return

    sheets = context.application.bot_data.get("sheets_manager")
    if not sheets:
        await send_md(
            update_or_cq,
            "❌ Layanan Google Sheets belum tersedia — tidak dapat menyimpan data.",
        )
        clear_steps(context, user.id)
        context.user_data.pop("add_flow", None)
        return

    # Ambil Witel/Divisi dari profil
    witel, divisi = await _get_user_profile_witel_divisi(sheets, user.id)
    if not witel or not divisi:
        await send_md(
            update_or_cq,
            "⚠️ Profil kamu belum lengkap (Witel/Divisi kosong). "
            "Jalankan /start untuk mengatur Witel & Divisi terlebih dahulu.",
        )
        return

    cat = state.get("category", "")
    specs = state.get("specs", {}) or {}

    cat_low = (cat or "").strip().lower()
    if cat_low in ("l2 switch", "sfp"):
        qty = 1
        specs["qty"] = 1
    else:
        qty = int(specs.get("qty") or 0)

    if qty <= 0:
        await send_md(update_or_cq, "⚠️ Quantity tidak valid.")
        return

    nama_barang_cell = _sheet_base_name(cat, specs)
    k1, k2, k3 = _format_specs_for_sheet_three(cat, specs)
    pemilik_nama = user.full_name or user.first_name or ""

    # Duplicate?
    try:
        dup_row, headers = await _find_duplicate_row_and_headers(
            sheets,
            nama_barang_cell,
            cat,
            witel,
            divisi,
            pemilik_nama,
            k1,
            k2,
            k3,
        )
    except Exception:
        dup_row = None
        headers = None

    if dup_row and headers:
        total_col = headers.get("Total Qty")
        ters_col = headers.get("Tersedia")
        status_col = headers.get("Status")

        try:
            ok1, new_total = await retry_async(
                sheets.async_increment_cell,
                INVENTARIS_SHEET,
                dup_row,
                total_col,
                qty,
                retries=4,
                delay=0.4,
                backoff=2.0,
                allowed_exceptions=(Exception,),
            )
            ok2, new_ters = await retry_async(
                sheets.async_increment_cell,
                INVENTARIS_SHEET,
                dup_row,
                ters_col,
                qty,
                retries=4,
                delay=0.4,
                backoff=2.0,
                allowed_exceptions=(Exception,),
            )

            try:
                cur_status = (
                    await sheets.async_get_cell_value(
                        INVENTARIS_SHEET,
                        dup_row,
                        status_col,
                    )
                    if status_col
                    else ""
                )
                if str(cur_status).strip().lower() in ("removed", "", None):
                    try:
                        await sheets.async_update_cell(
                            INVENTARIS_SHEET,
                            dup_row,
                            status_col,
                            "Tersedia",
                        )
                    except Exception:
                        logger.debug(
                            "Failed to update status to Tersedia for dup row",
                            exc_info=True,
                        )
            except Exception:
                logger.debug(
                    "Failed reading/updating status for dup row",
                    exc_info=True,
                )

            if ok1 and ok2:
                try:
                    await sheets.async_write_log(
                        str(user.id),
                        "TambahKeStok",
                        nama_barang_cell,
                        f"+{qty} (merged to row {dup_row})",
                    )
                except Exception:
                    pass

                await send_md(
                    update_or_cq,
                    f"✅ Item sudah ada — stok diperbarui: +{qty} pada "
                    f"*{escape_md(nama_barang_cell)}*.",
                    parse_mode="Markdown",
                )
                context.user_data.pop("add_flow", None)
                clear_steps(context, user.id)
                return
            else:
                logger.warning(
                    "increment_cell partially failed ok1=%s ok2=%s",
                    ok1,
                    ok2,
                )
        except Exception:
            logger.exception(
                "Failed incrementing existing row; will fall back to append",
                exc_info=True,
            )

    # Append new row
    try:
        headers = headers or await _ensure_inventaris_headers(sheets)
    except Exception as e:
        logger.exception(
            "Failed to ensure headers for add operation: %s",
            e,
        )
        await send_md(
            update_or_cq,
            "❌ Gagal menyiapkan kolom di Google Sheets. Coba lagi nanti.",
        )
        return

    max_cols = max(headers.values()) if headers else 12
    row = [""] * max_cols

    def _set(col_name: str, val: Any):
        idx = headers.get(col_name)
        if idx:
            row[idx - 1] = "" if val is None else str(val)

    _set("Nama Barang", nama_barang_cell)
    _set("Kategori", cat)
    _set("Witel", witel)
    _set("Divisi", divisi)
    _set("Total Qty", qty)
    _set("Tersedia", qty)
    _set("Status", "Tersedia")
    _set("Pemilik ID", user.id)
    _set("Pemilik Nama", pemilik_nama)
    _set("Keterangan 1", k1)
    _set("Keterangan 2", k2)
    _set("Serial Number", k3)

    ok = False
    try:
        ok = await retry_async(
            sheets.async_append_row,
            INVENTARIS_SHEET,
            row,
            retries=4,
            delay=0.8,
            backoff=2.0,
            allowed_exceptions=(Exception,),
        )
    except Exception:
        logger.exception("Failed to append row to sheet")

    if ok:
        try:
            await retry_async(
                sheets.async_write_log,
                str(user.id),
                "Tambah",
                nama_barang_cell,
                f"Qty {qty} | Witel={witel} Divisi={divisi}",
                retries=3,
                delay=0.5,
                backoff=2.0,
                allowed_exceptions=(Exception,),
            )
        except Exception:
            pass

        await send_md(
            update_or_cq,
            f"✅ Berhasil menambahkan *{escape_md(nama_barang_cell)}* sebanyak "
            f"*{qty}* ke Inventaris.",
            parse_mode="Markdown",
        )
    else:
        await send_md(
            update_or_cq,
            "❌ Gagal menyimpan ke Google Sheets. Pastikan layanan tersedia dan coba lagi.",
        )

    context.user_data.pop("add_flow", None)
    clear_steps(context, user.id)


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------
async def cancel_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    await cq.answer()

    from handlers.common import require_registration

    # tetap cek registrasi walaupun sedang membatalkan
    if not await require_registration(cq, context):
        return

    user = cq.from_user
    context.user_data.pop("add_flow", None)
    clear_steps(context, user.id)
    await send_md(cq, "✖️ Aksi dibatalkan.")


# Backward-compatible alias
async def handle_add_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await text_message_handler(update, context)


# ---------------------------------------------------------------------------
# Register handlers
# ---------------------------------------------------------------------------
def register_add_handlers(application):
    application.add_handler(CommandHandler("tambah", start_add))
    application.add_handler(CallbackQueryHandler(category_cb, pattern=r"^add_cat:"))
    application.add_handler(CallbackQueryHandler(pc_connector_cb, pattern=r"^pc_connector:"))
    application.add_handler(CallbackQueryHandler(pc_length_cb, pattern=r"^pc_length:"))
    application.add_handler(CallbackQueryHandler(sfp_rate_cb, pattern=r"^sfp_rate:"))
    application.add_handler(CallbackQueryHandler(sfp_dist_cb, pattern=r"^sfp_dist:"))
    application.add_handler(CallbackQueryHandler(atten_db_cb, pattern=r"^atten_db:"))
    application.add_handler(CallbackQueryHandler(l2_brand_cb, pattern=r"^l2_brand:"))
    application.add_handler(CallbackQueryHandler(l2_model_cb, pattern=r"^l2_model:"))
    application.add_handler(CallbackQueryHandler(qty_cb, pattern=r"^add_qty:"))
    application.add_handler(CallbackQueryHandler(confirm_add_cb, pattern=r"^add_confirm$"))
    application.add_handler(CallbackQueryHandler(cancel_cb, pattern=r"^add_cancel$"))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, text_message_handler)
    )


__all__ = ["register_add_handlers", "handle_add_message"]
