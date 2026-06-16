import os, re, html, json, time, hashlib, threading, subprocess, urllib.parse, http.server, socketserver, secrets

# =============================================================================
# KB Admin — User-CRUD über alle drei Plattformen (Docmost + Baserow + NocoDB).
# Felder: Kürzel / E-Mail / Passwort. Anlegen legt auf allen drei Plattformen an.
# "Löschen" = standardmäßig nur Deaktivieren (Docmost.deactivated_at /
# Baserow.is_active / NocoDB.blocked). Zusätzlich gibt es einen ADMIN-ONLY
# "Endgültig löschen" (Hard-Delete), der den User auf allen drei Plattformen
# wirklich entfernt — aber nur, wenn er nirgends eigene/geteilte Inhalte besitzt
# (sonst Verweigerung; siehe dm_/nc_/bw_delete_blockers + dm_/nc_/bw_hard_delete).
# Permanenter Admin + Worker werden beim Start geseedet und sind
# geschützt (nicht deaktivierbar). Worker-Passwort ist UI-gesperrt (Service-Account).
# Zugriff auf die DBs läuft über `docker exec` (docker.sock) — netzwerk-unabhängig.
#
# Rollen:
#  - Admin: Master-Passwort (ADMIN_PASSWORD bzw. selbst gesetzter Hash in state.json).
#           Darf alles inkl. Admin-PW ändern und Moderatoren verwalten.
#  - Moderator: meldet sich mit eigener E-Mail + Plattform-Passwort an (Verifikation
#           gegen Docmost via bcrypt-Vergleich, docker exec — netzwerkunabhängig).
#           Darf Nutzer-CRUD, aber NICHT geschützte Accounts, NICHT das Admin-PW,
#           NICHT Moderatoren verwalten.
# Persistenter Zustand (Admin-PW-Hash, Moderatorenliste): state.json unter STATE_DIR.
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

STATE_DIR = os.environ.get("STATE_DIR", "/data")
STATE_PATH = os.path.join(STATE_DIR, "state.json")

PROTECTED = {ADMIN_EMAIL, WORKER_EMAIL}   # nicht (de)aktivierbar / nicht entfernbar / nicht als Moderator
PW_LOCKED = {WORKER_EMAIL}                 # Passwort nicht über die UI änderbar (Service-Account)

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
NAME_RE = re.compile(r"^[\wÄÖÜäöüß .\-_'@]+$")
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
def sq(s): return str(s).replace("'", "''")

def _uuid_or_empty(s):
    # IDs aus psql-Output absichern: nur valide UUIDs durchlassen, sonst "" (= nicht
    # gefunden) — verhindert, dass Whitespace/Fehlerzeilen in Folge-Queries landen
    # und ein Delete still 0 Zeilen trifft.
    s = (s or "").strip()
    return s if UUID_RE.match(s) else ""

def docker(*args, input=None, timeout=180):
    r = subprocess.run(["docker", *args], capture_output=True, text=True, input=input, timeout=timeout)
    if r.returncode != 0: raise RuntimeError(f"docker {args[0]} failed: {r.stderr.strip()[:300]}")
    return r.stdout

# ============================== State (state.json) ===========================
# Persistenter Zustand über einen Lock geschützt (Lock ist NICHT reentrant ->
# Read-Modify-Write nur über update_state, das selbst lädt + speichert).
_state_lock = threading.Lock()

def _load_unlocked():
    try:
        with open(STATE_PATH) as f: return json.load(f)
    except Exception: return {}

def _save_unlocked(st):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f: json.dump(st, f)
    os.replace(tmp, STATE_PATH)          # atomar

def load_state():
    with _state_lock: return _load_unlocked()

def update_state(fn):
    with _state_lock:
        st = _load_unlocked(); fn(st); _save_unlocked(st); return st

# ---- Admin-UI-Passwort: scrypt-Hash in state.json, sonst Fallback auf Env ----
def hash_pw(pw):
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(pw.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)
    return "scrypt$" + salt.hex() + "$" + dk.hex()

def verify_pw(pw, stored):
    try:
        scheme, salth, dkh = stored.split("$")
        if scheme != "scrypt": return False
        dk = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salth), n=16384, r=8, p=1, dklen=32)
        return secrets.compare_digest(dk.hex(), dkh)
    except Exception:
        return False

