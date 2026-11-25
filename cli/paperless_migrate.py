#!/usr/bin/env python3
"""
paperless_migrate.py — salin dokumen dari satu Paperless-ngx ke Paperless-ngx lain via REST API,
dengan filter tag dan/atau storage path.

Yang ikut disalin:
  - file original (bukan archive), nama file asli
  - title, created date, ASN
  - tags, correspondent, document type, storage path  (dibuat otomatis di target bila belum ada, dicocokkan per NAMA)
  - custom fields + nilainya (termasuk select & document link)
  - content / hasil OCR dari sumber (di-PATCH ke target supaya teksnya identik, tidak bergantung OCR ulang)
  - notes (teks note + info penulis & tanggal asli sebagai prefix)

Yang TIDAK bisa disalin lewat API (keterbatasan Paperless):
  - tanggal "added" (akan jadi waktu impor), id dokumen, riwayat audit log
  - file archive (PDF/A hasil OCR) — target akan membuat archive sendiri dari original
  - penulis note asli (note dibuat atas nama user token target)

Pemakaian:
  pip install requests
  export SRC_URL=http://192.168.1.10:8000  SRC_TOKEN=xxxx
  export DST_URL=http://192.168.1.20:8000  DST_TOKEN=yyyy

  python3 paperless_migrate.py --tag "Keuangan" --dry-run
  python3 paperless_migrate.py --tag "Keuangan" --tag "2025"          # dokumen yg punya SEMUA tag ini
  python3 paperless_migrate.py --any-tag "Pajak" --any-tag "Invoice"  # punya SALAH SATU tag ini
  python3 paperless_migrate.py --storage-path "Arsip Kantor"
  python3 paperless_migrate.py --storage-path "Arsip Kantor" --mark-tag "migrated"

  # dengan tag penanda + log, lalu hapus dari sumber yang sudah terverifikasi ada di target:
  python3 paperless_migrate.py --storage-path "Arsip Kantor" \
      --mark-tag migrated --fail-tag migrate-gagal --dup-tag migrate-duplikat \
      --delete-source --delete-duplicates

Log: setiap dokumen dicatat di migrate_log.csv (waktu, src_id, judul, status, dst_id, pesan).
Status: OK, DUPLIKAT, GAGAL, VERIFIKASI_GAGAL, DIHAPUS_DARI_SUMBER.

Script bisa dijalankan ulang (resume): dokumen yang sudah sukses dicatat di state file
(default: migrate_state.json) dan dilewati pada run berikutnya.
"""

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime

import requests

PAGE_SIZE = 100


# ---------------------------------------------------------------- HTTP helper
class PL:
    def __init__(self, url, token, name):
        self.base = url.rstrip("/")
        self.name = name
        self.s = requests.Session()
        self.s.headers.update({
            "Authorization": f"Token {token}",
            "Accept": "application/json; version=9",
        })

    def _url(self, path):
        return path if path.startswith("http") else f"{self.base}{path}"

    def get(self, path, **kw):
        r = self.s.get(self._url(path), timeout=120, **kw)
        r.raise_for_status()
        return r

    def post(self, path, **kw):
        r = self.s.post(self._url(path), timeout=300, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"[{self.name}] POST {path} -> {r.status_code}: {r.text[:500]}")
        return r

    def patch(self, path, **kw):
        r = self.s.patch(self._url(path), timeout=120, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"[{self.name}] PATCH {path} -> {r.status_code}: {r.text[:500]}")
        return r

    def all(self, path, params=None):
        params = dict(params or {})
        params.setdefault("page_size", PAGE_SIZE)
        url = path
        while url:
            data = self.get(url, params=params).json()
            params = None  # 'next' URL sudah membawa query string
            yield from data.get("results", [])
            url = data.get("next")
            if url and url.startswith("http://") and self.base.startswith("https://"):
                url = "https://" + url[len("http://"):]


# ------------------------------------------------------- metadata mapping
SIMPLE_OBJECTS = {
    # endpoint: field yang ikut disalin saat membuat objek baru di target
    "tags": ["name", "color", "match", "matching_algorithm", "is_insensitive", "is_inbox_tag"],
    "correspondents": ["name", "match", "matching_algorithm", "is_insensitive"],
    "document_types": ["name", "match", "matching_algorithm", "is_insensitive"],
    "storage_paths": ["name", "path", "match", "matching_algorithm", "is_insensitive"],
}


