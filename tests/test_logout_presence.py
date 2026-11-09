"""Logout must synchronise presence (bug: "user stays ONLINE after logout").

Root cause under test
---------------------
``logout_view`` used to only call ``logout(request)``: it never touched the
presence state. After logout the row still said ``is_online=True`` with
``connection_count>0`` until (and unless) the browser happened to close its
WebSockets — which proxies delay by 10s+ and background tabs skip entirely.
No OFFLINE was ever broadcast, so every viewer (and every page reload) kept
showing the logged-out account as online.

The fix (see ``chat/presence.py`` + ``login/views.py``):

* ``force_offline``              — authoritative: zeroes ``connection_count``
  for every tab/device, flips ``is_online`` off and stamps ``last_seen`` at
  the logout moment,
* ``force_offline_and_announce`` — then broadcasts OFFLINE globally,
* ``close_user_sockets``         — force-closes every socket of the account
  through the per-user channel-layer group,
* ``_clear_redis_presence``      — drops stale Redis presence leftovers,
* ``commit_offline``'s ``is_online=True`` guard — a socket closing *after*
  the logout can no longer re-stamp ``last_seen`` or re-announce.

Scenarios mirror the bug report: viewer sees OFFLINE instantly, reloads and
re-opened DMs stay OFFLINE, re-login comes back ONLINE, multi-tab logout,
stale Redis correction, and old sockets/sessions can never restore the
outdated ONLINE state.
"""

import pytest
from asgiref.sync import sync_to_async
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from chat import presence
from tests.conftest import ws_connect
from tests.factories import MessageFactory


def _db(user):
    return type(user).objects.get(pk=user.pk)


def _login_client(user):
    """Force-login ``user``; return ``(client, sessionid)``.

    The cookie and the client share ONE session — exactly like a real
    browser — so logging out through the client invalidates the same
    session the WebSockets authenticated with.
    """
    client = Client()
    client.force_login(user)
    return client, client.cookies["sessionid"].value


class RecordingLayer:
    """Channel-layer stand-in that records every group_send."""

    def __init__(self):
        self.sent = []

    async def group_send(self, group, event):
        self.sent.append((group, event))


class _FakeRedis:
    """Minimal redis stand-in: the two presence hashes + hdel (no network)."""

    def __init__(self):
        self.hashes = {"online_users": {}, "last_seen": {}}

    def hgetall(self, name):
        return dict(self.hashes.get(name, {}))

    def hget(self, name, key):
        return self.hashes.get(name, {}).get(key)

    def hdel(self, name, key):
        self.hashes.setdefault(name, {}).pop(key, None)


async def _drain(ws, max_msgs=8, idle=0.15):
    got = []
    for _ in range(max_msgs):
        if await ws.receive_nothing(timeout=idle):
            break
        got.append(await ws.receive_json_from())
    return got


async def _expect_close(ws, max_msgs=5):
    """Read outputs until the server-initiated ``websocket.close`` arrives."""
    for _ in range(max_msgs):
        msg = await ws.receive_output(timeout=2)
        if msg["type"] == "websocket.close":
            return True
    return False


# --------------------------------------------------------------------------- #
# Sync: DB / view level
# --------------------------------------------------------------------------- #
def test_logout_marks_user_offline_and_zeroes_all_tabs(db, user):
    """Logout is authoritative: every tab's count cleared, last_seen stamped."""
    presence.open_connection(user.id)  # presence socket
    presence.open_connection(user.id)  # second tab / DM socket
    assert _db(user).is_online is True
    assert _db(user).connection_count == 2

    client, _ = _login_client(user)
    resp = client.get("/logout/")
    assert resp.status_code == 302  # redirected to the login page

    fresh = _db(user)
    assert fresh.is_online is False
    assert fresh.connection_count == 0  # no tab/device left counted
    assert fresh.last_seen is not None  # stamped at the logout moment


