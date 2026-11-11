"""Exercise existing user flows as well as the security fixes."""
import io
from datetime import timedelta
from pathlib import Path

import pytest
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command, CommandError
from django.urls import reverse
from django.utils import timezone

from inventory.models import AppConfig, InventoryItem, InventoryCollab, Note, LoginAttempt, InventoryShare
from inventory.backup_utils import create_user_inventory_backup, rotate_backups


def test_inventory_crud_dashboard_and_sorting(authenticated_client, user):
    payload = dict(name='Brick', part_id='3001', color='Red', quantity_total=12, quantity_used=2)
    assert authenticated_client.get(reverse('inventory:add')).status_code == 200
    assert authenticated_client.post(reverse('inventory:add'), payload).status_code == 302
    item = InventoryItem.objects.get(user=user)
    assert item.quantity_available == 10
    assert authenticated_client.get(reverse('inventory:dashboard')).status_code == 200
    for query in ['?sort=avail&dir=desc', '?q=Brick', '?sort=loc', '?page=999']:
        assert authenticated_client.get(reverse('inventory:list')+query).status_code == 200
    payload.update(quantity_used=3, storage_location='Drawer 1', version=item.updated_at.isoformat())
    assert authenticated_client.post(reverse('inventory:edit', args=[item.pk]), payload).status_code == 302
    item.refresh_from_db()
    assert item.quantity_used == 3 and item.storage_location == 'Drawer 1'
    bad = dict(payload, quantity_used=20)
    assert authenticated_client.post(reverse('inventory:edit', args=[item.pk]), bad).status_code == 200
    item.refresh_from_db()
    assert item.quantity_used == 3
    assert authenticated_client.post(reverse('inventory:delete', args=[item.pk])).status_code == 302
    assert not InventoryItem.objects.exists()


def test_notes_are_private_and_editable(authenticated_client, user, other_user):
    private = Note.objects.create(user=other_user, title='Private', description='Secret')
    assert authenticated_client.get(reverse('inventory:note_edit', args=[private.pk])).status_code == 404
    assert authenticated_client.get(reverse('inventory:note_create')).status_code == 200
    authenticated_client.post(reverse('inventory:note_create'), {'title': 'Build', 'description': 'Plan'})
    note = Note.objects.get(user=user)
    assert authenticated_client.get(reverse('inventory:note_edit', args=[note.pk])).status_code == 200
    authenticated_client.post(reverse('inventory:note_edit', args=[note.pk]), {'title': 'Build', 'description': 'Updated'})
    note.refresh_from_db()
    assert note.description == 'Updated'
    listing = authenticated_client.get(reverse('inventory:notes_list'))
    assert 'Private' not in listing.content.decode()
    assert authenticated_client.get(reverse('inventory:note_delete', args=[note.pk])).status_code == 200
    authenticated_client.post(reverse('inventory:note_delete', args=[note.pk]))
    assert not Note.objects.filter(pk=note.pk).exists()
    assert Note.objects.filter(pk=private.pk).exists()


def test_account_and_site_settings(authenticated_client, user):
    url = reverse('inventory:settings')
    for tab in ['account', 'site', 'sharing', 'invite', 'backups', 'maintenance', 'danger']:
        assert authenticated_client.get(url+'?tab='+tab).status_code == 200
    assert authenticated_client.get(url+'?tab=admin').status_code == 403
    authenticated_client.post(url+'?tab=site', {'items_per_page': 50, 'rebrickable_api_key': 'new-key'})
    user.userpreference.refresh_from_db()
    assert user.userpreference.items_per_page == 50
    assert user.userpreference.rebrickable_api_key == 'new-key'
    authenticated_client.post(url+'?tab=account', {'action': 'save_profile', 'username': user.username, 'email': 'new@example.com'})
    user.refresh_from_db()
    assert user.email == 'test@example.com'  # Profile edits cannot bypass verification.
    response = authenticated_client.post(url+'?tab=account', {'action': 'change_password', 'old_password': 'testpass123',
                                                            'new_password1': 'new-password-123', 'new_password2': 'new-password-123'})
    assert response.status_code == 302
    user.refresh_from_db()
    assert user.check_password('new-password-123')
    assert authenticated_client.get(reverse('inventory:list')).status_code == 200


def test_admin_settings_and_backup_lists(admin_client, user, inventory_item):
    url = reverse('inventory:settings')+'?tab=admin'
    assert admin_client.get(url).status_code == 200
    admin_client.post(url, {'site_name': 'My Shelf', 'allow_registration': '', 'default_from_email': 'owner@example.com'})
    assert not AppConfig.get_solo().allow_registration
    backup = create_user_inventory_backup(user)
    assert admin_client.get(reverse('inventory:settings')+'?tab=backups').status_code == 200
    data = admin_client.get(reverse('inventory:list_all_backups')).json()
    assert any(row['id'] == backup.pk for row in data['backups'])
    response = admin_client.post(reverse('inventory:delete_backup', args=[backup.pk]))
    assert response.status_code == 302
    assert not type(backup).objects.filter(pk=backup.pk).exists()


def test_delete_all_needs_both_confirmations(authenticated_client, inventory_item, other_user):
    theirs = InventoryItem.objects.create(user=other_user, name='Other', part_id='1', color='Red')
    url = reverse('inventory:delete_all_inventory')
    assert authenticated_client.get(url).status_code == 405
    authenticated_client.post(url, {'confirm_text': 'DELETE'})
    assert InventoryItem.objects.filter(pk=inventory_item.pk).exists()
    authenticated_client.post(url, {'confirm_text': 'DELETE', 'confirm_ack': 'on'})
    assert not InventoryItem.objects.filter(pk=inventory_item.pk).exists()
    assert InventoryItem.objects.filter(pk=theirs.pk).exists()


