from django.core.management.base import BaseCommand
from django.utils import timezone
from inventory.models import RateLimitBucket


class Command(BaseCommand):
    help = 'Remove expired rate-limit counters'

    def handle(self, *args, **options):
        count, _ = RateLimitBucket.objects.filter(expires_at__lte=timezone.now()).delete()
        self.stdout.write(f'Removed {count} expired rate-limit counters')
