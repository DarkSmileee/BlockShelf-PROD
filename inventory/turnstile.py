"""
Cloudflare Turnstile helpers.

Provides a thin wrapper around the Turnstile verification endpoint so we can
gate sensitive forms (login, signup, etc.) without duplicating request logic.
"""

from __future__ import annotations

import logging
from typing import Sequence

import requests
from django.conf import settings

from .constants import EXTERNAL_API_TIMEOUT

logger = logging.getLogger(__name__)

VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


def turnstile_is_enabled() -> bool:
    """
    Return True when Turnstile enforcement is enabled and properly configured.
    """
    if not getattr(settings, "TURNSTILE_ENABLED", False):
        return False
    site_key = getattr(settings, "TURNSTILE_SITE_KEY", "")
    secret_key = getattr(settings, "TURNSTILE_SECRET_KEY", "")
    if not site_key or not secret_key:
        logger.debug("Turnstile enabled but keys are missing; treating as disabled.")
        return False
    return True


def verify_turnstile_token(
    response_token: str | None,
    remote_ip: str | None = None,
) -> tuple[bool, Sequence[str]]:
    """
    Validate a Turnstile token via Cloudflare's verification endpoint.

    Returns (success, error_codes).
    """
    if not turnstile_is_enabled():
        return True, ()

    token = (response_token or "").strip()
    if not token:
        return False, ("missing-input-response",)

    data = {
        "secret": settings.TURNSTILE_SECRET_KEY,
        "response": token,
    }
    if remote_ip:
        data["remoteip"] = remote_ip

    try:
        resp = requests.post(
            VERIFY_URL,
            data=data,
            timeout=EXTERNAL_API_TIMEOUT,
        )
        resp.raise_for_status()
        payload = resp.json()
    except requests.RequestException as exc:
        logger.warning("Turnstile verification request failed: %s", exc)
        return False, ("network-error",)

    if payload.get("success"):
        return True, ()

    errors = tuple(payload.get("error-codes", ()) or ("unknown-error",))
    logger.info("Turnstile verification failed: %s", errors)
    return False, errors

