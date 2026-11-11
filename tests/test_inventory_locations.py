import pytest
from django.urls import reverse
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from inventory.models import InventoryItem

pytestmark = pytest.mark.django_db


def payload(location):
    return dict(name='Yellow brick', part_id='3003', color='Yellow',
                quantity_total=10, quantity_used=0, storage_location=location)


@pytest.mark.parametrize('account', ['user', 'admin_user'])
def test_separate_locations_save_and_search(client, request, account):
    user = request.getfixturevalue(account)
    client.force_login(user)
    for location in ['1562', '1563']:
        response = client.post(reverse('inventory:add'), payload(location), follow=True)
        assert b'Item added.' in response.content
        response = client.get(reverse('inventory:list'), {'q': location})
        assert [i.storage_location for i in response.context['page_obj']] == [location]
    assert InventoryItem.objects.filter(user=user).count() == 2


@pytest.mark.parametrize('location', ['1562', ''])
def test_duplicate_preserves_form_and_data(authenticated_client, user, location):
    existing = InventoryItem.objects.create(user=user, **payload(location))
    response = authenticated_client.post(reverse('inventory:add'), payload(location))
    assert response.status_code == 200
    assert b'already exist at this location' in response.content
    assert response.context['form'].is_bound
    assert response.context['form']['storage_location'].value() == location
    assert InventoryItem.objects.filter(user=user).count() == 1
    existing.refresh_from_db()
    assert existing.quantity_total == 10


def test_edit_collision_and_valid_update(authenticated_client, user):
    first = InventoryItem.objects.create(user=user, **payload('1562'))
    second = InventoryItem.objects.create(user=user, **payload('1563'))
    url = reverse('inventory:edit', args=[second.pk])
    response = authenticated_client.post(url, dict(payload('1562'), version=second.updated_at.isoformat()))
    assert b'already exist at this location' in response.content
    second.refresh_from_db()
    assert second.storage_location == '1563'
    response = authenticated_client.post(url, dict(payload('1563'), quantity_total=20, version=second.updated_at.isoformat()), follow=True)
    assert b'Item updated.' in response.content
    first.refresh_from_db(); second.refresh_from_db()
    assert (first.quantity_total, second.quantity_total) == (10, 20)


def test_import_keeps_locations_separate(authenticated_client, user):
    first = InventoryItem.objects.create(user=user, **payload('1562'))
    csv = b'name,part_id,color,quantity_total,storage_location\nYellow brick,3003,Yellow,20,1563\nYellow brick,3003,Yellow,30,1563\n'
    response = authenticated_client.post(reverse('inventory:import_csv'),
        {'file': SimpleUploadedFile('items.csv', csv, content_type='text/csv')}, follow=True)
    assert b'1 added, 1 updated' in response.content
    first.refresh_from_db()
    assert first.quantity_total == 10
    assert InventoryItem.objects.get(user=user, storage_location='1563').quantity_total == 30
    # An unspecified location is its own entry, never an arbitrary match.
    from inventory.views.inventory import process_import_rows
    process_import_rows(user, [dict(payload(''), quantity_total=5)])
    assert InventoryItem.objects.filter(user=user).count() == 3


def test_database_rejects_exact_duplicate(user):
    InventoryItem.objects.create(user=user, **payload('1562'))
    with pytest.raises(IntegrityError), transaction.atomic():
        InventoryItem.objects.bulk_create([InventoryItem(user=user, **payload('1562'))])


def test_other_inventory_remains_protected(authenticated_client, other_user):
    other = InventoryItem.objects.create(user=other_user, **payload('1562'))
    response = authenticated_client.post(reverse('inventory:edit', args=[other.pk]), payload('1563'))
    assert response.status_code == 403
    other.refresh_from_db()
    assert other.storage_location == '1562'
