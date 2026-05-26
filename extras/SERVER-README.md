# BRDL Knowledge-Base Server (Prod)

> Self-hosted KB (**Docmost**) + Tabellen-Tool (**Baserow**). Dieses README ist das Server-Gedächtnis —
> vor Änderungen hier reinschauen, nach Änderungen pflegen.
> Eingerichtet: 2026-05-22.

## 0. Zugang / Accounts
- **SSH:** `root@<IP>` per Key (Claude-Key `claude@brdl-test` in `/root/.ssh/authorized_keys`).
- **App-Admin (Docmost + Baserow):** `daniel.brunthaler@skidata.com`, Anzeigename **BRDL**.
  Passwort: 13371337 (interner HTTP-Testserver). Docmost-Rolle `owner`, Baserow `staff+superuser`.
- **Registrierung:** in beiden Apps **deaktiviert** → nur per Einladung.
  - Docmost: self-hosted standardmäßig invite-only (kein offenes Signup).
  - Baserow: `allow_new_signups=False` gesetzt.
  - ⚠️ Einladungen werden **per E-Mail** verschickt → brauchen **SMTP** (siehe §6), sonst kommen sie nicht an.

## 1. Server / Layout
- **OS:** Debian 13 (trixie). **6 vCPU / 15 GB RAM.**
- **Disk:** `/` 14 GB, `/var` 5,4 GB (klein!), **`/srv` 157 GB** = Daten-/Docker-Platte.
- **Docker:** v29 — **data-root = `/srv/docker`**, Storage `overlay2`.
  WICHTIG: Docker 29 nutzt sonst den containerd-Image-Store unter `/var/lib/containerd`
  (auf dem kleinen `/var`!). Deshalb in `/etc/docker/daemon.json`:
  `{"data-root":"/srv/docker","features":{"containerd-snapshotter":false}}`.
- **Stack:** `/srv/stack/` (Compose-Projekt). **Fork-Build:** `/srv/build/docmost`.

## 2. Dienste / URLs
| Dienst | URL | Backend |
|---|---|---|
| Landing | http://<IP> | Caddy (statisch, Links via Template) |
| **Docmost** (Custom-Image mit DB-Tabellen-Block) | **https://<IP>:3002** | Postgres `docmost-db` + Redis |
| **Baserow** | **https://<IP>:8888** | All-in-One (eigenes PG/Redis) |

- **HTTPS** terminiert Caddy mit **interner CA** (`tls internal`) + **`default_sni`** (nötig, weil IP-Zugriff kein SNI sendet).
  → beim ersten Besuch **Zertifikatswarnung akzeptieren** (oder Root-CA importieren, §5).
- App-Container haben **keine** eigenen Host-Ports; nur Caddy published 80/3002/8888.

## 3. Bedienung
```bash
cd /srv/stack
docker compose ps
docker compose up -d <dienst>
docker compose logs -f <dienst>
docker compose restart caddy        # nach Caddyfile-Änderung (oder: docker exec caddy caddy reload --config /etc/caddy/Caddyfile)
```
- Secrets: `/srv/stack/.env` (chmod 600) — `SERVER_IP`, Postgres-PW, `DOCMOST_APP_SECRET`, `BASEROW_SECRET_KEY`. **Nicht committen.**
- Custom-Docmost-Image neu bauen: `cd /srv/build/docmost && git pull && docker build -t docmost-custom:local . && cd /srv/stack && docker compose up -d docmost`.

## 4. ⚠️ IP ändern (DHCP-Wechsel oder neue statische IP)
Aktuell ist die IP **NICHT statisch** (DHCP). Bei Wechsel:
1. `nano /srv/stack/.env` → `SERVER_IP=<neue-IP>`
2. `cd /srv/stack && docker compose up -d` → erstellt Docmost (`APP_URL`), Baserow (`BASEROW_PUBLIC_URL`) und Caddy
   (`default_sni` + Site-Links) mit der neuen IP neu. Caddy stellt automatisch ein **neues internes Cert** für die neue IP aus.
3. Bookmarks/Clients auf neue IP anpassen.
- **Caddy-Cert/`default_sni`** und die **Landing-Links** ziehen die IP automatisch aus `${SERVER_IP}`/`{{env "SERVER_IP"}}`.
- **Achtung:** In Docmost-Seiten **fest eingebettete Baserow-Public-View-Links** enthalten die alte IP → brechen bei IP-Wechsel.
  Lösung: Hostname/DNS statt IP (siehe §5).

