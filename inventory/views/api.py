"""
API endpoints for inventory management.

This module contains API views for:
- Part lookup with rate limiting and caching
- Rebrickable bootstrap data import (admin-only)
- Bulk update operations for missing part names/images

All endpoints return JSON responses and include comprehensive error handling.
"""

import logging
import gzip
import os
import shutil
import time
import traceback
import zipfile
from typing import Any

import requests
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.db import transaction
from django.db.models import Q
from django.http import HttpResponseForbidden, JsonResponse
from django.utils.crypto import get_random_string
from django.views.decorators.http import require_GET, require_POST

from ..constants import (
    BULK_UPDATE_BATCH_SIZE_DEFAULT,
    BULK_UPDATE_BATCH_SIZE_MAX,
    BULK_UPDATE_BATCH_SIZE_MIN,
    EXTERNAL_API_TIMEOUT,
    LOG_MESSAGE_MAX_LENGTH,
    LOOKUP_CACHE_TTL,
    LOOKUP_LIMIT_PER_IP,
    LOOKUP_LIMIT_PER_MIN,
    LOOKUP_MISS_CACHE_TTL,
    LOOKUP_WINDOW_SECONDS,
    MAX_UPLOAD_SIZE,
    REBRICKABLE_BATCH_SIZE_DEFAULT,
    REBRICKABLE_BATCH_SIZE_MAX,
    REBRICKABLE_BATCH_SIZE_MIN,
    REBRICKABLE_JOB_CACHE_TTL,
    REBRICKABLE_MAX_ROWS,
)
from ..models import InventoryItem, RBColor, RBElement, RBPart
from ..decorators.rate_limit import consume_rate_limit
from ..utils import get_effective_config, sanitize_text, sanitize_url
from .helpers import (
    index_csv_rows,
    copy_bounded,
    create_temp_dir,
    digits_if_suffix,
    extract_element_id_from_url,
    extract_rebrickable_csvs,
    fetch_bricklink_name,
    fetch_rebrickable_part,
    get_client_ip,
    is_element_id,
    json_err,
    json_ok,
    read_csv_rows,
    single_part_token,
    to_int,
    to_str,
)

logger = logging.getLogger(__name__)


# -------------------------------------------------------------------------------------------------
# Rate Limiting Helper
# -------------------------------------------------------------------------------------------------

