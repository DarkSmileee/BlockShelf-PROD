import io
import os
from pathlib import Path
import subprocess
from datetime import timedelta
from unittest.mock import Mock
import zipfile

import pytest
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command, CommandError
from django.urls import reverse
from django.utils import timezone

from inventory.models import InventoryCollab, InventoryItem, InventoryShare, RBPart, Backup
from inventory.backup_utils import create_user_inventory_backup, backup_file_path


def upload(text, name='inventory.csv'):
    return SimpleUploadedFile(name, text.encode(), content_type='text/csv')


def test_invitation_requires_recipient_and_post(client, user, other_user):
    invite = InventoryCollab.objects.create(owner=other_user, collaborator=user)
    url = reverse('inventory:accept_invite', args=[invite.token])
    client.force_login(other_user)
    assert client.post(url).status_code == 403
    invite.refresh_from_db()
    assert invite.collaborator == user and invite.accepted_at is None
    client.force_login(user)
    assert client.get(url).status_code == 200
    invite.refresh_from_db()
    assert invite.accepted_at is None
    assert client.post(url).status_code == 302
    invite.refresh_from_db()
    assert invite.accepted_at is not None
    client.force_login(other_user)
    assert client.get(reverse('inventory:revoke_invite', args=[invite.pk])).status_code == 405
    invite.refresh_from_db()
    assert invite.is_active


@pytest.mark.parametrize('url', ['/login/', '/accounts/login/'])
def test_both_login_paths_enforce_turnstile(client, user, settings, mocker, url):
    settings.TURNSTILE_ENABLED = True
    settings.TURNSTILE_SITE_KEY = 'test'
    settings.TURNSTILE_SECRET_KEY = 'test'
    verify = mocker.patch('inventory.views.auth.verify_turnstile_token', return_value=(False, ['missing']))
    response = client.post(url, {'username': user.username, 'password': 'testpass123'})
    assert response.status_code == 400
    verify.assert_called_once()
    assert '_auth_user_id' not in client.session


@pytest.mark.parametrize('next_url,expected', [('https://evil.example/', '/inventory/'),
                                             ('//evil.example/', '/inventory/'),
                                             ('/inventory/notes/', '/inventory/notes/')])
def test_login_redirect(client, user, next_url, expected):
    response = client.post('/login/', {'username': user.username, 'password': 'testpass123', 'next': next_url})
    assert response.status_code == 302 and response.url == expected


def test_ip_headers_only_from_trusted_proxy(rf, settings):
    from inventory.decorators.rate_limit import get_client_ip
    settings.TRUSTED_PROXY_IPS = ['127.0.0.1']
    request = rf.get('/', REMOTE_ADDR='192.0.2.1', HTTP_X_FORWARDED_FOR='198.51.100.1', HTTP_X_REAL_IP='198.51.100.2')
    assert get_client_ip(request) == '192.0.2.1'
    request.META['REMOTE_ADDR'] = '127.0.0.1'
    assert get_client_ip(request) == '198.51.100.2'
    request.META['HTTP_X_REAL_IP'] = 'not an IP'
    assert get_client_ip(request) == '0.0.0.0'


def test_get_does_not_consume_login_attempts(client, db, settings):
    settings.ENABLE_RATE_LIMITING = True
    for _ in range(8):
        assert client.get('/login/').status_code == 200
    assert cache.get('login:127.0.0.1:attempts') is None


def test_private_unique_backups_and_authorization(user, other_user, inventory_item, client, settings, mocker):
    mocker.patch('inventory.backup_utils.timezone.now', return_value=timezone.now())
    first = create_user_inventory_backup(user)
    second = create_user_inventory_backup(user)
    assert first and second and first.file_path != second.file_path
    path = backup_file_path(first.file_path)
    assert path.is_relative_to(Path(settings.BACKUP_ROOT))
    assert not list(Path(settings.MEDIA_ROOT).rglob('*.csv'))
    client.force_login(other_user)
    assert client.get(reverse('inventory:download_backup', args=[first.pk])).status_code == 404
    client.force_login(user)
    response = client.get(reverse('inventory:download_backup', args=[first.pk]))
    assert response.status_code == 200
    # Consume the stream so Django's test client handles response cleanup.
    assert inventory_item.part_id.encode() in b"".join(response.streaming_content)
    assert response.closed
    first.delete()
    assert backup_file_path(second.file_path).exists()


