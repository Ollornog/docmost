#!/usr/bin/env python3
"""
kb-sync v2 (self-service): hält native Docmost-Tabellen synchron mit Baserow-Tabellen.
Steuerung über eine Baserow-Mapping-Tabelle "KB-Sync" — KEIN Code/Claude nötig:
Zeile anlegen mit (Baserow-Tabelle, Docmost-Space, Seitentitel, Aktiv) → kb-sync legt die
Docmost-Seite an (falls nötig) und aktualisiert sie. Trigger: Polling + /refresh + /webhook.
Nur Python-Standardbibliothek.
"""
import os, json, time, datetime, threading, urllib.request, urllib.error, http.cookiejar
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

IP           = os.environ.get("SERVER_IP","")
BW_URL       = os.environ["BASEROW_URL"];   BW_HOST = os.environ["BASEROW_HOST"]
BW_EMAIL     = os.environ["BASEROW_EMAIL"]; BW_PASS = os.environ["BASEROW_PASSWORD"]
BW_WS        = os.environ["BASEROW_WORKSPACE"]
MAP_TABLE    = os.environ["MAPPING_TABLE_ID"]
DM_URL       = os.environ["DOCMOST_URL"];   DM_HOST = os.environ["DOCMOST_HOST"]
DM_EMAIL     = os.environ["DOCMOST_EMAIL"]; DM_PASS = os.environ["DOCMOST_PASSWORD"]
DEFAULT_SPACE= os.environ.get("DEFAULT_DOCMOST_SPACE","Kundenportal")
POLL         = int(os.environ.get("POLL_SECONDS","30"))
PORT         = int(os.environ.get("PORT","8090"))
_lock = threading.Lock()

# ---------- HTTP-Helfer ----------
def _json(url, host, data=None, headers=None, opener=None, method=None):
    h = {"Host": host}
    if data is not None: h["Content-Type"]="application/json"; data=json.dumps(data).encode()
    if headers: h.update(headers)
    rq = urllib.request.Request(url, data, h, method=method)
    raw = (opener or urllib.request.build_opener()).open(rq, timeout=30).read().decode()
    return json.loads(raw) if raw.strip() else None

# ---------- Baserow (auth) ----------
def bw_token():
    return _json(f"{BW_URL}/api/user/token-auth/", BW_HOST, {"email":BW_EMAIL,"password":BW_PASS})["access_token"]
def bw_get(tok, path):
    return _json(f"{BW_URL}{path}", BW_HOST, headers={"Authorization":f"JWT {tok}"})
def bw_patch(tok, path, body):
    return _json(f"{BW_URL}{path}", BW_HOST, body, {"Authorization":f"JWT {tok}"}, method="PATCH")

def bw_find_table(tok, name):
    for app in bw_get(tok, "/api/applications/"):
        if app.get("type")=="database":
            for t in app.get("tables",[]):
                if t["name"].strip().lower()==name.strip().lower():
                    return t["id"]
    return None

# ---------- Baserow Slug -> Edit-URL Resolver (für „bearbeiten"-Button im Block) ----------
_slug_cache = {"map": {}, "ts": 0}
def bw_build_slug_map(tok):
    """{public-view-slug: edit-url} über alle Datenbanken/Tabellen/Views der Workspace."""
    m = {}
    for app in bw_get(tok, "/api/applications/"):
        if app.get("type") != "database": continue
        db_id = app["id"]
        for t in app.get("tables", []):
            try:
                views = bw_get(tok, f"/api/database/views/table/{t['id']}/")
            except Exception:
                continue
            for v in (views or []):
                slug = v.get("slug")
                if slug:
                    m[slug] = f"http://{BW_HOST}/database/{db_id}/table/{t['id']}/{v['id']}"
    return m
def bw_resolve_slug(slug):
    now = time.time()
    if slug not in _slug_cache["map"] or now - _slug_cache["ts"] > 300:
        tok = bw_token()
        _slug_cache["map"] = bw_build_slug_map(tok)
        _slug_cache["ts"] = now
    return _slug_cache["map"].get(slug)

def fmt(v):
    if v is None: return ""
    if isinstance(v, bool): return "✅" if v else "—"
    if isinstance(v, dict): return str(v.get("value",""))
    if isinstance(v, list): return ", ".join(fmt(x) for x in v)
    return str(v).replace("|","\\|")

def build_markdown(tok, table_id, title):
    fields = bw_get(tok, f"/api/database/fields/table/{table_id}/")
    names = [f["name"] for f in fields]
    rows = bw_get(tok, f"/api/database/rows/table/{table_id}/?user_field_names=true&size=200")["results"]
    md = ["| "+" | ".join(names)+" |", "|"+"|".join("---" for _ in names)+"|"]
    for r in rows:
        md.append("| "+" | ".join(fmt(r.get(n)) for n in names)+" |")
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    return (f"# {title}\n\nAutomatisch aus Baserow (Tabelle „{title}\") — lädt sofort, kein iframe.\n\n"
            + "\n".join(md) + f"\n\n_Zuletzt aktualisiert: {stamp} · {len(rows)} Zeilen · Quelle: Baserow (kb-sync)_"), len(rows)