def check_admin_pw(pw):
    h = load_state().get("admin_pw_hash")
    if h: return verify_pw(pw, h)
    return bool(ADMIN_PW) and secrets.compare_digest(pw, ADMIN_PW)

# ---- Moderatoren ----
def moderators():
    return set(load_state().get("moderators", []))

# ============================== Docmost ======================================
def dm_hash(pw):
    return docker("exec", "docmost", "node", "-e",
                  'console.log(require("bcrypt").hashSync(process.argv[1],12))', pw).strip()

def dm_psql(sql):
    return docker("exec", "-i", "docmost-db", "psql", "-U", "docmost", "-d", "docmost", "-tA", "-F", "|", "-c", sql)

def dm_verify(email, pw):
    # Moderator-Login: bcrypt-Vergleich gegen den Docmost-Hash; nur aktive Nutzer.
    h = dm_psql(f"select password from users where email='{sq(email)}' and workspace_id='{WORKSPACE_ID}' "
                f"and deactivated_at is null limit 1;").strip()
    if not h: return False
    h = h.splitlines()[0]
    out = docker("exec", "docmost", "node", "-e",
                 'process.stdout.write(require("bcrypt").compareSync(process.argv[1],process.argv[2])?"1":"0")',
                 pw, h).strip()
    return out == "1"

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

# ---- Hard-Delete (endgültig) ----
# Die NO-ACTION-FKs auf users teilen sich auf in:
#  OWN  = echte Inhalte (creator_id) -> blockt die Löschung; der Admin soll erst
#         deaktivieren bzw. die Inhalte übertragen, statt sie unbemerkt zu verlieren.
#  SOFT = bloße Referenzen ("zuletzt bearbeitet/hinzugefügt/eingeladen von") -> werden
#         auf den Workspace-Owner umgehängt, damit das DELETE durchläuft, ohne dass
#         fremde, noch existierende Inhalte mitgerissen werden.
# CASCADE-/SET-NULL-FKs (Sessions, Auth, Favoriten, Mitgliedschaften, …) regelt Postgres.
DM_OWN = [("pages", "creator_id", "Seiten"), ("spaces", "creator_id", "Spaces"),
          ("comments", "creator_id", "Kommentare"), ("groups", "creator_id", "Gruppen"),
          ("attachments", "creator_id", "Anhänge"), ("shares", "creator_id", "Freigaben"),
          ("ai_chats", "creator_id", "AI-Chats"), ("file_tasks", "creator_id", "Datei-Jobs")]
DM_SOFT = [("pages", "last_updated_by_id"), ("pages", "deleted_by_id"),
           ("page_history", "last_updated_by_id"), ("space_members", "added_by_id"),
           ("workspace_invitations", "invited_by_id")]

def dm_user_id(email):
    out = dm_psql(f"select id from users where email='{sq(email)}' and workspace_id='{WORKSPACE_ID}' limit 1;").strip().splitlines()
    return _uuid_or_empty(out[0].split("|")[0]) if out and out[0] else ""

def dm_owner_id():
    out = dm_psql(f"select id from users where role='owner' and workspace_id='{WORKSPACE_ID}' order by created_at limit 1;").strip().splitlines()
    return _uuid_or_empty(out[0].split("|")[0]) if out and out[0] else ""

def dm_soft_ref_count(uid):
    soft = 0
    for tbl, col in DM_SOFT:
        n = dm_psql(f"select count(*) from {tbl} where {col}='{sq(uid)}';").strip()
        if n and n != "0": soft += int(n)
    return soft

def dm_delete_blockers(email):
    uid = dm_user_id(email)
    if not uid: return []                      # nicht vorhanden -> nichts zu löschen, kein Blocker
    blk = []
    for tbl, col, label in DM_OWN:
        n = dm_psql(f"select count(*) from {tbl} where {col}='{sq(uid)}';").strip()
        if n and n != "0": blk.append(f"{n} {label}")
    # SOFT-Referenzen werden beim Delete auf den Workspace-Owner umgehängt. Gibt es
    # keinen umhängbaren Owner (Owner fehlt oder IST dieser User), würde der DELETE an
    # den NO-ACTION-FKs scheitern -> als Blocker melden, damit Phase 2 nie startet und
    # kein Teil-Löschstand über die Plattformen entsteht.
    owner = dm_owner_id()
    if (not owner or owner == uid) and dm_soft_ref_count(uid):
        blk.append("lose Referenzen ohne Owner zum Umhängen")
    return blk

