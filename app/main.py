"""Paperless Portal: dasbor, pencarian gabungan, cek file, task gagal, dan migrasi antar Paperless-ngx."""
import csv
import hmac
import io
import os
import time
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

import requests

from . import db, jobs
from .paperless import Paperless, PaperlessError

PORTAL_USER = os.environ.get("PORTAL_USER", "admin")
PORTAL_PASSWORD = os.environ.get("PORTAL_PASSWORD", "")
SECRET_KEY = os.environ.get("SECRET_KEY", "")
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "false").lower() in ("1", "true", "yes")
STATIC = Path(__file__).parent / "static"

if not PORTAL_PASSWORD or len(SECRET_KEY) < 16:
    raise SystemExit("Set PORTAL_PASSWORD dan SECRET_KEY (min. 16 karakter) di environment.")

@asynccontextmanager
async def lifespan(_app):
    db.init()
    jobs.start()
    yield


app = FastAPI(title="Paperless Portal", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, session_cookie="pportal",
                   max_age=12 * 3600, same_site="lax", https_only=COOKIE_SECURE)
pool = ThreadPoolExecutor(max_workers=16)


def friendly(exc):
    if isinstance(exc, requests.exceptions.SSLError):
        return "Masalah sertifikat TLS (coba matikan verifikasi TLS untuk instance ini)"
    if isinstance(exc, requests.Timeout):
        return "Timeout: Paperless terlalu lama merespons"
    if isinstance(exc, requests.ConnectionError):
        return "Tidak bisa dihubungi (mati, URL salah, atau masalah jaringan)"
    return str(exc)[:300]


@app.exception_handler(requests.RequestException)
@app.exception_handler(PaperlessError)
async def upstream_error(request: Request, exc: Exception):
    return JSONResponse({"detail": friendly(exc)}, status_code=502)


# ------------------------------------------------------------------ keamanan
@app.middleware("http")
async def csrf_guard(request: Request, call_next):
    # semua request yang mengubah data wajib membawa header ini (tidak bisa dikirim form lintas situs)
    if request.method in ("POST", "PUT", "DELETE", "PATCH") and request.url.path.startswith("/api/"):
        if request.headers.get("x-portal") != "1":
            return JSONResponse({"detail": "header X-Portal wajib"}, status_code=403)
    resp = await call_next(request)
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    return resp


def require_login(request: Request):
    if request.session.get("user") != PORTAL_USER:
        raise HTTPException(401, "belum login")
    return request.session["user"]


_fails = {}


class LoginIn(BaseModel):
    username: str
    password: str


@app.post("/api/login")
def login(body: LoginIn, request: Request):
    ip = request.client.host if request.client else "?"
    n, last = _fails.get(ip, (0, 0))
    if n >= 5 and time.time() - last < 300:
        raise HTTPException(429, "Terlalu banyak percobaan. Coba lagi 5 menit lagi.")
    ok = hmac.compare_digest(body.username, PORTAL_USER) & hmac.compare_digest(body.password, PORTAL_PASSWORD)
    if not ok:
        _fails[ip] = (n + 1, time.time())
        time.sleep(1)
        raise HTTPException(401, "Username atau password salah")
    _fails.pop(ip, None)
    request.session["user"] = PORTAL_USER
    return {"user": PORTAL_USER}


@app.post("/api/logout")
def logout(request: Request):
    request.session.clear()
    return {"ok": True}


@app.get("/api/me")
def me(user=Depends(require_login)):
    return {"user": user}


# ------------------------------------------------------------------ instances
class InstanceIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    url: str = Field(min_length=8)
    token: str = ""
    verify_tls: bool = True
    enabled: bool = True


def client(iid, timeout=30) -> Paperless:
    try:
        return jobs.client_for(iid, timeout)
    except ValueError as e:
        raise HTTPException(404, str(e))


def _probe(url, token, verify_tls, name="test"):
    pl = Paperless(url, token, name, verify_tls, timeout=15)
    t0 = time.time()
    version = pl.version()
    stats = pl.statistics()
    return {"version": version, "documents_total": stats.get("documents_total"),
            "latency_ms": int((time.time() - t0) * 1000)}


@app.get("/api/instances")
def instances(_=Depends(require_login)):
    return db.list_instances()


@app.post("/api/instances/test")
def test_instance(body: InstanceIn, id: int = 0, _=Depends(require_login)):
    token = body.token
    if not token and id:
        token = db.get_instance(id, with_token=True)["token"]
    try:
        return {"ok": True, **_probe(body.url, token, body.verify_tls)}
    except Exception as e:  # noqa
        return {"ok": False, "error": friendly(e)}


@app.post("/api/instances")
def add_instance(body: InstanceIn, _=Depends(require_login)):
    if not body.token:
        raise HTTPException(400, "token wajib diisi")
    try:
        return {"id": db.save_instance(body.name, body.url, body.token, body.verify_tls, enabled=body.enabled)}
    except Exception as e:  # noqa
        raise HTTPException(400, str(e))


