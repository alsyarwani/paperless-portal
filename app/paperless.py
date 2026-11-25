"""Klien REST API Paperless-ngx (sinkron, berbasis requests)."""
import requests

PAGE_SIZE = 100
MAX_API_VERSION = 9  # versi API tertinggi yang dipakai portal; diturunkan otomatis untuk Paperless lama


class PaperlessError(RuntimeError):
    pass


class Paperless:
    def __init__(self, url, token, name="paperless", verify_tls=True, timeout=60):
        self.base = url.rstrip("/")
        self.name = name
        self.timeout = timeout
        self.s = requests.Session()
        self.s.verify = verify_tls
        self.s.headers.update({"Authorization": f"Token {token}", "Accept": "application/json"})
        self.api_version = None
        self.server_version = "?"
        self._negotiated = False

    # ------------------------------------------------------------ dasar
    def _url(self, path):
        return path if path.startswith("http") else f"{self.base}{path}"

    def _negotiate(self):
        """Tanya versi API tertinggi yang didukung server (header X-Api-Version), lalu pakai
        min(server, MAX_API_VERSION). Paperless lama membalas 406 kalau diminta versi terlalu tinggi."""
        if self._negotiated:
            return
        # bukan /api/: di Paperless-ngx 3.x root /api/ membalas HTML dan menolak Accept JSON (406)
        r = self.s.get(self._url("/api/statistics/"), timeout=min(self.timeout, 20))
        self._check(r, "GET", "/api/statistics/")
        self.server_version = r.headers.get("X-Version") or "?"
        server_max = r.headers.get("X-Api-Version")
        try:
            v = min(int(server_max), MAX_API_VERSION) if server_max else None
        except ValueError:
            v = None
        if v:
            self.s.headers["Accept"] = f"application/json; version={v}"
        self.api_version = v
        self._negotiated = True

    def _check(self, r, method, path):
        if r.status_code >= 400:
            raise PaperlessError(f"[{self.name}] {method} {path} -> {r.status_code}: {r.text[:300]}")
        return r

    def get(self, path, **kw):
        self._negotiate()
        kw.setdefault("timeout", self.timeout)
        return self._check(self.s.get(self._url(path), **kw), "GET", path)

    def post(self, path, **kw):
        self._negotiate()
        kw.setdefault("timeout", 300)
        return self._check(self.s.post(self._url(path), **kw), "POST", path)

    def patch(self, path, **kw):
        self._negotiate()
        kw.setdefault("timeout", self.timeout)
        return self._check(self.s.patch(self._url(path), **kw), "PATCH", path)

    def all(self, path, params=None):
        params = dict(params or {})
        params.setdefault("page_size", PAGE_SIZE)
        url = path
        while url:
            data = self.get(url, params=params).json()
            params = None  # URL 'next' sudah membawa query string
            yield from data.get("results", [])
            url = data.get("next")
            if url and url.startswith("http://") and self.base.startswith("https://"):
                url = "https://" + url[len("http://"):]

    # ------------------------------------------------------------ bantuan
    def doc_url(self, doc_id):
        return f"{self.base}/documents/{doc_id}/details"

    def version(self):
        self._negotiate()
        return self.server_version

    def statistics(self):
        return self.get("/api/statistics/", timeout=20).json()

    def tasks(self, only_failed=False):
        data = self.get("/api/tasks/", params={"acknowledged": "false"}, timeout=30).json()
        if isinstance(data, dict):
            data = data.get("results", [])
        if only_failed:
            data = [t for t in data if t.get("status") == "FAILURE" and not t.get("acknowledged")]
        return data

    def acknowledge_tasks(self, ids):
        return self.post("/api/tasks/acknowledge/", json={"tasks": ids}).json()

    def checksum(self, doc_id):
        return self.get(f"/api/documents/{doc_id}/metadata/").json().get("original_checksum")

    def find_by_checksum(self, checksum):
        if not checksum:
            return None
        res = self.get("/api/documents/", params={"checksum__iexact": checksum, "page_size": 1}).json()
        hits = res.get("results", [])
        return hits[0]["id"] if hits else None

    def find_by_filename(self, name):
        res = self.get("/api/documents/", params={"original_filename__iexact": name,
                                                  "page_size": 25}).json()
        return res.get("results", [])

    def trash(self):
        """Daftar dokumen di Trash (Paperless-ngx >= 2.10). None jika tidak didukung."""
        try:
            self._negotiate()
            r = self.s.get(self._url("/api/trash/"), params={"page_size": 100}, timeout=self.timeout)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            out, data = [], r.json()
            out += data.get("results", [])
            nxt = data.get("next")
            while nxt:
                data = self.get(nxt).json()
                out += data.get("results", [])
                nxt = data.get("next")
            return out
        except requests.RequestException:
            return None

    def bulk_edit(self, doc_ids, method, parameters=None):
        return self.post("/api/documents/bulk_edit/", json={
            "documents": list(doc_ids), "method": method, "parameters": parameters or {}})

    def add_tag(self, doc_id, tag_id):
        if tag_id:
            self.bulk_edit([doc_id], "add_tag", {"tag": tag_id})

    def remove_tag(self, doc_id, tag_id):
        if tag_id:
            self.bulk_edit([doc_id], "remove_tag", {"tag": tag_id})

    def ensure_tag(self, name, cache=None):
        """Cari tag (case-insensitive) atau buat tanpa auto-matching. Return id."""
        if not name:
            return None
        objs = cache.values() if cache is not None else self.all("/api/tags/")
        for o in objs:
            if o["name"].lower() == name.lower():
                return o["id"]
        t = self.post("/api/tags/", json={"name": name, "matching_algorithm": 0}).json()
        if cache is not None:
            cache[t["id"]] = t
        return t["id"]

    def wait_task(self, task_id, timeout=1800, should_stop=None, poll=3):
        import time
        t0 = time.time()
        while time.time() - t0 < timeout:
            if should_stop and should_stop():
                return None, "dibatalkan saat menunggu consume"
            res = self.get("/api/tasks/", params={"task_id": task_id}).json()
            if isinstance(res, dict):
                res = res.get("results", [])
            res = [t for t in res if t.get("task_id") in (None, task_id)] or res
            if res:
                t = res[0]
                if t["status"] == "SUCCESS" and t.get("related_document"):
                    return int(t["related_document"]), None
                if t["status"] in ("FAILURE", "REVOKED"):
                    return None, t.get("result")
            time.sleep(poll)
        return None, "timeout menunggu consume"
