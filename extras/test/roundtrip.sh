#!/usr/bin/env bash
# =============================================================================
# roundtrip.sh — automatischer Regressionstest für den databaseTable-Block.
#
# Beweist nach einem Docmost-Update / Rebuild, dass der Custom-Block intakt ist:
#   1. (optional) Custom-Image bauen
#   2. isolierte Wegwerf-Instanz starten (Port 3009, ephemer)
#   3. Workspace-Setup über die API
#   4. Seite mit 2 databaseTable-Nodes (Baserow + NocoDB) per API anlegen
#   5. Seite zurücklesen und prüfen, dass BEIDE Nodes + ihre src erhalten sind
#      -> fängt den Nr.-1-Update-Fehler ab: Node serverseitig NICHT registriert
#         (collaboration.util.ts) => "unknown node type" => Nodes verschwinden.
#   6. Teardown (down -v)
#
# Exit 0 = PASS, Exit 1 = FAIL. Damit CI-tauglich.
#
# Nutzung:
#   bash extras/test/roundtrip.sh            # gegen vorhandenes docmost-custom:local
#   bash extras/test/roundtrip.sh --build    # vorher Image neu bauen
#   DOCMOST_IMAGE=docmost-custom:v0.90.1 bash extras/test/roundtrip.sh
# =============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
COMPOSE="$HERE/docker-compose.test.yml"
IMAGE="${DOCMOST_IMAGE:-docmost-custom:local}"
BASE="http://localhost:3009"
JAR="$(mktemp)"
export DOCMOST_IMAGE="$IMAGE"

# docker mit sudo, falls der User nicht in der docker-Gruppe ist
DOCKER="docker"; docker info >/dev/null 2>&1 || DOCKER="sudo docker"
DC="$DOCKER compose -f $COMPOSE"

cleanup() { echo "» Teardown"; $DC down -v >/dev/null 2>&1 || true; rm -f "$JAR" "$HERE"/.page.json 2>/dev/null || true; }
trap cleanup EXIT

fail() { echo "❌ FAIL: $*"; exit 1; }

if [ "${1:-}" = "--build" ]; then
  echo "» Baue $IMAGE aus $REPO"
  $DOCKER build -t "$IMAGE" "$REPO"
fi

$DOCKER image inspect "$IMAGE" >/dev/null 2>&1 || fail "Image $IMAGE existiert nicht (erst bauen, z.B. --build)"

echo "» Starte isolierte Test-Instanz (Port 3009)"
$DC up -d

echo -n "» Warte auf Docmost"
for i in $(seq 1 60); do
  if curl -fsS -m 3 "$BASE/api/health" >/dev/null 2>&1; then echo " — up"; break; fi
  echo -n "."; sleep 2
  [ "$i" = 60 ] && { $DC logs docmost | tail -30; fail "Docmost wurde nicht erreichbar"; }
done

echo "» Workspace-Setup"
WS=$(curl -fsS -m 15 -c "$JAR" -X POST "$BASE/api/auth/setup" -H 'Content-Type: application/json' \
  -d '{"name":"Tester","email":"test@local.test","password":"testpass123","workspaceName":"TEST"}')
SPACE=$(echo "$WS" | jq -r '.data.defaultSpaceId // empty')
[ -n "$SPACE" ] || fail "kein defaultSpaceId aus dem Setup ($WS)"

echo "» Seite mit 2 databaseTable-Nodes anlegen"
cat > "$HERE/.page.json" <<JSON
{"spaceId":"$SPACE","title":"roundtrip","format":"json","content":{"type":"doc","content":[
  {"type":"databaseTable","attrs":{"src":"http://host:8888/public/grid/SLUG_BW","source":"baserow","title":"BW"}},
  {"type":"databaseTable","attrs":{"src":"http://host:8089/nc/view/UUID_NC","source":"nocodb","title":"NC"}}
]}}
JSON
PID=$(curl -fsS -m 15 -b "$JAR" -X POST "$BASE/api/pages/create" -H 'Content-Type: application/json' \
  --data @"$HERE/.page.json" | jq -r '.data.id // empty')
[ -n "$PID" ] || fail "Seite konnte nicht angelegt werden"

echo "» Seite zurücklesen + prüfen"
INFO=$(curl -fsS -m 15 -b "$JAR" -X POST "$BASE/api/pages/info" -H 'Content-Type: application/json' -d "{\"pageId\":\"$PID\"}")
N=$(echo "$INFO" | jq '[.data.content.content[]? | select(.type=="databaseTable")] | length')
C=$(echo "$INFO" | jq -r '.data.content | tostring')

[ "$N" = "2" ]                  || fail "erwartet 2 databaseTable-Nodes, gefunden: $N (Node serverseitig nicht registriert?)"
echo "$C" | grep -q "SLUG_BW"   || fail "Baserow-src nach Round-Trip verloren"
echo "$C" | grep -q "UUID_NC"   || fail "NocoDB-src nach Round-Trip verloren"
echo "$C" | grep -q '"source":"baserow"' || fail "source=baserow verloren"
echo "$C" | grep -q '"source":"nocodb"'  || fail "source=nocodb verloren"

echo "✅ PASS — databaseTable-Block überlebt Create+Persist+Read (Server-Schema ok, beide Quellen intakt)"
