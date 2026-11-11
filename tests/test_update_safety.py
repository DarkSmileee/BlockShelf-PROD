"""Run the updater against fake commands and files, never a real service."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
BASH = Path('C:/Program Files/Git/bin/bash.exe') if os.name == 'nt' else Path(shutil.which('bash') or '/missing-bash')


@pytest.mark.parametrize('backup_units', ['present', 'absent'])
@pytest.mark.parametrize('failure', ['dump', 'archive', 'migrate', 'start', 'none'])
def test_update_requires_quiescent_backups_before_changing_code(tmp_path, failure, backup_units):
    assert b'\r\n' not in (ROOT/'scripts/update.sh').read_bytes()
    assert b'\r\n' not in (ROOT/'scripts/restore_database.sh').read_bytes()
    if not BASH.exists():
        pytest.skip('Bash is unavailable')
    probe = subprocess.run([str(BASH), '-c', 'exit 0'], capture_output=True)
    if probe.returncode:
        pytest.skip('Bash is blocked by this sandbox; run this test outside it')
    app = tmp_path / 'app'
    commands = tmp_path / 'bin'
    commands.mkdir()
    (app / '.venv/bin').mkdir(parents=True)
    config = tmp_path / 'config.env'
    config.write_text('test-only')
    (app / '.env').write_text('test-only')
    log = tmp_path / 'commands.log'
    def script(path, content):
        path.write_text('#!/bin/bash\n'+content, newline='\n')
        path.chmod(0o755)
    script(commands/'id', 'echo 0\n')
    script(commands/'flock', 'exit 0\n')
    script(commands/'sudo', 'shift 2\nexec "$@"\n')
    script(commands/'git', '''echo "git $*" >> "$TEST_LOG"
case "$1" in
  status) exit 0 ;;
  rev-parse) echo test-revision ;;
esac
''')
    script(commands/'tar', '''echo "tar $*" >> "$TEST_LOG"
[[ "$FAIL_STAGE" != archive ]]
''')
    script(commands/'systemctl', '''echo "systemctl $*" >> "$TEST_LOG"
if [[ "$BACKUP_UNITS" == absent && "$*" == *blockshelf-backup* ]]; then
    if [[ "$1" == show ]]; then echo not-found; exit 0; fi
    if [[ "$1" == is-active ]]; then exit 3; fi
    exit 1
fi
if [[ "$1" == show ]]; then echo loaded; fi
[[ "$FAIL_STAGE" != start || "$*" != "start blockshelf" ]]
''')
    script(commands/'install', 'echo "install $*" >> "$TEST_LOG"\n')
    script(commands/'sleep', 'exit 0\n')
    script(app/'.venv/bin/python', '''echo "python $*" >> "$TEST_LOG"
if [[ "$FAIL_STAGE" == migrate && "$*" == *"migrate --noinput"* ]]; then exit 1; fi
if [[ "$*" == *create_full_db_backup* ]]; then
    [[ "$FAIL_STAGE" != dump ]] || exit 1
    echo mock-backup > "$TEST_BACKUP"
    echo "$TEST_BACKUP"
fi
''')
    source = (ROOT/'scripts/update.sh').read_text()
    source = source.replace('APP_DIR=/opt/blockshelf', f'APP_DIR="{app.as_posix()}"')
    source = source.replace('ENV_FILE=/etc/blockshelf/.env', f'ENV_FILE="{config.as_posix()}"')
    source = source.replace('/var/lock/blockshelf-update.lock', f'"{(tmp_path/"lock").as_posix()}"')
    source = source.replace('/var/backups/blockshelf-update-XXXXXXXX', f'"{tmp_path.as_posix()}/backup-XXXXXXXX"')
    updater = tmp_path/'update.sh'
    updater.write_text(source, newline='\n')
    env = dict(os.environ, TEST_LOG=log.as_posix(), FAIL_STAGE=failure, BACKUP_UNITS=backup_units, TEST_BACKUP=(tmp_path/"database.sql").as_posix())
    # Bash receives its own PATH so fake commands are resolved before system commands.
    bin_path = commands.as_posix()
    if os.name == 'nt':
        bin_path = '/' + bin_path[0].lower() + bin_path[2:]
    command = f'export PATH="{bin_path}:$PATH"; bash "{updater.as_posix()}" tested-revision'
    result = subprocess.run([str(BASH), '-c', command], env=env, capture_output=True, text=True)
    assert log.exists(), result.stderr + result.stdout
    calls = log.read_text()
    if failure != 'none':
        assert result.returncode != 0
        assert 'systemctl stop blockshelf' in calls
        if failure in ('dump', 'archive'):
            assert 'git checkout' not in calls
    else:
        assert result.returncode == 0, result.stderr
        assert calls.index('systemctl stop blockshelf\n') < calls.index('create_full_db_backup') < calls.index('tar -tzf') < calls.index('git checkout')
        if backup_units == 'present':
            assert calls.index('systemctl stop blockshelf-backup.timer') < calls.index('create_full_db_backup')
        else:
            assert 'systemctl stop blockshelf-backup' not in calls
        assert 'redact_audit_secrets' in calls
        assert ('systemctl start blockshelf-backup.timer' in calls) == (backup_units == 'present')
        assert 'requirements.lock' in calls and '--upgrade' not in calls
