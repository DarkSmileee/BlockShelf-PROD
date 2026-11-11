from html.parser import HTMLParser
from pathlib import Path
import shutil
import subprocess
from urllib.parse import parse_qs, urlsplit

import pytest
from django.test import Client
from django.urls import reverse

from inventory.models import InventoryItem, InventoryShare


@pytest.mark.parametrize('authenticated', [False, True])
@pytest.mark.parametrize('theme', ['light', 'dark', 'system'])
def test_theme_cookie_works_for_every_user(client, user, authenticated, theme):
    if authenticated:
        client.force_login(user)
    before = dict(user.userpreference.__dict__)
    response = client.post(reverse('inventory:set_theme'), {'theme': theme})
    assert response.status_code == 200
    assert response.json() == {'ok': True, 'theme': theme}
    cookie = response.cookies['theme']
    assert cookie.value == theme and cookie['path'] == '/'
    assert cookie['samesite'] == 'Lax' and cookie['max-age'] == 365 * 24 * 60 * 60
    page = client.get('/login/')
    if not authenticated:
        assert f'data-cookie-theme="{theme}"' in page.content.decode()
    user.userpreference.refresh_from_db()
    assert user.userpreference.items_per_page == before['items_per_page']
    assert user.userpreference.rebrickable_api_key == before['rebrickable_api_key']


def test_theme_rejects_bad_input_and_requires_csrf(client):
    url = reverse('inventory:set_theme')
    assert client.get(url).status_code == 405
    for value in ['', 'invalid', '<script>']:
        response = client.post(url, {'theme': value})
        assert response.status_code == 400 and 'theme' not in response.cookies
    assert Client(enforce_csrf_checks=True).post(url, {'theme': 'dark'}).status_code == 403


class SortLinks(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.links = {}
        self.feed(html)

    def handle_starttag(self, tag, attributes):
        attributes = dict(attributes)
        if tag == 'a' and attributes.get('class') == 'sort-link':
            url = attributes['href']
            self.links[parse_qs(urlsplit(url).query)['sort'][0]] = url


@pytest.mark.parametrize('key,field', [
    ('name', 'name'), ('part', 'part_id'), ('color', 'color'),
    ('total', 'quantity_total'), ('used', 'quantity_used'),
    ('avail', 'quantity_available'), ('loc', 'storage_location'),
])
def test_public_sort_links_toggle_and_preserve_search(client, user, key, field):
    items = [
        InventoryItem.objects.create(user=user, name='Brick B', part_id='2', color='Red',
                                     quantity_total=20, quantity_used=5, storage_location='A'),
        InventoryItem.objects.create(user=user, name='Brick A', part_id='1', color='Blue',
                                     quantity_total=30, quantity_used=25, storage_location='B'),
    ]
    share = InventoryShare.objects.create(user=user)
    url = reverse('inventory:shared_inventory', args=[share.token])
    response = client.get(url, {'sort': key, 'dir': 'asc', 'q': 'Brick', 'page': 2})
    assert response.status_code == 200
    expected = sorted(items, key=lambda item: getattr(item, field))
    assert list(response.context['page_obj']) == expected
    links = SortLinks(response.content.decode()).links
    assert set(links) == {'name', 'part', 'color', 'total', 'used', 'avail', 'loc'}
    query = parse_qs(urlsplit(links[key]).query)
    assert query == {'sort': [key], 'dir': ['desc'], 'q': ['Brick']}
    descending = client.get(links[key])
    assert list(descending.context['page_obj']) == list(reversed(expected))
    assert 'sort-desc' in descending.content.decode()
    next_links = SortLinks(descending.content.decode()).links
    assert parse_qs(urlsplit(next_links[key]).query)['dir'] == ['asc']
    for other_key, other_url in next_links.items():
        if other_key != key:
            assert parse_qs(urlsplit(other_url).query)['dir'] == ['asc']


def test_theme_browser_logic():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required for the browser-script regression check')
    result = subprocess.run([node, str(Path(__file__).with_name('theme_behavior.cjs'))],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
