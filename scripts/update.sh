#!/bin/bash
# Deploy an explicitly selected, tested revision. Never discard local edits.
set -euo pipefail
umask 077
APP_DIR=/opt/blockshelf
APP_USER=blockshelf
ENV_FILE=/etc/blockshelf/.env
REVISION="${1:-}"
if [[ $(id -u) -ne 0 || -z "$REVISION" ]]; then
    echo 'Usage: sudo bash scripts/update.sh <tested-git-revision>' >&2
    exit 1
fi
cd "$APP_DIR"
exec 9>/var/lock/blockshelf-update.lock
flock -n 9 || { echo 'Another update is running' >&2; exit 1; }
[[ -f "$ENV_FILE" && -x .venv/bin/python && -r .env ]] || {
    echo "Missing runtime or .env; check the link to $ENV_FILE before updating." >&2; exit 1;
}
[[ -z $(sudo -u "$APP_USER" git status --porcelain --untracked-files=no) ]] || {
    echo 'Local tracked changes exist. Commit or preserve them before updating.' >&2; exit 1;
}
sudo -u "$APP_USER" git fetch origin
TARGET=$(sudo -u "$APP_USER" git rev-parse --verify "$REVISION^{commit}")
OLD=$(sudo -u "$APP_USER" git rev-parse HEAD)
sudo -u "$APP_USER" git cat-file -e "$TARGET:requirements.lock"
BACKUP_DIR=$(mktemp -d /var/backups/blockshelf-update-XXXXXXXX)
chmod 700 "$BACKUP_DIR"
printf '%s\n' "$OLD" > "$BACKUP_DIR/revision.txt"
.venv/bin/python -m pip freeze > "$BACKUP_DIR/requirements.txt"
# Stop HTTP writes and the scheduled writer before taking the final snapshot.
TIMER_WAS_ACTIVE=0
if systemctl is-active --quiet blockshelf-backup.timer; then
    TIMER_WAS_ACTIVE=1
fi
trap 'systemctl stop blockshelf || true; echo "Update failed; service remains stopped. Recovery files (verify completeness): $BACKUP_DIR; old revision: $OLD. Follow docs/AUDIT_FIXES.md." >&2' ERR
for unit in blockshelf-backup.timer blockshelf-backup.service; do
    load_state=$(systemctl show "$unit" --property=LoadState --value)
    if [[ "$load_state" != not-found ]]; then
        systemctl stop "$unit"
    fi
done
systemctl stop blockshelf
# The application command must return nonzero when the dump fails.
DB_BACKUP=$(sudo -u "$APP_USER" .venv/bin/python manage.py shell --verbosity 0 -c "from inventory.backup_utils import create_full_db_backup; from django.conf import settings; from pathlib import Path; backup = create_full_db_backup(); assert backup is not None, 'Database backup failed'; print(Path(getattr(settings, 'BACKUP_ROOT', settings.MEDIA_ROOT)) / backup.file_path)")
[[ -s "$DB_BACKUP" ]]
if [[ $(head -c 5 "$DB_BACKUP") == PGDMP ]]; then
    pg_restore --list "$DB_BACKUP" >/dev/null
fi
cp -- "$DB_BACKUP" "$BACKUP_DIR/database.backup"
# Keep configuration and uploaded files as well as code. Failed tar aborts the update.
tar --exclude='opt/blockshelf/.venv' --exclude='*/__pycache__' \
    --exclude='opt/blockshelf/staticfiles' --exclude='opt/blockshelf/private_imports' \
    --exclude='opt/blockshelf/logs' --exclude='opt/blockshelf/private_backups' --exclude='opt/blockshelf/media/backups' \
    -czf "$BACKUP_DIR/installation.tar.gz" -C / opt/blockshelf etc/blockshelf
# Verify the archive before changing code or schema.
tar -tzf "$BACKUP_DIR/installation.tar.gz" >/dev/null
sudo -u "$APP_USER" git checkout --detach "$TARGET"
sudo -u "$APP_USER" .venv/bin/python -m pip install -r requirements.lock
sudo -u "$APP_USER" .venv/bin/python manage.py check
sudo -u "$APP_USER" .venv/bin/python manage.py migrate --noinput
sudo -u "$APP_USER" .venv/bin/python manage.py redact_audit_secrets
sudo -u "$APP_USER" .venv/bin/python manage.py relocate_backups
sudo -u "$APP_USER" .venv/bin/python manage.py collectstatic --noinput
install -m 0644 scripts/blockshelf-backup.service /etc/systemd/system/
if [[ ! -f /etc/systemd/system/blockshelf-backup.timer ]]; then
    install -m 0644 scripts/blockshelf-backup.timer /etc/systemd/system/
fi
systemctl daemon-reload
systemctl start blockshelf
sleep 2
systemctl is-active --quiet blockshelf
if [[ "$TIMER_WAS_ACTIVE" == 1 ]]; then
    systemctl start blockshelf-backup.timer
fi
trap - ERR
echo "Installed $TARGET. Recovery files: $BACKUP_DIR"
echo 'Verify login, inventory, collaboration and readiness before accepting the release.'
