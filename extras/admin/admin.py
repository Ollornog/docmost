import os, re, html, time, threading, subprocess, urllib.parse, http.server, socketserver, secrets

# =============================================================================
# KB Admin — User-CRUD über alle drei Plattformen (Docmost + Baserow + NocoDB).
# Felder: Kürzel / E-Mail / Passwort. Anlegen legt auf allen drei Plattformen an.
# "Löschen" = nur Deaktivieren (Docmost.deactivated_at / Baserow.is_active /
# NocoDB.blocked). Permanenter Admin + Worker werden beim Start geseedet und sind
# geschützt (nicht deaktivierbar). Worker-Passwort ist UI-gesperrt (Service-Account).
# Zugriff auf die DBs läuft über `docker exec` (docker.sock) — netzwerk-unabhängig.
# =============================================================================

WORKSPACE_ID = os.environ["DOCMOST_WORKSPACE_ID"]
PORT = int(os.environ.get("PORT", "8091"))
ADMIN_PW = os.environ.get("ADMIN_PASSWORD", "")

ADMIN_EMAIL  = os.environ.get("ADMIN_EMAIL", "admin@kb.local").strip().lower()
ADMIN_KUERZEL = os.environ.get("ADMIN_KUERZEL", "ADM").strip()
ADMIN_SEED_PASS = os.environ.get("ADMIN_SEED_PASS", "")
WORKER_EMAIL = os.environ.get("WORKER_EMAIL", "kbsync@kb.local").strip().lower()
WORKER_KUERZEL = os.environ.get("WORKER_KUERZEL", "SYNC").strip()
KBSYNC_DM_PASS = os.environ.get("KBSYNC_DM_PASS", "")
KBSYNC_BW_PASS = os.environ.get("KBSYNC_BW_PASS", "")
KBSYNC_NC_PASS = os.environ.get("KBSYNC_NC_PASS", "")

NC_PG_USER = os.environ.get("NOCODB_PG_USER", "nocodb")
NC_PG_DB   = os.environ.get("NOCODB_PG_DB", "nocodb")

SESSION_TOKEN = secrets.token_urlsafe(32)
PROTECTED = {ADMIN_EMAIL, WORKER_EMAIL}   # nicht (de)aktivierbar / nicht entfernbar
PW_LOCKED = {WORKER_EMAIL}                 # Passwort nicht über die UI änderbar (Service-Account)

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
NAME_RE = re.compile(r"^[\wÄÖÜäöüß .\-_'@]+$")
def sq(s): return str(s).replace("'", "''")

def docker(*args, input=None, timeout=180):
    r = subprocess.run(["docker", *args], capture_output=True, text=True, input=input, timeout=timeout)
    if r.returncode != 0: raise RuntimeError(f"docker {args[0]} failed: {r.stderr.strip()[:300]}")
    return r.stdout

# ============================== Docmost ======================================
def dm_hash(pw):
    return docker("exec", "docmost", "node", "-e",
                  'console.log(require("bcrypt").hashSync(process.argv[1],12))', pw).strip()

def dm_psql(sql):
    return docker("exec", "-i", "docmost-db", "psql", "-U", "docmost", "-d", "docmost", "-tA", "-F", "|", "-c", sql)

def dm_upsert(name, email, pw, role="member"):
    h = dm_hash(pw)
    sql = (
        "insert into users (email,name,password,role,workspace_id,email_verified_at,locale) "
        f"values ('{sq(email)}','{sq(name)}','{sq(h)}','{sq(role)}','{WORKSPACE_ID}',now(),'de-DE') "
        "on conflict (email, workspace_id) do update set name=excluded.name,password=excluded.password,deactivated_at=NULL "
        "returning email;"
    )
    return dm_psql(sql).strip()

def dm_setpw(email, pw):
    h = dm_hash(pw)
    return dm_psql(f"update users set password='{sq(h)}' where email='{sq(email)}' and workspace_id='{WORKSPACE_ID}' returning email;").strip()