class Mapper:
    """Memetakan id objek sumber -> id objek target, berdasarkan nama."""

    def __init__(self, src: PL, dst: PL, dry_run: bool):
        self.src, self.dst, self.dry = src, dst, dry_run
        self.src_objs = {}   # endpoint -> {id: obj}
        self.dst_by_name = {}  # endpoint -> {name_lower: obj}
        self.cache = {}      # (endpoint, src_id) -> dst_id
        for ep in list(SIMPLE_OBJECTS) + ["custom_fields"]:
            self.src_objs[ep] = {o["id"]: o for o in src.all(f"/api/{ep}/")}
            self.dst_by_name[ep] = {o["name"].lower(): o for o in dst.all(f"/api/{ep}/")}

    def map_id(self, ep, src_id):
        if src_id is None:
            return None
        key = (ep, src_id)
        if key in self.cache:
            return self.cache[key]
        so = self.src_objs[ep].get(src_id)
        if so is None:
            return None  # objek tak terlihat oleh user token sumber
        do = self.dst_by_name[ep].get(so["name"].lower())
        if do is None:
            if self.dry:
                print(f"    [dry-run] akan membuat {ep[:-1]} '{so['name']}' di target")
                self.cache[key] = None
                return None
            if ep == "custom_fields":
                payload = {"name": so["name"], "data_type": so["data_type"]}
                if so.get("extra_data"):
                    payload["extra_data"] = so["extra_data"]
            else:
                payload = {k: so[k] for k in SIMPLE_OBJECTS[ep] if k in so}
            do = self.dst.post(f"/api/{ep}/", json=payload).json()
            self.dst_by_name[ep][do["name"].lower()] = do
            print(f"    + dibuat {ep[:-1]} '{do['name']}' di target (id {do['id']})")
        self.cache[key] = do["id"]
        return do["id"]

    def lookup_src_id(self, ep, name):
        for o in self.src_objs[ep].values():
            if o["name"].lower() == name.lower():
                return o["id"]
        sys.exit(f"ERROR: {ep[:-1]} '{name}' tidak ditemukan di sumber")

    # --- custom field values
    @staticmethod
    def _options(field):
        opts = (field.get("extra_data") or {}).get("select_options") or []
        # versi lama: list string (nilai = index); versi baru: list {id, label}
        out = []
        for i, o in enumerate(opts):
            if isinstance(o, dict):
                out.append((o.get("id"), o.get("label")))
            else:
                out.append((i, o))
        return out

    def map_custom_field_value(self, src_field_id, value, doc_map):
        sf = self.src_objs["custom_fields"].get(src_field_id)
        if sf is None:
            return None, None
        dst_field_id = self.map_id("custom_fields", src_field_id)
        if dst_field_id is None or value is None:
            return dst_field_id, value
        dtype = sf["data_type"]
        if dtype == "select":
            label = next((lbl for oid, lbl in self._options(sf) if str(oid) == str(value)), None)
            df = self.dst_by_name["custom_fields"][sf["name"].lower()]
            if "extra_data" not in df:  # ambil ulang detail field target
                df = self.dst.get(f"/api/custom_fields/{dst_field_id}/").json()
            match = next((oid for oid, lbl in self._options(df) if lbl == label), None)
            if match is None:
                print(f"    ! opsi select '{label}' tidak ada di field target '{sf['name']}', dilewati")
            return dst_field_id, match
        if dtype == "documentlink":
            mapped = [doc_map[str(v)] for v in (value or []) if str(v) in doc_map]
            return dst_field_id, mapped
        return dst_field_id, value


# ------------------------------------------------------------- main logic
def wait_task(dst: PL, task_id, timeout=1800):
    t0 = time.time()
    while time.time() - t0 < timeout:
        res = dst.get("/api/tasks/", params={"task_id": task_id}).json()
        if isinstance(res, dict):
            res = res.get("results", [])
        if res:
            t = res[0]
            if t["status"] == "SUCCESS":
                return int(t["related_document"]), None
            if t["status"] in ("FAILURE", "REVOKED"):
                return None, t.get("result")
        time.sleep(3)
    return None, "timeout menunggu consume"



# ------------------------------------------------------------- log & tag helpers
class RunLog:
    """Log CSV (append): satu baris per dokumen per run."""

    FIELDS = ["waktu", "src_id", "judul", "status", "dst_id", "pesan"]

    def __init__(self, path):
        self.path = path
        new = not os.path.exists(path)
        self.f = open(path, "a", newline="", encoding="utf-8")
        self.w = csv.writer(self.f)
        if new:
            self.w.writerow(self.FIELDS)

    def write(self, src_id, title, status, dst_id="", msg=""):
        self.w.writerow([datetime.now().isoformat(timespec="seconds"), src_id, title,
                         status, dst_id or "", str(msg).replace("\n", " ")[:500]])
        self.f.flush()


