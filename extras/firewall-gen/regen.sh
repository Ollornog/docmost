#!/bin/bash
docker exec -i baserow ./baserow.sh backend-cmd-with-db manage shell < /srv/stack/firewall-gen/gen_soll.py >> /srv/stack/firewall-gen/regen.log 2>&1
