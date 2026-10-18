import os
import sys
import io
import re
import csv
import json
import sqlite3
import logging
import asyncio
from datetime import datetime
from dotenv import load_dotenv
from PIL import Image
import requests
import matplotlib
matplotlib.use("Agg")  # Non-interactive backend for server
import matplotlib.pyplot as plt

# Reconfigure stdout/stderr for Unicode/Windows terminal
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
    constants
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes
)
from google import genai
from google.genai import types

# Load Environment Variables
load_dotenv()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
SHEETS_WEBHOOK_URL = os.getenv("GOOGLE_SHEETS_WEBHOOK_URL", "").strip()
SHEETS_VIEW_URL = os.getenv("GOOGLE_SHEET_URL", "https://docs.google.com/spreadsheets/d/1aulSNCWojSKzlp1L3LCmnMPzdsw1OXrUtTtI5Sz30_o/edit?usp=sharing")

if not BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN belum diset di file .env!")
if not GEMINI_KEY:
    raise ValueError("GEMINI_API_KEY belum diset di file .env!")

# Configure Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Initialize Gemini Client
gemini_client = genai.Client(api_key=GEMINI_KEY)
GEMINI_MODEL = "gemini-flash-latest"

# Lightweight Health-Check Server for Cloud Hosting (Render 24/7)
port_env = os.getenv("PORT")
if port_env:
    import threading
    from http.server import HTTPServer, BaseHTTPRequestHandler

    class HealthCheckHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-type", "text/plain")
            self.end_headers()
            self.wfile.write(b"FinScan AI Bot is running smoothly.")

        def log_message(self, format, *args):
            pass

    def start_health_check_server():
        port = int(port_env)
        server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
        logger.info(f"Health-check server running on port {port}")
        server.serve_forever()

    threading.Thread(target=start_health_check_server, daemon=True).start()

