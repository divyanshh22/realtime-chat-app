"""View & URL tests: routing, status codes, templates, context, access control,
404/405 behavior and the JSON API endpoints.
"""

import pytest
from django.test import Client
from django.urls import resolve, reverse

from chat.models import Message
from tests.factories import MessageFactory

pytestmark = pytest.mark.integration


# --------------------------------------------------------------------------- #
# URL name -> path resolution (reverse + resolve)
# --------------------------------------------------------------------------- #
URL_CASES = [
    ("login-view", {}, "/"),
    ("home-view", {}, "/home/"),
    ("profile-view", {}, "/profile/"),
    ("user_profile", {"username": "alice"}, "/profile/alice/"),
    ("register-view", {}, "/register/"),
    ("logout-view", {}, "/logout/"),
    ("chat:home-view", {}, "/chat/"),
    ("chat:search-view", {}, "/chat/search/"),
    ("chat:home-inbox-api", {}, "/chat/api/inbox/"),
    ("chat:send-message-api", {"username": "alice"}, "/chat/api/messages/alice/"),
    ("chat:messages-api", {"username": "alice"}, "/chat/api/messages/alice/history/"),
    ("chat:conversation-view", {"username": "alice"}, "/chat/alice/"),
    ("chat:user-status-api", {"user_id": 7}, "/chat/api/status/7/"),
    ("admin:index", {}, "/admin/"),
]


@pytest.mark.parametrize("name,kwargs,expected_path", URL_CASES)
def test_url_reverse_and_resolve(name, kwargs, expected_path):
    path = reverse(name, kwargs=kwargs)
    assert path == expected_path

    match = resolve(path)
    resolved_name = f"{match.namespace}:{match.url_name}" if match.namespace else match.url_name
    assert resolved_name == name


# --------------------------------------------------------------------------- #
# Anonymous access -> login redirect (with next)
# --------------------------------------------------------------------------- #
PROTECTED = [
    ("chat:home-view", {}, "get"),
    ("chat:search-view", {}, "get"),
    ("chat:home-inbox-api", {}, "get"),
    ("chat:conversation-view", {"username": "bob"}, "get"),
    ("chat:messages-api", {"username": "bob"}, "get"),
    ("chat:send-message-api", {"username": "bob"}, "post"),
    ("chat:user-status-api", {"user_id": 1}, "get"),
    ("profile-view", {}, "get"),
    ("logout-view", {}, "get"),
    ("user_profile", {"username": "bob"}, "get"),
]


@pytest.mark.parametrize("name,kwargs,method", PROTECTED)
def test_login_required_redirects_anonymous(name, kwargs, method):
    from urllib.parse import parse_qs, urlparse

    client = Client()
    resp = getattr(client, method)(reverse(name, kwargs=kwargs))

    assert resp.status_code == 302
    location = resp["Location"]
    assert urlparse(location).path == reverse("login-view")
    query = parse_qs(urlparse(location).query)
    assert query["next"][0] == reverse(name, kwargs=kwargs)


# --------------------------------------------------------------------------- #
# Pages: status code + template + context
# --------------------------------------------------------------------------- #
def test_login_page_renders(db):
    resp = Client().get(reverse("login-view"))
    assert resp.status_code == 200
    assert "login/login.html" in [t.name for t in resp.templates]


def test_register_page_renders(db):
    resp = Client().get(reverse("register-view"))
    assert resp.status_code == 200
    assert "login/register.html" in [t.name for t in resp.templates]


def test_home_page_renders_with_chat_users(auth_client, user, other_user):
    MessageFactory(sender=other_user, receiver=user, text="home preview text")

    resp = auth_client.get(reverse("chat:home-view"))

    assert resp.status_code == 200
    assert "chat/home.html" in [t.name for t in resp.templates]
    chat_users = list(resp.context["chat_users"])
    assert [u.username for u in chat_users] == [other_user.username]
    assert "home preview text" in resp.content.decode()


def test_home_page_empty_state(auth_client, user):
    resp = auth_client.get(reverse("chat:home-view"))
    assert resp.status_code == 200
    assert "No chats yet" in resp.content.decode()


def test_conversation_page_renders_with_context(auth_client, user, other_user):
    MessageFactory(sender=other_user, receiver=user, text="history one")
    MessageFactory(sender=user, receiver=other_user, text="history two")

    resp = auth_client.get(reverse("chat:conversation-view", kwargs={"username": other_user.username}))

    assert resp.status_code == 200
    assert "chat/conversation.html" in [t.name for t in resp.templates]
    assert resp.context["other_user"].id == other_user.id
    texts = [m.text for m in resp.context["chat_messages"]]
    assert texts == ["history one", "history two"]

    html = resp.content.decode()
    assert html.index("history one") < html.index("history two")