def dm_hard_delete(email):
    uid = dm_user_id(email)
    if not uid: return "—"
    owner = dm_owner_id()
    if owner and owner != uid:                 # Soft-Referenzen auf den Owner umhängen
        for tbl, col in DM_SOFT:
            dm_psql(f"update {tbl} set {col}='{sq(owner)}' where {col}='{sq(uid)}';")
    elif dm_soft_ref_count(uid):               # kein Owner umhängbar + SOFT-Refs -> niemals halb löschen
        raise RuntimeError("kein Workspace-Owner zum Umhängen der losen Referenzen")
    dm_psql(f"delete from users where id='{sq(uid)}' and workspace_id='{WORKSPACE_ID}';")
    return "ok"

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

def bw_psql(sql):
    # READ-ONLY-Pfad: direkt gegen das eingebettete Postgres (peer-Auth als OS-User
    # postgres). Spart den ~4s teuren Django-Shell-Boot bei jedem Seitenaufbau.
    # Schreibwege laufen WEITER über bw_shell (Django-Hashing/Handler nicht umgehen!).
    return docker("exec", "-i", "baserow", "su", "-s", "/bin/sh", "postgres",
                  "-c", "psql -d baserow -tA -F'|'", input=sql)

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

def bw_delete_blockers(email):
    # Verweigern, wenn der User der EINZIGE (oder der einzige Admin) eines Workspace
    # ist -> ein Hard-Delete würde diesen Workspace samt Daten verwaisen lassen.
    py = ("from django.contrib.auth import get_user_model\n"
          "from baserow.core.models import WorkspaceUser\n"
          "U=get_user_model()\n"
          f"u=U.objects.filter(email={email!r}).first()\n"
          "if not u:\n  print('BWB:')\n"
          "else:\n"
          "  b=[]\n"
          "  for wu in WorkspaceUser.objects.filter(user=u).select_related('workspace'):\n"
          "    ws=wu.workspace\n"
          "    total=WorkspaceUser.objects.filter(workspace=ws).count()\n"
          "    admins=WorkspaceUser.objects.filter(workspace=ws,permissions='ADMIN').count()\n"
          "    if total==1: b.append(\"einziges Mitglied von '%s'\" % ws.name)\n"
          "    elif wu.permissions=='ADMIN' and admins==1: b.append(\"einziger Admin von '%s'\" % ws.name)\n"
          "  print('BWB:'+'; '.join(b))\n")
    out = bw_shell(py)
    for line in out.splitlines():
        if line.startswith("BWB:"):
            rest = line[4:].strip()
            return [rest] if rest else []
    raise RuntimeError(f"Baserow-Blocker-Check fehlgeschlagen: {out[-200:]}")

def bw_hard_delete(email):
    # Django-ORM-Delete: kennt die Cascade-Reihenfolge (UserProfile, Auth-Provider, …).
    py = ("from django.contrib.auth import get_user_model\nU=get_user_model()\n"
          f"u=U.objects.filter(email={email!r}).first()\n"
          "if not u:\n  print('BWDEL:none')\n"
          "else:\n  u.delete()\n  print('BWDEL:ok')\n")
    out = bw_shell(py)
    if "BWDEL:" not in out: raise RuntimeError(f"Baserow-Delete fehlgeschlagen: {out[-200:]}")
    return "ok"

def bw_list():
    # Schneller Read direkt aus Postgres (siehe bw_psql).
    sql = "select email,coalesce(first_name,''),is_active,is_staff from auth_user order by email;"
    rows = []
    for line in bw_psql(sql).strip().splitlines():
        if "|" not in line: continue
        e, n, act, staff = line.split("|")
        rows.append({"email": e, "name": n, "state": "aktiv" if act == "t" else "deaktiviert", "staff": staff == "t"})
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
    uid = out[0].strip() if out else ""
    return uid if re.fullmatch(r"\S{4,40}", uid or "") else ""   # ein sauberes Token, kein Müll/Whitespace

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

# NocoDB pflegt keine DB-FKs auf nc_users_v2 -> Verweise sind lose; beim Hard-Delete
# müssen Mitgliedschaften/Tokens daher von Hand mitgelöscht werden.
NC_REFS = [("nc_base_users_v2", "fk_user_id"), ("workspace_user", "fk_user_id"),
           ("nc_org_users", "fk_user_id"), ("nc_user_refresh_tokens", "fk_user_id"),
           ("nc_user_comment_notifications_preference", "user_id")]