# Database Setup (Local SQLite for Fast Analytics & Safety)
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "expenses.db")

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            date TEXT,
            time TEXT,
            category TEXT,
            merchant TEXT,
            description TEXT,
            amount REAL,
            source TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS budgets (
            user_id INTEGER PRIMARY KEY,
            monthly_limit REAL DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()

init_db()

def save_transaction_to_db(user_id, date, time, category, merchant, description, amount, source):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO transactions (user_id, date, time, category, merchant, description, amount, source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (user_id, date, time, category, merchant, description, amount, source))
    tx_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return tx_id

MONTH_NAMES_ID = {
    "01": "Januari",
    "02": "Februari",
    "03": "Maret",
    "04": "April",
    "05": "Mei",
    "06": "Juni",
    "07": "Juli",
    "08": "Agustus",
    "09": "September",
    "10": "Oktober",
    "11": "November",
    "12": "Desember"
}

def format_month_label(month_prefix: str) -> str:
    """Mengubah format YYYY-MM menjadi 'Bulan Tahun' dalam Bahasa Indonesia."""
    try:
        y, m = month_prefix.split("-")
        return f"{MONTH_NAMES_ID.get(m, m)} {y}"
    except Exception:
        return month_prefix

def get_spreadsheet_id(url: str) -> str:
    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", url)
    return match.group(1) if match else "1aulSNCWojSKzlp1L3LCmnMPzdsw1OXrUtTtI5Sz30_o"

def fetch_google_sheets_data():
    """
    Mengambil data riil secara live dari Google Sheets (CSV Export endpoint).
    Menjamin 100% sinkronisasi antara Telegram dan spreadsheet pengguna.
    """
    ss_id = get_spreadsheet_id(SHEETS_VIEW_URL)
    csv_url = f"https://docs.google.com/spreadsheets/d/{ss_id}/gviz/tq?tqx=out:csv"
    try:
        res = requests.get(csv_url, timeout=8)
        if res.status_code == 200:
            reader = csv.reader(io.StringIO(res.text))
            header = next(reader, None)
            rows = []
            for r in reader:
                if len(r) >= 6:
                    tgl = r[0].strip()
                    wkt = r[1].strip()
                    kat = r[2].strip()
                    mer = r[3].strip()
                    dsc = r[4].strip()
                    amt_raw = r[5].replace("Rp", "").replace(".", "").replace(",", "").strip()
                    try:
                        amt = float(amt_raw)
                    except ValueError:
                        continue
                    rows.append({
                        "date": tgl,
                        "time": wkt,
                        "category": kat,
                        "merchant": mer,
                        "description": dsc,
                        "amount": amt,
                        "source": r[6].strip() if len(r) > 6 else "Google Sheets"
                    })
            if rows:
                return rows
    except Exception as e:
        logger.error(f"Error fetching Google Sheets live CSV: {e}")
    return None

def get_monthly_data(month_prefix: str):
    """
    Mengembalikan (total, breakdown, available_months) untuk prefix bulan (YYYY-MM).
    Memprioritaskan data Live dari Google Sheets dan otomatis meng-cache ke SQLite.
    """
    sheets_rows = fetch_google_sheets_data()
    if sheets_rows is not None:
        # Cache ke SQLite lokal untuk redundansi
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("DELETE FROM transactions")
            for r in sheets_rows:
                c.execute("""
                    INSERT INTO transactions (user_id, date, time, category, merchant, description, amount, source)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (0, r["date"], r["time"], r["category"], r["merchant"], r["description"], r["amount"], r["source"]))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.warning(f"Cache to SQLite warning: {e}")

        # Hitung langsung dari baris Google Sheets
        cat_sums = {}
        total = 0.0
        all_months = set()
        for r in sheets_rows:
            d = r.get("date", "")
            if len(d) >= 7:
                all_months.add(d[:7])
            if d.startswith(month_prefix):
                total += r["amount"]
                cat_sums[r["category"]] = cat_sums.get(r["category"], 0.0) + r["amount"]

        breakdown = sorted(cat_sums.items(), key=lambda x: x[1], reverse=True)
        available_months = sorted(list(all_months), reverse=True)
        return total, breakdown, available_months

    # Fallback ke SQLite jika koneksi Google Sheets bermasalah
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT COALESCE(SUM(amount), 0) FROM transactions
        WHERE date LIKE ?
    """, (f"{month_prefix}%",))
    total = cursor.fetchone()[0]

    cursor.execute("""
        SELECT category, SUM(amount) FROM transactions
        WHERE date LIKE ?
        GROUP BY category
        ORDER BY SUM(amount) DESC
    """, (f"{month_prefix}%",))
    breakdown = cursor.fetchall()

    cursor.execute("""
        SELECT DISTINCT substr(date, 1, 7) FROM transactions
        ORDER BY substr(date, 1, 7) DESC
    """)
    available_months = [row[0] for row in cursor.fetchall() if row[0]]
    conn.close()
    return total, breakdown, available_months

def get_monthly_total(user_id=None, month_prefix=None):
    if not month_prefix:
        month_prefix = datetime.now().strftime("%Y-%m")
    total, _, _ = get_monthly_data(month_prefix)
    return total

def get_monthly_category_breakdown(user_id=None, month_prefix=None):
    if not month_prefix:
        month_prefix = datetime.now().strftime("%Y-%m")
    _, breakdown, _ = get_monthly_data(month_prefix)
    return breakdown

def delete_last_transaction(user_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, merchant, amount, category FROM transactions
        WHERE user_id = ?
        ORDER BY id DESC LIMIT 1
    """, (user_id,))
    row = cursor.fetchone()
    if row:
        cursor.execute("DELETE FROM transactions WHERE id = ?", (row[0],))
        conn.commit()
    conn.close()
    return row

def sync_to_google_sheets(payload):
    """Sends recorded transaction to Google Sheets via Apps Script Webhook."""
    webhook_url = os.getenv("GOOGLE_SHEETS_WEBHOOK_URL", "").strip()
    if not webhook_url:
        logger.warning("GOOGLE_SHEETS_WEBHOOK_URL belum diset. Data hanya disimpan di SQLite.")
        return False, "URL Webhook Google Sheets belum diisi di .env"
    try:
        res = requests.post(webhook_url, json=payload, allow_redirects=True, timeout=12)
        if res.status_code == 200:
            return True, "Synced"
        return False, f"HTTP {res.status_code}"
    except Exception as e:
        logger.error(f"Failed to sync with Google Sheets: {e}")
        return False, str(e)


# AI Parsing Engine (Gemini 1.5/Flash)
CATEGORIES_LIST = [
    "Makanan & Minuman",
    "Transportasi",
    "Kebutuhan Harian",
    "Belanja & Pakaian",
    "Tagihan & Pulsa",
    "Hiburan & Lifestyle",
    "Kesehatan & Obat",
    "Pendidikan & Pekerjaan",
    "Lainnya"
]

AI_SYSTEM_INSTRUCTION = f"""
Kamu adalah asisten keuangan pintar bernama FinScan AI.
Tugasmu adalah menganalisis data pengeluaran (baik dari teks obrolan pengguna atau foto struk belanjaan) dan mengekstraknya ke format JSON murni.

Kategori yang diizinkan (pilih salah satu yang paling cocok):
{json.dumps(CATEGORIES_LIST, ensure_ascii=False)}

Format JSON yang WAJIB kamu hasilkan:
{{
  "is_expense": true,
  "merchant": "Nama Toko / Resto / Tempat (misal: Alfamart, SPBU Pertamina, Warteg Barokah, Tokopedia). Jika tidak tahu, beri nama umum.",
  "date": "YYYY-MM-DD (Gunakan tanggal saat ini jika tidak tertera di teks/struk)",
  "time": "HH:MM (Gunakan waktu saat ini jika tidak ada)",
  "category": "Pilih persis salah satu dari kategori di atas",
  "description": "Rincian singkat item yang dibeli / keterangan transaksi",
  "amount": 25000 (Angka bulat integer tanpa titik/koma/Rp)
}}

Aturan Penting:
1. Hanya balas dengan objek JSON valid. Jangan tambahkan tanda kutip markdown ```json atau teks pengantar lainnya.
2. Jika input sama sekali bukan pengeluaran uang (misal salam, sapaan, atau curhat biasa), isi "is_expense": false.
3. Konversi format kata gaul Indonesia: 'rb', 'ribu', 'k' = ribuan (contoh: 25rb = 25000, 150k = 150000, 1.5jt = 1500000).
"""

async def parse_text_expense(user_text: str):
    today = datetime.now().strftime("%Y-%m-%d")
    current_time = datetime.now().strftime("%H:%M")
    prompt = f"Tanggal hari ini: {today}, Jam sekarang: {current_time}.\nInput pengguna: '{user_text}'"

    try:
        response = gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=[prompt],
            config=types.GenerateContentConfig(
                system_instruction=AI_SYSTEM_INSTRUCTION,
                temperature=0.1
            )
        )
        text = response.text.strip()
        if text.startswith("```"):
            text = text.replace("```json", "").replace("```", "").strip()
        data = json.loads(text)
        return data
    except Exception as e:
        logger.error(f"Error parsing text with Gemini: {e}")
        return None

async def parse_receipt_image(image_bytes: bytes):
    today = datetime.now().strftime("%Y-%m-%d")
    current_time = datetime.now().strftime("%H:%M")
    prompt = f"Analisis foto struk kasir/belanja ini secara cermat. Tanggal hari ini: {today}, Jam: {current_time}. Ekstrak merchant, tanggal, total bayar riil, rincian barang, dan kategori."

    try:
        image = Image.open(io.BytesIO(image_bytes))
        response = gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=[image, prompt],
            config=types.GenerateContentConfig(
                system_instruction=AI_SYSTEM_INSTRUCTION,
                temperature=0.1
            )
        )
        text = response.text.strip()
        if text.startswith("```"):
            text = text.replace("```json", "").replace("```", "").strip()
        data = json.loads(text)
        return data
    except Exception as e:
        logger.error(f"Error parsing receipt image with Gemini: {e}")
        return None


# Helper: Format Rupiah
def format_rupiah(number: float) -> str:
    return f"Rp {int(number):,}".replace(",", ".")


# Matplotlib Donut Chart Generator for /rekap
def generate_spending_chart(category_rows, month_name):
    if not category_rows:
        return None

    labels = [r[0] for r in category_rows]
    amounts = [r[1] for r in category_rows]

    fig, ax = plt.subplots(figsize=(7, 7), facecolor="#0F172A")
    ax.set_facecolor("#0F172A")

    colors = ["#38BDF8", "#F472B6", "#FBBF24", "#34D399", "#A78BFA", "#FB7185", "#94A3B8"]
    wedges, texts, autotexts = ax.pie(
        amounts,
        labels=labels,
        autopct="%1.1f%%",
        startangle=140,
        colors=colors[:len(labels)],
        wedgeprops=dict(width=0.4, edgecolor="#0F172A", linewidth=2),
        pctdistance=0.75
    )

    for text in texts:
        text.set_color("#FFFFFF")
        text.set_fontsize(11)
        text.set_fontweight("bold")
    for autotext in autotexts:
        autotext.set_color("#0F172A")
        autotext.set_fontsize(10)
        autotext.set_fontweight("bold")

    total_amount = sum(amounts)
    center_text = f"Total:\n{format_rupiah(total_amount)}"
    ax.text(0, 0, center_text, ha="center", va="center", color="#F8FAFC", fontsize=13, fontweight="bold")
    ax.set_title(f"Distribusi Pengeluaran ({month_name})", color="#F8FAFC", fontsize=15, pad=20, fontweight="bold")

    buf = io.BytesIO()
    plt.tight_layout()
    plt.savefig(buf, format="png", dpi=150, facecolor=fig.get_facecolor(), edgecolor="none")
    buf.seek(0)
    plt.close(fig)
    return buf.getvalue()


# Telegram Bot Command Handlers
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    name = user.first_name if user else "Bestie"

    welcome_text = (
        f"✨ *Halo {name}! Welcome to FinScan AI!* 💸\n\n"
        "Asisten pencatat keuangan pribadi pintarmu yang terhubung langsung ke **Google Sheets**.\n\n"
        "⚡ *Cara Pakai Sangat Gampang:*\n"
        "1. 📸 *Kirim Foto Struk Belanjaan* (Alfamart, kafe, SPBU, resto) $\\rightarrow$ AI otomatis membaca item & totalnya!\n"
        "2. 💬 *Ketik Teks Santai* $\\rightarrow$ contoh:\n"
        "   • `Makan siang bakso 25rb`\n"
        "   • `Beli bensin pertalite 50k`\n"
        "   • `Kopi kenangan 22.000 lewat qris`\n\n"
        "📊 *Menu Perintah Cepat:*\n"
        "• `/rekap` - Lihat ringkasan bulanan & grafik visual\n"
        "• `/sheets` - Buka file Google Sheets kamu secara live\n"
        "• `/batal` - Hapus transaksi terakhir jika salah catat\n\n"
        "💡 _FinScan AI diciptakan oleh Ardiva Herdiyadi_\n\n"
        "Yuk coba sekarang! Ketik pengeluaran terakhirmu atau kirim foto struk di bawah ini:"
    )

    keyboard = [
        [
            InlineKeyboardButton("📊 Rekap Bulan Ini", callback_data="btn_rekap"),
            InlineKeyboardButton("🔗 Buka Google Sheets", url=SHEETS_VIEW_URL)
        ]
    ]

    await update.message.reply_text(
        text=welcome_text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=constants.ParseMode.MARKDOWN
    )

async def rekap_command(update: Update, context: ContextTypes.DEFAULT_TYPE, target_month: str = None):
    user_id = update.effective_user.id if update.effective_user else 0
    current_month = datetime.now().strftime("%Y-%m")
    month_prefix = target_month if target_month else current_month
    month_display = format_month_label(month_prefix)

    total, breakdown, available_months = get_monthly_data(month_prefix)

    # Tombol navigasi bulan interaktif
    keyboard = []
    month_nav_row = []
    for m in ["2026-08", "2026-09", "2026-10"]:
        if m in available_months or m == month_prefix:
            label = format_month_label(m).split()[0]
            if m == month_prefix:
                month_nav_row.append(InlineKeyboardButton(f"📍 {label}", callback_data=f"rekap_m_{m}"))
            else:
                month_nav_row.append(InlineKeyboardButton(f"📅 {label}", callback_data=f"rekap_m_{m}"))

    if month_nav_row:
        keyboard.append(month_nav_row)

    keyboard.append([
        InlineKeyboardButton("🔄 Sinkron Ulang Sheets", callback_data=f"rekap_refresh_{month_prefix}"),
        InlineKeyboardButton("🔗 Buka Sheets Live", url=SHEETS_VIEW_URL)
    ])

    if total == 0:
        msg = (
            f"📊 *REKAP PENGELUARAN — {month_display.upper()}*\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"Belum ada catatan pengeluaran di Google Sheets untuk bulan {month_display}.\n\n"
            f"💡 _FinScan AI by Ardiva Herdiyadi_"
        )
        if update.callback_query:
            await update.callback_query.answer()
            chat_id = update.callback_query.message.chat_id
        else:
            chat_id = update.message.chat_id

        await context.bot.send_message(
            chat_id=chat_id,
            text=msg,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=constants.ParseMode.MARKDOWN
        )
        return

    text_lines = [
        f"📊 *REKAP PENGELUARAN — {month_display.upper()}*",
        "━━━━━━━━━━━━━━━━━━━━━",
        f"💰 *Total Terpakai:* `{format_rupiah(total)}`",
        "",
        "📂 *Rincian Per Kategori:*"
    ]

    for cat, amt in breakdown:
        pct = (amt / total) * 100
        text_lines.append(f"• *{cat}:* {format_rupiah(amt)} `({pct:.1f}%)`")

    text_lines.append("\n💡 _FinScan AI • Created by Ardiva Herdiyadi_")
    summary_text = "\n".join(text_lines)

    # Generate Chart
    chart_bytes = generate_spending_chart(breakdown, month_display)

    if update.callback_query:
        await update.callback_query.answer()
        chat_id = update.callback_query.message.chat_id
    else:
        chat_id = update.message.chat_id

    if chart_bytes:
        await context.bot.send_photo(
            chat_id=chat_id,
            photo=chart_bytes,
            caption=summary_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=constants.ParseMode.MARKDOWN
        )
    else:
        await context.bot.send_message(
            chat_id=chat_id,
            text=summary_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=constants.ParseMode.MARKDOWN
        )

async def sheets_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📂 Klik untuk Membuka Google Sheets", url=SHEETS_VIEW_URL)]
    ]
    await update.message.reply_text(
        "📊 *File Google Sheets Kamu:*\nKlik tombol di bawah ini untuk melihat dan mengedit tabel secara langsung di browser atau ponselmu:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=constants.ParseMode.MARKDOWN
    )