def dm_set_active(email, active):
    if active:
        sql = f"update users set deactivated_at=NULL where email='{sq(email)}' and workspace_id='{WORKSPACE_ID}' returning email;"
    else:
        sql = (f"update users set deactivated_at=now() where email='{sq(email)}' and workspace_id='{WORKSPACE_ID}';"
               f"delete from user_sessions where user_id in (select id from users where email='{sq(email)}' and workspace_id='{WORKSPACE_ID}');")
    return dm_psql(sql)

def dm_list():
    sql = (f"select email,name,role,case when deactivated_at is null then 'aktiv' else 'deaktiviert' end "
           f"from users where workspace_id='{WORKSPACE_ID}' order by email;")
    rows = []
    for line in dm_psql(sql).strip().splitlines():
        if "|" not in line: continue
        p = line.split("|")
        rows.append({"email": p[0], "name": p[1], "role": p[2], "state": p[3]})
    return rows

def dm_seed_admin(kuerzel, email, seedpw):
    # idempotent: existiert die Admin-Mail -> nur Rolle/aktiv/Kürzel sicherstellen (PW NICHT überschreiben).
    if dm_psql(f"select 1 from users where email='{sq(email)}' and workspace_id='{WORKSPACE_ID}' limit 1;").strip():
        dm_psql(f"update users set name='{sq(kuerzel)}',role='owner',deactivated_at=NULL where email='{sq(email)}' and workspace_id='{WORKSPACE_ID}';")
        return "vorhanden"
    # sonst: bestehenden Owner (vom Erstsetup) auf die neutrale Admin-Identität umbenennen + Seed-PW.
    owner = dm_psql(f"select email from users where role='owner' and workspace_id='{WORKSPACE_ID}' order by created_at limit 1;").strip().splitlines()
    if owner:
        old = owner[0].split("|")[0]
        h = dm_hash(seedpw)
        dm_psql(f"update users set email='{sq(email)}',name='{sq(kuerzel)}',password='{sq(h)}',role='owner',deactivated_at=NULL "
                f"where email='{sq(old)}' and workspace_id='{WORKSPACE_ID}';")
        return f"umbenannt von {old}"
    dm_upsert(kuerzel, email, seedpw, role="owner")
    return "neu angelegt"

# ============================== Baserow ======================================
def bw_shell(py):
    return docker("exec", "-i", "baserow", "./baserow.sh", "backend-cmd-with-db", "manage", "shell", input=py, timeout=180)

def bw_upsert(name, email, pw, staff=False, setpw=True):
    py = (f"EMAIL={email!r}\nNAME={name!r}\nPW={pw!r}\nSTAFF={'True' if staff else 'False'}\nSETPW={'True' if setpw else 'False'}\n"
          "from django.contrib.auth import get_user_model\n"
          "from baserow.core.models import Settings\n"
          "from baserow.core.user.handler import UserHandler\n"
          "U=get_user_model()\n"
          "s=Settings.objects.first(); prev=s.allow_new_signups\n"
          "s.allow_new_signups=True; s.save()\n"
          "try:\n"
          "  u=U.objects.filter(email=EMAIL).first()\n"
          "  if u:\n"
          "    u.first_name=NAME; u.is_active=True\n"
          "    if SETPW: u.set_password(PW)\n"
          "    if STAFF: u.is_staff=True; u.is_superuser=True\n"
          "    u.save()\n"
          "  else:\n"
          "    u=UserHandler().create_user(name=NAME,email=EMAIL,password=PW,language='de')\n"
          "    if STAFF: u.is_staff=True; u.is_superuser=True; u.save()\n"
          "  u.profile.email_verified=True; u.profile.save()\n"
          "  print('BWOK', u.id)\n"
          "finally:\n"
          "  s.allow_new_signups=prev; s.save()\n")
    out = bw_shell(py)
    if "BWOK" not in out: raise RuntimeError(f"Baserow kein BWOK: {out[-300:]}")
    return True

def bw_setpw(email, pw):
    py = ("from django.contrib.auth import get_user_model\nU=get_user_model()\n"
          f"u=U.objects.filter(email={email!r}).first()\n"
          f"if u:\n  u.set_password({pw!r}); u.save(); print('BWOK')\n")
    return bw_shell(py)

