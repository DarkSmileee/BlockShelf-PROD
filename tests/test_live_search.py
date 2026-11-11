from pathlib import Path
import shutil
import subprocess
import pytest
from django.urls import reverse
from django.utils import timezone
from inventory.models import InventoryItem, InventoryCollab, InventoryShare


def test_search_javascript():
    node = shutil.which('node')
    assert node, 'Node is required for live search regression tests'
    result = subprocess.run([node, str(Path(__file__).with_name('live_search_behavior.cjs'))], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.django_db
@pytest.mark.parametrize('shared', [False, True])
def test_search_across_pages_and_scope(client, user, other_user, shared):
    for i in range(30):
        InventoryItem.objects.create(user=other_user, name=f'Brick {i:02}', part_id=str(i),
            color='Yellow', storage_location='1563' if i == 29 else '1562')
    InventoryItem.objects.create(user=user, name='Private item', part_id='secret', color='Yellow', storage_location='1563')
    if shared:
        share = InventoryShare.objects.create(user=other_user)
        url = reverse('inventory:shared_inventory', args=[share.token])
    else:
        client.force_login(user)
        InventoryCollab.objects.create(owner=other_user, collaborator=user, accepted_at=timezone.now(), can_edit=False)
        url = reverse('inventory:list')
    initial = client.get(url, {'owner': other_user.pk})
    assert 'Brick 29' not in initial.content.decode()
    response = client.get(url, {'q': '1563', 'owner': other_user.pk, 'sort': 'loc', 'dir': 'desc'})
    html = response.content.decode()
    assert [item.name for item in response.context['page_obj']] == ['Brick 29']
    assert 'Private item' not in html
    assert 'id="inventorySearch"' in html and 'js/inventory-search.js' in html
    assert 'id="inventoryResults"' in html and 'data-count="1"' in html
    assert 'role="status"' in html
    assert '>Edit</a>' not in html
    assert 'REAL-TIME FILTERING' not in html
    response = client.get(url, {'q': '', 'owner': other_user.pk})
    assert response.context['page_obj'].paginator.count == 30
    response = client.get(url, {'q': 'not-present', 'owner': other_user.pk})
    assert b'No items found' in response.content


def test_sort_restoration_preserves_notifications():
    node = shutil.which('node')
    assert node, 'Node is required for browser-script regression tests'
    result = subprocess.run([node, str(Path(__file__).with_name('sort_notification_behavior.cjs'))],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.django_db
def test_add_confirmation_has_notification_marker(authenticated_client):
    response = authenticated_client.post(reverse('inventory:add'), {
        'name': 'Test notification', 'part_id': '3003', 'color': 'Yellow',
        'quantity_total': 1, 'quantity_used': 0, 'storage_location': '1562',
    }, follow=True)
    assert response.status_code == 200
    assert b'data-flash-message' in response.content
    assert b'Item added.' in response.content
