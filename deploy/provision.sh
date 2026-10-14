#!/usr/bin/env bash
#
# Bring the Sewa Setu assistant pilot up on a bare Ubuntu 22.04 host.
#
#   scp -r deploy/ pilot.env  root@<host>:/opt/sewasetu/
#   ssh root@<host> bash /opt/sewasetu/app/deploy/provision.sh
#
# Everything the pilot needs lives here: the department's Postgres, the
# department's portal exactly as it was handed to us, the simulated SMS
# gateway, the two MCP servers, and supervisor to keep them all alive.
#
# It is written to be run twice. Re-running it reinstalls nothing it does not
# have to and never reloads the seed data over a live database.
#
# Why there is no Dockerfile
# --------------------------
# The handout ships a docker-compose file and it is the right way to run this on
# a developer's laptop; that file is still here and still works. On the pilot
# host there is no Docker daemon, and building one would mean shipping an image
# nobody has looked inside. Running the four processes natively under
# supervisor means the thing that is deployed is the thing in this repository.

set -euo pipefail

APP_DIR="${APP_DIR:-/opt/sewasetu/app}"
STATE_DIR="${STATE_DIR:-/opt/sewasetu}"
ENV_FILE="${ENV_FILE:-$STATE_DIR/pilot.env}"

log() { printf '\n== %s\n' "$*"; }

if [ ! -f "$ENV_FILE" ]; then
    echo "provision: $ENV_FILE is missing. Copy deploy/pilot.env.example there and fill it in." >&2
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
log "packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
    postgresql postgresql-contrib supervisor cron python3-venv python3-pip \
    ca-certificates curl

# The portal is Indian government software and the deemed-approval job runs at
# 02:00; both should mean 02:00 in Purvanchal, not wherever the host happens to
# be racked. The portal already computes its own clock in Asia/Kolkata, this
# makes cron agree with it.
ln -sf /usr/share/zoneinfo/Asia/Kolkata /etc/localtime
echo "Asia/Kolkata" > /etc/timezone

# ---------------------------------------------------------------------------
log "service names"
# The portal was built to run beside a Postgres called "db" and an SMS gateway
# called "smsgw" on a docker network, and reads those names straight out of
# config/app.ini. Rather than edit the department's configuration, give it the
# names it expects on loopback.
if ! grep -qE '^127\.0\.0\.1[[:space:]]+.*(^|[[:space:]])db([[:space:]]|$)' /etc/hosts; then
    echo "127.0.0.1 db smsgw" >> /etc/hosts
fi

# ---------------------------------------------------------------------------
log "postgres"
pg_ctlcluster 15 main start 2>/dev/null || true
for _ in $(seq 1 30); do
    su postgres -c 'pg_isready -q' && break
    sleep 1
done
su postgres -c 'pg_isready -q' || { echo "provision: postgres did not start" >&2; exit 1; }

su postgres -c "psql -tAc \"SELECT 1 FROM pg_roles WHERE rolname='sewasetu'\"" | grep -q 1 \
    || su postgres -c "psql -q -c \"CREATE ROLE sewasetu LOGIN PASSWORD 'sewasetu123'\""
su postgres -c "psql -tAc \"SELECT 1 FROM pg_database WHERE datname='sewasetu'\"" | grep -q 1 \
    || su postgres -c "createdb -O sewasetu sewasetu"

log "schema"
su postgres -c "psql -q -v ON_ERROR_STOP=1 -d sewasetu -f $APP_DIR/portal/schema.sql"
# Ours, applied after theirs. Idempotent, so it is safe on every run.
su postgres -c "psql -q -v ON_ERROR_STOP=1 -d sewasetu -f $APP_DIR/portal/schema_agent.sql"

ROWS=$(su postgres -c "psql -tAc 'SELECT count(*) FROM applications' -d sewasetu" | tr -d '[:space:]')
if [ "$ROWS" = "0" ]; then
    log "seed ($(du -h "$APP_DIR/seed/seed.sql" | cut -f1), about a minute)"
    su postgres -c "psql -q -v ON_ERROR_STOP=1 -d sewasetu -f $APP_DIR/seed/seed.sql"
    su postgres -c "psql -tAc 'SELECT count(*) FROM applications' -d sewasetu"
else
    log "seed skipped: $ROWS applications already present"
fi

# ---------------------------------------------------------------------------
log "python environments"
# Two of them on purpose. The portal keeps the department's exact pins, because
# it is their application and it should run on the versions they tested. The
# MCP servers get their own, so that installing ours can never move a version
# out from under theirs.
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