def test_search_page_renders_results(auth_client, user, other_user, third_user):
    resp = auth_client.get(reverse("chat:search-view"), {"q": other_user.username})

    assert resp.status_code == 200
    assert "chat/search.html" in [t.name for t in resp.templates]
    assert resp.context["query"] == other_user.username
    assert list(resp.context["results"]) == [other_user]


def test_search_excludes_self(auth_client, user):
    resp = auth_client.get(reverse("chat:search-view"), {"q": user.username})
    assert resp.status_code == 200
    assert list(resp.context["results"]) == []


def test_profile_page_renders(auth_client, user):
    resp = auth_client.get(reverse("profile-view"))
    assert resp.status_code == 200
    assert "login/profile.html" in [t.name for t in resp.templates]
    assert resp.context["profile"].user.id == user.id


def test_user_profile_page_renders(auth_client, user, other_user):
    resp = auth_client.get(reverse("user_profile", kwargs={"username": other_user.username}))
    assert resp.status_code == 200
    assert "login/user_profile.html" in [t.name for t in resp.templates]
    assert resp.context["profile_user"].id == other_user.id


# --------------------------------------------------------------------------- #
# 404 behavior
# --------------------------------------------------------------------------- #
def test_conversation_with_nonexistent_user_404(auth_client, user):
    resp = auth_client.get(reverse("chat:conversation-view", kwargs={"username": "ghost"}))
    assert resp.status_code == 404


def test_messages_api_for_nonexistent_user_404(auth_client, user):
    resp = auth_client.get(reverse("chat:messages-api", kwargs={"username": "ghost"}))
    assert resp.status_code == 404


def test_send_message_api_for_nonexistent_user_404(auth_client, user):
    resp = auth_client.post(reverse("chat:send-message-api", kwargs={"username": "ghost"}), {"text": "hi"})
    assert resp.status_code == 404


def test_user_status_api_for_nonexistent_id_404(auth_client, user):
    resp = auth_client.get(reverse("chat:user-status-api", kwargs={"user_id": 999999}))
    assert resp.status_code == 404
    assert resp.json() == {"error": "User not found"}


# --------------------------------------------------------------------------- #
# 405 behavior (allow-listed HTTP methods)
# --------------------------------------------------------------------------- #
WRONG_METHOD = [
    ("post", "chat:home-inbox-api", {}, "GET"),
    ("get", "chat:send-message-api", {"username": "bob"}, "POST"),
    ("post", "chat:messages-api", {"username": "bob"}, "GET"),
    ("post", "chat:user-status-api", {"user_id": 1}, "GET"),
]


@pytest.mark.parametrize("method,name,kwargs,allowed", WRONG_METHOD)
def test_wrong_http_method_returns_405(auth_client, user, method, name, kwargs, allowed):
    resp = getattr(auth_client, method)(reverse(name, kwargs=kwargs))
    assert resp.status_code == 405
    assert allowed in resp["Allow"]


# --------------------------------------------------------------------------- #
# Inbox API
# --------------------------------------------------------------------------- #
def test_home_inbox_api_shape_and_unread_count(auth_client, user, other_user, third_user):
    # only other_user has messages with me; third_user must be excluded
    MessageFactory(sender=other_user, receiver=user, text="inbox preview", is_read=False)

    resp = auth_client.get(reverse("chat:home-inbox-api"))

    assert resp.status_code == 200
    payload = resp.json()
    assert set(payload.keys()) == {"users"}
    assert len(payload["users"]) == 1  # only users I exchanged messages with

    entry = payload["users"][0]
    assert set(entry.keys()) == {
        "id",
        "username",
        "preview",
        "last_message_time",
        "unread_count",
        "is_online",
        "profile_pic_url",
        "conversation_url",
    }
    assert entry["username"] == other_user.username
    assert entry["preview"] == "inbox preview"
    assert entry["unread_count"] == 1
    assert entry["conversation_url"] == f"/chat/{other_user.username}/"


# --------------------------------------------------------------------------- #
# Message history API
# --------------------------------------------------------------------------- #
def test_messages_api_returns_only_this_thread_in_order(auth_client, user, other_user, third_user):
    from datetime import timedelta

    from django.utils import timezone

    now = timezone.now()
    m1 = MessageFactory(sender=user, receiver=other_user, text="thread first")
    m2 = MessageFactory(sender=other_user, receiver=user, text="thread second")
    MessageFactory(sender=other_user, receiver=third_user, text="other thread secret")
    Message.objects.filter(pk=m1.pk).update(timestamp=now - timedelta(minutes=2))
    Message.objects.filter(pk=m2.pk).update(timestamp=now - timedelta(minutes=1))

    resp = auth_client.get(reverse("chat:messages-api", kwargs={"username": other_user.username}))

    assert resp.status_code == 200
    data = resp.json()
    assert set(data.keys()) == {"messages"}
    texts = [m["text"] for m in data["messages"]]
    assert texts == ["thread first", "thread second"]
    assert "other thread secret" not in texts
    for m in data["messages"]:
        assert set(m.keys()) == {
            "id",
            "sender_id",
            "sender_username",
            "text",
            "photo_url",
            "timestamp",
            "is_read",
            "is_delivered",
        }


