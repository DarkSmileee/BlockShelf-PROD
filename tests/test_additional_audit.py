"""Regression coverage for the eight follow-up audit findings."""
import csv
import gzip
from io import StringIO

import pytest
from allauth.account.models import EmailAddress, EmailConfirmationHMAC
from allauth.account.internal.userkit import filter_users_by_email
from django.core import mail
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import connection
from django.urls import reverse
from django.utils import timezone

from inventory.models import InventoryItem, InventoryShare, RBPart
from inventory.views.helpers import index_csv_rows, read_csv_rows
from inventory.views.inventory import process_import_rows

pytestmark = pytest.mark.django_db


def test_email_replacement_requires_confirmation_and_retires_old_recovery(authenticated_client, user):
    old = EmailAddress.objects.create(user=user, email=user.email, primary=True, verified=True)
    url = reverse('account_email')
    response = authenticated_client.get(reverse('inventory:settings') + '?tab=account')
    assert url.encode() in response.content
    response = authenticated_client.post(url, {'email': 'replacement@example.com', 'action_add': ''})
    assert response.status_code == 302
    pending = EmailAddress.objects.get(user=user, email='replacement@example.com')
    user.refresh_from_db()
    assert user.email == old.email and not pending.verified
    assert EmailAddress.objects.filter(pk=old.pk).exists()
    assert any('replacement@example.com' in message.to for message in mail.outbox)
    assert authenticated_client.get(url).status_code == 200
    confirm = reverse('account_confirm_email', args=[EmailConfirmationHMAC(pending).key])
    response = authenticated_client.post(confirm)
    assert response.status_code == 302
    user.refresh_from_db(); pending.refresh_from_db()
    assert user.email == pending.email and pending.primary and pending.verified
    assert not EmailAddress.objects.filter(pk=old.pk).exists()
    assert not filter_users_by_email(old.email)
    assert user in filter_users_by_email(pending.email)


def test_profile_cannot_change_recovery_without_verification(authenticated_client, user):
    old = user.email
    authenticated_client.post(reverse('inventory:settings') + '?tab=account', {
        'action': 'update_profile', 'username': 'renamed', 'email': 'unverified@example.com',
    })
    user.refresh_from_db()
    assert user.username == 'renamed' and user.email == old


def import_row(item, **values):
    return {'part_id': item.part_id, 'color': item.color,
            'storage_location': item.storage_location, **values}


def test_import_locks_existing_rows_and_preserves_omitted_quantities(inventory_item, mocker):
    from django.db.models.query import QuerySet
    original = QuerySet.first
    observed = []
    def first(queryset):
        if queryset.model is InventoryItem:
            observed.append(queryset.query.select_for_update)
        return original(queryset)
    mocker.patch.object(QuerySet, 'first', first)
    process_import_rows(inventory_item.user, [import_row(inventory_item, name='Updated')])
    inventory_item.refresh_from_db()
    assert observed[0] is True  # Later reads belong to audit logging.
    assert (inventory_item.quantity_total, inventory_item.quantity_used) == (100, 10)
    assert inventory_item.name == 'Updated'


@pytest.mark.parametrize('notes,expected', [
    ('first', 'first\nsecond'), ('first\nsecond', 'first\nsecond'),
    ('third', 'first\nsecond\nthird'), ('', 'first\nsecond'),
])
def test_import_notes_never_truncate_existing_notes(inventory_item, notes, expected):
    inventory_item.notes = 'first\nsecond'; inventory_item.save()
    row = import_row(inventory_item, notes=notes)
    process_import_rows(inventory_item.user, [row, row])
    inventory_item.refresh_from_db()
    assert inventory_item.notes == expected


def test_share_limit_rejects_stale_readers(user, client):
    share = InventoryShare.objects.create(user=user, max_access_count=1)
    stale = InventoryShare.objects.get(pk=share.pk)
    assert share.record_access()
    assert not stale.record_access()
    response = client.get(reverse('inventory:shared_inventory', args=[share.token]))
    assert response.status_code == 410
    share.refresh_from_db()
    assert share.access_count == 1


@pytest.mark.parametrize('state', ['revoked', 'expired', 'rotated'])
def test_stale_share_cannot_reserve_after_invalidation(user, state):
    share = InventoryShare.objects.create(user=user)
    stale = InventoryShare.objects.get(pk=share.pk)
    if state == 'revoked':
        share.revoke()
    elif state == 'rotated':
        share.rotate_token()
    else:
        InventoryShare.objects.filter(pk=share.pk).update(expires_at=timezone.now())
    assert not stale.record_access()
    share.refresh_from_db()
    assert share.access_count == 0


@pytest.mark.parametrize('revoked', [False, True])
def test_unavailable_share_returns_404_without_inventory(client, user, inventory_item, revoked):
    token = 'unknown-token'
    if revoked:
        share = InventoryShare.objects.create(user=user); share.revoke(); token = share.token
    response = client.get(reverse('inventory:shared_inventory', args=[token]))
    assert response.status_code == 404
    assert inventory_item.name.encode() not in response.content
    assert b'Share unavailable' in response.content


