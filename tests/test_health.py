"""Project-health tests: system checks, migration completeness, admin,
template compilation, and a render of every page.

The management-command tests run in a subprocess with the **real** settings
module so they exercise exactly what CI runs (``manage.py check`` /
``makemigrations --check --dry-run``).
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.template.loader import get_template

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[1]
ALL_TEMPLATES = [
    "login/base.html",
    "login/login.html",
    "login/register.html",
    "login/profile.html",
    "login/user_profile.html",
    "login/about.html",
    "chat/home.html",
    "chat/conversation.html",
    "chat/search.html",
]


def _manage(*args, env_overrides=None):
    env = dict(os.environ)
    env.pop("DATABASE_URL", None)  # local sqlite for management commands
    env["DJANGO_SETTINGS_MODULE"] = "settings_test"
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        [sys.executable, "manage.py", *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO_ROOT,
        timeout=180,
    )


# --------------------------------------------------------------------------- #
# System checks & migrations
# --------------------------------------------------------------------------- #
def test_manage_py_check_passes():
    proc = _manage("check")
    assert proc.returncode == 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    assert "System check identified no issues" in proc.stdout


def test_no_missing_migrations():
    proc = _manage("makemigrations", "--check", "--dry-run")
    assert proc.returncode == 0, f"Missing migrations detected:\nstdout: {proc.stdout}\nstderr: {proc.stderr}"


def test_migration_plan_is_fully_applied(db):
    """All migrations are applied — no pending rows in the migration plan."""
    from django.db.migrations.executor import MigrationExecutor
    from django.db import connection

    executor = MigrationExecutor(connection)
    plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
    assert plan == [], f"Unapplied migrations: {plan}"


def test_migrations_apply_cleanly_from_scratch():
    """`migrate` runs from zero on a fresh database (as in CI/production).

    settings_test uses an in-memory SQLite database when DATABASE_URL is
    unset, so this subprocess starts from an empty schema every time and
    never touches db.sqlite3.
    """
    proc = _manage("migrate")
    assert proc.returncode == 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    assert "error" not in proc.stderr.lower()


# --------------------------------------------------------------------------- #
# Admin
# --------------------------------------------------------------------------- #
def test_admin_index_loads_for_superuser(db):
    User = get_user_model()
    admin_user = User.objects.create_superuser(
        username="admin_health", email="admin@example.com", password="Adm1n!Passw0rd"
    )
    client = Client()
    client.force_login(admin_user)

    resp = client.get("/admin/")

    assert resp.status_code == 200
    html = resp.content.decode()
    # registered models are listed
    assert "login" in html.lower()
    assert "chat" in html.lower()


@pytest.mark.parametrize(
    "model_path",
    ["login/customuser", "login/profilepic", "chat/message"],
)
def test_admin_model_changelist_loads(db, model_path):
    User = get_user_model()
    admin_user = User.objects.create_superuser(
        username="admin_health2", email="admin2@example.com", password="Adm1n!Passw0rd"
    )
    client = Client()
    client.force_login(admin_user)

    resp = client.get(f"/admin/{model_path}/")

    assert resp.status_code == 200
    assert "changelist" in resp.content.decode().lower()


def test_admin_requires_superuser(db):
    normal = Client()
    resp = normal.get("/admin/")
    assert resp.status_code == 302
    assert "/admin/login" in resp["Location"]


# --------------------------------------------------------------------------- #
# Templates compile
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("template_name", ALL_TEMPLATES)
def test_template_compiles_without_syntax_error(template_name):
    template = get_template(template_name)  # raises TemplateSyntaxError if broken
    assert template is not None


# --------------------------------------------------------------------------- #
# Every page renders at least once (end-to-end, real templates)
# --------------------------------------------------------------------------- #
def test_all_pages_render_once(auth_client, anon_client, user, other_user):
    from tests.factories import MessageFactory

    MessageFactory(sender=other_user, receiver=user, text="render probe")

    # public pages (anonymous client — login/register redirect authed users)
    public_pages = [("/", 200), ("/register/", 200), ("/about/", 200)]
    for path, expected in public_pages:
        resp = anon_client.get(path)
        assert resp.status_code == expected, f"{path} returned {resp.status_code}, expected {expected}"

    # authenticated pages
    auth_pages = [
        ("/home/", 302),  # login app's home = redirect to chat home
        ("/chat/", 200),  # chat home
        ("/chat/search/", 200),  # search
        (f"/chat/{other_user.username}/", 200),  # conversation
        ("/profile/", 200),  # profile
        (f"/profile/{other_user.username}/", 200),  # public profile
    ]
    for path, expected in auth_pages:
        resp = auth_client.get(path)
        assert resp.status_code == expected, f"{path} returned {resp.status_code}, expected {expected}"

    # the redirect target renders too (template chain exercised end-to-end)
    follow = auth_client.get("/home/", follow=True)
    assert follow.status_code == 200
    assert "chat/home.html" in [t.name for t in follow.templates]


def test_anonymous_login_page_renders_completely(anon_client):
    resp = anon_client.get("/")
    assert resp.status_code == 200
    html = resp.content.decode()
    assert "<form" in html
    assert "password" in html


def test_about_page_renders_for_everyone_with_credit(anon_client):
    """The About page is public and carries the exact developer credit."""
    resp = anon_client.get("/about/")
    assert resp.status_code == 200
    assert "login/about.html" in [t.name for t in resp.templates]

    html = resp.content.decode()
    assert "About WebChat" in html
    assert "What WebChat Offers" in html
    assert "Technology Behind WebChat" in html
    assert "Built with ❤️ by Divyansh" in html
    # technologies that are genuinely part of this project
    assert "Django Channels" in html
    assert "Redis" in html
    # Django REST Framework is NOT used — never claim it
    assert "Django REST Framework" not in html
