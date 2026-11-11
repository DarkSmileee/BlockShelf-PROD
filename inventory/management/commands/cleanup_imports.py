from django.core.management.base import BaseCommand
from inventory.views.helpers import cleanup_import_dirs


class Command(BaseCommand):
    help = 'Remove abandoned catalog uploads older than one hour'

    def handle(self, *args, **options):
        cleanup_import_dirs()