def test_backup_rotation_and_user_listing(authenticated_client, user, inventory_item):
    backups = [create_user_inventory_backup(user) for _ in range(3)]
    assert rotate_backups('user_inventory', user=user, keep_count=2) == 1
    data = authenticated_client.get(reverse('inventory:list_user_backups')).json()
    assert len(data['backups']) == 2
    assert backups[0].pk not in [row['id'] for row in data['backups']]
    with pytest.raises(ValueError):
        rotate_backups('user_inventory', user=user, keep_count=0)


def test_backup_commands_success_and_partial_failure(user, inventory_item, mocker):
    call_command('create_backups', all_users=True, keep=1)
    full = mocker.patch('inventory.management.commands.create_backups.create_full_db_backup', return_value=create_user_inventory_backup(user))
    call_command('create_backups', full_db=True)
    full.assert_called_once()
    mocker.patch('inventory.management.commands.create_backups.create_all_user_backups', return_value=(1, 1))
    with pytest.raises(CommandError):
        call_command('create_backups', all_users=True)


def test_failed_login_and_lockout(client, user, settings):
    settings.ENABLE_ACCOUNT_LOCKOUT = True
    for _ in range(4):
        assert client.post('/login/', {'username': user.username, 'password': 'wrong'}).status_code == 200
    assert client.post('/login/', {'username': user.username, 'password': 'wrong'}).status_code == 429
    assert client.post('/login/', {'username': user.username, 'password': 'testpass123'}).status_code == 429
    assert LoginAttempt.is_account_locked(user.username)[0]
    assert LoginAttempt.clear_attempts(user.username) == 6
    assert not LoginAttempt.is_account_locked(user.username)[0]
    assert client.post('/login/', {'username': user.username, 'password': 'testpass123'}).status_code == 302


def test_rate_limit_blocks_submissions(client, db, settings):
    settings.ENABLE_RATE_LIMITING = True
    for _ in range(5):
        assert client.post('/login/', {'username': 'missing', 'password': 'wrong'}).status_code in (200, 429)
    response = client.post('/login/', HTTP_ACCEPT='application/json')
    assert response.status_code == 429
    assert client.post('/login/').status_code == 429
    assert client.get('/login/').status_code == 200


def test_xlsx_import_roundtrip(authenticated_client, user):
    from openpyxl import Workbook
    workbook = Workbook()
    workbook.active.append(['name', 'part_id', 'color', 'quantity_total'])
    workbook.active.append(['Brick', '3001', 'Red', 12])
    stream = io.BytesIO()
    workbook.save(stream)
    workbook.close()
    response = authenticated_client.post(reverse('inventory:import_csv'), {
        'file': SimpleUploadedFile('inventory.xlsx', stream.getvalue())})
    assert response.status_code == 302
    assert InventoryItem.objects.get(user=user).quantity_total == 12


def test_catalog_zip_colors_parts_elements(admin_client, settings):
    import zipfile
    from inventory.models import RBColor, RBElement
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive:
        archive.writestr('colors.csv', 'id,name\n4,Red\n')
        archive.writestr('parts.csv', 'part_num,name\n3001,Brick\n')
        archive.writestr('elements.csv', 'element_id,part_num,color_id\n123456,3001,4\n')
    response = admin_client.post(reverse('inventory:reb_bootstrap_prepare'), {
        'dataset_file': SimpleUploadedFile('catalog.zip', stream.getvalue())})
    job = response.json()['job']
    for kind in ['colors', 'parts', 'elements']:
        response = admin_client.post(reverse('inventory:reb_bootstrap_run'), {'job': job, 'kind': kind})
        assert response.json()['done']
    assert RBColor.objects.get(pk=4).name == 'Red'
    assert RBElement.objects.get(pk='123456').part.name == 'Brick'
    assert not list(Path(settings.REBRICKABLE_TEMP_ROOT).iterdir())


def test_turnstile_network_failure_and_success(settings, mocker):
    import requests
    from inventory.turnstile import verify_turnstile_token
    settings.TURNSTILE_ENABLED = True
    settings.TURNSTILE_SITE_KEY = 'site'
    settings.TURNSTILE_SECRET_KEY = 'secret'
    assert verify_turnstile_token('')[0] is False
    post = mocker.patch('requests.post', side_effect=requests.RequestException('offline'))
    assert verify_turnstile_token('token')[0] is False
    post.side_effect = None
    post.return_value.json.return_value = {'success': True}
    assert verify_turnstile_token('token', '127.0.0.1')[0] is True


def test_collaboration_permission_changes_and_purge(authenticated_client, user, other_user):
    invite = InventoryCollab.objects.create(owner=user, collaborator=other_user, accepted_at=timezone.now())
    authenticated_client.post(reverse('inventory:update_invite', args=[invite.pk]), {'can_delete': '1'})
    invite.refresh_from_db()
    assert invite.can_delete and not invite.can_edit
    authenticated_client.post(reverse('inventory:purge_invite', args=[invite.pk]))
    assert InventoryCollab.objects.filter(pk=invite.pk).exists()
    authenticated_client.post(reverse('inventory:revoke_invite', args=[invite.pk]))
    authenticated_client.post(reverse('inventory:purge_invite', args=[invite.pk]))
    assert not InventoryCollab.objects.filter(pk=invite.pk).exists()