def bw_set_active(email, active):
    py = ("from django.contrib.auth import get_user_model\nU=get_user_model()\n"
          f"u=U.objects.filter(email={email!r}).first()\n"
          f"if u:\n  u.is_active={'True' if active else 'False'}\n  u.save()\n  print('BWOK')\n")
    return bw_shell(py)

def bw_list():
    py = ("from django.contrib.auth import get_user_model\nU=get_user_model()\n"
          "for u in U.objects.all().order_by('email'):\n"
          "  st='aktiv' if u.is_active else 'deaktiviert'\n"
          "  print(f'BWROW|{u.email}|{u.first_name}|{st}|{u.is_staff}')\n")
    out = bw_shell(py)
    rows = []
    for line in out.splitlines():
        if line.startswith("BWROW|"):
            _, e, n, st, staff = line.strip().split("|")
            rows.append({"email": e, "name": n, "state": st, "staff": staff == "True"})
    return rows

# ============================== NocoDB =======================================
def nc_psql(sql):
    return docker("exec", "-i", "nocodb-db", "psql", "-U", NC_PG_USER, "-d", NC_PG_DB, "-tA", "-F", "|", "-c", sql)

def nc_hash(pw):
    # Salt + Hash bcrypt-kompatibel im nocodb-Container erzeugen (gleiche Lib wie NocoDB selbst).
    js = ('const b=require("bcryptjs");const s=b.genSaltSync(10);'
          'process.stdout.write(s+" "+b.hashSync(process.argv[1],s));')
    out = docker("exec", "nocodb", "node", "-e", js, pw).strip()
    salt, h = out.split(" ", 1)
    return salt, h

def nc_get_id(email):
    out = nc_psql(f"select id from nc_users_v2 where email='{sq(email)}' limit 1;").strip().splitlines()
    return out[0] if out else ""

def nc_upsert(name, email, pw, roles="org-level-viewer", setpw=True):
    uid = nc_get_id(email)
    if uid:
        sets = [f"display_name='{sq(name)}'", "blocked=false", "is_deleted=false", "email_verified=true", "updated_at=now()"]
        if setpw:
            salt, h = nc_hash(pw)
            sets += [f"password='{sq(h)}'", f"salt='{sq(salt)}'", f"token_version='{secrets.token_hex(16)}'"]
        return nc_psql(f"update nc_users_v2 set {','.join(sets)} where email='{sq(email)}' returning email;").strip()
    salt, h = nc_hash(pw)
    rid = "us" + secrets.token_hex(7)   # NocoDB-User-IDs: 'us' + 14 Zeichen
    sql = ("insert into nc_users_v2 (id,email,canonical_email,password,salt,roles,display_name,"
           "email_verified,blocked,is_deleted,token_version,created_at,updated_at) "
           f"values ('{rid}','{sq(email)}','{sq(email.lower())}','{sq(h)}','{sq(salt)}','{sq(roles)}','{sq(name)}',"
           f"true,false,false,'{secrets.token_hex(16)}',now(),now()) returning email;")
    return nc_psql(sql).strip()

def nc_setpw(email, pw):
    salt, h = nc_hash(pw)
    return nc_psql(f"update nc_users_v2 set password='{sq(h)}',salt='{sq(salt)}',token_version='{secrets.token_hex(16)}' "
                   f"where email='{sq(email)}' returning email;").strip()

def nc_set_active(email, active):
    if active:
        return nc_psql(f"update nc_users_v2 set blocked=false where email='{sq(email)}' returning email;")
    return nc_psql(f"update nc_users_v2 set blocked=true,token_version='{secrets.token_hex(16)}' where email='{sq(email)}' returning email;")

def nc_list():
    sql = ("select email,coalesce(display_name,''),"
           "case when blocked then 'deaktiviert' else 'aktiv' end,"
           "case when roles like '%super%' then 'super' else '' end "
           "from nc_users_v2 where is_deleted is not true order by email;")
    rows = []
    for line in nc_psql(sql).strip().splitlines():
        if "|" not in line: continue
        p = line.split("|")
        rows.append({"email": p[0], "name": p[1], "state": p[2], "super": p[3] == "super"})
    return rows