def test_legacy_backups_relocate_without_changing_records(user, settings):
    relative = 'backups/user_inventory/1/old.csv'
    legacy = Path(settings.MEDIA_ROOT) / relative
    legacy.parent.mkdir(parents=True)
    legacy.write_text('important data')
    backup = Backup.objects.create(backup_type='user_inventory', user=user, file_path=relative, file_size=14)
    assert backup_file_path(relative) == legacy
    call_command('relocate_backups')
    backup.refresh_from_db()
    assert backup.file_path == relative
    assert not legacy.exists()
    assert backup_file_path(relative).read_text() == 'important data'
    call_command('relocate_backups')
    with pytest.raises(ValueError):
        backup_file_path('../outside')


def test_failed_dump_does_not_publish_backup(db, settings, mocker):
    from inventory.backup_utils import create_full_db_backup
    mocker.patch.dict(settings.DATABASES['default'], ENGINE='django.db.backends.postgresql', NAME='testdb', USER='test')
    mocker.patch('inventory.backup_utils.subprocess.run', side_effect=subprocess.CalledProcessError(1, 'pg_dump'))
    assert create_full_db_backup() is None
    assert not Backup.objects.exists()
    assert not list(Path(settings.BACKUP_ROOT).rglob('*.partial'))


def test_backup_command_reports_failure(db, mocker):
    mocker.patch('inventory.management.commands.create_backups.create_full_db_backup', return_value=None)
    with pytest.raises(CommandError):
        call_command('create_backups', full_db=True)


@pytest.mark.parametrize('quantity', ['10O', '1,000', '-1', '2.5', 'NaN', 'Infinity'])
def test_invalid_import_never_changes_inventory(authenticated_client, inventory_item, quantity):
    text = f'name,part_id,color,quantity_total\nChanged,3001,Red,"{quantity}"\n'
    response = authenticated_client.post(reverse('inventory:import_csv'), {'file': upload(text)}, follow=True)
    assert response.status_code == 200
    inventory_item.refresh_from_db()
    assert inventory_item.quantity_total == 100 and inventory_item.name == 'Test Brick 2x4'


def test_import_export_selected_inventory(authenticated_client, user, other_user):
    InventoryCollab.objects.create(owner=other_user, collaborator=user, accepted_at=timezone.now(), can_edit=True)
    url = reverse('inventory:import_csv') + f'?owner={other_user.pk}'
    response = authenticated_client.post(url, {'file': upload('name,part_id,color,quantity_total\nTheir brick,3001,Red,12.0\n')})
    assert response.status_code == 302
    item = InventoryItem.objects.get()
    assert item.user == other_user and item.quantity_total == 12
    exported = authenticated_client.get(reverse('inventory:export_csv') + f'?owner={other_user.pk}')
    assert 'Their brick' in exported.content.decode()
    InventoryCollab.objects.filter(owner=other_user).update(can_edit=False)
    assert authenticated_client.post(url, {'file': upload('name,part_id,color\nx,3002,Red')}).status_code == 403
    assert InventoryItem.objects.count() == 1


def prepare_catalog(admin_client, text='part_num,name\n3001,Brick\n3002,Plate\n'):
    response = admin_client.post(reverse('inventory:reb_bootstrap_prepare'), {'dataset_file': upload(text, 'parts.csv')})
    assert response.status_code == 200, response.content
    return response.json()['job']


def test_catalog_failure_rolls_back_and_does_not_advance(admin_client, mocker):
    job = prepare_catalog(admin_client)
    def rows(*args):
        yield {'part_num': '3001', 'name': 'Brick'}
        raise ValueError('bad next row')
    mocker.patch('inventory.views.api.read_csv_rows', side_effect=rows)
    response = admin_client.post(reverse('inventory:reb_bootstrap_run'), {'job': job, 'kind': 'parts', 'offset': 0})
    assert response.status_code == 400 and response.json()['offset'] == 0
    assert not RBPart.objects.exists()