@app.put("/api/instances/{iid}")
def edit_instance(iid: int, body: InstanceIn, _=Depends(require_login)):
    db.save_instance(body.name, body.url, body.token, body.verify_tls, iid=iid, enabled=body.enabled)
    return {"id": iid}


class EnabledIn(BaseModel):
    enabled: bool


@app.post("/api/instances/{iid}/enabled")
def toggle_instance(iid: int, body: EnabledIn, _=Depends(require_login)):
    db.set_enabled(iid, body.enabled)
    return {"id": iid, "enabled": body.enabled}


@app.delete("/api/instances/{iid}")
def del_instance(iid: int, _=Depends(require_login)):
    db.delete_instance(iid)
    return {"ok": True}


@app.get("/api/instances/{iid}/meta")
def instance_meta(iid: int, _=Depends(require_login)):
    pl = client(iid)
    tags = [{"name": t["name"], "count": t.get("document_count", 0)} for t in pl.all("/api/tags/")]
    sps = [{"name": s["name"], "count": s.get("document_count", 0), "path": s.get("path")}
           for s in pl.all("/api/storage_paths/")]
    return {"tags": sorted(tags, key=lambda x: x["name"].lower()),
            "storage_paths": sorted(sps, key=lambda x: x["name"].lower())}


# ------------------------------------------------------------------ dasbor
def _status(inst):
    out = {"id": inst["id"], "name": inst["name"], "url": inst["url"]}
    try:
        pl = client(inst["id"], timeout=15)
        t0 = time.time()
        out["version"] = pl.version()
        st = pl.statistics()
        out["latency_ms"] = int((time.time() - t0) * 1000)
        out["documents_total"] = st.get("documents_total")
        out["documents_inbox"] = st.get("documents_inbox")
        out["file_types"] = st.get("document_file_type_counts") or []
        try:
            out["failed_tasks"] = len(pl.tasks(only_failed=True))
        except Exception:  # noqa
            out["failed_tasks"] = None
        out["online"] = True
    except Exception as e:  # noqa
        out["online"] = False
        out["error"] = friendly(e)
    return out


@app.get("/api/dashboard")
def dashboard(_=Depends(require_login)):
    return list(pool.map(_status, db.active_instances()))


# ------------------------------------------------------------------ pencarian
def _search_one(inst, q, size):
    res = {"id": inst["id"], "name": inst["name"], "url": inst["url"], "results": [], "count": 0}
    try:
        pl = client(inst["id"], timeout=20)
        data = pl.get("/api/documents/", params={"query": q, "page_size": size}).json()
        res["count"] = data.get("count", 0)
        for d in data.get("results", []):
            hit = d.get("__search_hit__") or {}
            res["results"].append({
                "id": d["id"], "title": d["title"], "created": (d.get("created") or "")[:10],
                "file": d.get("original_file_name"), "link": pl.doc_url(d["id"]),
                "highlights": hit.get("highlights") or (d.get("content") or "")[:200],
                "score": hit.get("score")})
    except Exception as e:  # noqa
        res["error"] = friendly(e)
    return res


@app.get("/api/search")
def search(q: str, instances: str = "", size: int = 10, _=Depends(require_login)):
    q = q.strip()
    if not q:
        return []
    ids = {int(i) for i in instances.split(",") if i.strip().isdigit()}
    targets = [i for i in db.active_instances() if not ids or i["id"] in ids]
    size = max(1, min(size, 50))
    return list(pool.map(lambda i: _search_one(i, q, size), targets))


@app.get("/api/instances/{iid}/thumb/{doc_id}")
def thumb(iid: int, doc_id: int, _=Depends(require_login)):
    pl = client(iid)
    try:
        r = pl.get(f"/api/documents/{doc_id}/thumb/", timeout=20)
    except Exception:  # noqa
        raise HTTPException(404)
    return Response(r.content, media_type=r.headers.get("content-type", "image/webp"),
                    headers={"Cache-Control": "private, max-age=3600"})


# ------------------------------------------------------------------ task & cek file
@app.get("/api/instances/{iid}/tasks")
def instance_tasks(iid: int, failed: bool = True, _=Depends(require_login)):
    pl = client(iid)
    out = []
    for t in pl.tasks(only_failed=failed):
        out.append({"id": t.get("id"), "file": t.get("task_file_name"), "status": t.get("status"),
                    "created": (t.get("date_created") or "")[:19].replace("T", " "),
                    "result": (t.get("result") or "")[:500], "related_document": t.get("related_document")})
    return sorted(out, key=lambda x: x["created"], reverse=True)


class AckIn(BaseModel):
    ids: list[int]


@app.post("/api/instances/{iid}/tasks/ack")
def ack_tasks(iid: int, body: AckIn, _=Depends(require_login)):
    client(iid).acknowledge_tasks(body.ids)
    return {"ok": True}


class CheckIn(BaseModel):
    names: list[str]
    instances: list[int] = []


