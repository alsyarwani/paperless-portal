"""Penyimpanan SQLite: instance Paperless, mapping dokumen, job, dan log item job."""
import base64
import hashlib
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime

from cryptography.fernet import Fernet, InvalidToken

DATA_DIR = os.environ.get("PORTAL_DATA", "/data")
DB_PATH = os.path.join(DATA_DIR, "portal.db")
_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS instances (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    url TEXT NOT NULL,
    token_enc TEXT NOT NULL,
    verify_tls INTEGER NOT NULL DEFAULT 1,
    created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mappings (
    src_instance INTEGER NOT NULL,
    src_doc INTEGER NOT NULL,
    dst_instance INTEGER NOT NULL,
    dst_doc INTEGER NOT NULL,
    created TEXT NOT NULL,
    PRIMARY KEY (src_instance, src_doc, dst_instance)
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,              -- migrate | delete
    src_instance INTEGER NOT NULL,
    dst_instance INTEGER NOT NULL,
    params TEXT NOT NULL,
    status TEXT NOT NULL,            -- antri | berjalan | selesai | gagal | dibatalkan | terhenti
    total INTEGER DEFAULT 0,
    done INTEGER DEFAULT 0,
    ok INTEGER DEFAULT 0,
    dup INTEGER DEFAULT 0,
    fail INTEGER DEFAULT 0,
    skipped INTEGER DEFAULT 0,
    deleted INTEGER DEFAULT 0,
    message TEXT DEFAULT '',
    created TEXT NOT NULL,
    started TEXT,
    finished TEXT
);
CREATE TABLE IF NOT EXISTS job_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL,
    src_doc INTEGER,
    title TEXT,
    status TEXT,
    dst_doc INTEGER,
    msg TEXT,
    ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_items_job ON job_items(job_id, id);
"""


def now():
    return datetime.now().isoformat(timespec="seconds")


def _fernet():
    secret = os.environ.get("SECRET_KEY", "")
    if len(secret) < 16:
        raise RuntimeError("SECRET_KEY belum di-set atau terlalu pendek (min. 16 karakter)")
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    return Fernet(key)


def encrypt(text):
    return _fernet().encrypt(text.encode()).decode()


def decrypt(text):
    try:
        return _fernet().decrypt(text.encode()).decode()
    except InvalidToken:
        raise RuntimeError("Token tidak bisa didekripsi: SECRET_KEY berubah? Isi ulang token instance.")


@contextmanager
def conn():
    os.makedirs(DATA_DIR, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    c.row_factory = sqlite3.Row
    try:
        with _lock:
            yield c
            c.commit()
    finally:
        c.close()


def init():
    with conn() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(SCHEMA)
        # job yang masih berjalan saat container mati -> tandai terhenti
        c.execute("UPDATE jobs SET status='terhenti', finished=? WHERE status IN ('berjalan','antri')",
                  (now(),))


# ------------------------------------------------------------------ instances
def list_instances():
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT id, name, url, verify_tls, created FROM instances ORDER BY name")]


def get_instance(iid, with_token=False):
    with conn() as c:
        r = c.execute("SELECT * FROM instances WHERE id=?", (iid,)).fetchone()
    if not r:
        return None
    d = dict(r)
    tok = d.pop("token_enc")
    if with_token:
        d["token"] = decrypt(tok)
    return d


def save_instance(name, url, token, verify_tls=True, iid=None):
    with conn() as c:
        if iid:
            if token:
                c.execute("UPDATE instances SET name=?, url=?, token_enc=?, verify_tls=? WHERE id=?",
                          (name, url.rstrip("/"), encrypt(token), int(verify_tls), iid))
            else:
                c.execute("UPDATE instances SET name=?, url=?, verify_tls=? WHERE id=?",
                          (name, url.rstrip("/"), int(verify_tls), iid))
            return iid
        cur = c.execute("INSERT INTO instances(name,url,token_enc,verify_tls,created) VALUES(?,?,?,?,?)",
                        (name, url.rstrip("/"), encrypt(token), int(verify_tls), now()))
        return cur.lastrowid


def delete_instance(iid):
    with conn() as c:
        c.execute("DELETE FROM instances WHERE id=?", (iid,))


# ------------------------------------------------------------------ mappings
def get_mapping(src_i, dst_i):
    with conn() as c:
        return {str(r["src_doc"]): r["dst_doc"] for r in c.execute(
            "SELECT src_doc, dst_doc FROM mappings WHERE src_instance=? AND dst_instance=?", (src_i, dst_i))}


def add_mapping(src_i, src_doc, dst_i, dst_doc):
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO mappings VALUES(?,?,?,?,?)",
                  (src_i, int(src_doc), dst_i, int(dst_doc), now()))


def import_mappings(src_i, dst_i, mapping: dict):
    with conn() as c:
        c.executemany("INSERT OR REPLACE INTO mappings VALUES(?,?,?,?,?)",
                      [(src_i, int(k), dst_i, int(v), now()) for k, v in mapping.items()])
    return len(mapping)


def count_mappings(src_i, dst_i):
    with conn() as c:
        return c.execute("SELECT COUNT(*) FROM mappings WHERE src_instance=? AND dst_instance=?",
                         (src_i, dst_i)).fetchone()[0]


# ------------------------------------------------------------------ jobs
def create_job(kind, src_i, dst_i, params):
    with conn() as c:
        cur = c.execute("INSERT INTO jobs(kind,src_instance,dst_instance,params,status,created) "
                        "VALUES(?,?,?,?, 'antri', ?)", (kind, src_i, dst_i, json.dumps(params), now()))
        return cur.lastrowid


def update_job(jid, **fields):
    if not fields:
        return
    keys = ", ".join(f"{k}=?" for k in fields)
    with conn() as c:
        c.execute(f"UPDATE jobs SET {keys} WHERE id=?", (*fields.values(), jid))


def bump_job(jid, **inc):
    keys = ", ".join(f"{k}={k}+?" for k in inc)
    with conn() as c:
        c.execute(f"UPDATE jobs SET {keys} WHERE id=?", (*inc.values(), jid))


def get_job(jid):
    with conn() as c:
        r = c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["params"] = json.loads(d["params"])
    return d


def list_jobs(limit=50):
    with conn() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,))]
    for d in rows:
        d["params"] = json.loads(d["params"])
    return rows


def add_item(jid, src_doc, title, status, dst_doc=None, msg=""):
    with conn() as c:
        c.execute("INSERT INTO job_items(job_id,src_doc,title,status,dst_doc,msg,ts) VALUES(?,?,?,?,?,?,?)",
                  (jid, src_doc, title, status, dst_doc, str(msg)[:1000], now()))


def list_items(jid, status=None, after_id=0, limit=500):
    q = "SELECT * FROM job_items WHERE job_id=? AND id>?"
    args = [jid, after_id]
    if status:
        q += " AND status=?"
        args.append(status)
    q += " ORDER BY id LIMIT ?"
    args.append(limit)
    with conn() as c:
        return [dict(r) for r in c.execute(q, args)]


def all_items(jid):
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM job_items WHERE job_id=? ORDER BY id", (jid,))]