async def batal_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    deleted = delete_last_transaction(user_id)
    if deleted:
        tx_id, merchant, amount, category = deleted
        await update.message.reply_text(
            f"🗑️ *Transaksi Terakhir Dibatalkan!*\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"• Tempat: *{merchant}*\n"
            f"• Kategori: *{category}*\n"
            f"• Nominal: *{format_rupiah(amount)}*\n\n"
            f"_Data transaksi di atas telah dihapus dari database lokal._",
            parse_mode=constants.ParseMode.MARKDOWN
        )
    else:
        await update.message.reply_text("Tidak ada transaksi pengeluaran yang ditemukan untuk dibatalkan.")


# Message Handlers (Text & Photo)
async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text.strip()
    if user_text.startswith("/"):
        return

    chat_id = update.message.chat_id
    await context.bot.send_chat_action(chat_id=chat_id, action=constants.ChatAction.TYPING)

    parsed = await parse_text_expense(user_text)

    if not parsed or not parsed.get("is_expense"):
        await update.message.reply_text(
            "🤔 Hmm, pesanmu sepertinya bukan catatan pengeluaran.\n\n"
            "Coba ketik seperti ini:\n"
            "• `Makan siang mie ayam 18rb`\n"
            "• `Beli bensin 50rb`\n"
            "Atau langsung kirimkan foto struk belanjaanmu!",
            parse_mode=constants.ParseMode.MARKDOWN
        )
        return

    # Store in pending transaction context
    context.user_data["pending_tx"] = parsed
    context.user_data["pending_source"] = "Teks Chat"

    amount_str = format_rupiah(parsed.get("amount", 0))
    confirm_text = (
        f"📝 *Konfirmasi Pengeluaran:*\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🏪 *Tempat/Toko:* {parsed.get('merchant', '-')}\n"
        f"📁 *Kategori:* {parsed.get('category', 'Lainnya')}\n"
        f"📝 *Rincian:* {parsed.get('description', '-')}\n"
        f"📅 *Tanggal:* {parsed.get('date')} ({parsed.get('time')})\n"
        f"💰 *Nominal:* *{amount_str}*\n\n"
        f"Simpan catatan ini ke Google Sheets?"
    )

    keyboard = [
        [
            InlineKeyboardButton("✅ Simpan ke Sheets", callback_data="tx_confirm"),
            InlineKeyboardButton("❌ Batalkan", callback_data="tx_cancel")
        ]
    ]

    await update.message.reply_text(
        text=confirm_text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=constants.ParseMode.MARKDOWN
    )

