#!/usr/bin/env python3
"""
kb-data — serverseitiger Tabellen-Proxy mit Cache für den Docmost-`databaseTable`-Block.

Der Block ruft (same-origin über Caddy) `GET /kb-table?source=…&src=…` auf; dieser Dienst holt
die Daten **server-zu-server** über das interne Docker-Netz (Browser sieht Baserow/NocoDB nie),
authentifiziert als Worker-Account, normalisiert sie und cached sie auf Disk.

Stale-while-revalidate: gecachter Stand wird sofort geliefert, Refresh läuft im Hintergrund.
=> Kein leeres Aufflackern, immer der zuletzt bekannte Stand, kein CORS, keine Public-Views nötig.

Nur Python-Standardbibliothek.
"""
import os, re, json, time, threading, urllib.parse, urllib.request, urllib.error
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

IP        = os.environ.get("SERVER_IP", "")
PORT      = int(os.environ.get("PORT", "8090"))
CACHE_DIR = os.environ.get("CACHE_DIR", "/cache")
TTL       = int(os.environ.get("CACHE_TTL", "60"))          # Sekunden bis "stale"
SIZE      = int(os.environ.get("ROW_LIMIT", "200"))

BW_URL    = os.environ.get("BASEROW_URL", "http://baserow")
BW_HOST   = os.environ.get("BASEROW_HOST", f"{IP}:8888")    # Baserow prüft den Host strikt!
BW_EMAIL  = os.environ.get("BASEROW_EMAIL", "")
BW_PASS   = os.environ.get("BASEROW_PASSWORD", "")
BW_PUB    = os.environ.get("BASEROW_PUBLIC", f"http://{IP}:8888")

NC_URL    = os.environ.get("NOCODB_URL", "http://nocodb:8080")
NC_HOST   = os.environ.get("NOCODB_HOST", f"{IP}:8089")
NC_EMAIL  = os.environ.get("NOCODB_EMAIL", "")
NC_PASS   = os.environ.get("NOCODB_PASSWORD", "")
NC_PUB    = os.environ.get("NOCODB_PUBLIC", f"http://{IP}:8089")

os.makedirs(CACHE_DIR, exist_ok=True)

# ----------------------------- Wert-Formatierung (spiegelt fmt() im Client) ----------------
def fmt(v):
    if v is None: return ""
    if isinstance(v, bool): return "✓" if v else "—"
    if isinstance(v, list): return ", ".join(fmt(x) for x in v)
    if isinstance(v, dict):
        if "value" in v: return "" if v["value"] is None else str(v["value"])
        if "title" in v: return "" if v["title"] is None else str(v["title"])
        return ""
    return str(v)

# ----------------------------- HTTP-Helfer -------------------------------------------------
def http(url, host=None, headers=None, data=None, method=None, timeout=30):
    h = dict(headers or {})
    if host: h["Host"] = host
    body = None
    if data is not None:
        body = json.dumps(data).encode(); h["Content-Type"] = "application/json"
    rq = urllib.request.Request(url, body, h, method=method)
    try:
        raw = urllib.request.urlopen(rq, timeout=timeout).read().decode()
        return json.loads(raw) if raw.strip() else None
    except urllib.error.HTTPError as e:
        e.body = e.read().decode(errors="replace")[:300]
        raise

# ----------------------------- Auth-Caches (Worker-Token) ----------------------------------
_bw = {"jwt": None, "refresh": None}
_nc = {"xc": None}
_authlock = threading.Lock()

def bw_login():
    r = http(f"{BW_URL}/api/user/token-auth/", BW_HOST, data={"email": BW_EMAIL, "password": BW_PASS})
    _bw["jwt"], _bw["refresh"] = r["access_token"], r.get("refresh_token")
    return _bw["jwt"]

def bw_jwt():
    return _bw["jwt"] or bw_login()

def bw_call(path):
    """GET gegen Baserow als Worker; bei 401 Refresh, dann Re-Login."""
    for attempt in range(3):
        try:
            return http(f"{BW_URL}{path}", BW_HOST, headers={"Authorization": f"JWT {bw_jwt()}"})
        except urllib.error.HTTPError as e:
            if e.code != 401 or attempt == 2: raise
            with _authlock:
                try:
                    if _bw["refresh"]:
                        r = http(f"{BW_URL}/api/user/token-refresh/", BW_HOST, data={"refresh_token": _bw["refresh"]})
                        _bw["jwt"] = r["access_token"]; continue
                except Exception: pass
                bw_login()