def ensure_tag(pl: PL, cache: dict, name):
    if not name:
        return None
    for o in cache.values():
        if o["name"].lower() == name.lower():
            return o["id"]
    t = pl.post("/api/tags/", json={"name": name, "matching_algorithm": 0}).json()
    cache[t["id"]] = t
    return t["id"]


def set_tag(pl: PL, doc_id, tag_id, add=True):
    if tag_id:
        pl.post("/api/documents/bulk_edit/", json={
            "documents": [doc_id], "method": "add_tag" if add else "remove_tag",
            "parameters": {"tag": tag_id}})


def src_checksum(src: PL, doc_id):
    return src.get(f"/api/documents/{doc_id}/metadata/").json().get("original_checksum")


def find_in_dst(dst: PL, checksum):
    """Cari dokumen di target dengan checksum file original yang sama. Return id atau None."""
    if not checksum:
        return None
    res = dst.get("/api/documents/", params={"checksum__iexact": checksum,
                                             "page_size": 1, "fields": "id"}).json()
    hits = res.get("results", [])
    return hits[0]["id"] if hits else None


# ------------------------------------------------------------- main logic
def main():
    ap = argparse.ArgumentParser(description="Salin dokumen antar Paperless-ngx via API")
    ap.add_argument("--tag", action="append", default=[], help="wajib punya tag ini (boleh berulang, AND)")
    ap.add_argument("--any-tag", action="append", default=[], help="punya salah satu tag ini (OR)")
    ap.add_argument("--storage-path", help="filter nama storage path")
    ap.add_argument("--query", help="filter tambahan: full-text query Paperless")
    ap.add_argument("--mark-tag", default="migrated", help="tag untuk dokumen SUMBER yang sukses disalin (mis. 'migrated')")
    ap.add_argument("--fail-tag", default="migrate-gagal", help="tag untuk dokumen SUMBER yang gagal disalin (mis. 'migrate-gagal')")
    ap.add_argument("--dup-tag", default="migrate-duplikat", help="tag untuk dokumen SUMBER yang file-nya sudah ada di target "
                                      "(mis. 'migrate-duplikat')")
    ap.add_argument("--delete-source", action="store_true",
                    help="di akhir, hapus dari SUMBER dokumen yang sukses (dan terverifikasi ada di target)")
    ap.add_argument("--delete-duplicates", action="store_true",
                    help="bersama --delete-source: ikut hapus dokumen DUPLIKAT (file identik sudah ada di target)")
    ap.add_argument("--yes", action="store_true", help="jangan minta konfirmasi sebelum menghapus")
    ap.add_argument("--no-content", action="store_true", help="jangan timpa content target dengan OCR sumber")
    ap.add_argument("--state", default="migrate_state.json")
    ap.add_argument("--log", default="migrate_log.csv", help="file log CSV (default: migrate_log.csv)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    for v in ("SRC_URL", "SRC_TOKEN", "DST_URL", "DST_TOKEN"):
        if not os.environ.get(v):
            sys.exit(f"ERROR: environment variable {v} belum di-set")
    if not (a.tag or a.any_tag or a.storage_path or a.query):
        sys.exit("ERROR: beri minimal satu filter (--tag / --any-tag / --storage-path / --query)")

    src = PL(os.environ["SRC_URL"], os.environ["SRC_TOKEN"], "src")
    dst = PL(os.environ["DST_URL"], os.environ["DST_TOKEN"], "dst")
    m = Mapper(src, dst, a.dry_run)
    log = RunLog(a.log) if not a.dry_run else None

    state = {}
    if os.path.exists(a.state):
        with open(a.state) as f:
            state = json.load(f)  # {src_id(str): dst_id}

    params = {"ordering": "id"}
    if a.tag:
        params["tags__id__all"] = ",".join(str(m.lookup_src_id("tags", t)) for t in a.tag)
    if a.any_tag:
        params["tags__id__in"] = ",".join(str(m.lookup_src_id("tags", t)) for t in a.any_tag)
    if a.storage_path:
        params["storage_path__id"] = m.lookup_src_id("storage_paths", a.storage_path)
    if a.query:
        params["query"] = a.query

    # tag penanda di SUMBER (tidak ikut disalin ke target)
    src_tags = m.src_objs["tags"]
    mark_id = fail_id = dup_id = None
    if not a.dry_run:
        mark_id = ensure_tag(src, src_tags, a.mark_tag)
        fail_id = ensure_tag(src, src_tags, a.fail_tag)
        dup_id = ensure_tag(src, src_tags, a.dup_tag)
    helper_tags = {t for t in (mark_id, fail_id, dup_id) if t}
    for name in (a.mark_tag, a.fail_tag, a.dup_tag):  # juga saat dry-run
        if name:
            helper_tags |= {o["id"] for o in src_tags.values() if o["name"].lower() == name.lower()}

    docs = list(src.all("/api/documents/", params))
    print(f"Ditemukan {len(docs)} dokumen sesuai filter. Sudah tersalin sebelumnya: "
          f"{sum(1 for d in docs if str(d['id']) in state)}")

    ok = fail = dup = 0
    failed, dups = [], []          # (src_id, title, pesan) / (src_id, title, dst_id)
    pending_links = []

    for d in docs:
        sid = str(d["id"])
        if sid in state:
            continue
        print(f"\n#{d['id']}  {d['title']}")
        if mark_id and mark_id in d["tags"]:
            print("    sudah bertag penanda sukses, dilewati")
            continue

        tags = [t for t in (m.map_id("tags", t) for t in d["tags"] if t not in helper_tags) if t]
        corr = m.map_id("correspondents", d["correspondent"])
        dtype = m.map_id("document_types", d["document_type"])
        spath = m.map_id("storage_paths", d["storage_path"])

        if a.dry_run:
            n_tags = len([t for t in d["tags"] if t not in helper_tags])
            print(f"    [dry-run] akan disalin: tags={n_tags} notes={len(d.get('notes') or [])} "
                  f"custom_fields={len(d.get('custom_fields') or [])}")
            continue

        checksum = None
        try:
            # 0. sudah ada file identik di target? -> duplikat, jangan upload
            checksum = src_checksum(src, d["id"])
            existing = find_in_dst(dst, checksum)
            if existing:
                dup += 1
                dups.append((d["id"], d["title"], existing))
                set_tag(src, d["id"], fail_id, add=False)
                set_tag(src, d["id"], dup_id)
                log.write(d["id"], d["title"], "DUPLIKAT", existing, "file identik sudah ada di target")
                print(f"    DUPLIKAT: file identik sudah ada di target (id {existing}), tidak diupload")
                continue

            # 1. download original
            r = src.get(f"/api/documents/{d['id']}/download/", params={"original": "true"})
            fname = d.get("original_file_name") or f"{d['id']}.pdf"

            # 2. upload ke target
            form = {"title": d["title"]}
            if d.get("created"):
                form["created"] = d["created"]
            if d.get("archive_serial_number") is not None:
                form["archive_serial_number"] = str(d["archive_serial_number"])
            if corr: form["correspondent"] = str(corr)
            if dtype: form["document_type"] = str(dtype)
            if spath: form["storage_path"] = str(spath)
            data = list(form.items()) + [("tags", str(t)) for t in tags]
            task_id = dst.post("/api/documents/post_document/",
                               data=data, files={"document": (fname, r.content)}).json()
            if isinstance(task_id, dict):
                task_id = task_id.get("task_id")
            new_id, err = wait_task(dst, task_id)
            if not new_id:
                raise RuntimeError(f"consume gagal di target: {err}")

            # 3. paksa metadata identik (menimpa hasil auto-matching/workflow target)
            patch = {"title": d["title"], "tags": tags, "correspondent": corr,
                     "document_type": dtype, "storage_path": spath,
                     "archive_serial_number": d.get("archive_serial_number")}
            if d.get("created"):
                patch["created"] = d["created"]
            if not a.no_content:
                patch["content"] = d.get("content") or ""
            cfs, has_link = [], False
            for cf in d.get("custom_fields") or []:
                fid, val = m.map_custom_field_value(cf["field"], cf.get("value"), state)
                if fid:
                    cfs.append({"field": fid, "value": val})
                if m.src_objs["custom_fields"].get(cf["field"], {}).get("data_type") == "documentlink":
                    has_link = True
            patch["custom_fields"] = cfs
            dst.patch(f"/api/documents/{new_id}/", json=patch)
            if has_link:
                pending_links.append((d, new_id))

            # 4. notes
            for n in sorted(d.get("notes") or [], key=lambda x: x.get("created", "")):
                user = n.get("user")
                uname = user.get("username") if isinstance(user, dict) else user
                text = f"[{n.get('created', '')[:16].replace('T', ' ')} oleh {uname}]\n{n['note']}"
                dst.post(f"/api/documents/{new_id}/notes/", json={"note": text})

            # 5. tandai sumber: sukses (+ hapus tag gagal dari run sebelumnya)
            set_tag(src, d["id"], fail_id, add=False)
            set_tag(src, d["id"], mark_id)

            state[sid] = new_id
            with open(a.state, "w") as f:
                json.dump(state, f, indent=1)
            ok += 1
            log.write(d["id"], d["title"], "OK", new_id)
            print(f"    OK -> id target {new_id}")
        except Exception as e:  # noqa
            fail += 1
            failed.append((d["id"], d["title"], str(e)))
            try:
                set_tag(src, d["id"], fail_id)
            except Exception:  # noqa
                pass
            log.write(d["id"], d["title"], "GAGAL", "", e)
            print(f"    GAGAL: {e}")

    # 6. perbaiki custom field 'document link' setelah semua dokumen punya id target
    for d, new_id in pending_links:
        cfs = []
        for cf in d.get("custom_fields") or []:
            fid, val = m.map_custom_field_value(cf["field"], cf.get("value"), state)
            if fid:
                cfs.append({"field": fid, "value": val})
        dst.patch(f"/api/documents/{new_id}/", json={"custom_fields": cfs})

    # 7. ringkasan
    print("\n" + "=" * 60)
    print(f"Selesai. Sukses: {ok}, duplikat: {dup}, gagal: {fail}")
    if failed:
        print("\nDokumen GAGAL (id sumber - judul - pesan):")
        for i, t, msg in failed:
            print(f"  #{i}  {t}\n      {msg[:200]}")
    if a.dry_run:
        print("\n[dry-run] Tidak ada yang disalin/diubah dan log tidak ditulis. "
              "Jalankan tanpa --dry-run untuk eksekusi sebenarnya.")
    else:
        print(f"\nLog lengkap: {a.log}   State: {a.state}")

    # 8. hapus dari sumber (opsional)
    if a.delete_source:
        delete_from_source(a, src, dst, docs, state, log)


def delete_from_source(a, src, dst, docs, state, log):
    """Hapus dari SUMBER hanya dokumen yang file identiknya terverifikasi ada di target."""
    print("\n" + "=" * 60)
    print("Verifikasi sebelum menghapus dari sumber (cek checksum file di target)...")
    to_delete, skipped = [], []
    for d in docs:
        sid = str(d["id"])
        in_state = sid in state
        if not in_state and not a.delete_duplicates:
            continue
        try:
            found = find_in_dst(dst, src_checksum(src, d["id"]))
        except Exception as e:  # noqa
            print(f"  ! #{d['id']}: gagal verifikasi ({e}), dilewati")
            found = None
        if found:
            to_delete.append((d["id"], d["title"], found))
        elif in_state:
            skipped.append((d["id"], d["title"]))

    for i, t in skipped:
        print(f"  ! #{i} {t}: tercatat sukses tapi file tidak ditemukan di target -> TIDAK dihapus")
        if log:
            log.write(i, t, "VERIFIKASI_GAGAL", state.get(str(i), ""), "file tidak ditemukan di target")

    if not to_delete:
        print("Tidak ada dokumen yang bisa dihapus.")
        return
    print(f"\n{len(to_delete)} dokumen di SUMBER siap dihapus (file identik sudah ada di target).")
    if a.dry_run:
        print("[dry-run] tidak ada yang dihapus.")
        return
    if not a.yes:
        ans = input("Ketik HAPUS untuk menghapus dokumen ini dari sumber: ").strip()
        if ans != "HAPUS":
            print("Dibatalkan, tidak ada yang dihapus.")
            return
    ids = [i for i, _, _ in to_delete]
    for k in range(0, len(ids), 50):
        batch = ids[k:k + 50]
        src.post("/api/documents/bulk_edit/", json={"documents": batch, "method": "delete",
                                                    "parameters": {}})
        print(f"  dihapus {k + len(batch)}/{len(ids)}")
    for i, t, found in to_delete:
        log.write(i, t, "DIHAPUS_DARI_SUMBER", found, "")
    print("Selesai menghapus. Di Paperless versi baru dokumen masuk Trash dulu (bisa dipulihkan).")


if __name__ == "__main__":
    main()
