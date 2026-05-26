import os, re, html, subprocess, urllib.parse, http.server, socketserver, secrets

WORKSPACE_ID = os.environ["DOCMOST_WORKSPACE_ID"]
PORT = int(os.environ.get("PORT", "8091"))
ADMIN_PW = os.environ.get("ADMIN_PASSWORD", "13371337")
SESSION_TOKEN = secrets.token_urlsafe(32)
PROTECTED = {"kbsync@brdl.local", "daniel.brunthaler@skidata.com"}  # kein (De)Aktivieren
KBSYNC = "kbsync@brdl.local"                                          # kein PW-Reset

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
NAME_RE = re.compile(r"^[\wÄÖÜäöüß .\-_'@]+$")
def sq(s): return s.replace("'", "''")

def docker(*args, input=None, timeout=180):
    r = subprocess.run(["docker", *args], capture_output=True, text=True, input=input, timeout=timeout)
    if r.returncode != 0: raise RuntimeError(f"docker {args[0]} failed: {r.stderr.strip()[:300]}")
    return r.stdout

def dm_hash(pw):
    return docker("exec", "docmost", "node", "-e",
                  'console.log(require("bcrypt").hashSync(process.argv[1],12))', pw).strip()

def dm_psql(sql):
    return docker("exec", "-i", "docmost-db", "psql", "-U", "docmost", "-d", "docmost", "-tA", "-F", "|", "-c", sql)

def dm_upsert(name, email, pw):
    h = dm_hash(pw)
    sql = (
        "insert into users (email,name,password,role,workspace_id,email_verified_at,locale) "
        f"values ('{sq(email)}','{sq(name)}','{sq(h)}','member','{WORKSPACE_ID}',now(),'de-DE') "
        "on conflict (email, workspace_id) do update set name=excluded.name,password=excluded.password,deactivated_at=NULL "
        "returning email;"
    )
    return dm_psql(sql).strip()

def dm_setpw(email, pw):
    h = dm_hash(pw)
    sql = f"update users set password='{sq(h)}' where email='{sq(email)}' returning email;"
    return dm_psql(sql).strip()

def dm_set_active(email, active):
    if active:
        sql = f"update users set deactivated_at=NULL where email='{sq(email)}' returning email;"
    else:
        sql = (f"update users set deactivated_at=now() where email='{sq(email)}';"
               f"delete from user_sessions where user_id in (select id from users where email='{sq(email)}');")
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

def bw_shell(py):
    return docker("exec", "-i", "baserow", "./baserow.sh", "backend-cmd-with-db", "manage", "shell", input=py, timeout=180)

def bw_upsert(name, email, pw):
    py = (f"EMAIL={email!r}\nNAME={name!r}\nPW={pw!r}\n"
          "from django.contrib.auth import get_user_model\n"
          "from baserow.core.models import Settings\n"
          "from baserow.core.user.handler import UserHandler\n"
          "U=get_user_model()\n"
          "s=Settings.objects.first(); prev=s.allow_new_signups\n"
          "s.allow_new_signups=True; s.save()\n"
          "try:\n"
          "  u=U.objects.filter(email=EMAIL).first()\n"
          "  if u:\n"
          "    u.first_name=NAME; u.is_active=True; u.set_password(PW); u.save()\n"
          "  else:\n"
          "    u=UserHandler().create_user(name=NAME,email=EMAIL,password=PW,language='de')\n"
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
          "  v='yes' if u.profile.email_verified else 'no'\n"
          "  st='aktiv' if u.is_active else 'deaktiviert'\n"
          "  print(f'BWROW|{u.email}|{u.first_name}|{st}|{v}|{u.is_staff}')\n")
    out = bw_shell(py)
    rows = []
    for line in out.splitlines():
        if line.startswith("BWROW|"):
            _, e, n, st, v, staff = line.strip().split("|")
            rows.append({"email": e, "name": n, "state": st, "verified": v == "yes", "staff": staff == "True"})
    return rows