@pytest.mark.parametrize('compressed', [False, True])
def test_catalog_command_accepts_bom(tmp_path, compressed):
    path = tmp_path / ('parts.csv.gz' if compressed else 'parts.csv')
    raw = '\ufeffpart_num,name\n3003,Yellow brick\n'.encode('utf-8')
    path.write_bytes(gzip.compress(raw) if compressed else raw)
    call_command('load_rebrickable', parts=str(path), stdout=StringIO())
    assert RBPart.objects.get(pk='3003').name == 'Yellow brick'


def test_indexed_batches_seek_near_offset_and_handle_multiline_bom(tmp_path, mocker):
    path = tmp_path / 'parts.csv'
    with path.open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.writer(f); writer.writerow(['part_num', 'name'])
        writer.writerows((str(i), f'Brick {i}\nsecond line') for i in range(1005))
    index = index_csv_rows(str(path))
    assert index['count'] == 1005
    real_reader = csv.DictReader
    reads = []
    def counted(*args, **kwargs):
        for row in real_reader(*args, **kwargs):
            reads.append(row)
            yield row
    mocker.patch('inventory.views.helpers.csv.DictReader', side_effect=counted)
    rows = list(read_csv_rows(str(path), 999, 4, index))
    assert [row['part_num'] for row in rows] == ['999', '1000', '1001', '1002']
    assert rows[0]['name'] == 'Brick 999\nsecond line'
    assert len(reads) == 103  # At most 99 skipped records, regardless of total offset.
    assert list(read_csv_rows(str(path), 999, 4, index)) == rows
    assert list(read_csv_rows(str(path), 1005, 4, index)) == []


def test_catalog_bom_batch_failure_retry_and_next_batch(admin_client, mocker):
    data = '\ufeffpart_num,name\n' + ''.join(f'{i},Brick {i}\n' for i in range(205))
    response = admin_client.post(reverse('inventory:reb_bootstrap_prepare'), {
        'dataset_file': SimpleUploadedFile('parts.csv', data.encode('utf-8'))})
    assert response.status_code == 200
    job = response.json()['job']
    url = reverse('inventory:reb_bootstrap_run')
    params = {'job': job, 'kind': 'parts', 'offset': 0, 'batch_size': 100}
    def fail(*args):
        yield {'part_num': '0', 'name': 'Brick 0'}
        raise ValueError('interrupted')
    failed_reader = mocker.patch('inventory.views.api.read_csv_rows', side_effect=fail)
    response = admin_client.post(url, params)
    mocker.stop(failed_reader)
    assert response.status_code == 400 and not RBPart.objects.exists()
    for offset, expected in [(0, 100), (100, 200), (200, 205)]:
        params['offset'] = offset
        response = admin_client.post(url, params)
        assert response.status_code == 200, response.content
        assert response.json()['offset'] == expected
        assert RBPart.objects.count() == expected
    assert cache.get(f'rebjob:{job}') is None


def test_active_forms_still_render_and_change_password(authenticated_client, user):
    from django.contrib.auth.forms import PasswordChangeForm
    from inventory.forms import ImportCSVForm
    response = authenticated_client.get(reverse('inventory:settings') + '?tab=account')
    assert type(response.context['password_form']) is PasswordChangeForm
    response = authenticated_client.get(reverse('inventory:list'))
    assert isinstance(response.context['import_form'], ImportCSVForm)
    assert b'File includes header row' not in response.content
    response = authenticated_client.post(reverse('inventory:settings') + '?tab=account', {
        'action': 'change_password', 'old_password': 'testpass123',
        'new_password1': 'safe-new-password-42', 'new_password2': 'safe-new-password-42',
    })
    assert response.status_code == 302
    user.refresh_from_db()
    assert user.check_password('safe-new-password-42')


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(connection.vendor != 'postgresql', reason='Requires an isolated PostgreSQL test database')
def test_import_waits_for_concurrent_quantity_edit(user, inventory_item):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from django.db import connections, transaction
    selecting = Event()
    row = import_row(inventory_item, name='Imported name')
    def importing():
        def observe(execute, sql, params, many, context):
            if 'FOR UPDATE' in sql:
                selecting.set()
            return execute(sql, params, many, context)
        try:
            with connections['default'].execute_wrapper(observe):
                return process_import_rows(user, [row])
        finally:
            connections['default'].close()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            item = InventoryItem.objects.select_for_update().get(pk=inventory_item.pk)
            item.quantity_total = 200
            item.save(update_fields=['quantity_total', 'updated_at'])
            future = pool.submit(importing)
            assert selecting.wait(5)
            assert not future.done()
        assert future.result(timeout=10)[1] == 1
    inventory_item.refresh_from_db()
    assert inventory_item.quantity_total == 200
    assert inventory_item.name == 'Imported name'