def nc_delete_blockers(email):
    uid = nc_get_id(email)
    if not uid: return []
    n = nc_psql(f"select count(*) from nc_base_users_v2 where fk_user_id='{sq(uid)}' and roles ilike '%owner%';").strip()
    return [f"{n} eigene Base(s)"] if n and n != "0" else []

def nc_hard_delete(email):
    uid = nc_get_id(email)
    if not uid: return "—"
    for tbl, col in NC_REFS:                    # lose Verweise zuerst aufräumen
        # Tabelle ist versionsabhängig -> nur löschen, wenn vorhanden; existiert sie,
        # echte Fehler NICHT verschlucken (sonst Orphans bei stillem "Erfolg").
        if not nc_psql(f"select to_regclass('public.{tbl}');").strip():
            continue
        nc_psql(f"delete from {tbl} where {col}='{sq(uid)}';")
    nc_psql(f"delete from nc_users_v2 where id='{sq(uid)}';")
    return "ok"

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

def known_emails():
    out = set()
    for fn in (dm_list, bw_list, nc_list):
        try: out |= {u["email"] for u in fn()}
        except Exception: pass
    return out

def validate(name, email, pw):
    if not EMAIL_RE.match(email): return "Ungültige E-Mail"
    if not NAME_RE.match(name) or len(name) < 2: return "Ungültiges Kürzel (min. 2 Zeichen)"
    if len(pw) < 8: return "Passwort min. 8 Zeichen"
    return None

# ============================== HTML ========================================
# Inline-SVG-Favicon (Schild auf Navy), wird unter /admin/favicon.svg ausgeliefert.
FAVICON = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
           '<rect width="100" height="100" rx="22" fill="#1e3a5f"/>'
           '<text x="50" y="54" font-size="58" text-anchor="middle" dominant-baseline="central">🛡️</text></svg>')

LOGIN_PAGE = """<!doctype html><html lang=de><head><meta charset=utf-8>
<title>KB Admin · Login</title><link rel="icon" href="/admin/favicon.svg"><style>
body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:linear-gradient(135deg,#1e3a5f,#2d6a9f);display:flex;align-items:center;justify-content:center;min-height:100vh}
.box{background:#fff;padding:32px 28px;border-radius:12px;box-shadow:0 10px 40px rgba(0,0,0,.3);min-width:320px}
h1{margin:0 0 4px;font-size:1.4rem;color:#1e3a5f}
.muted{color:#888;font-size:13px;margin-bottom:18px}
input{width:100%;padding:10px;font:inherit;border:1px solid #bbb;border-radius:6px;margin-bottom:10px;box-sizing:border-box}
button{width:100%;padding:10px;font:inherit;background:#1e3a5f;color:#fff;border:none;border-radius:6px;cursor:pointer}
button:hover{background:#2d6a9f}
.err{background:#fcd6d6;color:#7a1d1d;padding:8px 12px;border-radius:6px;margin-bottom:10px;font-size:14px}
.hint{color:#999;font-size:12px;margin:-4px 0 14px}
</style></head><body><div class=box>
<h1>🛡️ KB Admin</h1><p class=muted>Anmeldung</p>
__ERR__
<form method=post action="/admin/login">
<input type=email name=email placeholder="E-Mail (leer = Admin)" autocomplete=username>
<input type=password name=password placeholder="Passwort" required autofocus autocomplete=current-password>
<p class=hint>Admin: E-Mail leer lassen + Master-Passwort. Moderator: eigene E-Mail + Plattform-Passwort.</p>
<button>Anmelden</button>
</form></div></body></html>"""