def combined():
    try: dm = {u["email"]: u for u in dm_list()}
    except Exception as e: dm = {}; print("dm err", e, flush=True)
    try: bw = {u["email"]: u for u in bw_list()}
    except Exception as e: bw = {}; print("bw err", e, flush=True)
    emails = sorted(set(dm) | set(bw))
    out = []
    for e in emails:
        d = dm.get(e); b = bw.get(e)
        out.append({"email": e,
                    "name": (d or {}).get("name") or (b or {}).get("name") or "",
                    "dm": (d or {}).get("state") or "—",
                    "bw": (b or {}).get("state") or "—",
                    "bw_staff": (b or {}).get("staff", False)})
    return out

def validate(name, email, pw):
    if not EMAIL_RE.match(email): return "Ungültige E-Mail"
    if not NAME_RE.match(name) or len(name) < 2: return "Ungültiger Name"
    if len(pw) < 8: return "Passwort min. 8 Zeichen"
    return None

LOGIN_PAGE = """<!doctype html><html lang=de><head><meta charset=utf-8>
<title>BRDL Admin · Login</title><style>
body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:linear-gradient(135deg,#1e3a5f,#2d6a9f);display:flex;align-items:center;justify-content:center;min-height:100vh}
.box{background:#fff;padding:32px 28px;border-radius:12px;box-shadow:0 10px 40px rgba(0,0,0,.3);min-width:320px}
h1{margin:0 0 4px;font-size:1.4rem;color:#1e3a5f}
.muted{color:#888;font-size:13px;margin-bottom:18px}
input{width:100%;padding:10px;font:inherit;border:1px solid #bbb;border-radius:6px;margin-bottom:10px;box-sizing:border-box}
button{width:100%;padding:10px;font:inherit;background:#1e3a5f;color:#fff;border:none;border-radius:6px;cursor:pointer}
button:hover{background:#2d6a9f}
.err{background:#fcd6d6;color:#7a1d1d;padding:8px 12px;border-radius:6px;margin-bottom:10px;font-size:14px}
</style></head><body><div class=box>
<h1>🛡️ BRDL Admin</h1><p class=muted>Bitte Passwort eingeben</p>
__ERR__
<form method=post action="/admin/login">
<input type=password name=password placeholder="Passwort" required autofocus>
<button>Anmelden</button>
</form></div></body></html>"""

