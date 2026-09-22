"""Regression tests for three security/reliability fixes.

1. Session regeneration must delete the old server-side session (with the
   proper cache key prefix) and must never reuse a sid that still exists in
   the cache (sid collision / session fixation protection).
2. ThemeLoader must reject theme names containing path traversal payloads
   even if the ctf_theme config value has been tampered with.
3. The Jinja template LRU cache must not retain entries keyed by a dead
   loader weakref, which can crash subsequent template loads with TypeError.
"""

import gc
import weakref
from unittest.mock import patch
from uuid import UUID

import jinja2
import pytest
from flask import render_template_string
from jinja2.exceptions import TemplateNotFound

from CTFd.cache import cache
from CTFd import SandboxedBaseEnvironment, ThemeLoader
from CTFd.utils.sessions import CachedSession
from tests.helpers import create_ctfd, destroy_ctfd, login_as_user, register_user


# ---------------------------------------------------------------------------
# Fix 1: session regeneration / sid collision
# ---------------------------------------------------------------------------


def test_regenerate_deletes_old_session_with_prefix():
    app = create_ctfd()
    with app.app_context():
        interface = app.session_interface
        prefix = interface.key_prefix

        sid = "regenerate-old-sid"
        cache.set(prefix + sid, "session-data")
        session = CachedSession(sid=sid, key_prefix=prefix)
        session.regenerate()

        # The old key must have been deleted immediately.
        assert cache.get(prefix + sid) is None
        assert session.sid is None
        assert session.modified is True
    destroy_ctfd(app)


def test_generate_sid_never_returns_existing_sid():
    app = create_ctfd()
    with app.app_context():
        interface = app.session_interface
        prefix = interface.key_prefix
        duplicated = "2d0ac3a8-b956-491a-9f53-d27cd33f2529"
        unique = "a00aff35-a12e-465a-8747-e18f78f60b13"

        cache.set(prefix + duplicated, "stale-session")
        try:
            with patch(
                "CTFd.utils.sessions.uuid4",
                side_effect=[UUID(duplicated), UUID(unique)],
            ):
                sid = interface._generate_sid()
            assert sid == unique
        finally:
            cache.delete(prefix + duplicated)
    destroy_ctfd(app)


def test_session_regeneration_on_login_rotates_sid():
    app = create_ctfd()
    with app.app_context():
        register_user(app)
        register_user(app, name="user1", email="user1@examplectf.com")

        duplicated = "85e61378-5bc4-4cc8-a37e-b03270b7b172"
        unique = "da876038-7602-4bb0-88b8-f7104094219f"

        # First user logs in and owns `duplicated`; when the second user's
        # regeneration draws the same uuid, the interface must skip it even
        # though the stale entry is still within its cache TTL.
        with patch(
            "CTFd.utils.sessions.uuid4", side_effect=[UUID(duplicated)]
        ):
            with login_as_user(app) as first:
                assert first.get("/settings").status_code == 200

        with patch(
            "CTFd.utils.sessions.uuid4",
            side_effect=[
                UUID(duplicated),
                UUID(duplicated),
                UUID(unique),
            ],
        ):
            with login_as_user(app, name="user1") as second:
                assert second.get("/settings").status_code == 200
    destroy_ctfd(app)


# ---------------------------------------------------------------------------
# Fix 2: ThemeLoader path traversal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "evil_theme",
    [
        "../..",
        "../../../../../../etc",
        "core/../../..",
        "/etc",
        "/etc/passwd",
        "..",
        ".",
        "core/../admin",
    ],
)
def test_themeloader_rejects_path_traversal_theme_name(evil_theme):
    app = create_ctfd()
    with app.app_context():
        loader = ThemeLoader()
        with patch("CTFd.utils.get_config", return_value=evil_theme):
            with pytest.raises(TemplateNotFound):
                loader.get_source(app.jinja_env, "page.html")
    destroy_ctfd(app)


def test_themeloader_allows_known_good_theme():
    app = create_ctfd()
    with app.app_context():
        loader = ThemeLoader(theme_name="core")
        source, _, _ = loader.get_source(app.jinja_env, "base.html")
        assert source
    destroy_ctfd(app)