def nc_login():
    r = http(f"{NC_URL}/api/v1/auth/user/signin", NC_HOST, data={"email": NC_EMAIL, "password": NC_PASS})
    _nc["xc"] = r["token"]; return _nc["xc"]

def nc_xc():
    return _nc["xc"] or nc_login()

def nc_call(path):
    for attempt in range(2):
        try:
            return http(f"{NC_URL}{path}", NC_HOST, headers={"xc-auth": nc_xc()})
        except urllib.error.HTTPError as e:
            if e.code not in (401, 403) or attempt == 1: raise
            with _authlock: nc_login()

# ----------------------------- ID-Parsing aus der src-URL ----------------------------------
def parse_baserow(src):
    m = re.search(r"/database/(\d+)/table/(\d+)(?:/(\d+))?", src)
    if m: return {"db": m.group(1), "table": m.group(2), "view": m.group(3)}
    s = re.search(r"/public/grid/([^/?#]+)", src)
    if s: return {"public": s.group(1)}
    raise ValueError("Baserow: tableId/Slug nicht erkannt")

def parse_nocodb(src):
    m = re.search(r"#/nc/[^/]+/(m[A-Za-z0-9]+)", src) or re.search(r"\b(m[A-Za-z0-9]{12,})\b", src)
    if m: return {"table": m.group(1)}
    s = re.search(r"/(?:nc/view|shared-view)/([^/?#]+)", src)
    if s: return {"public": s.group(1)}
    raise ValueError("NocoDB: tableId/UUID nicht erkannt")

# ----------------------------- Baserow: authentifiziert + Public-Fallback ------------------
def fetch_baserow(src):
    ids = parse_baserow(src)
    if "public" in ids:
        slug = ids["public"]
        info = http(f"{BW_URL}/api/database/views/{slug}/public/info/", BW_HOST)
        fields = [{"id": f["id"], "name": f["name"]} for f in info.get("fields", [])]
        data = http(f"{BW_URL}/api/database/views/grid/{slug}/public/rows/?size={SIZE}", BW_HOST)
        results = data.get("results", [])
        cols = [f["name"] for f in fields]
        rows = [[fmt(r.get(f"field_{f['id']}")) for f in fields] for r in results]
        gb = [g.get("field") for g in (info.get("view", {}).get("group_bys") or [])]
        return {"columns": cols, "rows": rows, "count": len(results),
                "title": info.get("view", {}).get("name", ""), "editUrl": BW_PUB,
                "fieldIds": [f["id"] for f in fields], "groupByIds": gb}
    tid = ids["table"]
    fields = bw_call(f"/api/database/fields/table/{tid}/")
    cols = [f["name"] for f in fields]; fids = [f["id"] for f in fields]
    data = bw_call(f"/api/database/rows/table/{tid}/?user_field_names=true&size={SIZE}")
    results = data.get("results", [])
    rows = [[fmt(r.get(name)) for name in cols] for r in results]
    title = ""
    try: title = (bw_call(f"/api/database/tables/{tid}/") or {}).get("name", "")
    except Exception: pass
    gb = []
    if ids.get("view"):
        try:
            v = bw_call(f"/api/database/views/{ids['view']}/?include=group_bys")
            gb = [g.get("field") for g in (v.get("group_bys") or [])]
        except Exception: pass
    edit = f"{BW_PUB}/database/{ids['db']}/table/{tid}/{ids.get('view') or ''}".rstrip("/")
    return {"columns": cols, "rows": rows, "count": len(results), "title": title,
            "editUrl": edit, "fieldIds": fids, "groupByIds": gb}

# ----------------------------- NocoDB: authentifiziert + Public-Fallback -------------------
NC_SYS = {"ID", "CreatedTime", "LastModifiedTime", "CreatedBy", "LastModifiedBy", "Order", "Deleted", "Meta"}