PAGE = """<!doctype html><html lang=de><head><meta charset=utf-8><title>BRDL Admin</title><style>
*{box-sizing:border-box}body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#f5f5f5}
.wrap{max-width:980px;margin:24px auto;padding:24px;background:#fff;border-radius:10px;box-shadow:0 1px 6px rgba(0,0,0,.08)}
h1{margin:0 0 4px}h2{margin-top:28px}
.muted{color:#888;font-size:13px}.muted a{color:#1e3a5f}
input,button{font:inherit;padding:8px 10px;border:1px solid #bbb;border-radius:6px}
button{cursor:pointer;background:#1e3a5f;color:#fff;border:none}
button:hover{background:#2d6a9f}
button.danger{background:#b3261e}button.danger:hover{background:#d3362e}
button.ghost{background:#e3e3e3;color:#222}button.ghost:hover{background:#cfcfcf}
form.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:8px 0}
input.flex{flex:1 1 200px}
table{width:100%;border-collapse:collapse;margin-top:8px;font-size:14px}
th,td{padding:8px 10px;text-align:left;border-bottom:1px solid #eee}
th{background:#f0f0f0;font-size:13px}
td .act{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.tag{font-size:12px;padding:2px 8px;border-radius:4px;background:#e3e3e3}
.tag.ok{background:#d6f5dc;color:#1b5e20}.tag.off{background:#fcd6d6;color:#7a1d1d}.tag.none{background:#eee;color:#666}
.msg{padding:10px 14px;border-radius:6px;margin:12px 0;font-size:14px}
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
<div class=topbar><h1>🛡️ BRDL Admin</h1>
<form method=post action="/admin/logout"><button class=ghost>Abmelden</button></form></div>
<p class=muted><a href="/">← zurück zur Landing</a></p>
__MSG__
<h2>Neuen Nutzer anlegen</h2>
<form class=row method=post action="/admin/create">
<input class=flex name=name placeholder="Name" required minlength=2>
<input class=flex name=email type=email placeholder="E-Mail" required>
<input class=flex name=password type=text placeholder="Passwort (min. 8)" required minlength=8>
<button>Anlegen (Docmost + Baserow)</button></form>
<p class=muted>Existiert die E-Mail schon, wird das Passwort gesetzt und der Nutzer reaktiviert.</p>
<h2>Nutzer</h2>
<table><thead><tr><th>E-Mail</th><th>Name</th><th>Docmost</th><th>Baserow</th><th>Aktion</th></tr></thead>
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
        any_active = (u["dm"] == "aktiv") or (u["bw"] == "aktiv")
        any_inactive = (u["dm"] == "deaktiviert") or (u["bw"] == "deaktiviert")
        protected = u["email"] in PROTECTED
        if protected:
            actions.append('<span class="tag none">🔒 geschützt</span>')
        else:
            if any_active:
                actions.append(f'<form method=post action="/admin/deactivate" style="display:inline;margin:0"><input type=hidden name=email value="{e}"><button class=danger>Deaktivieren</button></form>')
            if any_inactive:
                actions.append(f'<form method=post action="/admin/activate" style="display:inline;margin:0"><input type=hidden name=email value="{e}"><button class=ghost>Reaktivieren</button></form>')
        if u["email"] != KBSYNC:
            actions.append(f'<button class=ghost onclick="pw(\'{e}\')">PW ändern</button>')
        staff = " 🛡" if u.get("bw_staff") else ""
        out.append(f'<tr><td>{e}{staff}</td><td>{html.escape(u["name"])}</td><td>{tag(u["dm"])}</td><td>{tag(u["bw"])}</td><td><div class=act>{" ".join(actions)}</div></td></tr>')
    return "\n".join(out)

def render_page(msg_html=""):
    return PAGE.replace("__MSG__", msg_html).replace("__ROWS__", render_rows(combined()))

def render_login(err=False):
    e = '<div class="err">Falsches Passwort</div>' if err else ""
    return LOGIN_PAGE.replace("__ERR__", e)

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
        body = self.rfile.read(ln).decode()
        data = {k: v[0] for k, v in urllib.parse.parse_qs(body).items()}
        if path.endswith("/login"):
            if data.get("password", "") == ADMIN_PW:
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
                if email in PROTECTED: return self._redirect(f"/admin/?msg={urllib.parse.quote('err:Geschützter Account – hier nicht änderbar')}")
                err = validate(name, email, pw)
                if err: return self._redirect(f"/admin/?msg={urllib.parse.quote('err:' + err)}")
                dm_upsert(name, email, pw); bw_upsert(name, email, pw)
                return self._redirect(f"/admin/?msg={urllib.parse.quote(f'ok:Nutzer {email} angelegt (Docmost + Baserow), Passwort: {pw}')}")
            if path.endswith("/setpassword"):
                email = data.get("email", "").strip().lower(); pw = data.get("password", "")
                if not EMAIL_RE.match(email): return self._redirect("/admin/?msg=err:Ung%C3%BCltige+E-Mail")
                if email == KBSYNC: return self._redirect(f"/admin/?msg={urllib.parse.quote('err:kb-sync-Passwort nicht änderbar (Service-Account)')}")
                if len(pw) < 8: return self._redirect("/admin/?msg=err:Min.+8+Zeichen")
                dm_setpw(email, pw); bw_setpw(email, pw)
                return self._redirect(f"/admin/?msg={urllib.parse.quote(f'ok:Passwort für {email} geändert: {pw}')}")
            if path.endswith("/deactivate") or path.endswith("/activate"):
                email = data.get("email", "").strip().lower()
                if not EMAIL_RE.match(email): return self._redirect("/admin/?msg=err:Ung%C3%BCltige+E-Mail")
                if email in PROTECTED: return self._redirect(f"/admin/?msg={urllib.parse.quote('err:Geschützter Account – nicht (de)aktivierbar')}")
                active = path.endswith("/activate")
                dm_set_active(email, active); bw_set_active(email, active)
                return self._redirect(f"/admin/?msg={urllib.parse.quote(f'ok:{email} ' + ('aktiviert' if active else 'deaktiviert'))}")
            self._html("Not Found", 404)
        except Exception as ex:
            self._redirect(f"/admin/?msg={urllib.parse.quote('err:' + type(ex).__name__ + ': ' + str(ex)[:200])}")

if __name__ == "__main__":
    with socketserver.ThreadingTCPServer(("", PORT), H) as srv:
        print(f"admin auf :{PORT}", flush=True); srv.serve_forever()
