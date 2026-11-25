"""Mesin migrasi & penghapusan terverifikasi antar Paperless-ngx.

Dipakai oleh job runner. Semua progres dilaporkan lewat objek `ctx` (lihat jobs.JobContext).
"""
from .paperless import Paperless

SIMPLE_OBJECTS = {
    "tags": ["name", "color", "match", "matching_algorithm", "is_insensitive", "is_inbox_tag"],
    "correspondents": ["name", "match", "matching_algorithm", "is_insensitive"],
    "document_types": ["name", "match", "matching_algorithm", "is_insensitive"],
    "storage_paths": ["name", "path", "match", "matching_algorithm", "is_insensitive"],
}


class Mapper:
    """Memetakan id objek sumber -> id objek target berdasarkan nama (dibuat di target bila belum ada)."""

    def __init__(self, src: Paperless, dst: Paperless, dry_run: bool, log=print):
        self.src, self.dst, self.dry, self.log = src, dst, dry_run, log
        self.src_objs, self.dst_by_name, self.cache = {}, {}, {}
        for ep in list(SIMPLE_OBJECTS) + ["custom_fields"]:
            self.src_objs[ep] = {o["id"]: o for o in src.all(f"/api/{ep}/")}
            self.dst_by_name[ep] = {o["name"].lower(): o for o in dst.all(f"/api/{ep}/")}
        self.would_create = set()

    def map_id(self, ep, src_id):
        if src_id is None:
            return None
        key = (ep, src_id)
        if key in self.cache:
            return self.cache[key]
        so = self.src_objs[ep].get(src_id)
        if so is None:
            return None
        do = self.dst_by_name[ep].get(so["name"].lower())
        if do is None:
            if self.dry:
                self.would_create.add(f"{ep[:-1]}: {so['name']}")
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
            self.log(f"dibuat {ep[:-1]} '{do['name']}' di target")
        self.cache[key] = do["id"]
        return do["id"]

    def lookup_src_id(self, ep, name):
        for o in self.src_objs[ep].values():
            if o["name"].lower() == name.lower():
                return o["id"]
        raise ValueError(f"{ep[:-1]} '{name}' tidak ditemukan di sumber")

    @staticmethod
    def _options(field):
        opts = (field.get("extra_data") or {}).get("select_options") or []
        return [(o.get("id"), o.get("label")) if isinstance(o, dict) else (i, o) for i, o in enumerate(opts)]

    def map_custom_field_value(self, src_field_id, value, doc_map):
        sf = self.src_objs["custom_fields"].get(src_field_id)
        if sf is None:
            return None, None
        dst_field_id = self.map_id("custom_fields", src_field_id)
        if dst_field_id is None or value is None:
            return dst_field_id, value
        if sf["data_type"] == "select":
            label = next((l for oid, l in self._options(sf) if str(oid) == str(value)), None)
            df = self.dst_by_name["custom_fields"][sf["name"].lower()]
            if "extra_data" not in df:
                df = self.dst.get(f"/api/custom_fields/{dst_field_id}/").json()
            return dst_field_id, next((oid for oid, l in self._options(df) if l == label), None)
        if sf["data_type"] == "documentlink":
            return dst_field_id, [doc_map[str(v)] for v in (value or []) if str(v) in doc_map]
        return dst_field_id, value


def build_filter(m: Mapper, flt: dict):
    params = {"ordering": "id"}
    if flt.get("tags"):
        params["tags__id__all"] = ",".join(str(m.lookup_src_id("tags", t)) for t in flt["tags"])
    if flt.get("any_tags"):
        params["tags__id__in"] = ",".join(str(m.lookup_src_id("tags", t)) for t in flt["any_tags"])
    if flt.get("storage_path"):
        params["storage_path__id"] = m.lookup_src_id("storage_paths", flt["storage_path"])
    if flt.get("query"):
        params["query"] = flt["query"]
    if len(params) == 1:
        raise ValueError("Beri minimal satu filter (tag / storage path / query)")
    return params