PAGE = """<!doctype html><html lang=de><head><meta charset=utf-8><title>KB Admin</title><link rel="icon" href="/admin/favicon.svg"><style>
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
.tag.mod{background:#e7dcff;color:#4a2b8c}
.msg{padding:10px 14px;border-radius:6px;margin:12px 0;font-size:14px;word-break:break-word}
.msg.ok{background:#d6f5dc;color:#1b5e20}.msg.err{background:#fcd6d6;color:#7a1d1d}
.topbar{display:flex;justify-content:space-between;align-items:center;gap:12px}
.who{font-size:13px;color:#555}
.adminbox{margin-top:32px;padding:16px;border:1px solid #e6e6e6;border-radius:8px;background:#fafafa}
.adminbox h2{margin-top:0}
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
function harddel(em){
  if(!confirm("ENDGÜLTIG löschen: "+em+"\n\nWird auf ALLEN Plattformen unwiderruflich entfernt.\nNicht umkehrbar!")) return;
  var c = prompt("Zur Bestätigung die komplette E-Mail eintippen:");
  if(c===null) return;
  if(c.trim().toLowerCase() !== em.toLowerCase()){ alert("Bestätigung stimmt nicht – abgebrochen."); return; }
  var f = document.createElement("form");
  f.method = "POST"; f.action = "/admin/harddelete";
  var i1 = document.createElement("input"); i1.name = "email"; i1.value = em; f.appendChild(i1);
  var i2 = document.createElement("input"); i2.name = "confirm"; i2.value = c.trim(); f.appendChild(i2);
  // Der Server löscht synchron über drei Plattformen (mehrere Sekunden) -> Overlay
  // gegen Doppelklick/Wegnavigieren während des laufenden, irreversiblen Vorgangs.
  var o = document.createElement("div");
  o.style.cssText = "position:fixed;inset:0;background:rgba(0,0,0,.5);display:flex;align-items:center;justify-content:center;z-index:9999;color:#fff;font:600 18px system-ui,-apple-system,sans-serif";
  o.textContent = "Lösche " + em + " … bitte warten";
  document.body.appendChild(o);
  document.body.appendChild(f); f.submit();
}
</script>
</head><body><div class=wrap>
<div class=topbar><h1>🛡️ KB Admin</h1>
<div class=topbar><span class=who>__WHO__</span>
<form method=post action="/admin/logout" style="margin:0"><button class=ghost>Abmelden</button></form></div></div>
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
__ADMINBOX__
</div></body></html>"""

ADMINBOX = """<div class=adminbox>
<h2>🔑 Admin-UI-Passwort ändern</h2>
<p class=muted>Setzt das Master-Passwort dieser Admin-Oberfläche (wird in state.json gespeichert und überschreibt den .env-Wert).</p>
<form class=row method=post action="/admin/setadminpw">
<input class=flex name=password type=password placeholder="Neues Admin-Passwort (min. 8)" required minlength=8 autocomplete=new-password>
<button>Admin-Passwort setzen</button></form>
</div>"""

def tag(s):
    cls = "ok" if s == "aktiv" else ("off" if s == "deaktiviert" else "none")
    return f'<span class="tag {cls}">{html.escape(s)}</span>'

def render_rows(users, role, mods):
    is_admin = role == "admin"
    out = []
    for u in users:
        actions = []
        e = html.escape(u["email"])
        any_active = "aktiv" in (u["dm"], u["bw"], u["nc"])
        any_inactive = "deaktiviert" in (u["dm"], u["bw"], u["nc"])
        protected = u["email"] in PROTECTED
        is_mod = u["email"] in mods
        if protected:
            actions.append('<span class="tag none">🔒 geschützt</span>')
        else:
            if any_active:
                actions.append(f'<form method=post action="/admin/deactivate" style="display:inline;margin:0"><input type=hidden name=email value="{e}"><button class=danger>Deaktivieren</button></form>')
            if any_inactive:
                actions.append(f'<form method=post action="/admin/activate" style="display:inline;margin:0"><input type=hidden name=email value="{e}"><button class=ghost>Reaktivieren</button></form>')
        if u["email"] not in PW_LOCKED:
            actions.append(f'<button class=ghost onclick="pw(\'{e}\')">PW ändern</button>')
        # Moderator-Toggle nur für Admin und nur für nicht-geschützte Accounts.
        if is_admin and not protected:
            act = "remove" if is_mod else "add"
            lbl = "− Moderator" if is_mod else "+ Moderator"
            actions.append(f'<form method=post action="/admin/mod" style="display:inline;margin:0">'
                           f'<input type=hidden name=email value="{e}"><input type=hidden name=action value="{act}">'
                           f'<button class=ghost>{lbl}</button></form>')
        # Hard-Delete: nur Admin, nie für geschützte Accounts (endgültig, alle Plattformen).
        if is_admin and not protected:
            actions.append(f'<button class=danger onclick="harddel(\'{e}\')">🗑 Endgültig löschen</button>')
        badge = (" 🛡" if u.get("bw_staff") else "") + (" ★" if u.get("nc_super") else "")
        modtag = ' <span class="tag mod">MOD</span>' if (is_mod and is_admin) else ""
        out.append(f'<tr><td>{e}{badge}{modtag}</td><td>{html.escape(u["name"])}</td>'
                   f'<td>{tag(u["dm"])}</td><td>{tag(u["bw"])}</td><td>{tag(u["nc"])}</td>'
                   f'<td><div class=act>{" ".join(actions)}</div></td></tr>')
    return "\n".join(out)

