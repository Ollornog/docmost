# Testen des `databaseTable`-Blocks (Custom-Docmost-Fork)

> Ziel: nach **jedem Docmost-Update / Rebuild** schnell und zuverlässig prüfen, dass der
> Custom-Block (Baserow/NocoDB-Public-View → native Tabelle) noch intakt ist.
> Vorgeschichte/Build-Details: siehe `../../DOCMOST-FORK.md`.

## Test-Stufen im Überblick

| Stufe | Was wird geprüft | Wie | Browser nötig? |
|------|------------------|-----|----------------|
| 1 | **Build** läuft (Community-Edition, ohne privates `ee`-Submodul) | `docker build` bzw. `roundtrip.sh --build` | nein |
| 2 | **Server-Schema** akzeptiert den Node (kein „unknown node type") | `roundtrip.sh` (automatisch) | nein |
| 3 | **Client-Bundle** enthält Block (Slash-Menü, View) | grep im Image (s.u.) | nein |
| 4 | **Daten-Pfad**: die exakten Baserow/NocoDB-Public-APIs liefern noch die erwartete Form | curl gegen die Public-Views (s.u.) | nein |
| 5 | **End-to-End**: Link einfügen → native Tabelle rendert im Editor | manuell im Browser | **ja** |

Stufen 1–4 sind headless/automatisierbar (CI-tauglich), Stufe 5 ist der manuelle Abschluss-Check.

---

## Stufe 1+2 — Automatischer Regressionstest (der wichtigste)

```bash
bash extras/test/roundtrip.sh           # gegen vorhandenes docmost-custom:local
bash extras/test/roundtrip.sh --build   # Image vorher frisch bauen
```

Was es tut: baut (optional) das Image, startet eine **isolierte Wegwerf-Instanz** auf Port **3009**
(eigener Compose-Projektname, Postgres auf tmpfs — berührt keinen produktiven Stack), legt per API
eine Seite mit **2 `databaseTable`-Nodes** (Baserow + NocoDB) an, liest sie zurück und prüft, dass
**beide Nodes inkl. `src`/`source` erhalten** sind. Danach Teardown. **Exit 0 = PASS, 1 = FAIL.**

> Warum das der Kerntest ist: Der häufigste Update-Fehler ist, den Node serverseitig **nicht** zu
> registrieren (`apps/server/src/collaboration/collaboration.util.ts` — Import **und**
> `tiptapExtensions`-Array). Dann speichert Docmost die Seite, wirft den unbekannten Node aber weg →
> die Tabellen verschwinden. Genau das fängt der Round-Trip ab.

## Stufe 3 — Block im Client-Bundle?

```bash
IMG=docmost-custom:local
docker run --rm --entrypoint sh "$IMG" -c \
  'grep -rl "Datenbank-Tabelle" /app/apps/client/dist >/dev/null && echo "Slash-Label OK"; \
   grep -roh "databaseTable" /app/apps/client/dist | head -1'
```
Erwartung: „Slash-Label OK" + `databaseTable`. Fehlt es, wurde der Client-Teil
(`extensions.ts` / `menu-items.ts` / die View) beim Forward-Port vergessen.

## Stufe 4 — Daten-Pfad: liefern die Public-APIs noch die erwartete Form?

Der Block macht im **Browser** direkte CORS-`fetch()` gegen diese Endpunkte. Nach einem
Baserow/NocoDB-Update hier gegenchecken (Pfade/Felder können driften — dann
`apps/client/.../database-table/database-table-view.tsx` anpassen):

```bash
# Baserow (Public Grid View teilen -> Slug aus dem Link):  .../public/grid/<SLUG>
BW=http://<SERVER_IP>:8888; SLUG=<slug>
curl -s "$BW/api/database/views/$SLUG/public/info/"          | jq '.fields[] | {id,name}'   # Header
curl -s "$BW/api/database/views/grid/$SLUG/public/rows/?size=5" | jq '.results[0]'           # Zellen = field_<id> !

# NocoDB (Shared Grid View -> UUID aus dem Link):  .../nc/view/<UUID>
NC=http://<SERVER_IP>:8089; UUID=<uuid>
curl -s "$NC/api/v2/public/shared-view/$UUID/meta" | jq '{title, columns: [.columns[].title]}'
curl -s "$NC/api/v2/public/shared-view/$UUID/rows" | jq '.list[0]'
```
Wichtig: Baserow-Zellen sind nach **`field_<id>`** indiziert (Klartextname nur über `/public/info/`).
CORS muss bei beiden `*` sein (Stand 2026 gegeben) — sonst lädt der Block nicht (kein Server-Proxy).

## Stufe 5 — End-to-End im Browser (manueller Abschluss)

Voraussetzung: laufender Stack (Docmost `:3002`, Baserow `:8888`, NocoDB `:8089`) **über den
korrekten Host** (= `SERVER_IP` aus `.env`, **nicht** `localhost` — Baserow/NocoDB prüfen den Host
strikt, sonst 404).

1. **Baserow:** Tabelle anlegen → Grid-View → „Share" → Public-Link erzeugen → Link kopieren.
2. **NocoDB:** Tabelle anlegen → Grid-View → „Share View" → Public-Link kopieren.
3. **Docmost** (`:3002`, Seite öffnen):
   - **Slash-Test:** `/` tippen → „**Datenbank-Tabelle**" wählen → Public-Link ins Popover einfügen.
   - **Paste-Test:** denselben Link **direkt in die Seite einfügen** → Paste-Rule macht automatisch
     einen Block daraus.
   - **Erwartung:** native Tabelle mit Spalten/Zeilen + „aktualisieren"-Knopf. Mehrere Blöcke pro Seite.
4. **Reload-Test:** Seite neu laden → Tabellen sind weiterhin da (Persistenz/Schema ok).

**Bekannte Fallen bei Stufe 5:**
- **Reiner HTTP-Modus:** `navigator.clipboard` braucht Secure Context → der „Zelle kopieren"-Button
  nutzt den `execCommand`-Fallback. Erwartetes Verhalten, kein Bug.
- **Falscher Host:** Zugriff über `localhost`/IP statt des in `.env` gesetzten `SERVER_IP` → Baserow
  liefert 404, der Block bleibt leer.
- **Baserow-Limit:** Public-Rows sind auf **200 Zeilen** begrenzt (keine Pagination im Block).

---

## Nach einem Docmost-Update — Test-Schleife

1. Patch/Code forward-porten wie in `../../DOCMOST-FORK.md` (Abschnitt „Update-Handhabung") beschrieben.
2. `bash extras/test/roundtrip.sh --build`  → muss **PASS** sein (Stufen 1+2).
3. Stufe 3 (Client-Bundle) prüfen.
4. Stufe 4 nur, wenn Baserow/NocoDB ebenfalls aktualisiert wurden (API-Drift).
5. Stufe 5 (Browser) als Abnahme.
6. Erst dann das produktive `docmost`-Image auf das neue `docmost-custom:local` umstellen
   (`docker compose up -d docmost`), Rollback bereithalten.

## Schnell-Smoke gegen einen LAUFENDEN Stack (ohne isolierte Instanz)

```bash
# nutzt die produktive Instanz – legt eine Testseite im Default-Space an (danach ggf. löschen)
# (Setup nur beim allerersten Start möglich; sonst über Login-Cookie testen)
```
Für wiederholbare Tests immer `roundtrip.sh` (isoliert) bevorzugen — verändert keine echten Daten.