# --------------------------------------------------------------------- migrate
def run_migrate(ctx, src: Paperless, dst: Paperless, mapping: dict, p: dict):
    flt, opt = p.get("filter", {}), p.get("options", {})
    dry = bool(opt.get("dry_run"))
    m = Mapper(src, dst, dry, log=ctx.log)
    params = build_filter(m, flt)

    src_tags = m.src_objs["tags"]
    names = [opt.get("mark_tag"), opt.get("fail_tag"), opt.get("dup_tag")]
    helper = {o["id"] for o in src_tags.values() if o["name"].lower() in {n.lower() for n in names if n}}
    mark_id = fail_id = dup_id = None
    if not dry:
        mark_id, fail_id, dup_id = (src.ensure_tag(n, src_tags) for n in names)
        helper |= {t for t in (mark_id, fail_id, dup_id) if t}

    docs = list(src.all("/api/documents/", params))
    pending = [d for d in docs if str(d["id"]) not in mapping]
    already = len(docs) - len(pending)
    ctx.set(total=len(pending), skipped=already)
    ctx.log(f"{len(docs)} dokumen cocok filter; {already} sudah tersalin sebelumnya, {len(pending)} diproses.")

    pending_links = []
    for d in pending:
        if ctx.cancelled():
            ctx.log("Dibatalkan oleh pengguna.")
            return "dibatalkan"
        sid, title = d["id"], d["title"]
        if mark_id and mark_id in d["tags"]:
            ctx.item(sid, title, "DILEWATI", None, "sudah bertag penanda sukses")
            ctx.bump(done=1, skipped=1)
            continue
        try:
            tags = [t for t in (m.map_id("tags", t) for t in d["tags"] if t not in helper) if t]
            corr = m.map_id("correspondents", d["correspondent"])
            dtype = m.map_id("document_types", d["document_type"])
            spath = m.map_id("storage_paths", d["storage_path"])

            checksum = src.checksum(sid)
            existing = dst.find_by_checksum(checksum)
            if existing:
                if not dry:
                    src.remove_tag(sid, fail_id)
                    src.add_tag(sid, dup_id)
                ctx.item(sid, title, "DUPLIKAT", existing, "file identik sudah ada di target")
                ctx.bump(done=1, dup=1)
                continue

            if dry:
                n_tags = len([t for t in d["tags"] if t not in helper])
                ctx.item(sid, title, "AKAN_DISALIN", None,
                         f"tags={n_tags}, notes={len(d.get('notes') or [])}, "
                         f"custom_fields={len(d.get('custom_fields') or [])}, file={d.get('original_file_name')}")
                ctx.bump(done=1, ok=1)
                continue

            r = src.get(f"/api/documents/{sid}/download/", params={"original": "true"}, timeout=300)
            fname = d.get("original_file_name") or f"{sid}.pdf"
            form = {"title": title}
            if d.get("created"):
                form["created"] = d["created"]
            if d.get("archive_serial_number") is not None:
                form["archive_serial_number"] = str(d["archive_serial_number"])
            if corr: form["correspondent"] = str(corr)
            if dtype: form["document_type"] = str(dtype)
            if spath: form["storage_path"] = str(spath)
            data = list(form.items()) + [("tags", str(t)) for t in tags]
            task_id = dst.post("/api/documents/post_document/", data=data,
                               files={"document": (fname, r.content)}).json()
            if isinstance(task_id, dict):
                task_id = task_id.get("task_id")
            new_id, err = dst.wait_task(task_id, should_stop=ctx.cancelled)
            if not new_id:
                raise RuntimeError(f"consume gagal di target: {err}")

            patch = {"title": title, "tags": tags, "correspondent": corr, "document_type": dtype,
                     "storage_path": spath, "archive_serial_number": d.get("archive_serial_number")}
            if d.get("created"):
                patch["created"] = d["created"]
            if not opt.get("no_content"):
                patch["content"] = d.get("content") or ""
            cfs, has_link = [], False
            for cf in d.get("custom_fields") or []:
                fid, val = m.map_custom_field_value(cf["field"], cf.get("value"), mapping)
                if fid:
                    cfs.append({"field": fid, "value": val})
                if m.src_objs["custom_fields"].get(cf["field"], {}).get("data_type") == "documentlink":
                    has_link = True
            patch["custom_fields"] = cfs
            dst.patch(f"/api/documents/{new_id}/", json=patch)
            if has_link:
                pending_links.append((d, new_id))

            for n in sorted(d.get("notes") or [], key=lambda x: x.get("created", "")):
                user = n.get("user")
                uname = user.get("username") if isinstance(user, dict) else user
                text = f"[{n.get('created', '')[:16].replace('T', ' ')} oleh {uname}]\n{n['note']}"
                dst.post(f"/api/documents/{new_id}/notes/", json={"note": text})

            src.remove_tag(sid, fail_id)
            src.add_tag(sid, mark_id)
            mapping[str(sid)] = new_id
            ctx.map(sid, new_id)
            ctx.item(sid, title, "OK", new_id, d.get("original_file_name") or "")
            ctx.bump(done=1, ok=1)
        except Exception as e:  # noqa
            if not dry:
                try:
                    src.add_tag(sid, fail_id)
                except Exception:  # noqa
                    pass
            ctx.item(sid, title, "GAGAL", None, f"{d.get('original_file_name') or ''}: {e}")
            ctx.bump(done=1, fail=1)

    for d, new_id in pending_links:
        cfs = []
        for cf in d.get("custom_fields") or []:
            fid, val = m.map_custom_field_value(cf["field"], cf.get("value"), mapping)
            if fid:
                cfs.append({"field": fid, "value": val})
        try:
            dst.patch(f"/api/documents/{new_id}/", json={"custom_fields": cfs})
        except Exception as e:  # noqa
            ctx.log(f"gagal memperbaiki document link #{new_id}: {e}")

    if dry and m.would_create:
        ctx.log("Akan dibuat di target: " + ", ".join(sorted(m.would_create)))
    return "selesai"


