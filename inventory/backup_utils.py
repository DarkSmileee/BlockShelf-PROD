"""Private, uniquely named backups published only after successful writes."""
from contextlib import closing
import csv
import logging
import os
import sqlite3
import subprocess
from pathlib import Path
from uuid import uuid4

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured
from django.db import connection
from django.utils import timezone

from .models import Backup, InventoryItem

User = get_user_model()
logger = logging.getLogger(__name__)


def private_backup_root():
    root = Path(settings.BACKUP_ROOT).resolve()
    if root.is_relative_to(Path(settings.MEDIA_ROOT).resolve()):
        raise ImproperlyConfigured('BACKUP_ROOT must be outside MEDIA_ROOT')
    return root


def get_backup_dir():
    directory = private_backup_root() / 'backups'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return directory


def backup_file_path(relative_path):
    root = private_backup_root()
    path = (root / relative_path).resolve()
    if not path.is_relative_to(root / 'backups'):
        raise ValueError('Invalid backup path')
    if path.exists():
        return path
    # Existing records remain downloadable until relocate_backups is run.
    legacy_root = Path(settings.MEDIA_ROOT).resolve()
    legacy = (legacy_root / relative_path).resolve()
    if not legacy.is_relative_to(legacy_root / 'backups'):
        raise ValueError('Invalid legacy backup path')
    return legacy if legacy.exists() else path


def publish_backup(temp_path, final_path, **fields):
    if temp_path.stat().st_size == 0:
        raise ValueError('Backup is empty')
    temp_path.chmod(0o600)
    os.replace(temp_path, final_path)
    try:
        return Backup.objects.create(
            file_path=final_path.relative_to(private_backup_root()).as_posix(),
            file_size=final_path.stat().st_size, **fields)
    except Exception:
        final_path.unlink(missing_ok=True)
        raise


def create_full_db_backup(created_by=None, is_scheduled=False):
    temp_path = None
    try:
        config = settings.DATABASES['default']
        engine = config['ENGINE']
        directory = get_backup_dir() / 'full_db'
        directory.mkdir(exist_ok=True, mode=0o700)
        suffix = '.sqlite3' if 'sqlite' in engine else '.sql'
        name = f"db_backup_{timezone.now():%Y%m%d_%H%M%S}_{uuid4().hex}{suffix}"
        final_path = directory / name
        temp_path = final_path.with_suffix(final_path.suffix + '.partial')
        env = os.environ.copy()
        if 'postgresql' in engine:
            env['PGPASSWORD'] = config.get('PASSWORD') or ''
            command = ['pg_dump', '-U', config['USER'], '-d', config['NAME'], '-F', 'c', '-f', str(temp_path)]
            for flag, key in [('-h', 'HOST'), ('-p', 'PORT')]:
                if config.get(key):
                    command.extend([flag, str(config[key])])
            subprocess.run(command, env=env, check=True, capture_output=True)
        elif 'mysql' in engine:
            env['MYSQL_PWD'] = config.get('PASSWORD') or ''
            command = ['mysqldump', '--single-transaction', '-u', config['USER']]
            for flag, key in [('-h', 'HOST'), ('-P', 'PORT')]:
                if config.get(key):
                    command.extend([flag, str(config[key])])
            command.append(config['NAME'])
            with temp_path.open('wb') as output:
                subprocess.run(command, env=env, stdout=output, stderr=subprocess.PIPE, check=True)
        elif 'sqlite' in engine:
            connection.ensure_connection()
            with closing(sqlite3.connect(temp_path)) as destination:
                connection.connection.backup(destination)
        else:
            raise ValueError(f'Unsupported database engine: {engine}')
        return publish_backup(temp_path, final_path, backup_type='full_db',
                              created_by=created_by, is_scheduled=is_scheduled)
    except Exception:
        logger.exception('Failed to create full database backup')
        return None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def create_user_inventory_backup(user, created_by=None, is_scheduled=False):
    temp_path = None
    try:
        directory = get_backup_dir() / 'user_inventory' / str(user.pk)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        name = f"user_{user.pk}_inventory_{timezone.now():%Y%m%d_%H%M%S}_{uuid4().hex}.csv"
        final_path = directory / name
        temp_path = final_path.with_suffix('.csv.partial')
        fields = ['name', 'part_id', 'color', 'quantity_total', 'quantity_used',
                  'storage_location', 'image_url', 'notes', 'created_at', 'updated_at']
        with temp_path.open('w', encoding='utf-8', newline='') as output:
            writer = csv.writer(output)
            writer.writerow(fields)
            items = InventoryItem.objects.filter(user=user).order_by('name', 'part_id', 'color')
            for item in items.iterator():
                writer.writerow([getattr(item, field).isoformat() if field.endswith('_at')
                                 else getattr(item, field) for field in fields])
        return publish_backup(temp_path, final_path, backup_type='user_inventory', user=user,
                              created_by=created_by, is_scheduled=is_scheduled)
    except Exception:
        logger.exception('Failed to create user inventory backup')
        return None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def rotate_backups(backup_type: str, user: User | None = None, keep_count: int = 10):
    """
    Delete old backups, keeping only the most recent ones.

    Args:
        backup_type: 'full_db' or 'user_inventory'
        user: User for inventory backups (None for full DB backups)
        keep_count: Number of backups to keep
    """
    if keep_count < 1:
        raise ValueError('Keep at least one backup')
    query = Backup.objects.filter(backup_type=backup_type)

    if backup_type == 'user_inventory' and user:
        query = query.filter(user=user)
    elif backup_type == 'full_db':
        query = query.filter(user__isnull=True)

    # Get backups to delete (oldest ones beyond keep_count)
    old_backups = query.order_by('-created_at')[keep_count:]

    deleted_count = 0
    for backup in old_backups:
        try:
            backup.delete()  # This will also delete the physical file
            deleted_count += 1
        except Exception as e:
            logger.error(f"Failed to delete backup {backup.id}: {e}")

    if deleted_count > 0:
        logger.info(f"Rotated {deleted_count} old {backup_type} backups")

    return deleted_count


def create_all_user_backups(created_by: User | None = None, is_scheduled: bool = False, keep_count: int = 10):
    """
    Create inventory backups for all users.

    Args:
        created_by: User who triggered the backup
        is_scheduled: True if this is a scheduled backup

    Returns:
        Tuple of (success_count, fail_count)
    """
    users = User.objects.filter(is_active=True)
    success_count = 0
    fail_count = 0

    for user in users:
        backup = create_user_inventory_backup(user, created_by, is_scheduled)
        if backup:
            success_count += 1
            # Rotate old backups for this user
            rotate_backups('user_inventory', user=user, keep_count=keep_count)
        else:
            fail_count += 1

    logger.info(f"Created backups for {success_count} users, {fail_count} failed")
    return success_count, fail_count
