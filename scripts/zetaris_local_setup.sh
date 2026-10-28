#!/usr/bin/env bash
# One-time setup for the self-hosted Zetaris hackathon kit (Docker, http://localhost:3000).
# 1) starts a small Postgres ("tripwire-pg") on the Zetaris docker network,
# 2) creates the two Tripwire tables in it,
# 3) registers it in Zetaris Lightning as datasource "tripwire_pg" (via the Zetaris SQL API).
# After this, Tripwire reads policy and writes its ledger ONLY through Zetaris.
# Needs: ~/zetaris_api_key.txt (Zetaris API key). Never prints secrets.
set -euo pipefail
NET=$(docker inspect zetaris-api-1 -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{end}}')
PWF=~/.tripwire_pg_pw; [ -f "$PWF" ] || (umask 077; openssl rand -hex 12 > "$PWF")
PW=$(cat "$PWF")
docker ps -a --format '{{.Names}}' | grep -qx tripwire-pg || docker run -d --name tripwire-pg --network "$NET" \
  -e POSTGRES_USER=tripwire -e POSTGRES_PASSWORD="$PW" -e POSTGRES_DB=tripwire postgres:16-alpine >/dev/null
docker start tripwire-pg >/dev/null; sleep 3
docker exec -i tripwire-pg psql -q -U tripwire -d tripwire <<SQL
CREATE TABLE IF NOT EXISTS tripwire_policy_rules (rule_id text, action text, effect text, reason text,
  data_classes text, min_amount double precision, max_amount double precision, dest_not_suffix text, agents text);
CREATE TABLE IF NOT EXISTS tripwire_ledger (ts double precision, run_id text, chunk int, agent_id text, action text,
  resource text, data_class text, amount double precision, destination text, verdict text, matched text,
  explanation text, policy_version text);
SQL
zq() { curl -s --max-time 120 -X POST "${ZETARIS_REST_URL:-http://localhost:3000/api/proxy}/sql-editor/sqls/run" \
  -H 'Content-Type: application/json' -H "Authorization: Bearer $(tr -d '\n\r ' < ~/zetaris_api_key.txt)" \
  -H "X-Org-ID: ${ZETARIS_ORG_ID:-1}" -H "X-Request-ID: $(uuidgen | tr A-Z a-z)" \
  -d "$(python3 -c 'import json,sys,uuid;print(json.dumps({"queryId":str(uuid.uuid4()),"sql":sys.argv[1],"source":"SqlEditor","limit":100}))' "$1")"; echo; }
zq "SHOW DATASOURCES" | grep -q '"tripwire_pg"' || zq "CREATE DATASOURCE tripwire_pg OPTIONS (jdbcdriver \"org.postgresql.Driver\", jdbcurl \"jdbc:postgresql://tripwire-pg:5432/tripwire\", username \"tripwire\", user \"tripwire\", password \"$PW\")" | sed "s/$PW/[REDACTED]/g"
zq "REGISTER DATASOURCE TABLES FROM tripwire_pg" >/dev/null
echo "Zetaris datasources:"; zq "SHOW DATASOURCES"
echo "Tables in tripwire_pg:"; zq "SHOW TABLES IN tripwire_pg"