def test_logout_broadcasts_offline_and_force_closes_sockets(db, user, monkeypatch):
    """Logout must announce OFFLINE globally AND fan out force_disconnect."""
    recorder = RecordingLayer()
    monkeypatch.setattr(presence, "get_channel_layer", lambda: recorder)

    presence.open_connection(user.id)
    client, _ = _login_client(user)
    client.get("/logout/")

    # 1) the global presence broadcast
    group, event = recorder.sent[0]
    assert group == presence.PRESENCE_GROUP
    assert event["type"] == "presence_update"
    assert event["user_id"] == user.id
    assert event["username"] == user.username
    assert event["status"] == "offline"
    assert event["last_seen"]  # the logout moment, not None

    # 2) force-close for every socket of this account (all tabs/devices)
    assert recorder.sent[1] == (
        presence.user_group(user.id),
        {"type": "force_disconnect"},
    )
    assert len(recorder.sent) == 2  # nothing else was sent


def test_logout_clears_stale_redis_presence(db, user, other_user, monkeypatch):
    """A stale Redis 'online' leftover must be corrected by logout."""
    fake = _FakeRedis()
    monkeypatch.setattr("chat.views._redis_client", lambda: fake)

    # Leftover from an older deployment: Redis claims Div is online even
    # though the DB says offline — the inbox ORs the Redis flag, so it wins.
    fake.hashes["online_users"][str(user.id)] = "online"
    MessageFactory(sender=user, receiver=other_user)

    riya = Client()
    riya.force_login(other_user)
    inbox = riya.get(reverse("chat:home-inbox-api")).json()["users"]
    entry = next(u for u in inbox if u["id"] == user.id)
    assert entry["is_online"] is True  # stale Redis hazard demonstrated

    # Div logs out: the stale entry is dropped along with everything else.
    client, _ = _login_client(user)
    client.get("/logout/")
    assert str(user.id) not in fake.hashes["online_users"]

    inbox = riya.get(reverse("chat:home-inbox-api")).json()["users"]
    entry = next(u for u in inbox if u["id"] == user.id)
    assert entry["is_online"] is False  # stale status corrected


def test_logout_survives_redis_failure(db, user, monkeypatch):
    """Redis cleanup is best-effort: a dead Redis must not block logout."""

    def _explode():
        raise RuntimeError("redis down")

    monkeypatch.setattr("chat.views._redis_client", _explode)

    presence.open_connection(user.id)
    client, _ = _login_client(user)
    resp = client.get("/logout/")  # must not raise

    assert resp.status_code == 302
    assert _db(user).is_online is False  # DB presence cleanup still happened


def test_conversation_and_status_api_show_offline_after_logout(db, user, other_user):
    """Reloading / re-opening the DM renders OFFLINE with a last-seen stamp."""
    MessageFactory(sender=user, receiver=other_user)
    presence.open_connection(user.id)

    div_client, _ = _login_client(user)
    div_client.get("/logout/")

    riya = Client()
    riya.force_login(other_user)

    # Server-rendered DM header (what Riya sees after F5)
    page = riya.get(reverse("chat:conversation-view", kwargs={"username": user.username}))
    assert page.status_code == 200
    assert page.context["other_user"].is_online is False
    assert page.context["other_user"].last_seen_str  # "Last seen ... ago"

    # The status API the page calls on load agrees
    api = riya.get(reverse("chat:user-status-api", kwargs={"user_id": user.id})).json()
    assert api["is_online"] is False
    assert api["last_seen"]


def test_old_socket_cannot_restore_online_after_logout(db, user):
    """A lingering pre-logout socket must not resurrect ONLINE or restamp."""
    presence.open_connection(user.id)
    client, _ = _login_client(user)
    client.get("/logout/")
    logout_seen = _db(user).last_seen
    assert logout_seen is not None

    # The old socket keeps pinging: ignored, connection_count is 0.
    presence.touch_heartbeat(user.id)
    assert _db(user).is_online is False

    # The old socket finally closes: commit_offline is guarded by
    # is_online=True, so it must NOT re-stamp last_seen or re-announce.
    presence.close_connection(user.id)
    assert presence.commit_offline(user.id, when=timezone.now()) is None

    final = _db(user)
    assert final.is_online is False
    assert final.last_seen == logout_seen  # unchanged, not re-stamped