def nc_seed_admin(kuerzel, email, seedpw):
    if nc_get_id(email):
        nc_psql(f"update nc_users_v2 set display_name='{sq(kuerzel)}',roles='org-level-creator,super',"
                f"blocked=false,is_deleted=false,email_verified=true where email='{sq(email)}';")
        return "vorhanden"
    nc_upsert(kuerzel, email, seedpw, roles="org-level-creator,super")
    return "neu angelegt"

# ============================== Seeding ======================================
def seed_all():
    # läuft im Hintergrund; jede Plattform einzeln mit Retry, damit ein langsamer
    # Container den Start nicht blockiert und ein Ausfall die anderen nicht killt.
    def retry(label, fn):
        for i in range(20):
            try:
                r = fn(); print(f"[seed] {label}: {r if isinstance(r,str) else 'ok'}", flush=True); return
            except Exception as e:
                if i == 19: print(f"[seed] {label} FEHLER: {e}", flush=True); return
                time.sleep(6)
    # Admin (permanent, geschützt) — PW nur beim Anlegen, danach änderbar.
    retry("docmost-admin",  lambda: dm_seed_admin(ADMIN_KUERZEL, ADMIN_EMAIL, ADMIN_SEED_PASS))
    retry("baserow-admin",  lambda: bw_seed_admin(ADMIN_KUERZEL, ADMIN_EMAIL, ADMIN_SEED_PASS))
    retry("nocodb-admin",   lambda: nc_seed_admin(ADMIN_KUERZEL, ADMIN_EMAIL, ADMIN_SEED_PASS))
    # Worker (Service-Account) — PW jedes Mal auf KBSYNC_*_PASS erzwingen (muss zu kb-sync passen).
    retry("docmost-worker", lambda: dm_upsert(WORKER_KUERZEL, WORKER_EMAIL, KBSYNC_DM_PASS, role="member") and "ok")
    retry("baserow-worker", lambda: bw_upsert(WORKER_KUERZEL, WORKER_EMAIL, KBSYNC_BW_PASS, setpw=True) and "ok")
    retry("nocodb-worker",  lambda: nc_upsert(WORKER_KUERZEL, WORKER_EMAIL, KBSYNC_NC_PASS) and "ok")
    print("[seed] fertig", flush=True)

def bw_user_exists(email):
    py = ("from django.contrib.auth import get_user_model\nU=get_user_model()\n"
          f"print('YES' if U.objects.filter(email={email!r}).exists() else 'NO')\n")
    return "YES" in bw_shell(py)

def bw_seed_admin(kuerzel, email, seedpw):
    # PW nur beim Anlegen setzen (danach über die UI änderbar); Staff/aktiv immer sicherstellen.
    if bw_user_exists(email):
        bw_upsert(kuerzel, email, seedpw, staff=True, setpw=False); return "vorhanden"
    bw_upsert(kuerzel, email, seedpw, staff=True, setpw=True); return "neu angelegt"

# ============================== kombinierte Liste ============================
def combined():
    def safe(fn, label):
        try: return {u["email"]: u for u in fn()}
        except Exception as e: print(f"{label} err", e, flush=True); return {}
    dm = safe(dm_list, "dm"); bw = safe(bw_list, "bw"); nc = safe(nc_list, "nc")
    emails = sorted(set(dm) | set(bw) | set(nc))
    out = []
    for e in emails:
        d = dm.get(e); b = bw.get(e); n = nc.get(e)
        out.append({"email": e,
                    "name": (d or {}).get("name") or (b or {}).get("name") or (n or {}).get("name") or "",
                    "dm": (d or {}).get("state") or "—",
                    "bw": (b or {}).get("state") or "—",
                    "nc": (n or {}).get("state") or "—",
                    "bw_staff": (b or {}).get("staff", False),
                    "nc_super": (n or {}).get("super", False)})
    return out

