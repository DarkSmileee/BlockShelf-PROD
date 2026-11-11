#!/bin/bash
# One-time deployment of the reviewed WinSCP release; no Git checkout/reset.
set -Eeuo pipefail
umask 077
MODE=${1:---check}
[[ $EUID == 0 && ( $MODE == --check || $MODE == --deploy ) ]] || {
    echo 'Usage: sudo bash deploy-reviewed-release.sh --check|--deploy' >&2; exit 1;
}
APP=/opt/blockshelf
STAGE=/opt/blockshelf-stage-b2U1ssqk
ARCHIVE=/root/blockshelf-reviewed-2026-11-11.tar.gz
SITE=/etc/nginx/sites-enabled/blockshelf.be
ENV_FILE=/etc/blockshelf/.env
exec 9>/var/lock/blockshelf-update.lock
flock -n 9 || { echo 'Another deployment is running.' >&2; exit 1; }
cd "$APP"
for command in nginx systemctl curl pg_dump pg_restore tar sha256sum; do command -v "$command" >/dev/null; done
[[ -x .venv/bin/python && -r .env && -r "$ENV_FILE" ]]
[[ $(readlink -f .env) == "$ENV_FILE" ]]
printf '%s  %s\n' '1c797b2fdf99ecae8ea0778f999a29f6e1c82fe75c1227129668713be8f2099d' "$ARCHIVE" | sha256sum -c -
# Compare the release with the actual staging files used for the passing suite.
"$STAGE/.venv/bin/python" - "$ARCHIVE" "$STAGE" "$SITE" <<'PY'
import hashlib, pathlib, socket, sys, tarfile
archive, stage, site = sys.argv[1:]
with tarfile.open(archive) as tar:
    for member in tar.getmembers():
        name = pathlib.PurePosixPath(member.name)
        assert member.isfile() and not name.is_absolute() and '..' not in name.parts, member.name
        data = (pathlib.Path(stage) / member.name).read_bytes()
        if member.name == 'tests/test_audit_regressions.py':
            assert hashlib.sha256(data).hexdigest() == '5f96f240029c118f7c758381482b3ec62c49ac4399542a9c00b8f4c0a7dcddf8', 'Staging backup test differs'
        else:
            assert data == tar.extractfile(member).read(), f'Staging differs: {member.name}'
s = pathlib.Path(site).read_text()
assert s.count('proxy_pass http://unix:/run/blockshelf/blockshelf.sock;') == 1, 'Unexpected Nginx upstream'
assert s.count('location / {') == 1, 'Unexpected Nginx locations'
assert 'location ^~ /media/backups/' not in s, 'Review existing backup restriction'
assert 'proxy_set_header X-Real-IP' not in s, 'Review existing client IP header'
with socket.socket() as sock:
    sock.bind(('127.0.0.1', 8000))
print('PASS: release matches tested staging; Nginx layout and port 8000 checked.')
PY
sudo -u blockshelf "$STAGE/.venv/bin/python" -m pip check
nginx -t
systemctl is-active --quiet blockshelf
if [[ $MODE == --check ]]; then
    echo 'Preflight passed. No deployment performed.'
    exit 0
fi
RECOVERY=$(mktemp -d /var/backups/blockshelf-cutover-XXXXXXXX)
echo "Recovery directory: $RECOVERY"
TIMER_ACTIVE=0
if systemctl is-active --quiet blockshelf-backup.timer; then TIMER_ACTIVE=1; fi
printf '%s\n' "$TIMER_ACTIVE" > "$RECOVERY/timer-was-active.txt"
.venv/bin/python -m pip freeze > "$RECOVERY/requirements-before.txt"
# Keep writers stopped after any failure; do not guess at a database rollback.
failed() {
    trap - ERR INT TERM
    systemctl stop blockshelf blockshelf-backup.timer blockshelf-backup.service || true
    echo "DEPLOYMENT STOPPED. Leave writers stopped; recovery files: $RECOVERY" >&2
    echo 'Do not rerun or restore blindly. Preserve the error output for recovery.' >&2
    exit 1
}
trap failed ERR INT TERM
systemctl stop blockshelf-backup.timer blockshelf-backup.service
systemctl stop blockshelf
# Dump using the current runtime configuration without printing credentials.
sudo -u blockshelf .venv/bin/python - > "$RECOVERY/database.dump.partial" <<'PY'
import os, subprocess
os.environ['DJANGO_SETTINGS_MODULE'] = 'blockshelf_inventory.settings'
from django.conf import settings
db = settings.DATABASES['default']
assert db['ENGINE'] == 'django.db.backends.postgresql'
env = os.environ.copy(); env['PGPASSWORD'] = db.get('PASSWORD') or ''
command = ['pg_dump', '--format=custom', '--no-password', '--dbname', db['NAME'], '--username', db['USER']]
for flag, key in [('--host', 'HOST'), ('--port', 'PORT')]:
    if db.get(key): command.extend([flag, str(db[key])])