# ---------- Docmost (cookie) ----------
def dm_opener():
    cj=http.cookiejar.CookieJar(); op=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    _json(f"{DM_URL}/api/auth/login", DM_HOST, {"email":DM_EMAIL,"password":DM_PASS}, opener=op)
    return op
def dm_find_space(op, name):
    d=_json(f"{DM_URL}/api/spaces", DM_HOST, {"page":1}, opener=op)
    items=d.get("data",{}).get("items",[])
    for s in items:
        if s["name"].strip().lower()==name.strip().lower(): return s["id"]
    return None
def dm_page_exists(op, pid):
    try: _json(f"{DM_URL}/api/pages/info", DM_HOST, {"pageId":pid}, opener=op); return True
    except Exception: return False

# ---------- Sync-Lauf ----------
def run():
    with _lock:
        tok = bw_token(); op = dm_opener()
        rows = bw_get(tok, f"/api/database/rows/table/{MAP_TABLE}/?user_field_names=true&size=200")["results"]
        results=[]
        for row in rows:
            rid=row["id"]; tname=(row.get("Tabelle") or "").strip()
            if not tname: continue
            sname=DEFAULT_SPACE; ptitle=tname
            pid=(row.get("_SeitenID") or "").strip()
            status=""
            try:
                tid = bw_find_table(tok, tname)
                if not tid: raise RuntimeError(f"Baserow-Tabelle „{tname}\" nicht gefunden")
                sid = dm_find_space(op, sname)
                if not sid: raise RuntimeError(f"Docmost-Space „{sname}\" nicht gefunden")
                md, n = build_markdown(tok, tid, ptitle)
                if not pid or not dm_page_exists(op, pid):
                    created=_json(f"{DM_URL}/api/pages/create", DM_HOST,
                                  {"spaceId":sid,"title":ptitle,"format":"markdown","content":md}, opener=op)
                    pid=created["data"]["id"] if "data" in created else created["id"]
                else:
                    _json(f"{DM_URL}/api/pages/update", DM_HOST,
                          {"pageId":pid,"operation":"replace","format":"markdown","content":md}, opener=op)
                status=f"✅ {n} Zeilen · {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}"
            except Exception as e:
                status=f"❌ {e}"
            bw_patch(tok, f"/api/database/rows/table/{MAP_TABLE}/{rid}/?user_field_names=true",
                     {"Status":status,"_SeitenID":pid})
            results.append({tname:status})
        return {"mappings":len(results),"results":results,"at":datetime.datetime.now().isoformat(timespec="seconds")}

# ---------- Poll-Schleife ----------
def poller():
    while True:
        time.sleep(POLL)
        try: run()
        except Exception as e: print("poll-Fehler:",e,flush=True)

# ---------- HTTP ----------
class H(BaseHTTPRequestHandler):
    def _s(self,code,body,ctype="application/json"):
        b=body.encode() if isinstance(body,str) else body
        self.send_response(code); self.send_header("Content-Type",ctype)
        self.send_header("Access-Control-Allow-Origin","*")   # Block ruft aus dem Browser
        self.send_header("Content-Length",str(len(b))); self.end_headers(); self.wfile.write(b)
    def log_message(self,*a): pass
    def do_OPTIONS(self):
        self.send_response(204); self.send_header("Access-Control-Allow-Origin","*")
        self.send_header("Access-Control-Allow-Headers","*"); self.end_headers()
    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        path=urlparse(self.path); q=parse_qs(path.query)
        if path.path.startswith("/health"): return self._s(200,json.dumps({"ok":True}))
        if path.path.startswith("/resolve"):
            slug=(q.get("slug") or [""])[0]
            try:
                url=bw_resolve_slug(slug) if slug else None
                return self._s(200 if url else 404, json.dumps({"editUrl":url}))
            except Exception as e:
                return self._s(500, json.dumps({"error":str(e)}))
        if path.path.startswith("/refresh"):
            r=run(); return self._s(200,f"<h2>kb-sync ausgeführt</h2><pre>{json.dumps(r,indent=2,ensure_ascii=False)}</pre><p><a href='/refresh'>nochmal</a></p>","text/html; charset=utf-8")
        self._s(404,json.dumps({"error":"not found"}))
    def do_POST(self):
        if self.path.startswith("/webhook"):
            ln=int(self.headers.get("Content-Length","0") or 0)
            if ln: self.rfile.read(ln)
            return self._s(200,json.dumps(run()))
        self._s(404,json.dumps({"error":"not found"}))

if __name__=="__main__":
    print(f"kb-sync v2 (self-service) auf :{PORT}, Mapping-Tabelle {MAP_TABLE}, Poll {POLL}s",flush=True)
    threading.Thread(target=poller,daemon=True).start()
    try: run(); print("Initialer Lauf ok",flush=True)
    except Exception as e: print("Initial-Fehler:",e,flush=True)
    ThreadingHTTPServer(("0.0.0.0",PORT),H).serve_forever()