async def handle_photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    status_msg = await update.message.reply_text("🔍 *AI sedang menganalisis foto struk...*", parse_mode=constants.ParseMode.MARKDOWN)
    await context.bot.send_chat_action(chat_id=chat_id, action=constants.ChatAction.TYPING)

    # Download largest photo resolution
    photo_file = await update.message.photo[-1].get_file()
    photo_bytes = await photo_file.download_as_bytearray()

    parsed = await parse_receipt_image(bytes(photo_bytes))
    await status_msg.delete()

    if not parsed or not parsed.get("is_expense") or parsed.get("amount", 0) <= 0:
        await update.message.reply_text(
            "⚠️ Maaf, AI kesulitan membaca rincian dari struk ini. Pastikan foto cukup terang, fokus, dan total harga terlihat jelas ya!",
            parse_mode=constants.ParseMode.MARKDOWN
        )
        return

    context.user_data["pending_tx"] = parsed
    context.user_data["pending_source"] = "Foto Struk"

    amount_str = format_rupiah(parsed.get("amount", 0))
    confirm_text = (
        f"🧾 *Hasil Scan Struk Belanja:* ✨\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🏪 *Toko:* {parsed.get('merchant', '-')}\n"
        f"📁 *Kategori:* {parsed.get('category', 'Lainnya')}\n"
        f"🛒 *Rincian Barang:*\n_{parsed.get('description', '-')}_\n\n"
        f"📅 *Tanggal:* {parsed.get('date')} ({parsed.get('time')})\n"
        f"💰 *Total Bayar:* *{amount_str}*\n\n"
        f"Apakah data di atas sudah sesuai?"
    )

    keyboard = [
        [
            InlineKeyboardButton("✅ Simpan ke Sheets", callback_data="tx_confirm"),
            InlineKeyboardButton("❌ Batalkan", callback_data="tx_cancel")
        ]
    ]

    await update.message.reply_text(
        text=confirm_text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=constants.ParseMode.MARKDOWN
    )


