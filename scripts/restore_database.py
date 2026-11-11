"""Restore into a new destination; never drop or overwrite the live database."""
import argparse
from contextlib import closing
import gzip
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tempfile
from urllib.parse import unquote, urlsplit


def postgres_connection(url):
    parsed = urlsplit(url)
    if parsed.scheme not in ('postgres', 'postgresql') or not parsed.path.strip('/'):
        raise ValueError('A PostgreSQL DATABASE_URL is required')
    env = os.environ.copy()
    env.update(PGHOST=parsed.hostname or '', PGPORT=str(parsed.port or 5432),
               PGUSER=unquote(parsed.username or ''), PGPASSWORD=unquote(parsed.password or ''))
    return unquote(parsed.path.lstrip('/')), env


def restore_postgres(archive, target, url):
    current, env = postgres_connection(url)
    if target == current or not re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_]{0,62}', target):
        raise ValueError('Choose a different, simple database name for --target')
    subprocess.run(['pg_restore', '--list', str(archive)], env=env, check=True, stdout=subprocess.DEVNULL)
    # createdb fails if the destination exists or the role lacks permission.
    subprocess.run(['createdb', '--maintenance-db=postgres', target], env=env, check=True)
    subprocess.run(['pg_restore', '--exit-on-error', '--single-transaction', '--no-owner',
                    '--no-privileges', '--dbname', target, str(archive)], env=env, check=True)


def restore_sqlite(archive, target):
    target = Path(target).resolve()
    with closing(sqlite3.connect(Path(archive).resolve().as_uri() + '?mode=ro', uri=True)) as db:
        if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise ValueError('SQLite backup failed integrity check')
    # Exclusive creation cannot overwrite an existing database.
    with os.fdopen(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as destination, open(archive, 'rb') as source:
        shutil.copyfileobj(source, destination)
    target.chmod(0o600)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('backup', type=Path)
    parser.add_argument('engine', choices=['postgres', 'sqlite'])
    parser.add_argument('--target', required=True, help='NEW database name or SQLite file path')
    args = parser.parse_args()
    import environ
    root = Path(__file__).resolve().parent.parent
    environ.Env.read_env('/etc/blockshelf/.env')
    environ.Env.read_env(root / '.env')
    try:
        with tempfile.TemporaryDirectory(prefix='blockshelf-restore-') as temporary:
            archive = args.backup
            if archive.suffix == '.gz':
                expanded = Path(temporary) / 'backup'
                with gzip.open(archive, 'rb') as source, expanded.open('wb') as destination:
                    shutil.copyfileobj(source, destination)
                archive = expanded
            if args.engine == 'postgres':
                restore_postgres(archive, args.target, os.environ.get('DATABASE_URL', ''))
            else:
                restore_sqlite(archive, args.target)
    except (ValueError, OSError, sqlite3.Error, subprocess.CalledProcessError) as exc:
        parser.exit(1, f'Restore failed: {exc}\nThe live database was not replaced.\n')
    print('Restore completed in the NEW destination. Verify its data before any cutover.')


if __name__ == '__main__':
    main()