def validate(name, email, pw):
    if not EMAIL_RE.match(email): return "Ungültige E-Mail"
    if not NAME_RE.match(name) or len(name) < 2: return "Ungültiges Kürzel (min. 2 Zeichen)"
    if len(pw) < 8: return "Passwort min. 8 Zeichen"
    return None

# ============================== HTML ========================================
LOGIN_PAGE = """<!doctype html><html lang=de><head><meta charset=utf-8>
<title>KB Admin · Login</title><style>
body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:linear-gradient(135deg,#1e3a5f,#2d6a9f);display:flex;align-items:center;justify-content:center;min-height:100vh}
.box{background:#fff;padding:32px 28px;border-radius:12px;box-shadow:0 10px 40px rgba(0,0,0,.3);min-width:320px}
h1{margin:0 0 4px;font-size:1.4rem;color:#1e3a5f}
.muted{color:#888;font-size:13px;margin-bottom:18px}
input{width:100%;padding:10px;font:inherit;border:1px solid #bbb;border-radius:6px;margin-bottom:10px;box-sizing:border-box}
button{width:100%;padding:10px;font:inherit;background:#1e3a5f;color:#fff;border:none;border-radius:6px;cursor:pointer}
button:hover{background:#2d6a9f}
.err{background:#fcd6d6;color:#7a1d1d;padding:8px 12px;border-radius:6px;margin-bottom:10px;font-size:14px}
</style></head><body><div class=box>
<h1>🛡️ KB Admin</h1><p class=muted>Bitte Passwort eingeben</p>
__ERR__
<form method=post action="/admin/login">
<input type=password name=password placeholder="Passwort" required autofocus>
<button>Anmelden</button>
</form></div></body></html>"""

PAGE = """<!doctype html><html lang=de><head><meta charset=utf-8><title>KB Admin</title><style>
*{box-sizing:border-box}body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#f5f5f5}
.wrap{max-width:1040px;margin:24px auto;padding:24px;background:#fff;border-radius:10px;box-shadow:0 1px 6px rgba(0,0,0,.08)}
h1{margin:0 0 4px}h2{margin-top:28px}
.muted{color:#888;font-size:13px}.muted a{color:#1e3a5f}
input,button{font:inherit;padding:8px 10px;border:1px solid #bbb;border-radius:6px}
button{cursor:pointer;background:#1e3a5f;color:#fff;border:none}
button:hover{background:#2d6a9f}
button.danger{background:#b3261e}button.danger:hover{background:#d3362e}
button.ghost{background:#e3e3e3;color:#222}button.ghost:hover{background:#cfcfcf}
form.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:8px 0}
input.flex{flex:1 1 180px}
table{width:100%;border-collapse:collapse;margin-top:8px;font-size:14px}
th,td{padding:8px 10px;text-align:left;border-bottom:1px solid #eee}
th{background:#f0f0f0;font-size:13px}
td .act{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.tag{font-size:12px;padding:2px 8px;border-radius:4px;background:#e3e3e3}
.tag.ok{background:#d6f5dc;color:#1b5e20}.tag.off{background:#fcd6d6;color:#7a1d1d}.tag.none{background:#eee;color:#666}
.msg{padding:10px 14px;border-radius:6px;margin:12px 0;font-size:14px;word-break:break-word}
.msg.ok{background:#d6f5dc;color:#1b5e20}.msg.err{background:#fcd6d6;color:#7a1d1d}
.topbar{display:flex;justify-content:space-between;align-items:center}
</style>
<script>
function pw(em){
  var p = prompt("Neues Passwort für "+em+" (min. 8 Zeichen):");
  if(!p) return;
  if(p.length < 8){ alert("Mindestens 8 Zeichen!"); return; }
  var f = document.createElement("form");
  f.method = "POST"; f.action = "/admin/setpassword";
  var i1 = document.createElement("input"); i1.name = "email"; i1.value = em; f.appendChild(i1);
  var i2 = document.createElement("input"); i2.name = "password"; i2.value = p; f.appendChild(i2);
  document.body.appendChild(f); f.submit();
}
</script>
</head><body><div class=wrap>
<div class=topbar><h1>🛡️ KB Admin</h1>
<form method=post action="/admin/logout"><button class=ghost>Abmelden</button></form></div>
<p class=muted><a href="/">← zurück zur Landing</a> · Nutzerverwaltung über Docmost + Baserow + NocoDB</p>
__MSG__
<h2>Neuen Nutzer anlegen</h2>
<form class=row method=post action="/admin/create">
<input class=flex name=name placeholder="Kürzel" required minlength=2>
<input class=flex name=email type=email placeholder="E-Mail" required>
<input class=flex name=password type=text placeholder="Passwort (min. 8)" required minlength=8>
<button>Anlegen (alle Plattformen)</button></form>
<p class=muted>Existiert die E-Mail schon, wird das Passwort gesetzt und der Nutzer überall reaktiviert.</p>
<h2>Nutzer</h2>
<table><thead><tr><th>E-Mail</th><th>Kürzel</th><th>Docmost</th><th>Baserow</th><th>NocoDB</th><th>Aktion</th></tr></thead>
<tbody>__ROWS__</tbody></table>
</div></body></html>"""

