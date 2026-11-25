"""Antrian job latar belakang (satu worker, supaya Paperless tidak kebanjiran)."""
import queue
import threading
import traceback

from . import db
from .engine import run_delete, run_migrate
from .paperless import Paperless

_q = queue.Queue()
_cancel = {}
_worker = None


def client_for(iid, timeout=60):
    inst = db.get_instance(iid, with_token=True)
    if not inst:
        raise ValueError(f"instance {iid} tidak ada")
    return Paperless(inst["url"], inst["token"], inst["name"], bool(inst["verify_tls"]), timeout)


class JobContext:
    def __init__(self, job):
        self.job = job
        self.jid = job["id"]

    def cancelled(self):
        ev = _cancel.get(self.jid)
        return bool(ev and ev.is_set())

    def set(self, **fields):
        db.update_job(self.jid, **fields)

    def bump(self, **inc):
        db.bump_job(self.jid, **inc)

    def item(self, src_doc, title, status, dst_doc=None, msg=""):
        db.add_item(self.jid, src_doc, title, status, dst_doc, msg)

    def log(self, msg):
        db.add_item(self.jid, None, None, "INFO", None, msg)

    def map(self, src_doc, dst_doc):
        db.add_mapping(self.job["src_instance"], src_doc, self.job["dst_instance"], dst_doc)


def _run(jid):
    job = db.get_job(jid)
    ctx = JobContext(job)
    db.update_job(jid, status="berjalan", started=db.now())
    try:
        src = client_for(job["src_instance"])
        dst = client_for(job["dst_instance"])
        mapping = db.get_mapping(job["src_instance"], job["dst_instance"])
        fn = run_migrate if job["kind"] == "migrate" else run_delete
        result = fn(ctx, src, dst, mapping, job["params"])
        db.update_job(jid, status=result, finished=db.now())
    except Exception as e:  # noqa
        ctx.log(f"ERROR: {e}")
        traceback.print_exc()
        db.update_job(jid, status="gagal", message=str(e)[:500], finished=db.now())
    finally:
        _cancel.pop(jid, None)


def _loop():
    while True:
        jid = _q.get()
        try:
            ev = _cancel.get(jid)
            if ev and ev.is_set():
                db.update_job(jid, status="dibatalkan", finished=db.now())
                continue
            _run(jid)
        finally:
            _q.task_done()


def start():
    global _worker
    if _worker is None:
        _worker = threading.Thread(target=_loop, daemon=True, name="job-worker")
        _worker.start()


def submit(kind, src_i, dst_i, params):
    jid = db.create_job(kind, src_i, dst_i, params)
    _cancel[jid] = threading.Event()
    _q.put(jid)
    return jid


def cancel(jid):
    ev = _cancel.get(jid)
    if ev:
        ev.set()
        return True
    return False
