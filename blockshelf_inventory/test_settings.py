"""Isolated tests: never read production environment or connect to its services."""
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SECRET_KEY = 'isolated-tests-only'
DEBUG = False
ALLOWED_HOSTS = ['testserver', 'localhost']
INSTALLED_APPS = [
    'django.contrib.admin', 'django.contrib.auth', 'django.contrib.contenttypes',
    'django.contrib.sessions', 'django.contrib.messages', 'django.contrib.staticfiles',
    'django.contrib.sites', 'allauth', 'allauth.account', 'allauth.socialaccount',
    'allauth.socialaccount.providers.google', 'auditlog', 'inventory',
]
MIDDLEWARE = [
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware', 'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'allauth.account.middleware.AccountMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
]
ROOT_URLCONF = 'blockshelf_inventory.urls'
TEMPLATES = [{
    'BACKEND': 'django.template.backends.django.DjangoTemplates',
    'DIRS': [BASE_DIR / 'templates'], 'APP_DIRS': True,
    'OPTIONS': {'context_processors': [
        'django.template.context_processors.request',
        'django.contrib.auth.context_processors.auth',
        'django.contrib.messages.context_processors.messages',
        'inventory.context_processors.app_settings',
    ]},
}]
DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': ':memory:'}}
CACHES = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}
STORAGES = {'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
            'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}}
STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
SITE_ID = 1
USE_TZ = True
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
AUTHENTICATION_BACKENDS = ['django.contrib.auth.backends.ModelBackend', 'allauth.account.auth_backends.AuthenticationBackend']
PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']
ACCOUNT_LOGIN_METHODS = {'username'}
ACCOUNT_SIGNUP_FIELDS = ['username*', 'password1*', 'password2*']
ACCOUNT_EMAIL_VERIFICATION = 'none'
ACCOUNT_ADAPTER = 'inventory.adapters.AccountAdapter'
SOCIALACCOUNT_PROVIDERS = {'google': {'APP': {'client_id': 'test', 'secret': 'test', 'key': ''}}}
EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'
LOGIN_URL = 'login'
LOGIN_REDIRECT_URL = 'inventory:dashboard'
LOGOUT_REDIRECT_URL = 'login'
TURNSTILE_ENABLED = False
ENABLE_RATE_LIMITING = False
ENABLE_ACCOUNT_LOCKOUT = False
REBRICKABLE_API_KEY = 'test-key'
TRUSTED_PROXY_IPS = []

# Confirm a replacement address before retiring the previous recovery address.
ACCOUNT_CHANGE_EMAIL = True