def test_relogin_after_logout_comes_back_online(db, user):
    """Div logging in again (fresh session + socket) is ONLINE again."""
    presence.open_connection(user.id)
    client, _ = _login_client(user)
    client.get("/logout/")
    assert _db(user).is_online is False

    fresh_client = Client()
    assert fresh_client.login(username=user.username, password="Str0ng!Passw0rd") is True
    presence.open_connection(user.id)  # its new presence socket connects

    fresh = _db(user)
    assert fresh.is_online is True
    assert fresh.last_seen is None  # cleared while online


def test_logout_follows_existing_per_session_policy(db, user):
    """Logging out of ONE session keeps the existing global logout policy:
    Django invalidates only the session it was called on (no new
    global-session-invalidation feature was added by this fix)."""
    session_a, _ = _login_client(user)
    session_b = Client()
    session_b.force_login(user)

    resp = session_a.get("/logout/")
    assert resp.status_code == 302

    # The other session still works (per-session logout, as before)...
    assert session_b.get(reverse("chat:home-view")).status_code == 200
    # ...while the logged-out session is dead.
    assert session_a.get(reverse("chat:home-view")).status_code == 302


# --------------------------------------------------------------------------- #
# Async: over real WebSockets (/ws/presence/ + /ws/chat/)
# --------------------------------------------------------------------------- #
@pytest.mark.django_db(transaction=True)
async def test_viewer_sees_offline_instantly_and_all_sockets_close(user, other_user):
    """Riya's open DM updates instantly; Div's presence + DM sockets close."""
    div_client, div_cookie = await sync_to_async(_login_client)(user)
    _, riya_cookie = await sync_to_async(_login_client)(other_user)

    div_presence = ws_connect("/ws/presence/", cookie=div_cookie)
    div_dm = ws_connect(f"/ws/chat/{other_user.username}/", cookie=div_cookie)
    riya_presence = ws_connect("/ws/presence/", cookie=riya_cookie)
    for ws in (div_presence, div_dm, riya_presence):
        assert (await ws.connect())[0] is True
    await _drain(div_presence), await _drain(div_dm), await _drain(riya_presence)

    fresh = await sync_to_async(_db)(user)
    assert fresh.is_online is True
    assert fresh.connection_count == 2  # presence socket + DM socket

    resp = await sync_to_async(div_client.get)("/logout/")
    assert resp.status_code == 302

    # Riya (DM already open) receives OFFLINE immediately — no reload needed.
    event = await riya_presence.receive_json_from(timeout=2)
    assert event["type"] == "presence_update"
    assert event["user_id"] == user.id
    assert event["status"] == "offline"
    assert event["last_seen"]

    # Every socket of Div's account is force-closed (DM + presence).
    assert await _expect_close(div_presence) is True
    assert await _expect_close(div_dm) is True

    fresh = await sync_to_async(_db)(user)
    assert fresh.is_online is False
    assert fresh.connection_count == 0

    # The forced teardown must NOT re-announce or re-stamp anything.
    await div_presence.disconnect()
    await div_dm.disconnect()
    assert await riya_presence.receive_nothing(timeout=0.3) is True
    await riya_presence.disconnect()


@pytest.mark.django_db(transaction=True)
async def test_logout_closes_every_tab_of_the_account(user, other_user):
    """Two open tabs: logging out from one session closes both (and only
    Div's sockets — the viewer's socket survives)."""
    div_client, div_cookie = await sync_to_async(_login_client)(user)
    _, riya_cookie = await sync_to_async(_login_client)(other_user)

    tab_one = ws_connect("/ws/presence/", cookie=div_cookie)
    tab_two = ws_connect("/ws/presence/", cookie=div_cookie)
    riya = ws_connect("/ws/presence/", cookie=riya_cookie)
    for ws in (tab_one, tab_two, riya):
        assert (await ws.connect())[0] is True
    await _drain(tab_one), await _drain(tab_two), await _drain(riya)

    assert (await sync_to_async(_db)(user)).connection_count == 2

    resp = await sync_to_async(div_client.get)("/logout/")
    assert resp.status_code == 302

    assert await _expect_close(tab_one) is True
    assert await _expect_close(tab_two) is True

    event = await riya.receive_json_from(timeout=2)
    assert event["status"] == "offline" and event["user_id"] == user.id

    fresh = await sync_to_async(_db)(user)
    assert fresh.is_online is False
    assert fresh.connection_count == 0

    # Viewer's socket is untouched: it never closes.
    assert await riya.receive_nothing(timeout=0.3) is True
    await tab_one.disconnect()
    await tab_two.disconnect()
    await riya.disconnect()