## 5. Hostname/DNS + vertrauenswürdiges Zertifikat (empfohlen)
Sobald ein (interner) DNS-Name verfügbar ist:
1. DNS **A-Record** `kb.example.intern` → Server-IP.
2. `/srv/stack/.env`: `SERVER_IP=kb.example.intern` (alle URLs nutzen dann den Namen, IP-Wechsel egal).
3. `cd /srv/stack && docker compose up -d`.
- **Zertifikat** (Warnung loswerden), drei Wege:
  - **Interne CA importieren:** Root-CA liegt unter
    `/srv/stack/caddy/data/caddy/pki/authorities/local/root.crt` → in Browser/OS-Clients als vertrauenswürdig importieren.
  - **Eigenes Firmen-Zertifikat:** im Caddyfile `tls internal` ersetzen durch `tls /pfad/cert.pem /pfad/key.pem` (+ Mount).
  - **Öffentliches ACME** (nur falls der Name je öffentlich auflöst): `tls internal` entfernen → Caddy holt Let's-Encrypt-Cert.
- Optional sauberer: pro Dienst eine Subdomain auf **Port 443** statt :3002/:8888 (Caddyfile-Sites entsprechend umstellen).

## 6. E-Mail / SMTP (für Einladungen + Passwort-Reset!)
- **Aktuell ist KEIN SMTP konfiguriert** → Docmost **und** Baserow versenden keine echten Mails.
  Baserow „verschickt" Mails ins Leere (Konsole), Docmost-Einladungen kommen nicht an.
- Da Registrierung **nur per Einladung** läuft, ist SMTP für neue Nutzer **erforderlich**.
- Konfigurieren (in `/srv/stack/.env` + Compose-Env, dann `docker compose up -d`):
  - **Docmost:** `MAIL_DRIVER=smtp`, `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_SECURE=true|false`, `MAIL_FROM_ADDRESS`, `MAIL_FROM_NAME`.
  - **Baserow:** `EMAIL_SMTP=true`, `EMAIL_SMTP_HOST`, `EMAIL_SMTP_PORT`, `EMAIL_SMTP_USER`, `EMAIL_SMTP_PASSWORD`, `EMAIL_SMTP_USE_TLS=true`, `FROM_EMAIL`.
- Alternativ Nutzer ohne Mail anlegen: Docmost-Invite-Link manuell weitergeben / Baserow-User per Shell.

## 7. Custom-Docmost (databaseTable-Block)
- Image `docmost-custom:local`, gebaut aus Fork **github.com/Ollornog/docmost**, Branch `feat/database-table-block`
  (`main` enthält denselben Stand auf aktuellem Docmost). Doku/Update-Anleitung im Repo: `DOCMOST-FORK.md`, Patch `databaseTable-block.patch`.
- Funktion: Baserow/NocoDB **Public-View-Link → native Tabelle** in Docmost (kein iframe), mit Breite/Header/Hintergrund/Rahmen-Toggles,
  Zellauswahl + Kopieren (Clipboard mit `execCommand`-Fallback für HTTP), Edit-Deeplink.

## 8. Backups (NOCH OFFEN — TODO!)
- **Noch kein Backup eingerichtet.** Empfohlen (täglich, nach `/srv/backups`, Rotation):
  - Docmost-DB: `docker exec docmost-db pg_dump -U docmost docmost | gzip > ...`
  - Docmost-Storage: `/srv/stack/docmost/data`
  - Baserow: `docker exec baserow ./baserow.sh backend-cmd-with-db manage dumpdata ...` bzw. PG-Dump der embedded DB + `/srv/stack/baserow/data` (Attachments) sichern.
  - `.env` separat sichern (Secrets!).

## 9. Bekannte Stolperfallen
- Docker 29 + kleiner `/var` → containerd-Snapshotter aus, data-root `/srv/docker` (§1).
- HTTPS per IP braucht `default_sni` in Caddy (sonst TLS-Handshake-Fehler ohne SNI).
- IP ist DHCP → bei Wechsel §4. Eingebettete IP-Links brechen → Hostname nutzen (§5).
- SMTP fehlt → Einladungen/Passwort-Reset kommen nicht an (§6).

---
## 10. kb-sync (eingerichtet 2026-05-22) — ERSETZT §8-Hinweis "offen"
- **Service-Accounts** (Passwörter in `/srv/stack/.env`: `KBSYNC_BW_PASS`, `KBSYNC_DM_PASS`):
  Baserow `kbsync@brdl.local` (verifiziert), Docmost `kbsync@brdl.local` (Rolle member).
