from django.conf import settings

from .utils import get_effective_config
from .turnstile import turnstile_is_enabled


def app_settings(request):
    cfg = get_effective_config()
    return {
        "ALLOW_SELF_REG": bool(cfg.allow_registration),
        "TURNSTILE_ENABLED": turnstile_is_enabled(),
        "TURNSTILE_SITE_KEY": getattr(settings, "TURNSTILE_SITE_KEY", ""),
    }

