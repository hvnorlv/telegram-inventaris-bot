# config.py
"""
Konfigurasi utama untuk Bot Inventaris Telegram.

Keamanan:
- Jangan menyimpan kredensial atau token di repo.
- Set environment variables ini pada runtime / secret manager:
  - SPREADSHEET_ID
  - GOOGLE_CREDENTIALS_PATH
  - TELEGRAM_BOT_TOKEN

Fungsi util tersedia untuk load env dengan fallback; namun beberapa variabel diwajibkan.
"""

import os
from typing import List, Optional


# -------------------------
# Helper: baca environment dengan default
# -------------------------
def load_env_or_default(var_name: str, default: Optional[str] = None) -> Optional[str]:
    """Ambil nilai environment variable dengan fallback default.

    Mengembalikan None jika tidak ada dan default None juga.
    """
    value = os.environ.get(var_name)
    if value is not None:
        v = value.strip()
        if v != "":
            return v
    return default


# -------------------------
# Wajib di-set environment (keamanan)
# -------------------------
SPREADSHEET_ID = load_env_or_default("SPREADSHEET_ID", "1Dll8wQ5NvyB-5NW7n2i4vFBN_byHwSvdejfHTgo14HE")
GOOGLE_CREDENTIALS_PATH = load_env_or_default("GOOGLE_CREDENTIALS_PATH", "credentials.json")
BOT_TOKEN = load_env_or_default("TELEGRAM_BOT_TOKEN", "7753593716:AAEdstFEtxE7Xmd9oli-9UDfxV72ZdzKNf0")

_missing = []
if not SPREADSHEET_ID:
    _missing.append("SPREADSHEET_ID")
if not GOOGLE_CREDENTIALS_PATH:
    _missing.append("GOOGLE_CREDENTIALS_PATH")
if not BOT_TOKEN:
    _missing.append("TELEGRAM_BOT_TOKEN")

if _missing:
    # Fail early with pesan jelas agar admin tahu apa yang kurang.
    raise EnvironmentError(
        "Environment variables required but missing: "
        + ", ".join(_missing)
        + ".\nSet them in your environment or secret manager before starting the bot."
    )


# -------------------------
# Nama sheet sesuai struktur aplikasi
# -------------------------
USERS_SHEET = "Users"
INVENTARIS_SHEET = "Inventaris"
PEMINJAMAN_SHEET = "Peminjaman"
LOG_SHEET = "Log"
WITEL_SHEET = "Witel"


# -------------------------
# Aplikasi - batasan, preset, dll
# -------------------------
# Daftar kategori default untuk barang (ubah di sini jika perlu)
ITEM_CATEGORIES: List[str] = [
    "Patch Cord",
    "SFP",
    "Attenuator",
    "L2 Switch",
    "Custom",
]

# Alias agar seragam di modul inventory/handlers
PRESET_CATEGORIES = ITEM_CATEGORIES.copy()

# Pilihan quantity cepat (untuk tombol /tambah & /pinjam)
QUANTITY_OPTIONS = [1, 2, 3, 5, 10, 20, 50]

# Jumlah item per halaman saat menampilkan inventaris
ITEMS_PER_PAGE = int(load_env_or_default("ITEMS_PER_PAGE", "8"))

# Batas maksimum hari peminjaman (0 = tanpa batas)
MAX_BORROW_DAYS = int(load_env_or_default("MAX_BORROW_DAYS", "30"))

# -------------------------
# Logging & mode debug
# -------------------------
LOG_LEVEL = load_env_or_default("LOG_LEVEL", "INFO")


# -------------------------
# Helper opsional untuk melihat konfigurasi aktif
# -------------------------
def print_config_summary():
    """Cetak ringkasan konfigurasi aktif ke console (aman — tidak menampilkan token penuh)."""
    print("=== Konfigurasi Bot Inventaris ===")
    print(f"SPREADSHEET_ID       : {SPREADSHEET_ID[:8]}... (hidden)")
    print(f"GOOGLE_CREDENTIALS   : {GOOGLE_CREDENTIALS_PATH}")
    print(f"TELEGRAM_BOT_TOKEN   : {'set' if BOT_TOKEN else 'NOT SET'}")
    print(f"LOG_LEVEL            : {LOG_LEVEL}")
    print(f"ITEMS_PER_PAGE       : {ITEMS_PER_PAGE}")
    print(f"MAX_BORROW_DAYS      : {MAX_BORROW_DAYS}")
    print(f"ITEM_CATEGORIES      : {ITEM_CATEGORIES}")
    print("==================================")


if __name__ == "__main__":
    print_config_summary()
