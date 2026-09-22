from unittest.mock import Mock, patch
from uuid import UUID

from CTFd.cache import cache
from CTFd.utils.sessions import CachedSession, CachingSessionInterface
from tests.helpers import create_ctfd, destroy_ctfd, login_as_user, register_user


def test_sessions_set_httponly():
    app = create_ctfd()
    with app.app_context():
        with app.test_client() as client:
            r = client.get("/")
            cookie = dict(r.headers)["Set-Cookie"]
            assert "HttpOnly;" in cookie
    destroy_ctfd(app)


def test_sessions_set_samesite():
    app = create_ctfd()
    with app.app_context():
        with app.test_client() as client:
            r = client.get("/")
            cookie = dict(r.headers)["Set-Cookie"]
            assert "SameSite=" in cookie
    destroy_ctfd(app)


def test_session_invalidation_on_admin_password_change():
    app = create_ctfd()
    with app.app_context():
        register_user(app)
        with login_as_user(app, name="admin") as admin, login_as_user(app) as user:

            r = user.get("/settings")
            assert r.status_code == 200

            r = admin.patch("/api/v1/users/2", json={"password": "password2"})
            assert r.status_code == 200

            r = user.get("/settings")
            # User's password was changed
            # They should be logged out
            assert r.location.startswith("/login")
            assert r.status_code == 302
    destroy_ctfd(app)


def test_session_invalidation_on_user_password_change():
    app = create_ctfd()
    with app.app_context():
        register_user(app)
        with login_as_user(app) as user:

            r = user.get("/settings")
            assert r.status_code == 200

            data = {"confirm": "password", "password": "new_password"}

            r = user.patch("/api/v1/users/me", json=data)
            assert r.status_code == 200

            r = user.get("/settings")
            # User initiated their own password change
            # They should not be logged out
            assert r.status_code == 200
    destroy_ctfd(app)


# @patch.object(uuid, 'uuid4', side_effect=TEST_UUIDS)
# @patch.object(uuid, 'uuid4')
def test_session_with_duplicate_session_id():
    app = create_ctfd()
    with app.app_context():
        register_user(app)
        register_user(app, name="user1", email="user1@examplectf.com")

        TEST_UUIDS = [
            # First user login successful
            UUID("2d0ac3a8-b956-491a-9f53-d27cd33f2529"),
            UUID("85e61378-5bc4-4cc8-a37e-b03270b7b172"),
            # Second user gets a unique UUID then a duplicated one
            UUID("c47c907f-d508-4f23-a28a-a1af1e9d3f27"),
            UUID("85e61378-5bc4-4cc8-a37e-b03270b7b172"),
            UUID("85e61378-5bc4-4cc8-a37e-b03270b7b172"),
            UUID("85e61378-5bc4-4cc8-a37e-b03270b7b172"),
            UUID("85e61378-5bc4-4cc8-a37e-b03270b7b172"),
            UUID("85e61378-5bc4-4cc8-a37e-b03270b7b172"),
            # Second user should finally receive a unique UUID
            UUID("a00aff35-a12e-465a-8747-e18f78f60b13"),
            UUID("da876038-7602-4bb0-88b8-f7104094219f"),
        ]
        uuid_mock = Mock(side_effect=TEST_UUIDS)

        with patch(target="CTFd.utils.sessions.uuid4", new=uuid_mock):
            login_as_user(app)
        with patch(target="CTFd.utils.sessions.uuid4", new=uuid_mock):
            login_as_user(app, name="user1")
    destroy_ctfd(app)


def test_session_regenerate_deletes_prefixed_cache_key():
    """regenerate() must delete the real cache key (with key prefix) immediately"""
    app = create_ctfd()
    with app.app_context():
        register_user(app)
        interface = app.session_interface
        old_sid = "old-session-id-value"
        cache.set(
            interface.key_prefix + old_sid,
            interface.serializer.dumps({"id": 2, "nonce": "a", "hash": "b"}),
        )

        session = CachedSession(
            sid=old_sid, key_prefix=interface.key_prefix
        )
        session.regenerate()

        # Old session must be gone right away, not left around until TTL
        assert cache.get(interface.key_prefix + old_sid) is None
        assert session.sid is None
        assert session.modified is True
    destroy_ctfd(app)


def test_session_regenerate_does_not_delete_unprefixed_key():
    """The previous bug deleted the bare sid key instead of the prefixed one"""
    app = create_ctfd()
    with app.app_context():
        interface = app.session_interface
        sid = "abc-123-sid"
        session = CachedSession(sid=sid, key_prefix=interface.key_prefix)

        cache.set(interface.key_prefix + sid, "real-session-data")
        cache.set(sid, "unrelated-bare-key")
        session.regenerate()

        assert cache.get(interface.key_prefix + sid) is None
        assert cache.get(sid) == "unrelated-bare-key"

        cache.delete(sid)
    destroy_ctfd(app)


def test_generate_sid_avoids_existing_cache_entries():
    """_generate_sid must never return a sid that still exists in the cache"""
    app = create_ctfd()
    with app.app_context():
        interface = CachingSessionInterface(key_prefix="test-session-prefix-")
        collision_sid = "collision-uuid"
        cache.set(interface.key_prefix + collision_sid, "existing-session")

        with patch(
            "CTFd.utils.sessions.uuid4",
            side_effect=[
                collision_sid,
                collision_sid,
                "fresh-unused-uuid",
            ],
        ):
            new_sid = interface._generate_sid()

        assert new_sid == "fresh-unused-uuid"
        cache.delete(interface.key_prefix + collision_sid)
    destroy_ctfd(app)


def test_login_regenerates_session_and_clears_old_cache_entry():
    """A successful login must invalidate the anonymous pre-login session"""
    app = create_ctfd()
    with app.app_context():
        register_user(app)
        with app.test_client() as client:
            client.get("/login")
            with client.session_transaction() as sess:
                anon_sid = sess.sid
                nonce = sess.get("nonce")
            assert cache.get(app.session_interface.key_prefix + anon_sid) is not None

            r = client.post(
                "/login",
                data={"name": "user", "password": "password", "nonce": nonce},
            )
            assert r.status_code == 302

            with client.session_transaction() as sess:
                assert sess.sid != anon_sid
                assert sess["id"] == 2
            assert (
                cache.get(app.session_interface.key_prefix + anon_sid) is None
            )
    destroy_ctfd(app)
