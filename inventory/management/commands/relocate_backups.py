"""Move legacy media backups without changing their relative database paths."""
import shutil
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from inventory.backup_utils import get_backup_dir


class Command(BaseCommand):
    help = 'Move existing media/backups files to private backup storage'

    def handle(self, *args, **options):
        source = Path(settings.MEDIA_ROOT).resolve() / 'backups'
        destination = get_backup_dir()
        if source.is_symlink():
            raise CommandError('Refusing a symlinked legacy backup directory')
        if not source.exists():
            return
        for path in source.rglob('*'):
            if path.is_symlink():
                raise CommandError(f'Refusing backup symlink: {path}')
            if not path.is_file():
                continue
            target = destination / path.relative_to(source)
            if target.exists():
                raise CommandError(f'Target already exists; nothing overwritten: {target}')
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.move(str(path), str(target))
            target.chmod(0o600)
        self.stdout.write(self.style.SUCCESS('Legacy backups moved to private storage'))