def _rate_limit_ok(user_id: int, request) -> bool:
    """
    Rate limiting with both user-based and IP-based checks.
    Returns True if request is allowed, False if rate limit exceeded.

    Args:
        user_id: The ID of the user making the request
        request: The Django request object (used to extract IP address)

    Returns:
        True if request is allowed, False if rate limit exceeded
    """
    bucket = int(time.time() // LOOKUP_WINDOW_SECONDS)

    # Check user-based rate limit
    user_key = f"rl:lookup:user:{user_id}:{bucket}"
    if not consume_rate_limit(user_key, LOOKUP_LIMIT_PER_MIN, LOOKUP_WINDOW_SECONDS):
        return False
    if request:
        ip = get_client_ip(request)
        ip_key = f"rl:lookup:ip:{ip}:{bucket}"
        if not consume_rate_limit(ip_key, LOOKUP_LIMIT_PER_IP, LOOKUP_WINDOW_SECONDS):
            return False

    return True


# -------------------------------------------------------------------------------------------------
# API Endpoints
# -------------------------------------------------------------------------------------------------

@login_required
def lookup_part(request) -> JsonResponse:
    """
    Lookup part information by part number or element ID.

    Lookup order:
    1. Check cache for previously looked-up data
    2. Check local database (RBPart/RBElement tables)
    3. If data incomplete and rate limit allows, fetch from Rebrickable API
    4. If name still missing, try BrickLink scraping
    5. Cache result and return

    Query Parameters:
        part_id (str): The part number or element ID to lookup

    Returns:
        JsonResponse with structure:
        - For element ID: {ok, found, resolved: 'element', name, part_id, color, image_url, element_id}
        - For part number: {ok, found, resolved: 'part', name, part_id, image_url, element_id}
        - For not found: {ok: True, found: False}
        - For errors: {ok: False, error: str}

    Rate Limiting:
        - Per user: 60 requests per minute
        - Per IP: 120 requests per minute

    Caching:
        - Successful lookups: 24 hours
        - Failed lookups: 5 minutes
    """
    deadline = time.monotonic() + 15
    def allow_request():
        return time.monotonic() < deadline and _rate_limit_ok(request.user.id, request)

    raw = (request.GET.get('part_id') or '').strip()
    if not raw:
        return json_err('Missing part_id', 400)

    # Get user's API key from UserPreference, fallback to settings
    api_key = ""
    if hasattr(request.user, 'userpreference'):
        api_key = (request.user.userpreference.rebrickable_api_key or "").strip()
        if api_key:
            logger.info(f"Using user API key for {request.user.username} (length: {len(api_key)})")
    if not api_key:
        api_key = getattr(settings, "REBRICKABLE_API_KEY", "")
        if api_key:
            logger.info(f"Using settings.py API key for {request.user.username} (length: {len(api_key)})")
        else:
            logger.warning(f"No API key available for {request.user.username} - lookups will be limited")

    is_element = is_element_id(raw)
    cache_key = f"lookup:{'element' if is_element else 'part'}:{raw}"
    cached = cache.get(cache_key)
    if cached:
        return JsonResponse(cached)

    # ELEMENT PATH
    if is_element:
        el = RBElement.objects.select_related('part', 'color').filter(element_id=str(raw)).first()
        if el:
            rbpart = el.part
            img = (rbpart.image_url or '').strip()

            # Backfill part image once if missing
            if not img and api_key and allow_request():
                try:
                    r = requests.get(
                        f"https://rebrickable.com/api/v3/lego/parts/{rbpart.part_num}/",
                        headers={'Authorization': f'key {api_key}'},
                        timeout=EXTERNAL_API_TIMEOUT
                    )
                    if r.ok:
                        img = sanitize_url((r.json().get('part_img_url') or '').strip())
                        if img:
                            RBPart.objects.filter(part_num=rbpart.part_num).update(image_url=img)
                except requests.RequestException:
                    pass

            data = {
                'ok': True, 'found': True, 'resolved': 'element',
                'name': rbpart.name,
                'part_id': rbpart.part_num,
                'color': el.color.name,
                'image_url': img,
                'element_id': str(raw),
            }
            cache.set(cache_key, data, LOOKUP_CACHE_TTL)
            return JsonResponse(data)

        # External API fallback: /lego/elements/{id}/
        if not allow_request():
            return json_err('Rate limit exceeded. Please try again later.', 429)

        try:
            r = requests.get(
                f"https://rebrickable.com/api/v3/lego/elements/{raw}/",
                headers={'Authorization': f'key {api_key}'},
                timeout=EXTERNAL_API_TIMEOUT
            )
            if r.status_code == 404:
                miss = {'ok': True, 'found': False}
                cache.set(cache_key, miss, LOOKUP_MISS_CACHE_TTL)
                return JsonResponse(miss)
            r.raise_for_status()
            j = r.json()

            part = j.get('part') or {}
            color = j.get('color') or {}
            part_num = (part.get('part_num') or '').strip()
            name = sanitize_text((part.get('name') or '').strip())
            part_img = sanitize_url((part.get('part_img_url') or '').strip())
            color_id = color.get('id')
            color_name = sanitize_text((color.get('name') or '').strip())

            rbpart = None
            if part_num:
                rbpart, _ = RBPart.objects.update_or_create(
                    part_num=part_num,
                    defaults={'name': name, 'image_url': part_img}
                )
            rbcolor = None
            if color_id is not None:
                try:
                    cid = int(color_id)
                    rbcolor, _ = RBColor.objects.update_or_create(
                        id=cid, defaults={'name': color_name}
                    )
                except Exception:
                    rbcolor = None

            if rbpart and rbcolor:
                RBElement.objects.update_or_create(
                    element_id=str(raw), defaults={'part': rbpart, 'color': rbcolor}
                )

            data = {
                'ok': True, 'found': bool(part_num), 'resolved': 'element',
                'name': name,
                'part_id': part_num or str(raw),
                'color': color_name,
                'image_url': part_img,
                'element_id': str(raw),
            }
            cache.set(cache_key, data, LOOKUP_CACHE_TTL)
            return JsonResponse(data)

        except requests.RequestException as e:
            logger.exception(f"Element lookup failed for {raw}")
            return json_err(str(e), 502)

    # PART PATH (single token; trailing-letter fallback + BrickLink name fallback)
    token = single_part_token(raw)
    if not token:
        miss = {'ok': True, 'found': False}
        cache.set(cache_key, miss, LOOKUP_MISS_CACHE_TTL)
        return JsonResponse(miss)

    name = ''
    img = ''
    pn = token

    rb = RBPart.objects.filter(part_num=token).first()
    if rb:
        pn = rb.part_num
        name = (rb.name or '')
        img = (rb.image_url or '')

    if not rb:
        digits = digits_if_suffix(token)
        if digits != token:
            rb2 = RBPart.objects.filter(part_num=digits).first()
            if rb2:
                pn = rb2.part_num
                name = name or (rb2.name or '')
                img = img or (rb2.image_url or '')

    if (not name or not img) and api_key:
        try:
            res, _attempts = fetch_rebrickable_part(token, api_key, allow_request)
            if res:
                pn = res.get('part_num') or pn
                name = name or sanitize_text((res.get('name') or ''))
                img = img or sanitize_url((res.get('part_img_url') or ''))
                RBPart.objects.update_or_create(
                    part_num=pn, defaults={'name': name, 'image_url': img}
                )
        except requests.RequestException:
            logger.exception(f"Rebrickable API failed for part {token}")

    if not name:
        bl_name = fetch_bricklink_name(token, allow_request)
        if not bl_name:
            digits = digits_if_suffix(token)
            if digits != token:
                bl_name = fetch_bricklink_name(digits, allow_request)
        if bl_name:
            name = bl_name

    if not (name or img):
        miss = {'ok': True, 'found': False}
        cache.set(cache_key, miss, LOOKUP_MISS_CACHE_TTL)
        return JsonResponse(miss)

    data = {
        'ok': True, 'found': True, 'resolved': 'part',
        'name': name,
        'part_id': pn,
        'image_url': img,
        'element_id': extract_element_id_from_url(img),
    }
    cache.set(cache_key, data, LOOKUP_CACHE_TTL)
    return JsonResponse(data)


@login_required
@require_POST
def reb_bootstrap_prepare(request) -> JsonResponse:
    """
    Prepare Rebrickable bootstrap import (admin-only).

    Accepts a ZIP, CSV, or CSV.GZ file containing Rebrickable data,
    extracts/validates it, counts rows, and stores metadata in cache.

    POST Parameters:
        dataset_file (file): The uploaded file (ZIP/CSV/CSV.GZ)

    Returns:
        JsonResponse with structure:
        - Success: {ok: True, job: str, totals: {colors: int, parts: int, elements: int}}
        - Error: {ok: False, error: str}

    Security:
        - Admin-only endpoint
        - File size validation (max 50MB)
        - File type validation (ZIP/CSV/GZ only)
        - Row count validation (max 1M rows per file)

    Cache:
        Job metadata stored for 1 hour with key "rebjob:{job_id}"
    """
    if not request.user.is_staff:
        return HttpResponseForbidden("Admins only.")

    temp_root = None
    prepared = False
    try:
        upload = request.FILES.get("dataset_file")
        if not upload:
            return json_err("No file uploaded.", 400)

        # Validate file size (security)
        max_size = getattr(settings, 'MAX_UPLOAD_SIZE', MAX_UPLOAD_SIZE)
        if upload.size > max_size:
            return json_err(f"File too large. Maximum size is {max_size // (1024*1024)}MB.", 400)

        # Validate file type
        allowed_extensions = ['.zip', '.csv', '.csv.gz', '.gz']
        if not any(upload.name.lower().endswith(ext) for ext in allowed_extensions):
            return json_err("Invalid file type. Only ZIP, CSV, or CSV.GZ files allowed.", 400)

        temp_root = create_temp_dir()
        src_path = os.path.join(temp_root, os.path.basename(upload.name))
        with open(src_path, "wb") as dst:
            for chunk in upload.chunks():
                dst.write(chunk)

        files: dict[str, str] = {}
        if zipfile.is_zipfile(src_path):
            files = extract_rebrickable_csvs(src_path, temp_root)
            try:
                os.remove(src_path)
            except OSError:
                pass
        else:
            lname = os.path.basename(src_path).lower()
            if lname.endswith((".csv", ".csv.gz")):
                if "color" in lname and "part" not in lname and "element" not in lname:
                    files["colors"] = src_path
                elif "element" in lname:
                    files["elements"] = src_path
                else:
                    files["parts"] = src_path
            else:
                shutil.rmtree(temp_root, ignore_errors=True)
                return json_err("Unsupported file type.", 400)

        for kind, path in list(files.items()):
            if path.lower().endswith('.gz'):
                expanded = path[:-3]
                with gzip.open(path, 'rb') as source, open(expanded, 'wb') as destination:
                    copy_bounded(source, destination)
                os.remove(path)
                files[kind] = expanded

        totals: dict[str, int] = {}
        indexes = {}
        max_rows = getattr(settings, 'REBRICKABLE_MAX_ROWS', REBRICKABLE_MAX_ROWS)
        for kind, path in files.items():
            indexes[kind] = index_csv_rows(path, max_rows)
            totals[kind] = indexes[kind]['count']

        job_id = get_random_string(12)
        cache.set(
            f"rebjob:{job_id}",
            {
                "root": temp_root,
                "files": files,
                "totals": totals,
                "indexes": indexes,
            },
            REBRICKABLE_JOB_CACHE_TTL,
        )
        prepared = True
        return json_ok(job=job_id, totals=totals)
    except (ValueError, OSError, zipfile.BadZipFile) as exc:
        return json_err(str(exc), 400)
    except Exception:
        logger.exception("reb_bootstrap_prepare failed")
        return json_err("Unable to prepare import", 500)
    finally:
        if temp_root and not prepared:
            shutil.rmtree(temp_root, ignore_errors=True)


@login_required
@require_POST
def reb_bootstrap_run(request) -> JsonResponse:
    """
    Execute a batch import for Rebrickable bootstrap data (admin-only).

    Processes a batch of rows from a previously prepared import job,
    upserting data into RBColor, RBPart, or RBElement tables.

    POST Parameters:
        job (str): Job ID from reb_bootstrap_prepare
        kind (str): Type of data to import ('colors', 'parts', or 'elements')
        offset (int): Row offset to start processing from (default: 0)
        batch_size (int): Number of rows to process (default: 2000, max: 10000)

    Returns:
        JsonResponse with structure:
        {
            ok: True,
            done: bool,           # True if all rows processed
            kind: str,            # Type of data imported
            offset: int,          # New offset for next batch
            total: int,           # Total rows in file
            processed: int,       # Rows processed in this batch
            created: int,         # New records created
            updated: int,         # Existing records updated
            skipped: int,         # Invalid rows skipped
            messages: [str]       # Log messages (max 200)
        }

    Security:
        - Admin-only endpoint
        - Batch size limited to prevent memory issues
        - Foreign key safety (creates placeholders if needed)
        - Atomic transactions per batch
    """
    if not request.user.is_staff:
        return HttpResponseForbidden("Admins only.")

    try:
        job = (request.POST.get("job") or "").strip()
        kind = (request.POST.get("kind") or "").strip().lower()
        offset = max(0, to_int(request.POST.get("offset", "0"), 0))
        batch_size = to_int(
            request.POST.get("batch_size", str(REBRICKABLE_BATCH_SIZE_DEFAULT)),
            REBRICKABLE_BATCH_SIZE_DEFAULT
        )
        batch_size = max(REBRICKABLE_BATCH_SIZE_MIN, min(REBRICKABLE_BATCH_SIZE_MAX, batch_size))

        jobkey = f"rebjob:{job}"
        meta = cache.get(jobkey)
        if not meta:
            return json_err("Job not found or expired.", 400)

        files = meta.get("files", {})
        totals = meta.get("totals", {})
        path = files.get(kind)
        if not path:
            return json_err(f"No file for kind '{kind}'.", 400)

        total = int(totals.get(kind, 0))

        processed = created = updated = skipped = 0
        messages_log: list[str] = []

        # Upsert helpers (FK-safe)
        def upsert_color(row: dict[str, Any]) -> None:
            """Upsert a color record from CSV row."""
            nonlocal created, updated, skipped
            try:
                cid = int((row.get("id") or row.get("color_id") or "").strip())
            except (ValueError, TypeError):
                skipped += 1
                return
            name = (row.get("name") or "").strip()
            obj, was_created = RBColor.objects.update_or_create(
                id=cid, defaults={"name": name or ""}
            )
            if was_created:
                created += 1
            else:
                updated += 1

        def upsert_part(row: dict[str, Any]) -> None:
            """Upsert a part record from CSV row."""
            nonlocal created, updated, skipped
            part_num = (row.get("part_num") or "").strip()
            if not part_num:
                skipped += 1
                return
            name = (row.get("name") or "").strip()
            obj, was_created = RBPart.objects.update_or_create(
                part_num=part_num,
                defaults={"name": name or ""},
            )
            if was_created:
                created += 1
            else:
                updated += 1

        def upsert_element(row: dict[str, Any]) -> None:
            """Upsert an element record from CSV row."""
            nonlocal created, updated, skipped
            # Accept common header variants from Rebrickable CSVs
            element_id = (row.get("element_id") or row.get("id") or "").strip()
            part_num   = (row.get("part_num") or row.get("part") or row.get("part_id") or "").strip()
            color_val  = row.get("color_id") or row.get("color") or row.get("colorid")

            if not (element_id and part_num and color_val not in (None, "")):
                skipped += 1
                return
            try:
                cid = int(color_val)
            except (ValueError, TypeError):
                skipped += 1
                return

            # Ensure RBPart and RBColor exist (create placeholders if missing)
            part, _ = RBPart.objects.get_or_create(part_num=part_num, defaults={"name": ""})
            color, _ = RBColor.objects.get_or_create(id=cid, defaults={"name": ""})

            obj, was_created = RBElement.objects.update_or_create(
                element_id=element_id, defaults={"part": part, "color": color}
            )
            if was_created:
                created += 1
            else:
                updated += 1

        # Process a slice in a transaction
        try:
            with transaction.atomic():
                for row in read_csv_rows(path, offset, batch_size, meta['indexes'][kind]):
                    if kind == "colors":
                        upsert_color(row)
                    elif kind == "parts":
                        upsert_part(row)
                    elif kind == "elements":
                        upsert_element(row)
                    else:
                        skipped += 1
                    processed += 1
        except Exception as e:
            logger.exception(f"Batch parse/save error for {kind} at offset {offset}")
            return JsonResponse({'ok': False, 'error': 'Batch failed; no rows in this batch were saved.', 'offset': offset}, status=400)

        new_offset = offset + processed
        done = new_offset >= total
        if done:
            completed = set(meta.get('completed', [])) | {kind}
            meta['completed'] = list(completed)
            if all(k in completed or not count for k, count in totals.items()):
                shutil.rmtree(meta['root'], ignore_errors=True)
                cache.delete(jobkey)
            else:
                cache.set(jobkey, meta, REBRICKABLE_JOB_CACHE_TTL)
                os.utime(meta['root'], None)
        else:
            cache.set(jobkey, meta, REBRICKABLE_JOB_CACHE_TTL)
            os.utime(meta['root'], None)

        return json_ok(
            done=done,
            kind=kind,
            offset=new_offset,
            total=total,
            processed=processed,
            created=created,
            updated=updated,
            skipped=skipped,
            messages=messages_log[:LOG_MESSAGE_MAX_LENGTH],
        )
    except Exception:
        logger.exception("reb_bootstrap_run failed")
        return json_err(traceback.format_exc(), 500)


@login_required
@require_GET
def bulk_update_missing_preview(request) -> JsonResponse:
    """
    Preview bulk update for items with missing names or images.

    Counts how many items will be updated and how many will be skipped
    due to invalid/multi-part IDs.

    Returns:
        JsonResponse with structure:
        {
            count: int,              # Number of eligible items to update
            skipped_multi: int,      # Number skipped (multi-part IDs)
            skipped_examples: [      # Up to 6 examples of skipped items
                {id: int, part_id: str}
            ]
        }

    Eligibility:
        - Item has name='Unknown' OR missing/empty image_url
        - Item has a non-empty part_id
        - part_id is a single alphanumeric token (no spaces/commas)
    """
    try:
        base = (InventoryItem.objects
                .filter(user=request.user)
                .filter(Q(name__iexact="unknown") | Q(image_url__isnull=True) | Q(image_url__exact=""))
                .exclude(part_id__isnull=True).exclude(part_id__exact=""))

        eligible = 0
        skipped_multi = 0
        examples: list[dict[str, Any]] = []

        for it in base.only("id", "part_id"):
            token = single_part_token(it.part_id)
            if token:
                eligible += 1
            else:
                skipped_multi += 1
                if len(examples) < 6:
                    examples.append({"id": it.id, "part_id": to_str(it.part_id)})

        return JsonResponse({
            "count": eligible,
            "skipped_multi": skipped_multi,
            "skipped_examples": examples,
        })
    except Exception as e:
        logger.exception("bulk_update_missing_preview failed")
        return JsonResponse({"error": str(e)}, status=500)


@login_required
@require_POST
def bulk_update_missing_batch(request) -> JsonResponse:
    """
    Execute a batch update for items with missing names or images.

    Processes a batch of inventory items, looking up missing data from:
    1. Local database (RBPart table)
    2. Rebrickable API (if rate limit allows)
    3. BrickLink scraping (for names only, if API fails)

    POST Parameters:
        after_id (int): Process items with ID > this value (default: 0)
        batch_size (int): Number of items to process (default: 50, max: 500)

    Returns:
        JsonResponse with structure:
        {
            ok: True,
            done: bool,              # True if no more items to process
            last_id: int,            # Last processed item ID (use for next batch)
            processed: int,          # Number of eligible items processed
            updated_names: int,      # Items with name updated
            updated_images: int,     # Items with image updated
            skipped: int,            # Items skipped (no data found/invalid)
            api_calls: int,          # Number of Rebrickable API calls made
            messages: [str]          # Log messages (max 200)
        }

    Rate Limiting:
        Subject to standard lookup rate limits (60/min per user, 120/min per IP)

    Updates:
        - If name is 'Unknown', tries to fetch real name
        - If image_url is empty, tries to fetch image URL
        - All changes are saved to database
        - Results are logged in messages array
    """
    try:
        after_id = to_int(request.POST.get("after_id", "0"), 0)
        batch_size = to_int(
            request.POST.get("batch_size", str(BULK_UPDATE_BATCH_SIZE_DEFAULT)),
            BULK_UPDATE_BATCH_SIZE_DEFAULT
        )
        batch_size = max(BULK_UPDATE_BATCH_SIZE_MIN, min(BULK_UPDATE_BATCH_SIZE_MAX, batch_size))

        qs = (InventoryItem.objects
              .filter(user=request.user)
              .filter(Q(name__iexact="unknown") | Q(image_url__isnull=True) | Q(image_url__exact=""))
              .exclude(part_id__isnull=True).exclude(part_id__exact="")
              .filter(id__gt=after_id)
              .order_by("id")[:batch_size])

        items = list(qs)
        if not items:
            return JsonResponse({
                "done": True, "last_id": after_id, "processed": 0,
                "updated_names": 0, "updated_images": 0, "skipped": 0, "api_calls": 0,
                "messages": [],
            })

        # Get user's API key from UserPreference, fallback to settings
        api_key = ""
        if hasattr(request.user, 'userpreference'):
            api_key = (request.user.userpreference.rebrickable_api_key or "").strip()
        if not api_key:
            api_key = getattr(settings, "REBRICKABLE_API_KEY", "")

        updated_names = 0
        updated_images = 0
        skipped = 0
        api_calls = 0
        last_id = after_id
        messages_log: list[str] = []

        processed_eligible = 0
        deadline = time.monotonic() + 15
        retry_after = 0
        def allow_request():
            nonlocal retry_after
            if time.monotonic() >= deadline:
                retry_after = 1
                return False
            if not _rate_limit_ok(request.user.id, request):
                retry_after = LOOKUP_WINDOW_SECONDS - int(time.time()) % LOOKUP_WINDOW_SECONDS + 1
                return False
            return True

        for item in items:
            if time.monotonic() >= deadline:
                break
            previous_id = last_id
            last_id = item.id

            need_name = (item.name or "").strip().lower() == "unknown"
            need_img  = not (item.image_url or "").strip()
            if not (need_name or need_img):
                skipped += 1
                continue

            token = single_part_token(item.part_id)
            if not token:
                skipped += 1
                messages_log.append(f"Skipped (multiple IDs): #{item.id} '{to_str(item.part_id)}'")
                continue

            processed_eligible += 1

            part_name = ""
            part_img  = ""

            rb = RBPart.objects.filter(part_num=token).first()
            if rb:
                part_name = (rb.name or "").strip()
                part_img  = (rb.image_url or "").strip()
            if not rb:
                digits = digits_if_suffix(token)
                if digits != token:
                    rb2 = RBPart.objects.filter(part_num=digits).first()
                    if rb2:
                        part_name = part_name or (rb2.name or "").strip()
                        part_img  = part_img  or (rb2.image_url or "").strip()

            if ((need_name and not part_name) or (need_img and not part_img)) and api_key:
                try:
                    res, attempts = fetch_rebrickable_part(token, api_key, allow_request)
                    api_calls += attempts
                    if res:
                        pn = res.get("part_num") or token
                        nm = sanitize_text((res.get("name") or "").strip())
                        img = sanitize_url((res.get("part_img_url") or "").strip())
                        part_name = part_name or nm
                        part_img  = part_img  or img
                        RBPart.objects.update_or_create(
                            part_num=pn, defaults={"name": nm, "image_url": img}
                        )
                except requests.RequestException:
                    logger.exception(f"API call failed for part {token}")

            used_bricklink = False
            if need_name and not part_name:
                bl_name = fetch_bricklink_name(token, allow_request)
                if not bl_name:
                    digits = digits_if_suffix(token)
                    if digits != token:
                        bl_name = fetch_bricklink_name(digits, allow_request)
                if bl_name:
                    part_name = bl_name
                    used_bricklink = True
                    messages_log.append(
                        f"BrickLink fallback: #{item.id} part '{token}' → name='{bl_name}'. No image/buy available."
                    )
                elif not retry_after:
                    messages_log.append(
                        f"Not found on Rebrickable and BrickLink: #{item.id} part '{token}'."
                    )

            if retry_after:
                last_id = previous_id
                processed_eligible -= 1
                messages_log.append("Lookup paused; this item will be retried.")
                break

            changed = False
            if need_name and part_name:
                item.name = part_name
                changed = True
                if used_bricklink:
                    messages_log.append(f"Updated name via BrickLink: #{item.id} '{token}' → '{part_name}'")
                else:
                    messages_log.append(f"Updated name: #{item.id} '{token}' → '{part_name}'")

            if need_img and part_img:
                item.image_url = part_img
                changed = True
                messages_log.append(f"Updated image from Rebrickable: #{item.id} '{token}'")

            if changed:
                # Lock only while saving, after network I/O; preserve validation and audit signals.
                with transaction.atomic():
                    current = InventoryItem.objects.select_for_update().filter(pk=item.pk).first()
                    fields = []
                    if current is not None and current.part_id == item.part_id:
                        if need_name and part_name and current.name.strip().lower() == 'unknown':
                            current.name = part_name
                            fields.append('name')
                        if need_img and part_img and not current.image_url.strip():
                            current.image_url = part_img
                            fields.append('image_url')
                        if fields:
                            current.save(update_fields=fields + ['updated_at'])
                            updated_names += int('name' in fields)
                            updated_images += int('image_url' in fields)
            else:
                skipped += 1

        return JsonResponse({
            "done": False,
            "retry_after": retry_after,
            "last_id": last_id,
            "processed": processed_eligible,
            "updated_names": updated_names,
            "updated_images": updated_images,
            "skipped": skipped,
            "api_calls": api_calls,
            "messages": messages_log[:LOG_MESSAGE_MAX_LENGTH],
        })
    except Exception:
        logger.exception("bulk_update_missing_batch failed")
        return JsonResponse({"error": "Internal server error"}, status=500)
