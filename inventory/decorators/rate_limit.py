"""
Rate limiting decorator for views.
Implements configurable rate limiting using Django cache backend.
"""

from ipaddress import ip_address
from datetime import timedelta
from django.db.models import F
from django.db import OperationalError
from django.utils import timezone
from ..models import RateLimitBucket
import logging
import time
from functools import wraps
from typing import Callable, Optional

from django.conf import settings
from django.core.cache import cache
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render

logger = logging.getLogger(__name__)


def get_client_ip(request: HttpRequest) -> str:
    """
    Get the client's IP address from request, respecting proxy headers.
    Handles X-Forwarded-For and X-Real-IP headers.
    """
    remote = request.META.get('REMOTE_ADDR', '')
    candidate = remote
    if remote in getattr(settings, 'TRUSTED_PROXY_IPS', []):
        candidate = request.META.get('HTTP_X_REAL_IP', remote)
    try:
        return str(ip_address(candidate))
    except ValueError:
        return '0.0.0.0'



def counter_rows(key, window):
    now = timezone.now()
    expiry = now + timedelta(seconds=window)
    RateLimitBucket.objects.get_or_create(key=key, defaults={'expires_at': expiry})
    rows = RateLimitBucket.objects.filter(key=key)
    rows.filter(expires_at__lte=now).update(count=0, expires_at=expiry)
    return rows


def consume_rate_limit(key, limit, window):
    # Conditional UPDATE reserves one attempt without a read/modify/write race.
    try:
        return bool(counter_rows(key, window).filter(count__lt=limit).update(count=F('count') + 1))
    except OperationalError:
        logger.exception('Rate limit storage unavailable')
        return False


def rate_limit(
    attempts: int = 5,
    window: int = 900,  # 15 minutes in seconds
    block_duration: Optional[int] = None,
    key_prefix: str = 'rate_limit',
    progressive_delays: bool = True,
) -> Callable:
    """
    Rate limiting decorator for views.

    Args:
        attempts: Maximum number of attempts allowed within the time window
        window: Time window in seconds
        block_duration: How long to block after exceeding limit (defaults to window)
        key_prefix: Prefix for cache keys (to differentiate endpoints)
        progressive_delays: Enable progressive lockout durations

    Usage:
        @rate_limit(attempts=5, window=900, key_prefix='login')
        def login_view(request):
            ...
    """
    if block_duration is None:
        block_duration = window

    def decorator(view_func: Callable) -> Callable:
        @wraps(view_func)
        def wrapped_view(request: HttpRequest, *args, **kwargs) -> HttpResponse:
            # Check if rate limiting is enabled
            if request.method != 'POST' or not getattr(settings, 'ENABLE_RATE_LIMITING', True):
                return view_func(request, *args, **kwargs)

            # Get client IP
            client_ip = get_client_ip(request)

            # Create cache key
            cache_key = f"{key_prefix}:{client_ip}"
            attempts_key = f"{cache_key}:attempts"
            blocked_key = f"{cache_key}:blocked"
            violation_count_key = f"{cache_key}:violations"

            # Check if client is currently blocked
            blocked_until = cache.get(blocked_key)
            if blocked_until:
                time_remaining = int(blocked_until - time.time())
                if time_remaining > 0:
                    logger.warning(
                        f"Rate limit exceeded for {client_ip} on {key_prefix}. "
                        f"Blocked for {time_remaining} more seconds."
                    )

                    # Return appropriate error response
                    if request.headers.get('Accept') == 'application/json':
                        return JsonResponse(
                            {
                                'error': 'Too many requests',
                                'message': f'Rate limit exceeded. Please try again in {time_remaining} seconds.',
                                'retry_after': time_remaining
                            },
                            status=429
                        )

                    return render(
                        request,
                        'errors/rate_limit.html',
                        {
                            'time_remaining': time_remaining,
                            'retry_after': time_remaining
                        },
                        status=429
                    )
                else:
                    # Block expired, clear it
                    cache.delete(blocked_key)

            if not consume_rate_limit(attempts_key, attempts, window):
                # Calculate block duration (progressive if enabled)
                if progressive_delays:
                    try:
                        rows = counter_rows(violation_count_key, 86400)
                        rows.update(count=F('count') + 1)
                        violation_count = rows.values_list('count', flat=True).get()
                    except OperationalError:
                        violation_count = 1

                    # Progressive delays: 5min, 15min, 1hr, 24hr
                    delay_schedule = [300, 900, 3600, 86400]
                    actual_block_duration = delay_schedule[min(violation_count - 1, len(delay_schedule) - 1)]
                else:
                    actual_block_duration = block_duration

                blocked_until = time.time() + actual_block_duration
                cache.set(blocked_key, blocked_until, actual_block_duration)

                logger.warning(
                    f"Rate limit exceeded for {client_ip} on {key_prefix}. "
                    f"Blocked for {actual_block_duration} seconds. "
                    f"Attempt limit: {attempts}"
                )

                # Return error response
                if request.headers.get('Accept') == 'application/json':
                    return JsonResponse(
                        {
                            'error': 'Too many requests',
                            'message': f'Rate limit exceeded. Please try again in {actual_block_duration} seconds.',
                            'retry_after': actual_block_duration
                        },
                        status=429
                    )

                return render(
                    request,
                    'errors/rate_limit.html',
                    {
                        'time_remaining': actual_block_duration,
                        'retry_after': actual_block_duration
                    },
                    status=429
                )

            # Allow the request
            return view_func(request, *args, **kwargs)

        return wrapped_view
    return decorator


def reset_rate_limit(request: HttpRequest, key_prefix: str) -> None:
    """
    Reset rate limit for a specific client and endpoint.
    Useful after successful authentication.

    Args:
        request: The HTTP request object
        key_prefix: The rate limit key prefix to reset
    """
    client_ip = get_client_ip(request)
    cache_key = f"{key_prefix}:{client_ip}"

    # Clear all rate limit keys for this client/endpoint
    RateLimitBucket.objects.filter(key=f"{cache_key}:attempts").delete()
    cache.delete(f"{cache_key}:attempts")
    cache.delete(f"{cache_key}:blocked")

    logger.debug(f"Reset rate limit for {client_ip} on {key_prefix}")