def tag(s):
    cls = "ok" if s == "aktiv" else ("off" if s == "deaktiviert" else "none")
    return f'<span class="tag {cls}">{html.escape(s)}</span>'

def render_rows(users):
    out = []
    for u in users:
        actions = []
        e = html.escape(u["email"])
        any_active = "aktiv" in (u["dm"], u["bw"], u["nc"])
        any_inactive = "deaktiviert" in (u["dm"], u["bw"], u["nc"])
        protected = u["email"] in PROTECTED
        if protected:
            actions.append('<span class="tag none">🔒 geschützt</span>')
        else:
            if any_active:
                actions.append(f'<form method=post action="/admin/deactivate" style="display:inline;margin:0"><input type=hidden name=email value="{e}"><button class=danger>Deaktivieren</button></form>')
            if any_inactive:
                actions.append(f'<form method=post action="/admin/activate" style="display:inline;margin:0"><input type=hidden name=email value="{e}"><button class=ghost>Reaktivieren</button></form>')
        if u["email"] not in PW_LOCKED:
            actions.append(f'<button class=ghost onclick="pw(\'{e}\')">PW ändern</button>')
        badge = (" 🛡" if u.get("bw_staff") else "") + (" ★" if u.get("nc_super") else "")
        out.append(f'<tr><td>{e}{badge}</td><td>{html.escape(u["name"])}</td>'
                   f'<td>{tag(u["dm"])}</td><td>{tag(u["bw"])}</td><td>{tag(u["nc"])}</td>'
                   f'<td><div class=act>{" ".join(actions)}</div></td></tr>')
    return "\n".join(out)

def render_page(msg_html=""):
    return PAGE.replace("__MSG__", msg_html).replace("__ROWS__", render_rows(combined()))

def render_login(err=False):
    return LOGIN_PAGE.replace("__ERR__", '<div class="err">Falsches Passwort</div>' if err else "")

def is_authed(handler):
    for kv in handler.headers.get("Cookie", "").split(";"):
        k, _, v = kv.strip().partition("=")
        if k == "admin_sess" and v == SESSION_TOKEN: return True
    return False

