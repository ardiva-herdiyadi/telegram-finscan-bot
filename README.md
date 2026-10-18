# 💸 FinScan AI — Asisten Pencatat Keuangan Pintar (Telegram & Google Sheets)

FinScan AI adalah bot Telegram pintar yang memanfaatkan **Google Gemini Multimodal AI** untuk mendeteksi struk belanjaan dan mencatat pengeluaran keuangan secara otomatis langsung ke **Google Sheets** pribadi Anda.

---

## ✨ Fitur Unggulan

1. **📸 Scan Foto Struk Belanjaan (OCR + Vision AI)**
   - Cukup foto struk belanjaan kasir (Alfamart, Indomaret, kafe, SPBU, restoran).
   - AI secara cerdas mengekstrak nama toko/merchant, tanggal & jam, daftar rincian barang, total bayar riil, serta mengelompokkan ke kategori yang tepat.
2. **💬 Input Teks Bahasa Santai & Gaul (Natural Language Processing)**
   - Catat pengeluaran secepat mengetik chat:
     - `Makan siang mie ayam bakso 25rb`
     - `Beli bensin pertalite 50k`
     - `Kopi susu 22 ribu lewat qris`
3. **📊 Terhubung Live ke Google Sheets**
   - Setiap transaksi yang dikonfirmasi langsung tertulis sebagai baris baru di Google Sheets secara *real-time*.
4. **🛡️ Kartu Konfirmasi Interaktif**
   - Menampilkan tinjauan data sebelum disimpan: `[ ✅ Simpan ke Sheets ]` dan `[ ❌ Batalkan ]` agar spreadsheet selalu akurat.
5. **📈 Laporan Pengeluaran & Grafik Visual (`/rekap`)**
   - Agregasi pengeluaran bulanan dilengkapi grafik lingkaran modern (*Donut Chart PNG*) untuk melihat persentase per kategori.
6. **🗑️ Pembatalan Cepat (`/batal`)**
   - Salah catat? Cukup ketik `/batal` untuk menghapus transaksi terakhir.

---

## 🛠️ Tech Stack

* **Bahasa:** Python 3.12+
* **Framework Bot:** `python-telegram-bot (v22+)`
* **AI Engine:** Google Gemini Flash (`google-genai` SDK)
* **Spreadsheet Integration:** Google Apps Script Web App Webhook
* **Database Lokal & Analitik:** SQLite3
* **Visualisasi Data:** `matplotlib`
* **Deployment:** Render.com (24/7 Cloud Web Service)

---

## 👨‍💻 Creator & Portofolio
* **Creator:** Ardiva Herdiyadi
* **Bot Username:** `@finscan_ardiva_bot`
