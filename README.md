# Paperless Portal

Satu halaman web untuk mengelola beberapa Paperless-ngx sekaligus:

- **Dasbor**: status semua instance (online/offline, versi, jumlah dokumen, inbox, task gagal, waktu respons).
- **Cari**: pencarian full-text ke semua Paperless sekaligus, lengkap dengan thumbnail dan cuplikan teks.
- **Migrasi**: salin dokumen antar Paperless dengan filter storage path, tag, atau query. Yang ikut tersalin: file original, judul, tanggal, ASN, tag, correspondent, document type, storage path, custom field, teks OCR, dan notes. Ada mode dry-run dan progres langsung.
- **Hapus dari sumber**: lihat pratinjau dulu, lalu hapus hanya dokumen yang file identiknya (checksum sama) **terverifikasi ada di tujuan**.
- **Riwayat job**: log per dokumen (OK / DUPLIKAT / GAGAL / ...) yang bisa difilter dan diunduh sebagai CSV.
- **Cek file**: cari berdasarkan nama file asli di semua instance, termasuk di Trash.
- **Task gagal**: daftar task consume yang error. Bisa langsung dicek file-nya atau ditandai sudah dibaca.

Token API Paperless disimpan **terenkripsi** di server portal dan tidak pernah dikirim ke browser.

## Pasang (Docker)

Ambil kode ke server, misalnya ke `/opt/paperless-portal`. Repo ini private, jadi `git clone https://github.com/alsyarwani/paperless-portal.git` akan meminta login: pakai username GitHub dan *personal access token* (bukan password). Bisa juga salin folder dengan `scp -r`. Lalu jalankan:

```bash
sudo bash -c '
set -e
cd /opt/paperless-portal
[ -f .env ] || cp .env.example .env
grep -q "^SECRET_KEY=.\+" .env || sed -i "s/^SECRET_KEY=.*/SECRET_KEY=$(openssl rand -hex 32)/" .env
mkdir -p data && chown 1000:1000 data
grep -E "^(PORTAL_USER|PORTAL_BIND|PORTAL_PORT|COOKIE_SECURE)=" .env
echo ">> Edit .env: isi PORTAL_PASSWORD (dan PORTAL_BIND bila perlu)"
'
```

Setelah `.env` diisi, pakai image siap pakai dari GHCR. Karena repo private, image-nya juga private, jadi server perlu login sekali dulu dengan personal access token yang punya izin `read:packages`:

```bash
echo "<TOKEN>" | sudo docker login ghcr.io -u alsyarwani --password-stdin
cd /opt/paperless-portal && sudo docker compose pull && sudo docker compose up -d && sudo docker compose logs --tail 20
```

Image dibangun otomatis oleh GitHub Actions setiap ada push ke `main` (tag `latest` dan `sha-xxxxxxx`). Untuk memperbarui portal di server, cukup jalankan `docker compose pull && docker compose up -d`. Kalau mau build sendiri di server tanpa GHCR, jalankan `docker compose up -d --build`.

Lewat **Portainer**: Stacks → Add stack → *Repository* (URL repo di atas) atau *Upload* folder ini. Isi environment variable seperti di `.env.example`.

### Akses dan reverse proxy

- Secara default port **8650** hanya terbuka di `127.0.0.1`. Untuk membukanya lewat tailnet, set `PORTAL_BIND` ke IP tailnet host.
- Di aaPanel, buat reverse proxy ke `http://<IP>:8650`. Kalau diakses lewat HTTPS, set `COOKIE_SECURE=true` lalu jalankan `docker compose up -d`.
- **`SECRET_KEY` jangan diganti** setelah instance disimpan. Token terenkripsi dengan kunci ini; kalau kuncinya berubah, semua token harus diisi ulang.

## Pemakaian

1. **Pengaturan → Tambah instance**: isi nama, URL (misalnya `https://paperless.serrumah.my.id`), dan API token (dari Profil → API Auth Token di Paperless). Klik *Tes koneksi*, lalu *Simpan*.
2. **Migrasi**: pilih sumber, tujuan, dan filter, lalu *Dry-run* untuk melihat hasilnya dulu, kemudian *Jalankan migrasi*. Progres bisa dipantau di halaman job. Kalau halaman ditutup, job tetap berjalan di server.
3. **Hapus dari sumber**: klik *Pratinjau yang akan dihapus* dengan sumber, tujuan, dan filter yang sama. Di halaman job, ketik `HAPUS` lalu tekan tombol hapus. Setiap dokumen diverifikasi ulang sebelum dihapus. Di Paperless versi baru, dokumen yang dihapus masuk Trash dulu.
4. **Sudah pernah memakai `paperless_migrate.py`?** Buka Migrasi → *Impor riwayat*, lalu unggah `migrate_state.json`, supaya dokumen yang sudah dipindah tidak diproses lagi. Kalaupun tidak diimpor, dokumen yang sudah ada di tujuan akan terdeteksi sebagai DUPLIKAT lewat checksum dan tidak diunggah ulang.

### Perilaku migrasi

| Status | Arti |
|---|---|
| `OK` | Berhasil disalin. Dokumen sumber diberi tag `migrated`. |
| `DUPLIKAT` | File identik sudah ada di tujuan, jadi tidak diunggah. Dokumen sumber diberi tag `migrate-duplikat`. |
| `GAGAL` | Gagal diproses di tujuan (misalnya Tika/Gotenberg tidak ada untuk file Office). Dokumen sumber diberi tag `migrate-gagal` dan **tidak pernah dihapus**. |
| `DILEWATI` | Sudah tercatat dipindah sebelumnya. |

- Tag penanda hanya dipasang di sumber dan tidak pernah ikut disalin ke tujuan.
- Teks OCR di tujuan ditimpa dengan teks dari sumber, jadi hasil OCR paperless-gpt tetap terbawa.
- Hanya satu job yang berjalan pada satu waktu. Job berikutnya masuk antrian, supaya Paperless tidak kewalahan.
- Kalau container di-restart saat job berjalan, status job menjadi `terhenti`. Jalankan ulang job dengan filter yang sama; dokumen yang sudah sukses akan dilewati.

### Keterbatasan (dari API Paperless)

- File archive (PDF hasil OCR) tidak ikut disalin. Tujuan membuat archive sendiri dari file original.
- Tanggal "added" menjadi waktu impor. Notes tercatat atas nama pemilik token tujuan, tapi penulis dan tanggal aslinya ditulis di awal teks note.

## Keamanan

- Login memakai satu akun dari `.env`. Ada jeda dan penguncian 5 menit setelah 5 kali salah password.
- Cookie sesi `SameSite=Lax` dan `HttpOnly`, dan setiap request yang mengubah data wajib membawa header khusus (perlindungan CSRF).
- Jangan buka port ini ke internet tanpa HTTPS. Sebaiknya akses lewat tailnet atau reverse proxy saja.
- Data portal (instance, riwayat, mapping) ada di `./data/portal.db`. Sertakan file ini dalam backup.

## Script CLI

Folder `cli/` berisi versi baris perintah yang dipakai sebelum portal ini ada:

- `paperless_migrate.py`: migrasi dengan filter, log CSV, tag penanda, dan `--delete-source`.
- `cek_file.py`: cek apakah file (berdasarkan nama aslinya) ada di sumber, Trash, dan tujuan.