mkdir -p /var/sewasetu/uploads /var/log/sewasetu "$STATE_DIR"

# ---------------------------------------------------------------------------
log "supervisor"
cat > /etc/supervisor/conf.d/sewasetu.conf <<CONF
; Generated by deploy/provision.sh from $ENV_FILE. Edit the env file, not this.

[program:smsgw]
command=$STATE_DIR/venv-portal/bin/python $APP_DIR/smsgw/app.py
directory=$APP_DIR/smsgw
autostart=true
autorestart=true
stdout_logfile=/var/log/sewasetu/smsgw.log
stderr_logfile=/var/log/sewasetu/smsgw.err.log

[program:portal]
command=$STATE_DIR/venv-portal/bin/gunicorn --bind 0.0.0.0:8000 --workers 2 --timeout 60 wsgi:app
directory=$APP_DIR/portal
environment=SECRET_KEY="$SECRET_KEY",ADMIN_USERNAME="$ADMIN_USERNAME",ADMIN_PASSWORD="$ADMIN_PASSWORD",PUBLIC_BASE_URL="$PUBLIC_BASE_URL",MCP_RESOURCES="$CITIZEN_MCP_URL,$OFFICER_MCP_URL",AGENT_PILOT_BLOCKS="$AGENT_PILOT_BLOCKS"
autostart=true
autorestart=true
stdout_logfile=/var/log/sewasetu/portal.log
stderr_logfile=/var/log/sewasetu/portal.err.log

[program:citizen-mcp]
command=$STATE_DIR/venv-mcp/bin/python -m sewasetu_mcp citizen
directory=$APP_DIR/mcp
environment=PYTHONPATH="$APP_DIR/mcp",PORTAL_BASE_URL="$PUBLIC_BASE_URL",MCP_PUBLIC_URL="$CITIZEN_MCP_URL",MCP_PORT="8101",MCP_BIND_HOST="0.0.0.0"
autostart=true
autorestart=true
stdout_logfile=/var/log/sewasetu/citizen-mcp.log
stderr_logfile=/var/log/sewasetu/citizen-mcp.err.log

[program:officer-mcp]
command=$STATE_DIR/venv-mcp/bin/python -m sewasetu_mcp officer
directory=$APP_DIR/mcp
environment=PYTHONPATH="$APP_DIR/mcp",PORTAL_BASE_URL="$PUBLIC_BASE_URL",MCP_PUBLIC_URL="$OFFICER_MCP_URL",MCP_PORT="8102",MCP_BIND_HOST="0.0.0.0"
autostart=true
autorestart=true
stdout_logfile=/var/log/sewasetu/officer-mcp.log
stderr_logfile=/var/log/sewasetu/officer-mcp.err.log

[program:cron]
command=/usr/sbin/cron -f
autostart=true
autorestart=true
stdout_logfile=/var/log/sewasetu/cron.log
stderr_logfile=/var/log/sewasetu/cron.err.log
CONF

# The department's own crontab file, pointed at where the code actually is.
sed "s#python /app/scripts/deemed_approval.py#$STATE_DIR/venv-portal/bin/python $APP_DIR/portal/scripts/deemed_approval.py#" \
    "$APP_DIR/portal/deploy/crontab" > /etc/cron.d/sewasetu
chmod 0644 /etc/cron.d/sewasetu

# ---------------------------------------------------------------------------
log "deemed approvals that fell due while we were setting up"
"$STATE_DIR/venv-portal/bin/python" "$APP_DIR/portal/scripts/deemed_approval.py" || true

log "starting"
mkdir -p /var/log/supervisor
if [ "${START_SUPERVISORD:-1}" != "1" ]; then
    # deploy/bootstrap.sh runs supervisor in the foreground as PID 1 on the
    # pilot host; on a normal machine we start it here instead.
    echo "supervisor left to the caller (START_SUPERVISORD=$START_SUPERVISORD)"
else
    if supervisorctl status >/dev/null 2>&1; then
        supervisorctl reread >/dev/null
        supervisorctl update >/dev/null
        supervisorctl restart all >/dev/null
    else
        supervisord -c /etc/supervisor/supervisord.conf
    fi
    sleep 3
    supervisorctl status
fi

log "done"
cat <<DONE
Portal        $PUBLIC_BASE_URL
Citizen MCP   $CITIZEN_MCP_URL/mcp
Officer MCP   $OFFICER_MCP_URL/mcp
Pilot blocks  $AGENT_PILOT_BLOCKS
Logs          /var/log/sewasetu/
DONE