def _check_instance(inst, names):
    out = {"id": inst["id"], "name": inst["name"], "files": {}}
    try:
        pl = client(inst["id"], timeout=30)
        trash = pl.trash()
        tmap = {}
        for d in trash or []:
            tmap.setdefault((d.get("original_file_name") or "").lower(), []).append(d)
        for n in names:
            found = []
            for d in pl.find_by_filename(n):
                found.append({"id": d["id"], "title": d["title"], "link": pl.doc_url(d["id"]),
                              "checksum": pl.checksum(d["id"])})
            out["files"][n] = {"docs": found,
                               "trash": [{"id": d["id"], "title": d["title"]} for d in tmap.get(n.lower(), [])],
                               "trash_supported": trash is not None}
    except Exception as e:  # noqa
        out["error"] = friendly(e)
    return out


@app.post("/api/checkfiles")
def checkfiles(body: CheckIn, _=Depends(require_login)):
    names = [n.strip() for n in body.names if n.strip()][:300]
    targets = [i for i in db.active_instances() if not body.instances or i["id"] in body.instances]
    per_inst = list(pool.map(lambda i: _check_instance(i, names), targets))
    rows = []
    for n in names:
        row = {"name": n, "instances": []}
        sums = set()
        for r in per_inst:
            f = r.get("files", {}).get(n, {})
            for d in f.get("docs", []):
                if d.get("checksum"):
                    sums.add(d["checksum"])
            row["instances"].append({"id": r["id"], "name": r["name"], "error": r.get("error"),
                                     "docs": f.get("docs", []), "trash": f.get("trash", [])})
        row["found_in"] = sum(1 for i in row["instances"] if i["docs"])
        row["identical"] = len(sums) <= 1
        rows.append(row)
    return rows


# ------------------------------------------------------------------ jobs
class JobIn(BaseModel):
    kind: str = Field(pattern="^(migrate|delete)$")
    src: int
    dst: int
    filter: dict
    options: dict = {}


@app.post("/api/jobs")
def create_job(body: JobIn, _=Depends(require_login)):
    if body.src == body.dst:
        raise HTTPException(400, "sumber dan tujuan tidak boleh sama")
    a, b = db.get_instance(body.src), db.get_instance(body.dst)
    if not a or not b:
        raise HTTPException(404, "instance sumber/tujuan tidak ada")
    if a["url"].rstrip("/").lower() == b["url"].rstrip("/").lower():
        raise HTTPException(400, "Sumber dan tujuan adalah Paperless yang sama (URL sama, hanya beda user). "
                                 "Migrasi di dalam satu Paperless tidak diperlukan; atur izin/pemilik dokumen di Paperless.")
    if body.kind == "delete" and not body.options.get("dry_run") and body.options.get("confirm") != "HAPUS":
        raise HTTPException(400, "konfirmasi HAPUS wajib untuk penghapusan")
    jid = jobs.submit(body.kind, body.src, body.dst, {"filter": body.filter, "options": body.options})
    return {"id": jid}


@app.get("/api/jobs")
def list_jobs(_=Depends(require_login)):
    names = {i["id"]: i["name"] for i in db.list_instances()}
    out = db.list_jobs()
    for j in out:
        j["src_name"], j["dst_name"] = names.get(j["src_instance"], "?"), names.get(j["dst_instance"], "?")
    return out


@app.get("/api/jobs/{jid}")
def get_job(jid: int, _=Depends(require_login)):
    j = db.get_job(jid)
    if not j:
        raise HTTPException(404)
    names = {i["id"]: (i["name"], i["url"]) for i in db.list_instances()}
    j["src_name"], j["src_url"] = names.get(j["src_instance"], ("?", ""))
    j["dst_name"], j["dst_url"] = names.get(j["dst_instance"], ("?", ""))
    return j


@app.get("/api/jobs/{jid}/items")
def job_items(jid: int, after: int = 0, status: str = "", _=Depends(require_login)):
    return db.list_items(jid, status or None, after)


@app.get("/api/jobs/{jid}/csv")
def job_csv(jid: int, _=Depends(require_login)):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["waktu", "src_id", "judul", "status", "dst_id", "pesan"])
    for it in db.all_items(jid):
        w.writerow([it["ts"], it["src_doc"] or "", it["title"] or "", it["status"], it["dst_doc"] or "", it["msg"]])
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="job-{jid}.csv"'})


@app.post("/api/jobs/{jid}/cancel")
def cancel_job(jid: int, _=Depends(require_login)):
    return {"ok": jobs.cancel(jid)}


class ImportIn(BaseModel):
    src: int
    dst: int
    state: dict


@app.post("/api/import-state")
def import_state(body: ImportIn, _=Depends(require_login)):
    clean = {str(k): int(v) for k, v in body.state.items() if str(k).isdigit()}
    return {"imported": db.import_mappings(body.src, body.dst, clean)}


@app.get("/api/mappings/count")
def mapping_count(src: int, dst: int, _=Depends(require_login)):
    return {"count": db.count_mappings(src, dst)}


# ------------------------------------------------------------------ halaman
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/healthz")
def healthz():
    return {"ok": True}