@pytest.mark.django_db(transaction=True)
async def test_relogin_announces_online_again_to_viewer(user, other_user):
    """After logout, Div logging back in re-announces ONLINE to Riya."""
    div_client, div_cookie = await sync_to_async(_login_client)(user)
    _, riya_cookie = await sync_to_async(_login_client)(other_user)

    old_ws = ws_connect("/ws/presence/", cookie=div_cookie)
    riya = ws_connect("/ws/presence/", cookie=riya_cookie)
    assert (await old_ws.connect())[0] is True
    assert (await riya.connect())[0] is True
    await _drain(old_ws), await _drain(riya)

    await sync_to_async(div_client.get)("/logout/")
    offline = await riya.receive_json_from(timeout=2)
    assert offline["status"] == "offline"
    assert await _expect_close(old_ws) is True
    await old_ws.disconnect()

    # Div logs in again on a new session; its fresh presence socket connects.
    _, new_cookie = await sync_to_async(_login_client)(user)
    new_ws = ws_connect("/ws/presence/", cookie=new_cookie)
    assert (await new_ws.connect())[0] is True
    await _drain(new_ws)

    online = await riya.receive_json_from(timeout=2)
    assert online["type"] == "presence_update"
    assert online["user_id"] == user.id
    assert online["status"] == "online"

    fresh = await sync_to_async(_db)(user)
    assert fresh.is_online is True
    assert fresh.connection_count == 1

    await new_ws.disconnect()
    await riya.disconnect()


@pytest.mark.django_db(transaction=True)
async def test_get_status_after_logout_reports_offline(user, other_user):
    """'Who's online' asked when a DM loads must report OFFLINE + last_seen."""
    div_client, div_cookie = await sync_to_async(_login_client)(user)
    div_ws = ws_connect("/ws/presence/", cookie=div_cookie)
    assert (await div_ws.connect())[0] is True
    await _drain(div_ws)

    await sync_to_async(div_client.get)("/logout/")
    assert await _expect_close(div_ws) is True
    await div_ws.disconnect()

    _, riya_cookie = await sync_to_async(_login_client)(other_user)
    riya = ws_connect("/ws/presence/", cookie=riya_cookie)
    assert (await riya.connect())[0] is True
    await _drain(riya)

    await riya.send_json_to({"type": "get_status", "user_ids": [user.id]})
    reply = await riya.receive_json_from(timeout=2)
    assert reply["type"] == "bulk_status"
    assert reply["statuses"][str(user.id)]["status"] == "offline"
    assert reply["statuses"][str(user.id)]["last_seen"]
    await riya.disconnect()


@pytest.mark.django_db(transaction=True)
async def test_old_session_cannot_reconnect_after_logout(user):
    """The pre-logout sessionid is dead: an old tab can never reconnect and
    therefore can never restore the outdated ONLINE state."""
    div_client, div_cookie = await sync_to_async(_login_client)(user)

    ws = ws_connect("/ws/presence/", cookie=div_cookie)
    assert (await ws.connect())[0] is True
    await _drain(ws)

    resp = await sync_to_async(div_client.get)("/logout/")
    assert resp.status_code == 302
    assert await _expect_close(ws) is True
    await ws.disconnect()

    stale = ws_connect("/ws/presence/", cookie=div_cookie)
    connected, close_code = await stale.connect()
    assert connected is False  # session flushed -> anonymous -> rejected
    assert close_code == 1000

    assert (await sync_to_async(_db)(user)).is_online is False
