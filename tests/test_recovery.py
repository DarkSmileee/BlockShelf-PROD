"""Recovery tests only use temporary files and mocked PostgreSQL commands."""
import ast
from contextlib import closing
import importlib.util
from pathlib import Path
import sqlite3
import subprocess

import environ
import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('restore_database', ROOT / 'scripts/restore_database.py')
restore = importlib.util.module_from_spec(spec)
spec.loader.exec_module(restore)


@pytest.mark.parametrize('scheme', ['postgres', 'postgresql'])
def test_restore_connection_formats(scheme):
    name, env = restore.postgres_connection(f'{scheme}://owner:p%40ss@localhost:5432/live')
    assert name == 'live' and env['PGPASSWORD'] == 'p@ss'


def test_restore_never_targets_live_database(mocker):
    run = mocker.patch.object(restore.subprocess, 'run')
    with pytest.raises(ValueError):
        restore.restore_postgres('backup.sql', 'live', 'postgresql://user:pass@localhost/live')
    run.assert_not_called()


def test_restore_validates_before_creating_database(mocker):
    run = mocker.patch.object(restore.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, 'pg_restore'))
    with pytest.raises(subprocess.CalledProcessError):
        restore.restore_postgres('invalid.sql', 'recovery', 'postgresql://user:pass@localhost/live')
    assert run.call_count == 1
    assert run.call_args.args[0][:2] == ['pg_restore', '--list']


def test_restore_no_create_permission_leaves_live_untouched(mocker):
    run = mocker.patch.object(restore.subprocess, 'run', side_effect=[None, subprocess.CalledProcessError(1, 'createdb')])
    with pytest.raises(subprocess.CalledProcessError):
        restore.restore_postgres('backup.sql', 'recovery', 'postgresql://user:pass@localhost/live')
    assert [call.args[0][0] for call in run.call_args_list] == ['pg_restore', 'createdb']


def test_restore_uses_transaction_and_new_database(mocker):
    run = mocker.patch.object(restore.subprocess, 'run')
    restore.restore_postgres('backup.sql', 'recovery', 'postgresql://user:pass@localhost/live')
    command = run.call_args.args[0]
    assert '--single-transaction' in command and '--exit-on-error' in command
    assert command[command.index('--dbname') + 1] == 'recovery'


def test_sqlite_restore_preserves_existing_destination(tmp_path):
    source = tmp_path / 'backup.sqlite3'
    with closing(sqlite3.connect(source)) as db:
        db.execute('CREATE TABLE inventory (quantity INTEGER)')
        db.execute('INSERT INTO inventory VALUES (42)')
        db.commit()
    target = tmp_path / 'recovery.sqlite3'
    restore.restore_sqlite(source, target)
    with closing(sqlite3.connect(target)) as db:
        assert db.execute('SELECT quantity FROM inventory').fetchone() == (42,)
    before = target.read_bytes()
    with pytest.raises(FileExistsError):
        restore.restore_sqlite(source, target)
    assert target.read_bytes() == before


def test_smtp_environment_is_read(monkeypatch):
    values = {'EMAIL_HOST': 'mail.example.test', 'EMAIL_PORT': '587',
              'EMAIL_HOST_USER': 'sender', 'EMAIL_HOST_PASSWORD': 'test-only',
              'EMAIL_USE_TLS': 'True', 'EMAIL_USE_SSL': 'False', 'EMAIL_TIMEOUT': '8'}
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    tree = ast.parse((ROOT/'blockshelf_inventory/settings.py').read_text())
    assignments = [node for node in tree.body if isinstance(node, ast.Assign)
                   and any(isinstance(target, ast.Name) and target.id in values for target in node.targets)]
    namespace = {'env': environ.Env()}
    exec(compile(ast.Module(body=assignments, type_ignores=[]), 'email_settings', 'exec'), namespace)
    assert namespace['EMAIL_HOST'] == values['EMAIL_HOST']
    assert namespace['EMAIL_HOST_PASSWORD'] == values['EMAIL_HOST_PASSWORD']
    assert namespace['EMAIL_PORT'] == 587 and namespace['EMAIL_USE_TLS'] is True
    assert namespace['EMAIL_USE_SSL'] is False and namespace['EMAIL_TIMEOUT'] == 8