async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user_id = query.from_user.id

    if data == "btn_rekap":
        await rekap_command(update, context)
        return

    if data.startswith("rekap_m_"):
        target_m = data.replace("rekap_m_", "")
        await rekap_command(update, context, target_month=target_m)
        return

    if data.startswith("rekap_refresh_"):
        target_m = data.replace("rekap_refresh_", "")
        await rekap_command(update, context, target_month=target_m)
        return

    if data == "tx_cancel":
        await query.answer()
        context.user_data.pop("pending_tx", None)
        await query.edit_message_text("❌ *Transaksi dibatalkan.*", parse_mode=constants.ParseMode.MARKDOWN)
        return

    if data == "tx_confirm":
        await query.answer()
        tx = context.user_data.get("pending_tx")
        source = context.user_data.get("pending_source", "Chat")

        if not tx:
            await query.edit_message_text("⚠️ Data transaksi sudah kedaluwarsa. Silakan ulangi input.", parse_mode=constants.ParseMode.MARKDOWN)
            return

        # 1. Save to SQLite
        save_transaction_to_db(
            user_id=user_id,
            date=tx.get("date"),
            time=tx.get("time"),
            category=tx.get("category"),
            merchant=tx.get("merchant"),
            description=tx.get("description"),
            amount=tx.get("amount"),
            source=source
        )

        # 2. Sync to Google Sheets
        sheets_payload = {
            "date": tx.get("date"),
            "time": tx.get("time"),
            "category": tx.get("category"),
            "merchant": tx.get("merchant"),
            "description": tx.get("description"),
            "amount": tx.get("amount"),
            "source": source
        }
        synced, sync_info = sync_to_google_sheets(sheets_payload)

        # 3. Monthly Total
        current_month = datetime.now().strftime("%Y-%m")
        total_month = get_monthly_total(user_id, current_month)

        sync_status_text = "✅ *Tersimpan & Terhubung ke Google Sheets!*" if synced else "✅ *Tersimpan di Database Lokal!* _(Sinkronisasi Sheets opsional)_"

        success_text = (
            f"{sync_status_text}\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"🏪 *Tempat:* {tx.get('merchant')}\n"
            f"📁 *Kategori:* {tx.get('category')}\n"
            f"💰 *Nominal:* *{format_rupiah(tx.get('amount'))}*\n\n"
            f"📊 *Total Pengeluaran Bulan Ini:* `{format_rupiah(total_month)}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"💡 _FinScan AI by Ardiva Herdiyadi_"
        )

        keyboard = [
            [
                InlineKeyboardButton("📊 Lihat Rekap", callback_data="btn_rekap"),
                InlineKeyboardButton("📂 Buka Sheets", url=SHEETS_VIEW_URL)
            ]
        ]

        context.user_data.pop("pending_tx", None)
        await query.edit_message_text(
            text=success_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=constants.ParseMode.MARKDOWN
        )


async def post_init(application):
    """Register commands in Telegram's Menu button."""
    await application.bot.set_my_commands([
        BotCommand("start", "⚡ Mulai & Petunjuk FinScan"),
        BotCommand("rekap", "📊 Rekap Pengeluaran & Grafik"),
        BotCommand("sheets", "📂 Buka Google Sheets Live"),
        BotCommand("batal", "🗑️ Batalkan Transaksi Terakhir")
    ])


def main():
    print("==========================================")
    print("[*] Menginisialisasi FinScan AI Bot...")
    print(f"[*] AI Engine: Google Gemini ({GEMINI_MODEL})")
    print(f"[*] Database: SQLite ({DB_PATH})")
    print("==========================================")

    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # Handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("rekap", rekap_command))
    application.add_handler(CommandHandler("sheets", sheets_command))
    application.add_handler(CommandHandler("batal", batal_command))
    application.add_handler(CallbackQueryHandler(callback_router))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo_message))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))

    print("[+] Bot FinScan AI aktif dan mendengarkan pesan...")
    application.run_polling()


if __name__ == "__main__":
    main()
