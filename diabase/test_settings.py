"""Smoke tests: the Django project boots and core wiring is sound."""

import pytest
from django.apps import apps
from django.urls import reverse


def test_installed_apps_include_core():
    assert apps.is_installed("core")


@pytest.mark.django_db
def test_admin_is_wired(client):
    response = client.get(reverse("admin:login"))
    assert response.status_code == 200
