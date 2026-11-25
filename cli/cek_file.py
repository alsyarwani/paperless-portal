#!/usr/bin/env python3
"""
cek_file.py — cek apakah file (berdasarkan nama file asli) ada di Paperless sumber dan/atau target.

Pakai env yang sama dengan paperless_migrate.py (SRC_URL, SRC_TOKEN, DST_URL, DST_TOKEN).

  python3 cek_file.py 0000242.xlsx 0000241.xlsx 0000234.docx
  python3 cek_file.py --file daftar.txt          # satu nama file per baris

Untuk tiap file ditampilkan:
  - id & judul di SUMBER (termasuk kalau ada di Trash)
  - id & judul di TARGET
  - apakah file-nya identik (checksum sama)
"""
import argparse
import os
import sys

import requests


def session(token):
    s = requests.Session()
    s.headers.update({"Authorization": f"Token {token}", "Accept": "application/json; version=9"})
    return s


def find_by_filename(s, base, name):
    r = s.get(f"{base}/api/documents/", params={"original_filename__iexact": name,
                                                 "page_size": 25}, timeout=60)
    r.raise_for_status()
    return r.json().get("results", [])


def find_by_checksum(s, base, checksum):
    r = s.get(f"{base}/api/documents/", params={"checksum__iexact": checksum, "page_size": 5}, timeout=60)
    r.raise_for_status()
    return r.json().get("results", [])


def checksum_of(s, base, doc_id):
    r = s.get(f"{base}/api/documents/{doc_id}/metadata/", timeout=60)
    r.raise_for_status()
    return r.json().get("original_checksum")


def load_trash(s, base):
    """Dokumen di Trash sumber (Paperless-ngx >= 2.10). Return {nama_file_lower: [doc]}."""
    out = {}
    url, params = f"{base}/api/trash/", {"page_size": 100}
    try:
        while url:
            r = s.get(url, params=params, timeout=60)
            if r.status_code == 404:
                return None  # versi lama, tidak ada trash
            r.raise_for_status()
            data = r.json()
            for d in data.get("results", []):
                key = (d.get("original_file_name") or "").lower()
                out.setdefault(key, []).append(d)
            url, params = data.get("next"), None
    except requests.RequestException:
        return None
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("names", nargs="*", help="nama file asli, mis. 0000242.xlsx")
    ap.add_argument("--file", help="file teks berisi daftar nama file (satu per baris)")
    a = ap.parse_args()

    names = list(a.names)
    if a.file:
        with open(a.file) as f:
            names += [ln.strip() for ln in f if ln.strip()]
    if not names:
        sys.exit("Beri nama file, mis: python3 cek_file.py 0000242.xlsx 0000241.xlsx")
    for v in ("SRC_URL", "SRC_TOKEN", "DST_URL", "DST_TOKEN"):
        if not os.environ.get(v):
            sys.exit(f"ERROR: environment variable {v} belum di-set")

    sb, db = os.environ["SRC_URL"].rstrip("/"), os.environ["DST_URL"].rstrip("/")
    ss, ds = session(os.environ["SRC_TOKEN"]), session(os.environ["DST_TOKEN"])
    trash = load_trash(ss, sb)

    ringkas = {"aman": 0, "hanya_sumber": 0, "hilang": 0}
    for name in names:
        print(f"\n=== {name}")
        src_docs = find_by_filename(ss, sb, name)
        src_trash = (trash or {}).get(name.lower(), [])
        dst_docs = find_by_filename(ds, db, name)

        for d in src_docs:
            print(f"  SUMBER : #{d['id']}  {d['title']}")
        for d in src_trash:
            print(f"  SUMBER (di Trash): #{d['id']}  {d['title']}")
        if not src_docs and not src_trash:
            print("  SUMBER : tidak ada" + ("" if trash is not None else " (Trash tidak bisa dicek)"))

        # cocokkan juga via checksum (kalau nama file di target berbeda)
        if not dst_docs:
            for d in src_docs:
                dst_docs += find_by_checksum(ds, db, checksum_of(ss, sb, d["id"]))
        for d in dst_docs:
            print(f"  TARGET : #{d['id']}  {d['title']}")
        if not dst_docs:
            print("  TARGET : tidak ada")

        if src_docs and dst_docs:
            cs_src = checksum_of(ss, sb, src_docs[0]["id"])
            cs_dst = checksum_of(ds, db, dst_docs[0]["id"])
            print(f"  File identik: {'YA' if cs_src == cs_dst else 'TIDAK (isi file berbeda!)'}")

        if dst_docs:
            ringkas["aman"] += 1
            print("  -> AMAN: sudah ada di target")
        elif src_docs or src_trash:
            ringkas["hanya_sumber"] += 1
            print("  -> BELUM di target, masih ada di sumber" + (" (di Trash, pulihkan dulu)" if src_trash and not src_docs else ""))
        else:
            ringkas["hilang"] += 1
            print("  -> TIDAK DITEMUKAN di mana pun (cek Trash manual / judul berbeda)")

    print("\n" + "=" * 50)
    print(f"Sudah di target: {ringkas['aman']} | Hanya di sumber: {ringkas['hanya_sumber']} | "
          f"Tidak ditemukan: {ringkas['hilang']}")


if __name__ == "__main__":
    main()
