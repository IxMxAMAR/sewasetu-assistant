#!/usr/bin/env bash
#
# Bring the Sewa Setu assistant pilot up on a bare Ubuntu 22.04 host.
#
#   deploy/bootstrap.sh calls this on the pilot host. On a machine you have a
#   shell on:
#     cp deploy/pilot.env.example /opt/sewasetu/pilot.env   # then edit it
#     bash deploy/provision.sh
#
# Everything the pilot needs is here: PostgreSQL, the department's portal
# exactly as it was handed to us, the simulated SMS gateway, the two MCP
# servers, and supervisor to keep them all alive. It is written to be run
# twice: re-running reinstalls nothing it does not have to and never reloads
# the seed over a live database.
#
# Where state lives
# -----------------
# The pilot host's container disk is wiped when the pod restarts, so anything
# that must survive lives under PERSIST_DIR (the pod volume, mounted at
# /workspace). That is the database, the uploaded age proofs, the logs and the
# two Python environments. The code itself is re-cloned on every boot by
# bootstrap.sh, so it does not need to survive.
#
# Why there is no Dockerfile
# --------------------------
# The handout ships a docker-compose file and it is the right way to run this on
# a laptop; that file is still here and still works. On the pilot host there is
# no Docker daemon, and the alternative would be an image nobody has looked
# inside. Running the five processes natively under supervisor means the thing
# that is deployed is the thing in this repository.

set -euo pipefail

APP_DIR="${APP_DIR:-/opt/sewasetu/app}"
# Everything that has to outlive a process: the database, the uploaded age
# proofs, the logs, the two Python environments. Point PERSIST_DIR at a pod
# volume if the host has one; on the pilot pod it is the container disk, which
# survives a service restart but not a pod stop/start.
PERSIST_DIR="${PERSIST_DIR:-/opt/sewasetu/state}"
STATE_DIR="${STATE_DIR:-$PERSIST_DIR}"
PGDATA="${PGDATA:-$PERSIST_DIR/pgdata}"
ENV_FILE="${ENV_FILE:-$STATE_DIR/pilot.env}"
LOG_DIR="$PERSIST_DIR/logs"

log() { printf '\n== %s\n' "$*"; }

if [ ! -f "$ENV_FILE" ]; then
    echo "provision: $ENV_FILE is missing. See deploy/pilot.env.example." >&2
    exit 1
fi

set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a

: "${PUBLIC_BASE_URL:?set PUBLIC_BASE_URL in $ENV_FILE}"
: "${CITIZEN_MCP_URL:?set CITIZEN_MCP_URL in $ENV_FILE}"
: "${OFFICER_MCP_URL:?set OFFICER_MCP_URL in $ENV_FILE}"
: "${ADMIN_PASSWORD:?set ADMIN_PASSWORD in $ENV_FILE}"
: "${SECRET_KEY:?set SECRET_KEY in $ENV_FILE}"
AGENT_PILOT_BLOCKS="${AGENT_PILOT_BLOCKS:-Sonari,Namti}"

if [ ! -d "$APP_DIR/portal" ] || [ ! -d "$APP_DIR/mcp" ]; then
    echo "provision: $APP_DIR does not look like this repository." >&2
    exit 1
fi

# ---------------------------------------------------------------------------
log "state under $PERSIST_DIR"
mkdir -p "$STATE_DIR" "$PERSIST_DIR/uploads" "$LOG_DIR"
chmod 600 "$ENV_FILE"

# ---------------------------------------------------------------------------
log "packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
    postgresql postgresql-contrib supervisor cron python3-venv python3-pip \
    ca-certificates curl

# The portal is Indian government software and the deemed-approval job runs at
# 02:00; both should mean 02:00 in Purvanchal, not wherever the host is racked.
# The portal already computes its own clock in Asia/Kolkata; this makes cron
# agree with it.
ln -sf /usr/share/zoneinfo/Asia/Kolkata /etc/localtime
echo "Asia/Kolkata" > /etc/timezone

# ---------------------------------------------------------------------------
log "service names"
# The portal was built to run beside a Postgres called "db" and an SMS gateway
# called "smsgw" on a docker network, and reads those names straight out of
# config/app.ini. Rather than edit the department's configuration, give it the
# names it expects on loopback.
if ! grep -qE '^127\.0\.0\.1[[:space:]]+.*[[:space:]]db([[:space:]]|$)' /etc/hosts; then
    echo "127.0.0.1 db smsgw" >> /etc/hosts
fi

# ---------------------------------------------------------------------------
log "postgres"
# Debian's packaging wants to own the cluster layout under /etc and
# /var/lib, neither of which survives a pod restart. So we run the server
# ourselves out of PGDATA, which does.
PG_BIN="$(ls -d /usr/lib/postgresql/*/bin | sort -V | tail -1)"
echo "using $PG_BIN"