def test_themeloader_traversal_via_tampered_config_does_not_escape():
    app = create_ctfd()
    with app.app_context():
        from CTFd.utils import set_config

        # Simulate a tampered ctf_theme row in the database config table.
        set_config("ctf_theme", "../../../../../../etc")
        with pytest.raises(TemplateNotFound):
            render_template_string("{% include 'page.html' %}")
    destroy_ctfd(app)


# ---------------------------------------------------------------------------
# Fix 3: dead loader weakref entries in the template LRU cache
# ---------------------------------------------------------------------------


def test_template_cache_purges_entries_when_loader_is_gced():
    app = create_ctfd()
    with app.app_context():
        env = SandboxedBaseEnvironment.__new__(SandboxedBaseEnvironment)
        # A tiny cache so un-purged dead entries would immediately be evicted.
        env.cache = jinja2.utils.LRUCache(2)
        env.globals = {}

        class FakeTemplate:
            is_up_to_date = True

            def __init__(self, name):
                self.name = name
                self.globals = {}

        class FakeLoader:
            def load(self, environment, name, globals):
                return FakeTemplate(name)

        loader = FakeLoader()
        env.loader = loader
        env.auto_reload = False

        # Register the same finalizer the real constructor installs.
        weakref.finalize(
            loader,
            SandboxedBaseEnvironment._purge_loader_cache_entries,
            env.cache,
            weakref.ref(loader),
        )

        with patch("CTFd.utils.get_config", return_value="core"):
            SandboxedBaseEnvironment._load_template(env, "a.html", {})
            SandboxedBaseEnvironment._load_template(env, "b.html", {})
        assert len(env.cache) == 2

        # The environment itself is what strongly references a real loader;
        # dropping both references allows it (and the weakref) to be collected.
        del env.loader
        del loader
        gc.collect()

        # Stale entries must have been cleaned up the moment the loader died.
        assert len(env.cache) == 0

        # Re-attach a live loader and exercise the cache: with the stale
        # entries gone, no TypeError/KeyError may surface during churn/hits.
        env.loader = FakeLoader()
        with patch("CTFd.utils.get_config", return_value="core"):
            for name in ("a.html", "b.html", "c.html", "d.html"):
                tpl = SandboxedBaseEnvironment._load_template(env, name, {})
                assert tpl.name == name
            tpl = SandboxedBaseEnvironment._load_template(env, "a.html", {})
        assert tpl.name == "a.html"
        for key in env.cache.keys():
            assert key[0]() is not None
    destroy_ctfd(app)


def test_load_template_never_crashes_with_stale_dead_entries():
    app = create_ctfd()
    with app.app_context():
        env = SandboxedBaseEnvironment.__new__(SandboxedBaseEnvironment)
        env.cache = jinja2.utils.LRUCache(2)
        env.globals = {}

        class FakeTemplate:
            is_up_to_date = True

            def __init__(self, name):
                self.name = name
                self.globals = {}

        class FakeLoader:
            def load(self, environment, name, globals):
                return FakeTemplate(name)

        # Poison the cache manually with a stale dead weakref key, mimicking a
        # loader that was GC'd without a finalizer (legacy state).
        gone_loader = FakeLoader()
        dead_ref = weakref.ref(gone_loader)
        env.cache[(dead_ref, "a.html")] = FakeTemplate("a.html")
        del gone_loader
        gc.collect()
        assert dead_ref() is None

        env.loader = FakeLoader()
        env.auto_reload = False

        # The defensive purge path must remove the poisoned entry without
        # hashing it (which would raise "weak object has gone away") and keep
        # the cache's internal queue/mapping consistent.
        env._purge_dead_loader_entries()
        assert len(env.cache) == 0

        # Must not raise TypeError/KeyError; the template is loaded fresh
        # from the live loader and heavy churn through the tiny cache works.
        with patch("CTFd.utils.get_config", return_value="core"):
            for name in ("a.html", "b.html", "c.html", "d.html"):
                tpl = SandboxedBaseEnvironment._load_template(env, name, {})
                assert tpl.name == name
            tpl = SandboxedBaseEnvironment._load_template(env, "a.html", {})
        assert tpl.name == "a.html"
        for key in env.cache.keys():
            assert key[0]() is not None
    destroy_ctfd(app)