subprocess.run(command, env=env, check=True)
PY
test -s "$RECOVERY/database.dump.partial"
pg_restore --list "$RECOVERY/database.dump.partial" >/dev/null
mv "$RECOVERY/database.dump.partial" "$RECOVERY/database.dump"
# Preserve the previous virtualenv too, allowing recovery without downloading dependencies.
tar --exclude='opt/blockshelf/.cache' --exclude='opt/blockshelf/logs' \
    --exclude='*/__pycache__' -czf "$RECOVERY/installation.tar.gz" \
    -C / opt/blockshelf etc/blockshelf etc/nginx etc/systemd/system
tar -tzf "$RECOVERY/installation.tar.gz" >/dev/null
(cd "$RECOVERY"; sha256sum database.dump installation.tar.gz > SHA256SUMS)
fingerprint() {
    sudo -u blockshelf .venv/bin/python - <<'PY'
import hashlib, os
os.environ['DJANGO_SETTINGS_MODULE'] = 'blockshelf_inventory.settings'
from django.db import connection
h = hashlib.sha256()
with connection.cursor() as cursor:
    cursor.execute('SELECT row_to_json(i)::text FROM inventory_inventoryitem i ORDER BY id')
    for row in cursor:
        h.update(row[0].encode()); h.update(b'\n')
print(h.hexdigest())
PY
}
fingerprint > "$RECOVERY/inventory-before.sha256"
# Extract only the checked source members; retain media, .env and server Git history.
cat "$ARCHIVE" | sudo -u blockshelf tar --no-same-owner -xz -C "$APP"
install -o blockshelf -g blockshelf -m 0644 "$STAGE/tests/test_audit_regressions.py" "$APP/tests/test_audit_regressions.py"
.venv/bin/python - "$ENV_FILE" "$SITE" <<'PY'
from pathlib import Path
import re, sys
env_path, site_path = map(Path, sys.argv[1:])
s = env_path.read_text()
values = {
    'GUNICORN_BIND': '127.0.0.1:8000',
    'TRUSTED_PROXY_IPS': '127.0.0.1,::1',
    'BACKUP_ROOT': '/opt/blockshelf/private_backups',
    'REBRICKABLE_TEMP_ROOT': '/opt/blockshelf/private_imports',
}
for key, value in values.items():
    s = re.sub(r'^\s*(?:export\s+)?' + key + r'\s*=.*$', '', s, flags=re.M)
    s = s.rstrip() + '\n' + key + '=' + value + '\n'
env_path.write_text(s)
s = site_path.read_text()
s = s.replace('proxy_pass http://unix:/run/blockshelf/blockshelf.sock;',
              'proxy_pass http://127.0.0.1:8000;\n       proxy_set_header X-Real-IP $remote_addr;')
s = s.replace('location / {', 'location ^~ /media/backups/ { return 404; }\n\n   location / {')
site_path.write_text(s)
PY
nginx -t
systemctl reload nginx
install -d -o blockshelf -g blockshelf -m 0700 "$APP/private_backups" "$APP/private_imports"
sudo -u blockshelf .venv/bin/python -m pip install -r requirements.lock
sudo -u blockshelf .venv/bin/python -m pip check
sudo -u blockshelf .venv/bin/python manage.py check
sudo -u blockshelf .venv/bin/python manage.py migrate --noinput
fingerprint > "$RECOVERY/inventory-after.sha256"
cmp "$RECOVERY/inventory-before.sha256" "$RECOVERY/inventory-after.sha256"
sudo -u blockshelf .venv/bin/python manage.py redact_audit_secrets
sudo -u blockshelf .venv/bin/python manage.py relocate_backups
sudo -u blockshelf .venv/bin/python manage.py collectstatic --noinput
install -m 0644 scripts/blockshelf-backup.service /etc/systemd/system/blockshelf-backup.service
systemctl daemon-reload
systemctl start blockshelf
# Readiness retries are bounded, and requests go through local Nginx with the real hostname.
ready=0
for attempt in {1..15}; do
    if curl --noproxy '*' --resolve blockshelf.be:443:127.0.0.1 \
        --silent --show-error --fail --max-time 5 \
        https://blockshelf.be/inventory/health/readiness/ > "$RECOVERY/readiness.json"; then
        ready=1; break
    fi
    sleep 2
done
[[ $ready == 1 ]]
systemctl is-active --quiet blockshelf
status=$(curl --noproxy '*' --resolve blockshelf.be:443:127.0.0.1 \
    --silent --show-error --max-time 10 -o /dev/null -w '%{http_code}' \
    https://blockshelf.be/media/backups/deployment-probe)
[[ $status == 404 ]]
status=$(curl --noproxy '*' --resolve blockshelf.be:443:127.0.0.1 \
    --silent --show-error --max-time 10 -o /dev/null -w '%{http_code}' \
    https://blockshelf.be/login/)
[[ $status == 200 ]]
if [[ $TIMER_ACTIVE == 1 ]]; then systemctl start blockshelf-backup.timer; fi
trap - ERR INT TERM
echo "DEPLOYMENT COMPLETE. Recovery directory: $RECOVERY"
echo 'Inventory fingerprint unchanged; readiness, login page and backup URL protection passed.'
echo 'Check normal-user login, locations 1562/1563, live search and backup download in the browser.'
echo 'Email remains deferred. This file-based release leaves Git changes; do not git pull/reset.'