class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a, **kw): pass
    def _html(self, body, code=200):
        b = body.encode("utf-8")
        self.send_response(code); self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def _redirect(self, loc):
        self.send_response(303); self.send_header("Location", loc); self.end_headers()
    def _msg(self, m):
        self._redirect(f"/admin/?msg={urllib.parse.quote(m)}")
    def do_GET(self):
        p = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(p.query)
        if not is_authed(self):
            return self._html(render_login(err=(qs.get("e") == ["1"])))
        msg_html = ""
        if "msg" in qs:
            m = qs["msg"][0]; cls = "ok" if m.startswith("ok:") else "err"
            msg_html = f'<div class="msg {cls}">{html.escape(m[3:] if len(m) > 3 else m)}</div>'
        try: self._html(render_page(msg_html))
        except Exception as ex: self._html(f"<pre>{html.escape(str(ex))}</pre>", 500)
    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path.rstrip("/")
        ln = int(self.headers.get("Content-Length", "0"))
        data = {k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(ln).decode()).items()}
        if path.endswith("/login"):
            if ADMIN_PW and data.get("password", "") == ADMIN_PW:
                self.send_response(303)
                self.send_header("Set-Cookie", f"admin_sess={SESSION_TOKEN}; HttpOnly; Path=/admin; SameSite=Strict; Max-Age=43200")
                self.send_header("Location", "/admin/"); self.end_headers(); return
            return self._redirect("/admin/?e=1")
        if path.endswith("/logout"):
            self.send_response(303)
            self.send_header("Set-Cookie", "admin_sess=; HttpOnly; Path=/admin; Max-Age=0")
            self.send_header("Location", "/admin/"); self.end_headers(); return
        if not is_authed(self):
            return self._redirect("/admin/")
        try:
            if path.endswith("/create"):
                name = data.get("name", "").strip(); email = data.get("email", "").strip().lower(); pw = data.get("password", "")
                if email in PROTECTED: return self._msg("err:Geschützter Account – hier nicht änderbar")
                err = validate(name, email, pw)
                if err: return self._msg("err:" + err)
                errs = []
                for label, fn in (("Docmost", lambda: dm_upsert(name, email, pw)),
                                  ("Baserow", lambda: bw_upsert(name, email, pw)),
                                  ("NocoDB",  lambda: nc_upsert(name, email, pw))):
                    try: fn()
                    except Exception as ex: errs.append(f"{label}: {type(ex).__name__}")
                if errs: return self._msg(f"err:Teilweise fehlgeschlagen ({'; '.join(errs)}) – E-Mail {email}")
                return self._msg(f"ok:Nutzer {email} auf allen Plattformen angelegt, Passwort: {pw}")
            if path.endswith("/setpassword"):
                email = data.get("email", "").strip().lower(); pw = data.get("password", "")
                if not EMAIL_RE.match(email): return self._msg("err:Ungültige E-Mail")
                if email in PW_LOCKED: return self._msg("err:Service-Account – Passwort nicht über die UI änderbar")
                if len(pw) < 8: return self._msg("err:Min. 8 Zeichen")
                errs = []
                for label, fn in (("Docmost", lambda: dm_setpw(email, pw)),
                                  ("Baserow", lambda: bw_setpw(email, pw)),
                                  ("NocoDB",  lambda: nc_setpw(email, pw))):
                    try: fn()
                    except Exception as ex: errs.append(f"{label}: {type(ex).__name__}")
                if errs: return self._msg(f"err:Teilweise fehlgeschlagen ({'; '.join(errs)})")
                return self._msg(f"ok:Passwort für {email} überall geändert: {pw}")
            if path.endswith("/deactivate") or path.endswith("/activate"):
                email = data.get("email", "").strip().lower()
                if not EMAIL_RE.match(email): return self._msg("err:Ungültige E-Mail")
                if email in PROTECTED: return self._msg("err:Geschützter Account – nicht (de)aktivierbar")
                active = path.endswith("/activate")
                errs = []
                for label, fn in (("Docmost", lambda: dm_set_active(email, active)),
                                  ("Baserow", lambda: bw_set_active(email, active)),
                                  ("NocoDB",  lambda: nc_set_active(email, active))):
                    try: fn()
                    except Exception as ex: errs.append(f"{label}: {type(ex).__name__}")
                if errs: return self._msg(f"err:Teilweise fehlgeschlagen ({'; '.join(errs)})")
                return self._msg(f"ok:{email} überall " + ("aktiviert" if active else "deaktiviert"))
            self._html("Not Found", 404)
        except Exception as ex:
            self._msg("err:" + type(ex).__name__ + ": " + str(ex)[:200])

if __name__ == "__main__":
    threading.Thread(target=seed_all, daemon=True).start()
    with socketserver.ThreadingTCPServer(("", PORT), H) as srv:
        srv.allow_reuse_address = True
        print(f"KB Admin auf :{PORT}", flush=True); srv.serve_forever()