def test_completed_catalog_cleans_temp_files(admin_client):
    job = prepare_catalog(admin_client)
    directory = Path(cache.get(f'rebjob:{job}')['root'])
    response = admin_client.post(reverse('inventory:reb_bootstrap_run'), {'job': job, 'kind': 'parts'})
    assert response.status_code == 200 and response.json()['done']
    assert RBPart.objects.count() == 2
    assert not directory.exists() and cache.get(f'rebjob:{job}') is None


def test_maintenance_preserves_concurrent_edit(authenticated_client, inventory_item, mocker):
    InventoryItem.objects.filter(pk=inventory_item.pk).update(name='Unknown')
    def lookup(*args):
        InventoryItem.objects.filter(pk=inventory_item.pk).update(quantity_total=200, name='My better name')
        return {'name': 'Catalog name', 'part_img_url': 'https://example.com/brick.jpg', 'part_num': '3001'}, 1
    mocker.patch('inventory.views.api.fetch_rebrickable_part', side_effect=lookup)
    response = authenticated_client.post(reverse('inventory:bulk_update_batch'))
    assert response.status_code == 200
    inventory_item.refresh_from_db()
    assert inventory_item.quantity_total == 200
    assert inventory_item.name == 'My better name'
    assert inventory_item.image_url == 'https://example.com/brick.jpg'


def test_maintenance_returns_progress_before_deadline(authenticated_client, inventory_items, mocker):
    clock = Mock(side_effect=[0, 0, 16])
    mocker.patch('inventory.views.api.time.monotonic', clock)
    mocker.patch('inventory.views.api.fetch_rebrickable_part', return_value=(None, 0))
    response = authenticated_client.post(reverse('inventory:bulk_update_batch'))
    assert response.status_code == 200
    assert response.json()['last_id'] == inventory_items[0].pk
    assert response.json()['processed'] == 1


def test_bricklink_respects_same_call_budget(authenticated_client, mocker):
    mocker.patch('inventory.views.api._rate_limit_ok', return_value=False)
    request = mocker.patch('requests.get')
    response = authenticated_client.get(reverse('inventory:lookup_part'), {'part_id': 'unknown'})
    assert response.status_code == 200
    request.assert_not_called()


def test_share_refresh_renews_expired_link(authenticated_client, user):
    old = InventoryShare.objects.create(user=user, expires_at=timezone.now()-timedelta(days=1), access_count=10)
    authenticated_client.post(reverse('inventory:create_share_link'))
    old.refresh_from_db()
    fresh = InventoryShare.objects.get(user=user, is_active=True)
    assert not old.is_active and fresh.token != old.token
    assert fresh.expires_at > timezone.now() and fresh.access_count == 0


def test_upload_limits_during_reading(authenticated_client, mocker):
    mocker.patch('inventory.views.inventory.CSV_IMPORT_MAX_ROWS', 1)
    response = authenticated_client.post(reverse('inventory:import_csv'), {'file': upload('name,part_id,color\na,1,Red\nb,2,Red')})
    assert response.status_code == 302 and not InventoryItem.objects.exists()


def test_expanded_archive_limit(admin_client, mocker, settings):
    mocker.patch('inventory.views.helpers.MAX_EXPANDED_BYTES', 20)
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('parts.csv', 'part_num,name\n' + '1,Brick\n' * 50)
    response = admin_client.post(reverse('inventory:reb_bootstrap_prepare'), {
        'dataset_file': SimpleUploadedFile('catalog.zip', data.getvalue())})
    assert response.status_code == 400
    assert not list(Path(settings.REBRICKABLE_TEMP_ROOT).iterdir())


def test_expired_imports_removed(settings):
    from inventory.views.helpers import create_temp_dir
    directory = Path(create_temp_dir())
    (directory/'parts.csv').write_text('old upload')
    os.utime(directory, (0, 0))
    call_command('cleanup_imports')
    assert not directory.exists()