if [ ! -s "$PGDATA/PG_VERSION" ]; then
    install -d -o postgres -g postgres -m 700 "$PGDATA"
    # Loopback only, so the unix socket is trusted and the TCP path is not: the
    # setup commands below run as the postgres superuser over the socket, and
    # the portal connects over TCP as its own role with its own password.
    su postgres -c "$PG_BIN/initdb -D '$PGDATA' --encoding=UTF8 --locale=C.UTF-8 \
        --auth-local=trust --auth-host=scram-sha-256" >/dev/null
fi

# Ours, not Debian's, and re-asserted on every run so an existing data
# directory from an earlier revision of this script gets corrected too. The
# socket stays where Debian puts it, because that is where psql looks by
# default and the alternative is remembering -h on every command below.
install -d -o postgres -g postgres -m 2775 /var/run/postgresql
for setting in \
    "listen_addresses = '127.0.0.1'" \
    "port = 5432" \
    "unix_socket_directories = '/var/run/postgresql'"
do
    key="${setting%% =*}"
    if grep -qE "^[[:space:]]*${key}[[:space:]]*=" "$PGDATA/postgresql.conf"; then
        sed -i "s|^[[:space:]]*${key}[[:space:]]*=.*|${setting}|" "$PGDATA/postgresql.conf"
    else
        echo "$setting" >> "$PGDATA/postgresql.conf"
    fi
done

STARTED_PG=0
if ! su postgres -c "$PG_BIN/pg_isready -q"; then
    # pg_ctl opens this as the postgres user before the server exists.
    install -o postgres -g postgres -m 644 /dev/null "$LOG_DIR/postgres.log"
    su postgres -c "$PG_BIN/pg_ctl -D '$PGDATA' -l '$LOG_DIR/postgres.log' -w start"
    STARTED_PG=1
fi

su postgres -c "psql -tAc \"SELECT 1 FROM pg_roles WHERE rolname='sewasetu'\"" \
    | grep -q 1 \
    || su postgres -c "psql -q -c \"CREATE ROLE sewasetu LOGIN PASSWORD 'sewasetu123'\""
su postgres -c "psql -tAc \"SELECT 1 FROM pg_database WHERE datname='sewasetu'\"" \
    | grep -q 1 \
    || su postgres -c "createdb -O sewasetu sewasetu"

log "schema"
su postgres -c "psql -q -v ON_ERROR_STOP=1 -d sewasetu -f '$APP_DIR/portal/schema.sql'"
# Ours, applied after theirs. Idempotent, so it is safe on every run.
su postgres -c "psql -q -v ON_ERROR_STOP=1 -d sewasetu -f '$APP_DIR/portal/schema_agent.sql'"

ROWS=$(su postgres -c "psql -tAc 'SELECT count(*) FROM applications' -d sewasetu" | tr -d '[:space:]')
if [ "$ROWS" = "0" ]; then
    log "seed ($(du -h "$APP_DIR/seed/seed.sql" | cut -f1))"
    su postgres -c "psql -q -v ON_ERROR_STOP=1 -d sewasetu -f '$APP_DIR/seed/seed.sql'"
    su postgres -c "psql -tAc 'SELECT count(*) FROM applications' -d sewasetu"
else
    log "seed skipped: $ROWS applications already present"
fi

# ---------------------------------------------------------------------------
log "python environments"
# Two of them on purpose. The portal keeps the department's exact pins, because
# it is their application and it should run on the versions they tested. The
# MCP servers get their own, so installing ours can never move a version out
# from under theirs.
if [ ! -x "$STATE_DIR/venv-portal/bin/python" ]; then
    python3 -m venv "$STATE_DIR/venv-portal"
fi
"$STATE_DIR/venv-portal/bin/pip" install -q --upgrade pip
"$STATE_DIR/venv-portal/bin/pip" install -q -r "$APP_DIR/portal/requirements.txt"

if [ ! -x "$STATE_DIR/venv-mcp/bin/python" ]; then
    python3 -m venv "$STATE_DIR/venv-mcp"
fi
"$STATE_DIR/venv-mcp/bin/pip" install -q --upgrade pip
"$STATE_DIR/venv-mcp/bin/pip" install -q -r "$APP_DIR/mcp/requirements.txt"

mkdir -p /var/sewasetu
# Both of these used to end up one level too deep: ln -s into an existing
# directory puts the link *inside* it, so the portal was logging to a file on
# the container disk rather than to the directory we collect.
rm -rf /var/sewasetu/uploads /var/log/sewasetu
ln -sfn "$PERSIST_DIR/uploads" /var/sewasetu/uploads
ln -sfn "$LOG_DIR" /var/log/sewasetu