- Privater Baserow-Workspace **„Admin" (id 99)** mit Steuer-Tabelle **„KB-Sync" (id 508)**,
  Felder `Tabelle` / `Status` / `_SeitenID`. Mitglieder: Admin (daniel, ADMIN) + Service. Sonst niemand.
- **Self-service-Bedienung:** Baserow → Workspace „Admin" → Tabelle „KB-Sync" → Zeile anlegen, in Spalte
  `Tabelle` den Namen einer Baserow-Tabelle eintragen → kb-sync legt in Docmost (Space „Auto-Snapshots")
  automatisch eine native Tabelle an + hält sie aktuell (Poll 60 s; `Status`/`_SeitenID` werden automatisch gefüllt).
- ⚠️ Damit kb-sync eine **Quelltabelle** findet, muss `kbsync@brdl.local` **Mitglied des Workspaces** sein,
  in dem diese Tabelle liegt (in Baserow den Workspace mit dem Service-User teilen).
- ⚠️ Snapshots liegen im Docmost-Space **„Auto-Snapshots"** (vom Service-User erstellt). Damit der Admin sie
  sieht: daniel als Mitglied dieses Space hinzufügen, sobald er existiert.
- HTTPS-Resolver (Edit-Deeplinks im DB-Tabellen-Block) via Caddy: `https://<IP>:8090`.

## 11. Backups (eingerichtet 2026-05-22)
- Script **`/srv/stack/backup.sh`**, Cron **täglich 03:00** (root crontab). Ziel `/srv/backups/<datum>/`,
  **Rotation 14 Tage**, Log `/srv/backups/backup.log`.
- Inhalt je Lauf: Docmost-DB (`pg_dump`, gz), Docmost-Storage (tgz), **Baserow** (`manage backup_baserow`), `.env`.
- **Restore (grob):**
  - Docmost-DB: `gunzip -c docmost-db.sql.gz | docker exec -i docmost-db psql -U docmost docmost`
  - Docmost-Storage: nach `/srv/stack/docmost/data` entpacken.
  - Baserow: `BWPW=$(docker exec baserow sh -c 'cut -d= -f2- /baserow/data/.pgpass'); docker exec -e PGPASSWORD="$BWPW" baserow ./baserow.sh backend-cmd-with-db manage restore_baserow -h localhost -d baserow -U baserow -p 5432 -f <baserow.tar.gz>`
- Empfehlung: `/srv/backups` zusätzlich **extern** wegsichern (anderer Host/NAS).

## 12. E-Mail / Verifikation (Ergänzung zu §6)
- Baserow `email_verification = enforced` → eingeladene Nutzer müssen Mail bestätigen. Ohne SMTP nur per Shell
  (`u.profile.email_verified=True`) oder Setting auf `no_verification`. → SMTP bald einrichten (§6).

---
## 13. Produktiv-Content „Netzwerk & Firewall" (2026-05-22)
**Baserow** – DB „Firewall" im Workspace „Netzwerk und Firewall" (id 100):
- **Kunden**, **Gerätetypen** (Stammdaten; Bereich SKI/PARKEN/HANDSHAKE), **Zonen**, **Externe Ziele** (Internet-/KK-Services),
  **Geräte** (pro Kunde, mit IP/Gateway), **Firewall-Regeln** (3 Ebenen), **Soll-Matrix** (abgeleitet).
- **Regeln pflegen** (Tabelle „Firewall-Regeln", Feld `Geltung`):
  - `Allgemein` → gilt für alle.
  - `Gerätegruppe` + `Gerätegruppe`-Link → gilt für jedes Gerät dieses Typs.
  - `Kunde` + `Kunde`-Link → kundenspezifisch.
- **Soll-Matrizen (abgeleitet):**
  - Kundenspezifisch → Tabelle **„Soll-Matrix" + View je Kunde** (Gerätegruppen-Regeln × Geräte des Kunden mit echten IPs + Allgemein + Kunde).
  - Allgemein → Baserow Public-View **„Allgemeine Soll-Matrix"** (Geltung≠Kunde), eingebettet als **native Tabelle** in Docmost-Space „Netzwerk und Firewalls" (Seite „Allgemeine Firewallmatrix", databaseTable-Block).
- **Generator:** `/srv/stack/firewall-gen/gen_soll.py` (läuft per Cron alle 30 Min via `regen.sh`). Manuell: `bash /srv/stack/firewall-gen/regen.sh`.
- **Neuen Kunden anlegen:** Zeile in „Kunden" → Geräte in „Geräte" (Kunde+Gerätetyp+IP+Gateway) → ggf. kundenspez. Regeln. Generator legt Soll-Matrix-Zeilen + Kunden-View automatisch an.
- Docmost-Space „Netzwerk und Firewalls": Seiten „Allgemeine Firewallmatrix" + „Abkürzungen / Glossar".
- Aufbau erfolgte über die kb-sync-Service-Accounts (Baserow WS 100 ADMIN; Docmost NUF-Space Mitglied).

## 14. Root-CA / Zertifikat (wichtig für den databaseTable-Block!)
- Caddy interne Root-CA zum Import: **http://10.19.207.80/caddy-root.crt** (liegt unter /srv/stack/caddy/site/).
- **Muss in Browser/OS als vertrauenswürdige Stammzertifizierungsstelle importiert werden**, sonst kann der databaseTable-Block in Docmost die Baserow-Daten nicht per fetch laden (Cross-Origin-Fetch verlangt gültiges Cert; das Wegklicken der Warnung reicht NICHT).
- Windows: Doppelklick -> Lokaler Computer -> Vertrauenswürdige Stammzertifizierungsstellen. Firefox: eigener Cert-Store.
- Bei späterem echten Hostname/Cert (siehe §5) entfällt das.

## 15. AKTUELLER MODUS: reines HTTP (seit 2026-05-22)
- Auf Wunsch vorerst **kein HTTPS** (Cross-Origin-fetch des databaseTable-Blocks scheiterte sonst am self-signed Cert).
- URLs: **http://<IP>:3002** (Docmost), **http://<IP>:8888** (Baserow), http://<IP>:8090 (kb-sync), http://<IP> (Landing). Caddy: `auto_https off`, reine HTTP-Reverse-Proxies.
- App-URLs: APP_URL/BASEROW_PUBLIC_URL = http://… (in docker-compose.yml).
- **Copy-Buttons im Block** funktionieren über HTTP via execCommand-Fallback.
- §5/§14 (HTTPS/CA) gelten nur, falls HTTPS reaktiviert wird.
- **HTTPS wieder einschalten:** in caddy/Caddyfile die Sites auf `https://{$SERVER_IP}:PORT { tls internal }` + global `default_sni {$SERVER_IP}` zurück, App-URLs in docker-compose.yml auf https://, Block-Seiten-Links + Landing auf https, dann `docker compose up -d` + Caddy-Restart; CA importieren (§14).

## 16. Hostname `<IP>.nip.io` (Workaround für Chrome HTTPS-Memory)
- Nach kurzer HTTPS-Phase hatte Chrome `10.19.207.80` als HTTPS-Site internalisiert (über HSTS hinaus) und upgradete `fetch()` zu https → `ERR_SSL_PROTOCOL_ERROR`.
- Lösung: **SERVER_IP=10.19.207.80.nip.io** in /srv/stack/.env. nip.io ist ein freier öffentlicher Wildcard-DNS (`<IP>.nip.io` → diese IP). Frischer Hostname, den der Browser nie als HTTPS gesehen hat → HTTP funktioniert sauber.
- URLs jetzt: **http://10.19.207.80.nip.io:3002** (Docmost), **http://10.19.207.80.nip.io:8888** (Baserow), :8090 (kb-sync). Landing http://10.19.207.80.nip.io.
- Direkter IP-Zugriff (http://10.19.207.80:8888) gibt jetzt 404 (Baserow prüft Host gegen BASEROW_PUBLIC_URL) — bewusst.
- Bei IP-Wechsel: `SERVER_IP=<neue-IP>.nip.io` in .env, `docker compose up -d`.

## 17. Admin-UI (Nutzer verwalten)
- URL: **http://10.19.207.80.nip.io/admin/**
- Auth: **admin / 13371337** (Caddy BasicAuth, Hash in /srv/stack/caddy/Caddyfile).
- Funktion: legt Nutzer mit Name+E-Mail+Passwort in BEIDEN Apps an (Docmost + Baserow), reaktiviert bei erneutem Anlegen, deaktiviert/reaktiviert.
- Service: /srv/stack/admin/ (Python http.server in Container admin, mounted docker.sock fuer docker exec in docmost/docmost-db/baserow).
- Docmost: direkter DB-Insert (bcrypt via docker exec docmost node).
- Baserow: via Django shell (Settings.allow_new_signups temp. on, UserHandler.create_user, profile.email_verified=True).
