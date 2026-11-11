"""Regression cases reproduced in the second full audit."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
import json
import threading

import pytest
from django.core.cache import cache
from django.core.management import call_command
from django.db import connections
from django.urls import reverse
from django.utils import timezone

from inventory.models import (AppConfig, InventoryItem, InventoryShare, LoginAttempt,
                              RBColor, RBPart, RBElement, RateLimitBucket)
from inventory.decorators.rate_limit import consume_rate_limit


@pytest.mark.django_db
def test_stale_edit_and_missing_version_keep_current_quantity(authenticated_client, inventory_item):
    url = reverse('inventory:edit', args=[inventory_item.pk])
    old_form = authenticated_client.get(url).context['form']
    data = {name: old_form[name].value() or '' for name in old_form.fields}
    fresh = dict(data, quantity_total=200)
    assert authenticated_client.post(url, fresh).status_code == 302
    stale = authenticated_client.post(url, dict(data, notes='Keep my notes'))
    assert stale.status_code == 409
    assert b'Keep my notes' in stale.content and b'changed since you opened' in stale.content
    inventory_item.refresh_from_db()
    assert inventory_item.quantity_total == 200
    data.pop('version')
    assert authenticated_client.post(url, data).status_code == 200
    inventory_item.refresh_from_db()
    assert inventory_item.quantity_total == 200


@pytest.mark.django_db
def test_bulk_does_not_apply_old_part_data(authenticated_client, inventory_item, mocker):
    InventoryItem.objects.filter(pk=inventory_item.pk).update(name='Unknown')
    def lookup(*args):
        InventoryItem.objects.filter(pk=inventory_item.pk).update(part_id='9999')
        return {'part_num': '3001', 'name': 'Wrong brick', 'part_img_url': 'https://example.test/old.jpg'}, 1
    mocker.patch('inventory.views.api.fetch_rebrickable_part', side_effect=lookup)
    assert authenticated_client.post(reverse('inventory:bulk_update_batch')).status_code == 200
    inventory_item.refresh_from_db()
    assert inventory_item.part_id == '9999'
    assert inventory_item.name == 'Unknown' and inventory_item.image_url == ''


@pytest.mark.django_db
def test_admin_login_uses_lockout_and_turnstile(client, admin_user, settings, mocker):
    settings.ENABLE_ACCOUNT_LOCKOUT = True
    for _ in range(5):
        LoginAttempt.record_attempt(admin_user.username, '127.0.0.1', False)
    credentials = {'username': admin_user.username, 'password': 'adminpass123'}
    assert client.post('/admin/login/', credentials).status_code == 429
    assert '_auth_user_id' not in client.session
    LoginAttempt.clear_attempts(admin_user.username)
    settings.TURNSTILE_ENABLED = True
    settings.TURNSTILE_SITE_KEY = settings.TURNSTILE_SECRET_KEY = 'synthetic'
    mocker.patch('inventory.views.auth.verify_turnstile_token', return_value=(False, ('missing-input-response',)))
    assert client.post('/admin/login/', credentials).status_code == 400
    assert '_auth_user_id' not in client.session
    settings.TURNSTILE_ENABLED = False
    response = client.post('/admin/login/', credentials)
    assert response.status_code == 302 and response.url == '/admin/'
    assert client.get('/admin/').status_code == 200


@pytest.mark.django_db
def test_admin_login_retains_staff_gate(client, user):
    response = client.post('/admin/login/', {'username': user.username, 'password': 'testpass123'})
    assert response.status_code == 403 and '_auth_user_id' not in client.session
    client.force_login(user)
    assert client.get('/admin/login/').status_code == 403


@pytest.mark.django_db
def test_mandatory_email_verification_is_enforced(client, user, settings):
    from allauth.account.models import EmailAddress
    settings.ACCOUNT_EMAIL_VERIFICATION = 'mandatory'
    settings.EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'
    credentials = {'username': user.username, 'password': 'testpass123'}
    response = client.post('/login/', credentials)
    assert response.status_code == 302
    assert '_auth_user_id' not in client.session
    assert response.url == reverse('account_email_verification_sent')
    email, _ = EmailAddress.objects.get_or_create(user=user, email=user.email)
    email.verified = True; email.primary = True; email.save()
    assert client.post('/login/', credentials).status_code == 302
    assert client.session['_auth_user_id'] == str(user.pk)


@pytest.mark.django_db
def test_registration_default_and_existing_admin_choice(settings):
    from inventory.utils import get_effective_config
    settings.ALLOW_REGISTRATION = False
    AppConfig.objects.all().delete(); cache.clear()
    assert get_effective_config().allow_registration is False
    config = AppConfig.get_solo(); config.allow_registration = True; config.save()
    assert get_effective_config().allow_registration is True


@pytest.mark.django_db
def test_api_secrets_excluded_and_history_redacted(user):
    from auditlog.models import LogEntry
    from django.contrib.contenttypes.models import ContentType
    pref = user.userpreference
    pref.rebrickable_api_key = 'SYNTHETIC-SECRET'; pref.items_per_page = 50; pref.save()
    entries = LogEntry.objects.filter(content_type=ContentType.objects.get_for_model(pref))
    assert all('SYNTHETIC-SECRET' not in json.dumps(e.changes) for e in entries)
    old = LogEntry.objects.create(content_type=ContentType.objects.get_for_model(pref),
        object_pk=str(pref.pk), object_repr='Preference', action=1,
        changes={'rebrickable_api_key': ['OLD-SECRET', 'NEW-SECRET'], 'items_per_page': [25, 50]},
        changes_text=json.dumps({'rebrickable_api_key': ['OLD-SECRET', 'NEW-SECRET']}),
        serialized_data={'fields': {'rebrickable_api_key': 'NEW-SECRET', 'items_per_page': 50}})
    call_command('redact_audit_secrets', verbosity=0)
    call_command('redact_audit_secrets', verbosity=0)
    old.refresh_from_db()
    assert old.changes == {'items_per_page': [25, 50]}
    assert json.loads(old.changes_text) == {}
    assert old.serialized_data == {'fields': {'items_per_page': 50}}
    pref.refresh_from_db(); assert pref.rebrickable_api_key == 'SYNTHETIC-SECRET'


@pytest.mark.django_db(transaction=True)
def test_atomic_limit_with_parallel_connections():
    RateLimitBucket.objects.create(key='parallel', expires_at=timezone.now()+timedelta(seconds=60))
    barrier = threading.Barrier(6)
    def attempt(_):
        try:
            barrier.wait(timeout=10)
            return consume_rate_limit('parallel', 1, 60)
        finally:
            # Django deliberately ignores close() for shared in-memory SQLite.
            db = connections['default']
            if db.vendor == 'sqlite' and db.connection is not None:
                db.connection.close()
                db.connection = None
            connections.close_all()
    with ThreadPoolExecutor(max_workers=6) as pool:
        outcomes = list(pool.map(attempt, range(6)))
    assert sum(outcomes) == 1
    assert RateLimitBucket.objects.get(key='parallel').count == 1


@pytest.mark.django_db
def test_counter_expiry_and_cleanup():
    assert consume_rate_limit('expiry', 1, 60)
    assert not consume_rate_limit('expiry', 1, 60)
    RateLimitBucket.objects.filter(key='expiry').update(expires_at=timezone.now()-timedelta(seconds=1))
    assert consume_rate_limit('expiry', 1, 60)
    RateLimitBucket.objects.create(key='expired', expires_at=timezone.now()-timedelta(seconds=1))
    call_command('cleanup_rate_limits')
    assert RateLimitBucket.objects.filter(key='expiry').exists()
    assert not RateLimitBucket.objects.filter(key='expired').exists()


@pytest.mark.django_db
@pytest.mark.parametrize('shared', [False, True])
def test_element_search_respects_colour_and_owner(client, user, other_user, shared):
    part = RBPart.objects.create(part_num='3003', name='Brick')
    colour = RBColor.objects.create(id=14, name='Yellow')
    RBElement.objects.create(element_id='654321', part=part, color=colour)
    good = InventoryItem.objects.create(user=user, name='Brick', part_id='3003', color='Yellow')
    InventoryItem.objects.create(user=user, name='Brick', part_id='3003', color='Red')
    InventoryItem.objects.create(user=other_user, name='Private', part_id='3003', color='Yellow')
    if shared:
        share = InventoryShare.objects.create(user=user)
        url = reverse('inventory:shared_inventory', args=[share.token])
    else:
        client.force_login(user); url = reverse('inventory:list')
    response = client.get(url, {'q': '654321'})
    assert list(response.context['page_obj']) == [good]


@pytest.mark.django_db
def test_bulk_rate_limit_retries_same_item(authenticated_client, inventory_item, mocker):
    from inventory.views.helpers import fetch_rebrickable_part
    InventoryItem.objects.filter(pk=inventory_item.pk).update(name='Unknown')
    limit = mocker.patch('inventory.views.api._rate_limit_ok', return_value=False)
    network = mocker.patch('requests.get')
    url = reverse('inventory:bulk_update_batch')
    response = authenticated_client.post(url).json()
    assert response['last_id'] == 0 and response['processed'] == 0
    assert response['retry_after'] > 0
    assert not any('Not found' in line for line in response['messages'])
    network.assert_not_called()
    limit.return_value = True
    mocker.patch('inventory.views.api.fetch_rebrickable_part', return_value=(
        {'part_num': '3001', 'name': 'Found brick', 'part_img_url': 'https://example.test/image.jpg'}, 1))
    response = authenticated_client.post(url, {'after_id': response['last_id']}).json()
    inventory_item.refresh_from_db()
    assert response['updated_names'] == 1 and inventory_item.name == 'Found brick'


@pytest.mark.django_db
def test_page_size_server_bounds_and_legacy_clamp(authenticated_client, user):
    from inventory.forms import UserSettingsForm
    for value in [0, 4, 501, 1000000]:
        assert not UserSettingsForm({'items_per_page': value}, instance=user.userpreference).is_valid()
    assert UserSettingsForm({'items_per_page': 500}, instance=user.userpreference).is_valid()
    type(user.userpreference).objects.filter(user=user).update(items_per_page=1000000)
    response = authenticated_client.get(reverse('inventory:list'))
    assert response.context['page_obj'].paginator.per_page == 500


def test_proxy_and_csp_match_application_resources():
    from urllib.parse import urlsplit
    import re
    root = Path(__file__).resolve().parents[1]
    nginx = (root/'nginx/conf.d/blockshelf.conf').read_text()
    gunicorn = (root/'gunicorn.conf.py').read_text()
    assert "get_env_str('GUNICORN_BIND', '127.0.0.1:8000')" in gunicorn
    assert 'server 127.0.0.1:8000;' in nginx
    assert '--bind 127.0.0.1:8000' in (root/'deploy/blockshelf.service').read_text()
    csp = re.search(r'Content-Security-Policy "([^"]+)"', nginx)[1]
    directives = {part.split()[0]: part.split()[1:] for part in csp.split(';') if part.strip()}
    for template in ['base.html', 'registration/login.html']:
        source = (root/'templates'/template).read_text()
        for url in re.findall(r'<script[^>]+src="(https:[^"]+)"', source):
            parsed = urlsplit(url)
            assert f'{parsed.scheme}://{parsed.netloc}' in directives['script-src']
    assert 'https://challenges.cloudflare.com' in directives['frame-src']
    assert 'https://cdn.jsdelivr.net' in directives['style-src']


def test_installer_creates_owned_directory_and_pins_release():
    root = Path(__file__).resolve().parents[1]
    source = (root/'scripts/install_blockshelf.sh').read_text()
    assert source.index('install -d -o') < source.index('git clone')
    assert 'checkout --detach "$REVISION"' in source
    assert 'pip install -r $APP_DIR/requirements.lock' in source
    assert 'pip install -r $APP_DIR/requirements.txt' not in source


def test_maintenance_browser_retry():
    import shutil
    import subprocess
    node = shutil.which('node')
    assert node, 'Node is required for browser-script tests'
    result = subprocess.run([node, str(Path(__file__).with_name('maintenance_retry.cjs'))], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