# ---------------------------------------------------------------------------
log "supervisor"
# Everything logs to the container's stdout rather than to files. On a host with
# no console and no SSH, "runpodctl pod logs" is the only way to see a
# traceback, and a traceback that is only in a file is a traceback nobody reads.
LOGGING='stdout_logfile=/dev/stdout
stdout_logfile_maxbytes=0
redirect_stderr=true'

cat > /etc/supervisor/conf.d/sewasetu.conf <<CONF
; Generated by deploy/provision.sh from $ENV_FILE. Edit the env file, not this.

[program:postgres]
command=$PG_BIN/postgres -D $PGDATA
user=postgres
autostart=true
autorestart=true
$LOGGING

[program:smsgw]
command=$STATE_DIR/venv-portal/bin/python $APP_DIR/smsgw/app.py
directory=$APP_DIR/smsgw
autostart=true
autorestart=true
$LOGGING

[program:portal]
command=$STATE_DIR/venv-portal/bin/gunicorn --bind 0.0.0.0:8000 --workers 2 --timeout 60 --access-logfile - wsgi:app
directory=$APP_DIR/portal
environment=SECRET_KEY="$SECRET_KEY",ADMIN_USERNAME="$ADMIN_USERNAME",ADMIN_PASSWORD="$ADMIN_PASSWORD",PUBLIC_BASE_URL="$PUBLIC_BASE_URL",MCP_RESOURCES="$CITIZEN_MCP_URL,$OFFICER_MCP_URL",AGENT_PILOT_BLOCKS="$AGENT_PILOT_BLOCKS",EVAL_RESULTS="$APP_DIR/evals/results/latest.json"
autostart=true
autorestart=true
$LOGGING

[program:citizen-mcp]
command=$STATE_DIR/venv-mcp/bin/python -m sewasetu_mcp citizen
directory=$APP_DIR/mcp
environment=PYTHONPATH="$APP_DIR/mcp",PORTAL_BASE_URL="$PUBLIC_BASE_URL",MCP_PUBLIC_URL="$CITIZEN_MCP_URL",MCP_PORT="8101",MCP_BIND_HOST="0.0.0.0"
autostart=true
autorestart=true
$LOGGING

[program:officer-mcp]
command=$STATE_DIR/venv-mcp/bin/python -m sewasetu_mcp officer
directory=$APP_DIR/mcp
environment=PYTHONPATH="$APP_DIR/mcp",PORTAL_BASE_URL="$PUBLIC_BASE_URL",MCP_PUBLIC_URL="$OFFICER_MCP_URL",MCP_PORT="8102",MCP_BIND_HOST="0.0.0.0"
autostart=true
autorestart=true
$LOGGING

[program:cron]
command=/usr/sbin/cron -f
autostart=true
autorestart=true
$LOGGING
CONF

# The department's own crontab file, pointed at where the code actually is.
sed "s#python /app/scripts/deemed_approval.py#$STATE_DIR/venv-portal/bin/python $APP_DIR/portal/scripts/deemed_approval.py#" \
    "$APP_DIR/portal/deploy/crontab" > /etc/cron.d/sewasetu
chmod 0644 /etc/cron.d/sewasetu

# ---------------------------------------------------------------------------
log "deemed approvals that fell due while we were setting up"
"$STATE_DIR/venv-portal/bin/python" "$APP_DIR/portal/scripts/deemed_approval.py" || true

# ---------------------------------------------------------------------------
log "starting"
mkdir -p /var/log/supervisor
if [ "${START_SUPERVISORD:-1}" != "1" ]; then
    # deploy/bootstrap.sh runs supervisor in the foreground as PID 1 on the
    # pilot host, and needs the database to itself in a moment.
    echo "supervisor left to the caller (START_SUPERVISORD=$START_SUPERVISORD)"
    if [ "$STARTED_PG" = "1" ]; then
        su postgres -c "$PG_BIN/pg_ctl -D '$PGDATA' -m fast -w stop"
    fi
else
    if supervisorctl status >/dev/null 2>&1; then
        supervisorctl reread >/dev/null
        supervisorctl update >/dev/null
        supervisorctl restart all >/dev/null
    else
        if [ "$STARTED_PG" = "1" ]; then
            su postgres -c "$PG_BIN/pg_ctl -D '$PGDATA' -m fast -w stop"
        fi
        supervisord -c /etc/supervisor/supervisord.conf
    fi
    sleep 4
    supervisorctl status
fi

log "done"
cat <<DONE
Portal        $PUBLIC_BASE_URL
Citizen MCP   $CITIZEN_MCP_URL/mcp
Officer MCP   $OFFICER_MCP_URL/mcp
Pilot blocks  $AGENT_PILOT_BLOCKS
Logs          $LOG_DIR
DONE