def test_messages_api_has_no_pagination(auth_client, user, other_user):
    """KNOWN LIMITATION (documented): the history endpoint returns ALL messages,
    there is no pagination/limit implemented in this app."""
    for i in range(25):
        MessageFactory(sender=user, receiver=other_user, text=f"bulk {i}")

    resp = auth_client.get(reverse("chat:messages-api", kwargs={"username": other_user.username}))
    assert len(resp.json()["messages"]) == 25


# --------------------------------------------------------------------------- #
# Send message API
# --------------------------------------------------------------------------- #
def test_send_message_api_creates_and_returns_payload(auth_client, user, other_user):
    resp = auth_client.post(
        reverse("chat:send-message-api", kwargs={"username": other_user.username}),
        {"text": "hello over http"},
    )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["type"] == "message"
    message = payload["message"]
    assert set(message.keys()) == {
        "id",
        "sender_id",
        "sender_username",
        "text",
        "photo_url",
        "timestamp",
        "is_read",
        "is_delivered",
    }
    assert message["sender_id"] == user.id
    assert message["sender_username"] == user.username
    assert message["text"] == "hello over http"

    msg = Message.objects.get()
    assert msg.sender == user
    assert msg.receiver == other_user


def test_conversation_post_saves_message(auth_client, user, other_user):
    resp = auth_client.post(
        reverse("chat:conversation-view", kwargs={"username": other_user.username}),
        {"text": "posted from conversation page"},
    )

    assert resp.status_code == 302
    assert resp["Location"] == reverse("chat:conversation-view", kwargs={"username": other_user.username})
    msg = Message.objects.get()
    assert msg.sender == user
    assert msg.receiver == other_user
    assert msg.text == "posted from conversation page"


# --------------------------------------------------------------------------- #
# Access control between users
# --------------------------------------------------------------------------- #
def test_user_cannot_read_other_users_thread(auth_client, user, other_user, third_user):
    MessageFactory(sender=other_user, receiver=third_user, text="B_TO_C_SECRET_1")
    MessageFactory(sender=third_user, receiver=other_user, text="B_TO_C_SECRET_2")
    MessageFactory(sender=user, receiver=third_user, text="A_TO_C_OWN_MESSAGE")

    # history API: A reading the thread with C must never see B<->C messages
    resp = auth_client.get(reverse("chat:messages-api", kwargs={"username": third_user.username}))
    texts = [m["text"] for m in resp.json()["messages"]]
    assert "B_TO_C_SECRET_1" not in texts
    assert "B_TO_C_SECRET_2" not in texts
    assert "A_TO_C_OWN_MESSAGE" in texts

    # conversation page: same isolation server-side rendered
    page = auth_client.get(reverse("chat:conversation-view", kwargs={"username": third_user.username}))
    html = page.content.decode()
    assert "B_TO_C_SECRET_1" not in html
    assert "B_TO_C_SECRET_2" not in html

    # home inbox: B must not appear in A's chat list at all
    home = auth_client.get(reverse("chat:home-view"))
    assert other_user.username not in home.content.decode()


def test_user_posts_are_never_injected_into_other_users_thread(auth_client, user, other_user, third_user):
    MessageFactory(sender=other_user, receiver=third_user, text="B_TO_C_SECRET")
    before = Message.objects.filter(sender=other_user, receiver=third_user).count()

    resp = auth_client.post(
        reverse("chat:send-message-api", kwargs={"username": third_user.username}),
        {"text": "A speaking to C"},
    )

    assert resp.status_code == 200
    msg = Message.objects.get(text="A speaking to C")
    assert msg.sender == user  # attributed to the real author, never spoofed
    assert msg.receiver == third_user
    # the B<->C thread is untouched
    assert Message.objects.filter(sender=other_user, receiver=third_user).count() == before


# --------------------------------------------------------------------------- #
# User status API
# --------------------------------------------------------------------------- #
def test_user_status_api_returns_presence(auth_client, user, other_user):
    resp = auth_client.get(reverse("chat:user-status-api", kwargs={"user_id": other_user.id}))

    assert resp.status_code == 200
    data = resp.json()
    assert set(data.keys()) == {"user_id", "username", "is_online", "last_seen"}
    assert data["user_id"] == other_user.id
    assert data["username"] == other_user.username
    assert data["is_online"] is False


# --------------------------------------------------------------------------- #
# Custom error pages
# --------------------------------------------------------------------------- #
def test_custom_404_page_used(db):
    """A custom branded 404 template exists and is what Django serves."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    assert (root / "login" / "templates" / "404.html").exists()

    resp = Client().get("/definitely/not/a/real/path/xyz/")
    assert resp.status_code == 404
    assert "404.html" in [t.name for t in resp.templates]
