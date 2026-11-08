"""Capture against a running Django admin: tokens on forms that were never
submitted stay out of the saved recording (#1910).

A real app (Django's admin, served by wsgiref from a subprocess) renders a
fresh masked ``csrfmiddlewaretoken`` on its login, add and logout forms. The
test signs in, saves a synthetic group, and lands on a changelist whose
forms' token is never sent. Every token the app rendered must be absent
from ``network.jsonl``; field names and ordinary content must survive.
Skipped when Django or Chromium is not installed.
"""

from __future__ import annotations

import os
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path

import pytest

from openadapt_flow import network_capture as nc

_SERVER = r"""
import sys
from wsgiref.simple_server import WSGIRequestHandler, make_server

import django
from django.conf import settings

settings.configure(
    DEBUG=False,
    ALLOWED_HOSTS=["127.0.0.1"],
    SECRET_KEY="synthetic-test-key-for-a-throwaway-app-0123456789",
    ROOT_URLCONF="__main__",
    INSTALLED_APPS=[
        "django.contrib.admin",
        "django.contrib.auth",
        "django.contrib.contenttypes",
        "django.contrib.sessions",
        "django.contrib.messages",
    ],
    MIDDLEWARE=[
        "django.contrib.sessions.middleware.SessionMiddleware",
        "django.middleware.common.CommonMiddleware",
        "django.middleware.csrf.CsrfViewMiddleware",
        "django.contrib.auth.middleware.AuthenticationMiddleware",
        "django.contrib.messages.middleware.MessageMiddleware",
    ],
    TEMPLATES=[{
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": [
            "django.template.context_processors.request",
            "django.contrib.auth.context_processors.auth",
            "django.contrib.messages.context_processors.messages",
        ]},
    }],
    DATABASES={"default": {
        "ENGINE": "django.db.backends.sqlite3", "NAME": sys.argv[1],
    }},
    USE_TZ=True,
    STATIC_URL="/static/",
)
django.setup()

from django.contrib import admin  # noqa: E402
from django.contrib.auth.models import User  # noqa: E402
from django.core.management import call_command  # noqa: E402
from django.core.wsgi import get_wsgi_application  # noqa: E402
from django.urls import path  # noqa: E402

urlpatterns = [path("admin/", admin.site.urls)]

call_command("migrate", verbosity=0)
User.objects.create_superuser("synthetic-admin", password=sys.stdin.readline().strip())


class Quiet(WSGIRequestHandler):
    def log_message(self, *args):
        pass


httpd = make_server("127.0.0.1", 0, get_wsgi_application(), handler_class=Quiet)
print(httpd.server_port, flush=True)
httpd.serve_forever()
"""

_TOKEN_INPUT = re.compile(r'name="csrfmiddlewaretoken" value="([^"]+)"')
GROUP = "Synthetic triage group 7f3a"


@pytest.fixture
def django_admin(tmp_path: Path):
    pytest.importorskip("django")
    script = tmp_path / "app.py"
    script.write_text(_SERVER)
    password = secrets.token_urlsafe(18)
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(p for p in sys.path if p))
    proc = subprocess.Popen(
        [sys.executable, str(script), str(tmp_path / "app.sqlite3")],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(password + "\n")
        proc.stdin.close()
        port = proc.stdout.readline().strip()
        if not port.isdigit():
            _, err = proc.communicate(timeout=10)
            pytest.fail(f"django admin did not start: {err[-2000:]}")
        yield f"http://127.0.0.1:{port}", password
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_a_django_admin_recording_keeps_no_rendered_token(django_admin, tmp_path):
    sync_api = pytest.importorskip("playwright.sync_api")
    base, password = django_admin
    with sync_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=True)
        except Exception as exc:  # noqa: BLE001 - no browser installed
            pytest.skip(f"chromium unavailable: {exc}")
        try:
            context = browser.new_context()
            capture = nc.NetworkCapture(context).start()
            t0 = time.monotonic()
            page = context.new_page()
            page.goto(f"{base}/admin/login/?next=/admin/auth/group/add/")
            page.fill("#id_username", "synthetic-admin")
            page.fill("#id_password", password)
            page.click("input[type=submit]")
            page.wait_for_url("**/admin/auth/group/add/")
            page.fill("#id_name", GROUP)
            page.click("input[name=_save]")
            page.wait_for_url(re.compile(r".*/admin/auth/group/$"))
            page.wait_for_load_state("networkidle")
            final_dom = page.content()
            capture.stop()
            raw = [dict(line) for line in capture._lines]
            capture.write(tmp_path, t0)
        finally:
            browser.close()

    rendered = set()
    for line in raw:
        rendered.update(_TOKEN_INPUT.findall(line.get("response_text") or ""))
    sent = {
        value
        for line in raw
        for name, value in line.get("form") or []
        if name == "csrfmiddlewaretoken"
    }
    # Login, add and changelist pages each rendered a fresh token, and the
    # changelist's (its action and logout forms) was never submitted.
    assert len(rendered) >= 3
    assert rendered - sent
    assert set(_TOKEN_INPUT.findall(final_dom)) & (rendered - sent)

    saved_lines = nc.load(tmp_path)
    saved = (tmp_path / nc.NETWORK_FILE).read_text()
    for token in rendered | sent:
        assert token not in saved
    assert password not in saved
    # Field names and ordinary content survive.
    assert saved.count('name=\\"csrfmiddlewaretoken\\"') >= len(rendered)
    posts = [line for line in saved_lines if line["method"] == "POST"]
    assert [name for name, _ in posts[-1]["form"]] == [
        "csrfmiddlewaretoken",
        "name",
        "_save",
    ]
    assert ["name", GROUP] in posts[-1]["form"]
    assert any(
        GROUP in (line.get("response_text") or "") and line["method"] == "GET"
        for line in saved_lines
    )