# --------------------------------------------------------------------- delete
def run_delete(ctx, src: Paperless, dst: Paperless, mapping: dict, p: dict):
    """Hapus dari sumber hanya dokumen yang file identiknya (checksum) terverifikasi ada di target."""
    flt, opt = p.get("filter", {}), p.get("options", {})
    preview = bool(opt.get("dry_run"))
    include_dup = bool(opt.get("include_duplicates"))
    only_ids = {int(i) for i in (opt.get("doc_ids") or [])}

    m = Mapper(src, dst, True, log=ctx.log)
    docs = list(src.all("/api/documents/", build_filter(m, flt)))
    if only_ids:
        docs = [d for d in docs if d["id"] in only_ids]
    ctx.set(total=len(docs))
    ctx.log(f"Memverifikasi {len(docs)} dokumen (checksum di target)...")

    to_delete = []
    for d in docs:
        if ctx.cancelled():
            return "dibatalkan"
        sid, title = d["id"], d["title"]
        known = str(sid) in mapping
        try:
            found = dst.find_by_checksum(src.checksum(sid))
        except Exception as e:  # noqa
            ctx.item(sid, title, "VERIFIKASI_ERROR", None, str(e))
            ctx.bump(done=1, fail=1)
            continue
        if found and (known or include_dup):
            to_delete.append((sid, title, found))
            ctx.item(sid, title, "SIAP_HAPUS" if preview else "TERVERIFIKASI", found,
                     "tercatat sukses" if known else "duplikat (file identik ada di target)")
            ctx.bump(done=1, ok=1)
        elif found:
            ctx.item(sid, title, "DILEWATI", found, "file ada di target tapi bukan hasil migrasi; "
                                                    "centang 'ikut hapus duplikat' untuk menghapus")
            ctx.bump(done=1, skipped=1)
        else:
            ctx.item(sid, title, "TIDAK_ADA_DI_TARGET" if not known else "VERIFIKASI_GAGAL", None,
                     "tidak dihapus" + (" (tercatat sukses tapi file tidak ditemukan di target)" if known else ""))
            ctx.bump(done=1, skipped=1)

    if preview:
        ctx.log(f"Pratinjau: {len(to_delete)} dokumen siap dihapus dari sumber.")
        return "selesai"

    ids = [i for i, _, _ in to_delete]
    for k in range(0, len(ids), 50):
        if ctx.cancelled():
            return "dibatalkan"
        batch = ids[k:k + 50]
        src.bulk_edit(batch, "delete")
        ctx.bump(deleted=len(batch))
        ctx.log(f"dihapus {k + len(batch)}/{len(ids)}")
    for sid, title, found in to_delete:
        ctx.item(sid, title, "DIHAPUS", found, "")
    ctx.log(f"{len(ids)} dokumen dihapus dari sumber (masuk Trash bila fitur Trash aktif).")
    return "selesai"
