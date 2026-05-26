#!/bin/bash
# BRDL KB — tägliches Backup: Docmost-DB+Storage, Baserow (offiziell), .env. Rotation: 14 Tage.
set -uo pipefail
TS=$(date +%F_%H%M); DEST=/srv/backups/$TS; mkdir -p "$DEST"
exec >> /srv/backups/backup.log 2>&1
echo "=== $TS START ==="
docker exec docmost-db pg_dump -U docmost docmost | gzip > "$DEST/docmost-db.sql.gz" && echo "docmost-db ok"
tar czf "$DEST/docmost-storage.tgz" -C /srv/stack/docmost data 2>/dev/null && echo "docmost-storage ok"
docker exec baserow mkdir -p /baserow/data/backups 2>/dev/null
BWPW=$(docker exec baserow sh -c 'cut -d= -f2- /baserow/data/.pgpass' 2>/dev/null | tr -d '\r\n')
if docker exec -e PGPASSWORD="$BWPW" baserow ./baserow.sh backend-cmd-with-db manage backup_baserow \
     -h localhost -d baserow -U baserow -p 5432 -f /baserow/data/backups/baserow.tar.gz >/dev/null 2>&1; then
  mv /srv/stack/baserow/data/backups/baserow.tar.gz "$DEST/baserow.tar.gz" 2>/dev/null && echo "baserow ok"
else echo "baserow FEHLER"; fi
cp /srv/stack/.env "$DEST/env.bak" && echo "env ok"
ls -1dt /srv/backups/*/ 2>/dev/null | tail -n +15 | xargs -r rm -rf
echo "=== $TS FERTIG: $(du -sh "$DEST" 2>/dev/null | cut -f1) ==="