def render_page(sess, msg_html=""):
    role = sess["role"]
    mods = moderators() if role == "admin" else set()
    who = "Angemeldet als Admin" if role == "admin" else f"Moderator: {html.escape(sess.get('email',''))}"
    adminbox = ADMINBOX if role == "admin" else ""
    return (PAGE.replace("__WHO__", who)
                .replace("__MSG__", msg_html)
                .replace("__ROWS__", render_rows(combined(), role, mods))
                .replace("__ADMINBOX__", adminbox))

def render_login(err=False):
    return LOGIN_PAGE.replace("__ERR__", '<div class="err">Anmeldung fehlgeschlagen</div>' if err else "")

# ============================== Sessions ====================================
SESSIONS = {}   # token -> {"role": "admin"|"mod", "email": str}

def new_session(role, email=""):
    t = secrets.token_urlsafe(32)
    SESSIONS[t] = {"role": role, "email": email}
    return t

def session_of(handler):
    for kv in handler.headers.get("Cookie", "").split(";"):
        k, _, v = kv.strip().partition("=")
        if k == "admin_sess" and v in SESSIONS:
            return SESSIONS[v]
    return None

def drop_sessions_for(email):
    for t in [t for t, s in SESSIONS.items() if s.get("email") == email]:
        SESSIONS.pop(t, None)

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
        if p.path.endswith("/favicon.svg"):
            b = FAVICON.encode("utf-8")
            self.send_response(200); self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Cache-Control", "max-age=86400")
            self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b); return
        qs = urllib.parse.parse_qs(p.query)
        sess = session_of(self)
        if not sess:
            return self._html(render_login(err=(qs.get("e") == ["1"])))
        msg_html = ""
        if "msg" in qs:
            m = qs["msg"][0]; cls = "ok" if m.startswith("ok:") else "err"
            msg_html = f'<div class="msg {cls}">{html.escape(m[3:] if len(m) > 3 else m)}</div>'
        try: self._html(render_page(sess, msg_html))
        except Exception as ex: self._html(f"<pre>{html.escape(str(ex))}</pre>", 500)
    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path.rstrip("/")
        ln = int(self.headers.get("Content-Length", "0"))
        data = {k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(ln).decode()).items()}
        if path.endswith("/login"):
            email = data.get("email", "").strip().lower()
            pw = data.get("password", "")
            role = who = None
            if not email:                              # Admin-Login (Master-Passwort)
                if check_admin_pw(pw): role, who = "admin", ""
            else:                                      # Moderator-Login
                if email in moderators() and email not in PROTECTED and dm_verify(email, pw):
                    role, who = "mod", email
            if role:
                tok = new_session(role, who)
                self.send_response(303)
                self.send_header("Set-Cookie", f"admin_sess={tok}; HttpOnly; Path=/admin; SameSite=Strict; Max-Age=43200")
                self.send_header("Location", "/admin/"); self.end_headers(); return
            return self._redirect("/admin/?e=1")
        if path.endswith("/logout"):
            for kv in self.headers.get("Cookie", "").split(";"):
                k, _, v = kv.strip().partition("=")
                if k == "admin_sess": SESSIONS.pop(v, None)
            self.send_response(303)
            self.send_header("Set-Cookie", "admin_sess=; HttpOnly; Path=/admin; Max-Age=0")
            self.send_header("Location", "/admin/"); self.end_headers(); return
        sess = session_of(self)
        if not sess:
            return self._redirect("/admin/")
        is_admin = sess["role"] == "admin"
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
                if email in PROTECTED: return self._msg("err:Geschützter Account – hier nicht änderbar")
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
                # Deaktivierter Moderator verliert Mod-Status + offene Sessions.
                if not active and email in moderators():
                    update_state(lambda st: st.__setitem__("moderators", [m for m in st.get("moderators", []) if m != email]))
                    drop_sessions_for(email)
                return self._msg(f"ok:{email} überall " + ("aktiviert" if active else "deaktiviert"))
            # ---- admin-only ab hier ----
            if path.endswith("/setadminpw"):
                if not is_admin: return self._msg("err:Nur Admin")
                pw = data.get("password", "")
                if len(pw) < 8: return self._msg("err:Admin-Passwort min. 8 Zeichen")
                update_state(lambda st: st.__setitem__("admin_pw_hash", hash_pw(pw)))
                return self._msg("ok:Admin-UI-Passwort geändert")
            if path.endswith("/mod"):
                if not is_admin: return self._msg("err:Nur Admin")
                email = data.get("email", "").strip().lower(); action = data.get("action", "")
                if not EMAIL_RE.match(email): return self._msg("err:Ungültige E-Mail")
                if email in PROTECTED: return self._msg("err:Geschützter Account – kein Moderator")
                if action == "add":
                    if email not in known_emails(): return self._msg("err:Unbekannte E-Mail – Nutzer existiert nicht")
                    update_state(lambda st: st.__setitem__("moderators", sorted(set(st.get("moderators", [])) | {email})))
                    return self._msg(f"ok:{email} ist jetzt Moderator")
                if action == "remove":
                    update_state(lambda st: st.__setitem__("moderators", [m for m in st.get("moderators", []) if m != email]))
                    drop_sessions_for(email)
                    return self._msg(f"ok:{email} ist kein Moderator mehr")
                return self._msg("err:Unbekannte Aktion")
            if path.endswith("/harddelete"):
                if not is_admin: return self._msg("err:Nur Admin darf endgültig löschen")
                email = data.get("email", "").strip().lower()
                if not EMAIL_RE.match(email): return self._msg("err:Ungültige E-Mail")
                if email in PROTECTED: return self._msg("err:Geschützter Account – nicht löschbar")
                if data.get("confirm", "").strip().lower() != email:
                    return self._msg("err:Bestätigung stimmt nicht – nicht gelöscht")
                # Phase 1: read-only Blocker-Checks auf ALLEN Plattformen (nichts mutieren).
                try:
                    blk = []
                    for label, fn in (("Docmost", dm_delete_blockers), ("NocoDB", nc_delete_blockers), ("Baserow", bw_delete_blockers)):
                        b = fn(email)
                        if b: blk.append(f"{label}: {', '.join(b)}")
                except Exception as ex:
                    return self._msg("err:Prüfung fehlgeschlagen: " + type(ex).__name__ + " " + str(ex)[:150])
                if blk:
                    return self._msg("err:Nicht gelöscht (besitzt Inhalte) – " + " | ".join(blk) + ". Stattdessen deaktivieren.")
                # Phase 2: jetzt löschen — Docmost ZUERST. Es ist die einzige Plattform mit
                # NO-ACTION-FKs, also die einzige, die trotz Phase-1-Check noch an einer FK
                # scheitern könnte; schlägt sie fehl, ist anderswo noch nichts gelöscht.
                # (Echte Atomarität über drei separate DBs ist nicht möglich -> Reihenfolge
                # + per-Plattform-Report; Cleanup nur bei vollständigem Erfolg.)
                done, errs = [], []
                for label, fn in (("Docmost", dm_hard_delete), ("Baserow", bw_hard_delete), ("NocoDB", nc_hard_delete)):
                    try: fn(email); done.append(label)
                    except Exception as ex: errs.append(f"{label}: {type(ex).__name__}")
                if errs:
                    return self._msg(f"err:Teilweise gelöscht – OK: {', '.join(done) or 'keine'} | Fehler: {'; '.join(errs)}. Bitte manuell prüfen.")
                # nur bei Voll-Erfolg: Mod-Status + offene Sessions des Users entfernen
                if email in moderators():
                    update_state(lambda st: st.__setitem__("moderators", [m for m in st.get("moderators", []) if m != email]))
                drop_sessions_for(email)
                return self._msg(f"ok:{email} endgültig auf allen Plattformen gelöscht")
            self._html("Not Found", 404)
        except Exception as ex:
            self._msg("err:" + type(ex).__name__ + ": " + str(ex)[:200])

if __name__ == "__main__":
    threading.Thread(target=seed_all, daemon=True).start()
    with socketserver.ThreadingTCPServer(("", PORT), H) as srv:
        srv.allow_reuse_address = True
        print(f"KB Admin auf :{PORT}", flush=True); srv.serve_forever()