def fetch_nocodb(src):
    ids = parse_nocodb(src)
    if "public" in ids:
        uuid = ids["public"]
        meta = http(f"{NC_URL}/api/v2/public/shared-view/{uuid}/meta", NC_HOST)
        data = http(f"{NC_URL}/api/v2/public/shared-view/{uuid}/rows", NC_HOST)
        lst = data.get("list") or data.get("rows") or (data if isinstance(data, list) else [])
        cols = [c["title"] for c in (meta.get("columns") or []) if c.get("title") and c.get("uidt") not in NC_SYS and c.get("show") is not False]
        if not cols and lst: cols = [k for k in lst[0].keys() if k not in NC_SYS]
        rows = [[fmt(r.get(c)) for c in cols] for r in lst]
        return {"columns": cols, "rows": rows, "count": len(lst), "title": meta.get("title", ""),
                "editUrl": NC_PUB, "fieldIds": None, "groupByIds": None}
    tid = ids["table"]
    meta = nc_call(f"/api/v2/meta/tables/{tid}")
    cols = [c["title"] for c in (meta.get("columns") or []) if c.get("title") and c.get("uidt") not in NC_SYS]
    data = nc_call(f"/api/v2/tables/{tid}/records?limit={SIZE}")
    lst = data.get("list", [])
    rows = [[fmt(r.get(c)) for c in cols] for r in lst]
    base = meta.get("base_id") or meta.get("source_id") or ""
    edit = f"{NC_PUB}/dashboard/#/nc/{base}/{tid}" if base else NC_PUB
    return {"columns": cols, "rows": rows, "count": len(lst), "title": meta.get("title", ""),
            "editUrl": edit, "fieldIds": None, "groupByIds": None}

def fetch(source, src):
    if source == "baserow": return fetch_baserow(src)
    if source == "nocodb":  return fetch_nocodb(src)
    raise ValueError(f"Unbekannte Quelle: {source}")

# ----------------------------- Cache (Memory + Disk) + stale-while-revalidate --------------
_mem = {}            # key -> {"data":…, "ts":…}
_inflight = set()
_clock = threading.Lock()

def _ckey(source, src): return source + ":" + re.sub(r"[^A-Za-z0-9]+", "_", src)[:160]
def _cpath(key): return os.path.join(CACHE_DIR, key + ".json")

def cache_get(key):
    if key in _mem: return _mem[key]
    try:
        with open(_cpath(key)) as f: e = json.load(f); _mem[key] = e; return e
    except Exception: return None

def cache_put(key, data):
    e = {"data": data, "ts": time.time()}
    _mem[key] = e
    try:
        tmp = _cpath(key) + ".tmp"
        with open(tmp, "w") as f: json.dump(e, f)
        os.replace(tmp, _cpath(key))
    except Exception: pass
    return e

def refresh(source, src, key):
    """Holt frisch und schreibt in den Cache; dedupliziert parallele Refreshes."""
    with _clock:
        if key in _inflight: return
        _inflight.add(key)
    try:
        cache_put(key, fetch(source, src))
    except Exception as ex:
        print(f"[refresh] {key}: {type(ex).__name__}: {ex}", flush=True)
    finally:
        with _clock: _inflight.discard(key)

def serve(source, src, force):
    key = _ckey(source, src)
    ent = cache_get(key)
    now = time.time()
    if force or ent is None:
        try:
            ent = cache_put(key, fetch(source, src))
            return {**ent["data"], "fetchedAt": int(ent["ts"] * 1000), "stale": False}
        except Exception as ex:
            if ent is None:
                msg = getattr(ex, "body", "") or str(ex)
                return {"error": f"{type(ex).__name__}: {msg}"[:300]}
            # Fetch fehlgeschlagen, aber alter Stand vorhanden -> stale liefern
            return {**ent["data"], "fetchedAt": int(ent["ts"] * 1000), "stale": True}
    stale = (now - ent["ts"]) > TTL
    if stale:
        threading.Thread(target=refresh, args=(source, src, key), daemon=True).start()
    return {**ent["data"], "fetchedAt": int(ent["ts"] * 1000), "stale": stale}

# ----------------------------- HTTP-Server -------------------------------------------------
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        u = urllib.parse.urlparse(self.path); q = urllib.parse.parse_qs(u.query)
        if u.path.rstrip("/").endswith("/health"):
            return self._json({"ok": True})
        if u.path.rstrip("/").endswith("/kb-table"):
            source = (q.get("source", [""])[0]).strip()
            src = (q.get("src", [""])[0]).strip()
            force = q.get("refresh", ["0"])[0] in ("1", "true")
            if not src or not source:
                return self._json({"error": "source und src erforderlich"}, 400)
            try:
                return self._json(serve(source, src, force))
            except Exception as ex:
                return self._json({"error": f"{type(ex).__name__}: {ex}"[:300]}, 200)
        self._json({"error": "not found"}, 404)

if __name__ == "__main__":
    print(f"kb-data auf :{PORT} (cache {CACHE_DIR}, ttl {TTL}s)", flush=True)
    ThreadingHTTPServer(("", PORT), H).serve_forever()
