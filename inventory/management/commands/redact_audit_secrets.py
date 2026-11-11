import json
from django.core.management.base import BaseCommand, CommandError
from auditlog.models import LogEntry


class Command(BaseCommand):
    help = 'Remove historical API-key copies from preference audit records'

    def handle(self, *args, **options):
        count = 0
        entries = LogEntry.objects.filter(content_type__app_label='inventory', content_type__model='userpreference')
        for entry in entries.iterator():
            fields = []
            for field in ('changes', 'changes_text'):
                original = getattr(entry, field)
                if not original:
                    continue
                try:
                    data = json.loads(original) if isinstance(original, str) else dict(original)
                except (ValueError, TypeError):
                    raise CommandError(f'Unrecognized audit format in record {entry.pk}; inspect it privately.')
                if 'rebrickable_api_key' in data:
                    del data['rebrickable_api_key']
                    setattr(entry, field, json.dumps(data) if isinstance(original, str) else data)
                    fields.append(field)
            if entry.serialized_data and 'rebrickable_api_key' in entry.serialized_data.get('fields', {}):
                del entry.serialized_data['fields']['rebrickable_api_key']
                fields.append('serialized_data')
            if fields:
                entry.save(update_fields=fields)
                count += 1
        self.stdout.write(f'Redacted {count} audit records')
