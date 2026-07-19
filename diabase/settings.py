"""Django settings for Diabase.

Configuration comes from environment variables (the container is the unit
of deployment); every default here is a development-only fallback.
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# Development fallback only — the compose setup generates a real secret.
SECRET_KEY = os.environ.get("DIABASE_SECRET_KEY", "insecure-dev-only-key")

DEBUG = os.environ.get("DIABASE_DEBUG", "1") == "1"

ALLOWED_HOSTS = [h for h in os.environ.get("DIABASE_ALLOWED_HOSTS", "").split(",") if h]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "core",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "diabase.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "diabase.wsgi.application"

# SQLite for local hacking; the compose setup points this at the internal
# Postgres (which also hosts the audit trail and pgvector memory).
DATABASES = {
    "default": {
        "ENGINE": os.environ.get("DIABASE_DB_ENGINE", "django.db.backends.sqlite3"),
        "NAME": os.environ.get("DIABASE_DB_NAME", BASE_DIR / "db.sqlite3"),
        "USER": os.environ.get("DIABASE_DB_USER", ""),
        "PASSWORD": os.environ.get("DIABASE_DB_PASSWORD", ""),
        "HOST": os.environ.get("DIABASE_DB_HOST", ""),
        "PORT": os.environ.get("DIABASE_DB_PORT", ""),
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
LANGUAGES = [("en", "English"), ("it", "Italiano")]
LOCALE_PATHS = [BASE_DIR / "locale"]

TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
