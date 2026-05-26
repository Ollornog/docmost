# Baserow ↔ Docmost Integration — Gesamtdoku

> Bewahrt, was wir erarbeitet haben: Custom-Docmost-Block, kb-sync-Snapshot-Service,
> Admin-UI, Generator-Beispiel, Caddy-Setups und Stolperfallen. Stand bei Projekt-Einstampfung.

## 1. Was ist hier
| Pfad | Inhalt |
|---|---|
| **packages/editor-ext/src/lib/database-table.ts** + **apps/client/.../database-table/** | Der eigentliche Tiptap-Node mit React-NodeView (Custom-Docmost-Block) |
| **apps/server/src/collaboration/collaboration.util.ts** | Server-Schema-Registrierung des Nodes |
| **databaseTable-block.patch** | Additiver Patch aller Code-Aenderungen (re-anwendbar auf neuere Docmost-Versionen) |
| **DOCMOST-FORK.md** | Block-Doku, inkl. Update-Anleitung |
| **PITCH.md** | Upstream-PR-Pitch |
| **extras/kb-sync/sync.py** | Snapshot-Service: liest Baserow-Mapping-Tabelle, schreibt native Docmost-Tabellen |
| **extras/admin/admin.py** + **Dockerfile** | Admin-UI fuer User-Verwaltung in beiden Apps |
| **extras/firewall-gen/** | Beispiel-Generator (Firewall-Soll-Matrix aus Regeln + Geraeten) |
| **extras/caddy/** | Caddyfile-Varianten (HTTP+nip.io / HTTPS+interne CA) + Landing |
| **extras/compose-snippets/docker-compose.full.yml** | Vollstaendige Compose-Datei (Referenz) |
| **extras/backup.sh** | Tagliches Backup-Script (Docmost-DB + Storage, Baserow, .env) |
| **extras/SERVER-README.md** | Server-Operations-Doku (IP-Wechsel, DNS, SMTP, etc.) |

## 2. Architektur in einem Satz
Docmost mit Custom-Block, der Baserow-Public-View-Links als **native Tabelle** rendert (kein iframe), wahlweise live per Browser-fetch ODER per kb-sync-Snapshot als echte Docmost-Seite. Generator-Skripte koennen aus Baserow-Daten abgeleitete Soll-Matrizen erzeugen.

```
   ┌──────────┐         ┌──────────────┐
   │ Browser  │◄────────┤  Caddy       │  Reverse Proxy + Auth + Landing
   └────┬─────┘         └──┬───────┬───┘
        │                  │       │
        │ http(s)          ▼       ▼
        │              docmost  baserow
        │                  ▲       ▲
        │                  │       │  Service-Accounts (kbsync@brdl.local)
        │             ┌────┴───────┴────┐
        │             │   kb-sync       │  liest Mapping aus Baserow,
        │             │   (Container)   │  schreibt Snapshot-Seiten in Docmost
        │             └─────────────────┘
        │
        └─► databaseTable-Block (live fetch direkt zu Baserow)
```

## 3. Der databaseTable-Block (Kurzform — Details in DOCMOST-FORK.md)
- **Was:** Tiptap-Node `databaseTable`. Slash-Command „Datenbank-Tabelle", Paste-Rule fuer Baserow-/NocoDB-Public-View-Links.
- **Wie:** React-NodeView holt info+rows per fetch im Browser, baut Mantine-Table.
- **Features:** Breiten-Modi (Normal/Angepasst/Minimal), Header-Zeile/-Spalte, Zeilennummern, Header-Farbe (orange/grau/weiss), Streifen, Rahmen-Modi, Zellauswahl + Strg+C, Tabelle-kopieren, Live-Refresh, Edit-Deeplink (via kb-sync-Resolver), **Baserow-Group-By respektiert (rowspan-Zellverbund)**.

## 4. kb-sync (Snapshot-Service)
**Zweck:** Aus einer Baserow-Tabelle wird automatisch eine native Tabelle in einer Docmost-Seite gebaut/aktualisiert. **Selbstbedienung** ueber eine Baserow-Mapping-Tabelle, kein Code-Eingriff noetig.

### Setup
1. Baserow-Workspace „Admin" (nur Admin + kb-sync-Service als Mitglied).
2. Tabelle **„KB-Sync"** mit Spalten:
   - **Tabelle** (Text, Primary) — Name der Baserow-Tabelle, die gespiegelt werden soll.
   - **Status** (Long Text, auto-gefuellt) — z. B. „✅ N Zeilen", Fehlermeldung etc.
   - **_SeitenID** (Text, auto-gefuellt) — die Docmost-Page-ID, sobald angelegt.
3. Service-Account **kbsync@brdl.local** (in Baserow + Docmost). Baserow-Email verifiziert! Sonst 401.
4. Docker-Service mit Env (siehe **extras/kb-sync/sync.py** Kommentar oben):
   - BASEROW_URL, BASEROW_HOST, BASEROW_EMAIL, BASEROW_PASSWORD, BASEROW_WORKSPACE, MAPPING_TABLE_ID
   - DOCMOST_URL, DOCMOST_HOST, DOCMOST_EMAIL, DOCMOST_PASSWORD, DEFAULT_DOCMOST_SPACE
   - POLL_SECONDS, PORT
5. Endpoints: GET /refresh (HTML, manueller Trigger), POST /webhook, GET /resolve?slug=… (gibt Edit-URL zur Public-View zurueck — wird vom Block fuer den „Bearbeiten"-Knopf gebraucht).

### Loop pro Poll
1. Lese alle Zeilen der KB-Sync-Mapping-Tabelle.
2. Pro Zeile: Baserow-Tabelle per Name finden, Zeilen lesen, **Markdown-Tabelle** generieren.
3. Wenn _SeitenID leer: Docmost-Seite anlegen, ID zurueck in die Zeile schreiben.
4. Sonst: existierende Seite per `pages/update {operation:'replace',format:'markdown'}` aktualisieren.
5. Status-Spalte mit Ergebnis (oder Fehlermeldung) befuellen.

### Wichtig
- Docmost-`pages/update` benoetigt **`format:'markdown'`** + `operation:'replace'` (Collab-Editor uebernimmt sonst).
- Baserow-API: `/api/database/rows/table/<id>/?user_field_names=true&size=200` — schluessel sind Feldnamen.
- `BASEROW_HOST`/`DOCMOST_HOST` muessen exakt zur jeweiligen `BASEROW_PUBLIC_URL`/`APP_URL` passen (Host-Header-Check).

## 5. Admin-UI
**Zweck:** Web-UI zum Anlegen/Deaktivieren/Reaktivieren von Nutzern in **beiden** Apps gleichzeitig + Passwort-Reset. Hinter Login.

### Code in extras/admin/
- **admin.py** — http.server (stdlib) mit eigener Cookie-Auth.
- **Dockerfile** — python:3.12-alpine + docker-cli (mountet /var/run/docker.sock fuer `docker exec`-Operationen).

### Was passiert wo
- **Docmost-Anlegen:** bcrypt-Hash via `docker exec docmost node -e ...` -> direkter INSERT in users-Tabelle (ON CONFLICT (email, workspace_id)).
- **Baserow-Anlegen:** Django-Shell — Settings.allow_new_signups temporaer an, UserHandler.create_user(...), `profile.email_verified=True`, Setting zurueck.
- **Deaktivieren:** Docmost setzt deactivated_at + loescht user_sessions; Baserow setzt is_active=False.
- **Passwort-Reset:** UPDATE im DB (Docmost) + `u.set_password()` (Baserow).

### Schutz
- **PROTECTED** = {kbsync@brdl.local, daniel.brunthaler@skidata.com} — keine Deaktivierung.
- **KBSYNC** = kbsync@brdl.local — zusaetzlich kein Passwort-Reset (wuerde die Service-Anmeldung brechen, Pwd liegt in .env).

### Caddy-Routing
```
:80 {
  @admin path /admin /admin/*
  handle @admin { reverse_proxy admin:8091 }
  handle { root * /srv/site; templates; file_server }
}
```
Admin-Container braucht docker.sock (RW), Netzwerk „frontend".

### Warum nicht Caddy-BasicAuth?
Wir haben das probiert. Chrome zickt: zerlegt manchmal `chrome-error://chromewebdata`-Loops, Inkognito-Probleme. Eigene Cookie-Auth ist robuster und konsistenter ueber alle Browser.

## 6. Firewall-Generator (Use-Case-Beispiel)
**Modell:** 6 Baserow-Tabellen — Kunden, Geraetetypen, Geraete, Zonen, Externe Ziele, Firewall-Regeln. Regeln haben drei Ebenen ueber Feld `Geltung`:
- **Allgemein** — gilt fuer alle.
- **Geraetegruppe** + `Geraetegruppe`-Link — gilt fuer jedes Geraet dieses Typs.
- **Kunde** + `Kunde`-Link — kundenspezifisch.

**Generator (extras/firewall-gen/gen_soll.py):**
- Liest Regeln, Geraete, Kunden.
- Pro Kunde: expandiert die Geraetegruppen-Regeln auf die tatsaechlichen Geraete dieses Kunden (mit echten IPs eingesetzt) + Allgemein + Kunde.
- Schreibt in eine separate Baserow-Tabelle **„Soll-Matrix"** mit Spalte Kunde + erzeugt pro Kunde eine gefilterte Grid-View.
- Cron-Wrapper **regen.sh** alle 30 Min via `docker exec baserow ./baserow.sh backend-cmd-with-db manage shell < gen_soll.py`.

Dazu eine **allgemeine** Public-Grid-View (Geltung != Kunde), die im Docmost-Block als native Tabelle eingebettet wird.

## 7. Caddy — zwei Varianten

### A) HTTP + nip.io (was wir am Ende benutzt haben)
**Datei:** extras/caddy/Caddyfile.http  
**Hostname:** `<IP>.nip.io` als Wildcard-DNS (oeffentlicher Service, loest jede `<IP>.nip.io` auf die IP auf). Genutzt, weil Browser nach kurzer HTTPS-Phase hartnaeckig HTTPS erzwingen wollten (HSTS persistent ueber Inkognito). Ein **frischer** Hostname umgeht das.

**Trade-offs:**
- Vorteil: kein Cert-Import pro Client, kein DNS noetig.
- Nachteil: Clipboard-API tot (kein Secure Context) — der Block hat dafuer `execCommand`-Fallback. Manche Apps (Baserow „Link kopieren"-Button) crashen ohne Workaround.

### B) HTTPS + interne CA (sauber, aber CA-Import pro Client)
**Datei:** extras/caddy/Caddyfile.https-internal  
Caddy `tls internal` erzeugt eigene CA, signiert Certs fuer den Server. **Default-SNI noetig**, sonst TLS-Handshake-Fehler bei IP-Zugriff (Browser sendet kein SNI fuer IP-Literals).

**Trade-offs:**
- Vorteil: Clipboard, Secure Cookies, alles sauber.
- Nachteil: Root-CA (extras/caddy/data/caddy/pki/authorities/local/root.crt) muss in Browsern/OS als vertrauenswuerdig importiert werden — sonst Cross-Origin-fetch im Block bricht.

### C) HTTPS + echte Certs (Empfehlung fuer Produktion)
**Let's-Encrypt mit DNS-01-Challenge** (z. B. Caddy + caddy-dns/hetzner-Plugin). Domain bei einem DNS-Provider mit API. Server selbst muss **nicht** oeffentlich erreichbar sein — nur API-Calls zur DNS-Zone. Browser vertrauen automatisch, kein Client-Setup.

## 8. Compose & ENV — was muss gesetzt sein
**Siehe extras/compose-snippets/docker-compose.full.yml** fuer den fertigen Stand.

**Kritische ENV-Variablen (.env):**
- SERVER_IP — entweder die IP, **<IP>.nip.io**, oder ein Hostname. Wird in APP_URL/BASEROW_PUBLIC_URL substituiert.
- DOCMOST_PG_DB/USER/PASS, DOCMOST_APP_SECRET — random.
- BASEROW_SECRET_KEY — random.
- KBSYNC_BW_PASS, KBSYNC_DM_PASS — Passwoerter der Service-Accounts.
- DOCMOST_WORKSPACE_ID — die UUID des einen Docmost-Workspaces.

**Container-Netzwerke:**
- frontend: docmost, baserow, kb-sync, admin, caddy
- backend: docmost-db, docmost-redis, docmost

## 9. Backups
**extras/backup.sh** (Cron taeglich 03:00):
- Docmost: pg_dump + tgz vom Storage.
- Baserow: `manage backup_baserow` (Argumente exakt: -h localhost -d baserow -U baserow -p 5432, PGPASSWORD aus /baserow/data/.pgpass).
- .env separat.
- Rotation: 14 Tage.
**Wichtig:** zusaetzlich extern wegsichern (lokales Backup haelt nicht bei VM-Verlust).

## 10. Stolperfallen die wir gelernt haben
1. **HSTS persistent** — Caddys `tls internal` setzt HSTS-Header. Browser merken sich das auch in Inkognito und upgrade danach jeden http-fetch zu https. Loeschen via chrome://net-internals/#hsts reicht oft NICHT, weil Chrome weitere interne Speicher hat (HTTPS-First-Mode, Site-Engagement). **Konsequenz:** wenn ein Host je HTTPS gesehen wurde, ist HTTP danach in Chrome schwer wieder durchsetzbar. Alternative: anderer Hostname (nip.io-Trick).
2. **Cross-Origin-fetch zu self-signed Cert ist tot** — auch wenn der User „Trotzdem fortfahren" beim Browsen geklickt hat, fetch() von einem anderen Origin zu dem Cert-Host scheitert. Loesung: CA importieren oder echtes Cert.
3. **Secure Context fuer navigator.clipboard** — nur HTTPS oder localhost. Eigene Buttons brauchen `execCommand`-Fallback.
4. **Baserow Host-Header strikt** — wenn `BASEROW_PUBLIC_URL=http://10.19.207.80:8888` gesetzt ist, antwortet Baserow bei `Host: 10.19.207.80.nip.io:8888` mit 404. Loesung: `SERVER_IP` in .env auf den verwendeten Hostnamen aendern, alles recreaten.
5. **Baserow Email-Verifikation** — Setting `email_verification = enforced` (Default). Per API/Shell angelegte Nutzer sind `profile.email_verified = False` und koennen nicht einloggen. Loesung: `u.profile.email_verified = True; u.profile.save()`. Macht das Admin-UI automatisch.
6. **Baserow `manage backup_baserow`** — verlangt -h/-d/-U/-p **und** PGPASSWORD im Env. PGPASSFILE klappt nicht, weil /baserow/data/.pgpass kein .pgpass-Format ist, sondern `export DATABASE_PASSWORD=...`. Erst cut + PGPASSWORD setzen.
7. **Docmost Unique-Constraint** auf `(email, workspace_id)`, nicht `email` allein. `ON CONFLICT (email)` schlaegt fehl, `ON CONFLICT (email, workspace_id)` geht.
8. **Docker 29 + kleines /var** — containerd-Snapshotter ist default an, speichert Images unter /var/lib/containerd. `data-root` in daemon.json greift nur fuer Docker-eigene Daten. Loesung: `{"features": {"containerd-snapshotter": false}}` + `"data-root": "/srv/docker"`.
9. **Baserow Group-By-View** zeigt im Public-API standardmaessig keine Felder (fields: []), bis pro Feld eine GridViewFieldOptions-Zeile mit `hidden=False` existiert. Bei API-erzeugten Views muss man die anlegen — `vh.update_field_options(...)` BRAUCHT `transaction.atomic()`, sonst „select_for_update outside transaction".
10. **Caddy `basicauth` vs eigene Cookie-Auth** — Chrome zickt bei BasicAuth in Inkognito und mit chrome-error-Loops. Eigene Cookie-Auth im Admin-Container ist robuster.
11. **Mantine-Striped + rowspan** — eine rowspan-Zelle erbt den Streifen-BG der ersten Gruppen-Zeile, nicht den eigenen. Loesung: expliziter BG (var(--mantine-color-body) bzw. gray-0), nicht `transparent`, sonst scheint Streifen durch.

## 11. Wo war das im Produktivbetrieb?
Zwei Server (beide jetzt eingestampft):
- Eval **10.19.206.121** — /opt/stack/ — alles in einem (Docmost, Docmost-Dev mit Custom-Image, Baserow, NocoDB, MediaWiki, kb-sync, Caddy, Mailpit).
- Prod-Versuch **10.19.207.80** — /srv/stack/ — schlanker: Docmost (Custom-Image), Baserow, kb-sync, admin, Caddy. **Beschrieben in extras/SERVER-README.md**.

Beide Setups bauen auf der **gleichen** Code-Basis dieses Forks (Branch `feat/database-table-block`).
