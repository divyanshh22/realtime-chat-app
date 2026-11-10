"""Delivery / receipt synchronisation tests — the full ✓ / ✓✓ / green ✓✓ lifecycle.

Root cause regression-tested here
--------------------------------
``Message.is_delivered`` used to be written ONLY by code paths tied to the
recipient's conversation socket (open DM / DM page GET / DM-socket connect).
A recipient who was online anywhere else — home page, another DM, second
tab — received nothing, so the sender kept a single tick until the recipient
opened that exact conversation. Long-lived accounts looked worst because
their accumulated flags were never re-synced; the data was fine, the event
routing was not.

The fix (see ``chat/delivery.py``):

* every send path pushes ``deliver_incoming`` to the recipient's PERSONAL
  group (presence socket + every open chat socket),
* a presence (re)connect flushes the whole inbox backlog — only ever
  ``is_delivered``, never ``is_read``,
* only live sockets flip the flag (no "online flag => delivered" shortcut)
* only the flipping socket broadcasts the receipt (no duplicates across
  tabs).

Tick semantics under test: single ✓ = saved not delivered, double ✓✓ =
delivered to an active session of the recipient, green ✓✓ = conversation
opened and the message read.
"""

import pytest
from asgiref.sync import sync_to_async
from django.test import Client
from django.urls import reverse

from chat import delivery, presence
from chat.consumers import ChatConsumer
from chat.models import Message
from chat.views import get_room_group_name
from tests.conftest import ws_connect
from tests.factories import MessageFactory


async def _drain(ws, max_msgs=8, idle=0.15):
    """Consume any pending frames (status announces etc.), return them."""
    got = []
    for _ in range(max_msgs):
        if await ws.receive_nothing(timeout=idle):
            break
        got.append(await ws.receive_json_from())
    return got


class RecordingLayer:
    """Captures group_send calls made by the views (no real consumers)."""

    def __init__(self):
        self.sent = []

    async def group_send(self, group, event):
        self.sent.append((group, event))


# --------------------------------------------------------------------------- #
# Unit: single source of truth + idempotent flag flipping
# --------------------------------------------------------------------------- #
def test_room_group_name_is_single_source_of_truth(db):
    assert delivery.room_group_name(3, 7) == "chat_3_7"
    for a, b in [(3, 7), (7, 3)]:
        expected = delivery.room_group_name(a, b)
        # HTTP and WebSocket paths must never drift apart — a drift means
        # receipts would be broadcast to a group the sender doesn't join.
        assert get_room_group_name(a, b) == expected
        assert ChatConsumer._get_room_group_name(a, b) == expected


def test_mark_delivered_flips_exactly_once_and_never_reads(db, user, other_user):
    msg = MessageFactory(sender=user, receiver=other_user)

    assert delivery.mark_delivered(other_user.id, msg.id) == [msg.id]  # the winner
    assert delivery.mark_delivered(other_user.id, msg.id) == []  # idempotent
    assert delivery.mark_delivered(user.id, msg.id) == []  # not its inbox
    assert delivery.mark_delivered(other_user.id, 999_999) == []  # missing row
    assert delivery.mark_delivered(other_user.id, "abc") == []  # junk id
    assert delivery.mark_delivered(other_user.id, True) == []  # bool is not an id

    fresh = Message.objects.get(pk=msg.pk)
    assert (fresh.is_delivered, fresh.is_read) == (True, False)  # delivered, NOT read


def test_flush_groups_by_sender_and_never_touches_read(db, user, other_user, third_user):
    from_other = [MessageFactory(sender=other_user, receiver=user, text=f"o{i}") for i in range(2)]
    from_third = MessageFactory(sender=third_user, receiver=user, text="t0")
    # legacy pre-is_delivered row: read long ago, flag never backfilled
    legacy = MessageFactory(sender=other_user, receiver=user, text="legacy", is_read=True, is_delivered=False)
    # someone else's inbox / already delivered — must not be re-announced
    not_mine = MessageFactory(sender=other_user, receiver=third_user, text="x")
    already = MessageFactory(sender=user, receiver=other_user, text="y", is_delivered=True)

    backlog = dict(delivery.flush_undelivered(user.id))

    room_other = delivery.room_group_name(other_user.id, user.id)
    room_third = delivery.room_group_name(third_user.id, user.id)
    assert set(backlog) == {room_other, room_third}
    assert set(backlog[room_other]) == {m.id for m in from_other} | {legacy.id}
    assert backlog[room_third] == [from_third.id]

    for m in from_other + [from_third]:
        row = Message.objects.get(pk=m.pk)
        assert (row.is_delivered, row.is_read) == (True, False)  # delivered, NOT read
    legacy_row = Message.objects.get(pk=legacy.pk)
    assert (legacy_row.is_delivered, legacy_row.is_read) == (True, True)  # read preserved

    # second flush: nothing pending -> no receipts, nothing re-announced
    assert delivery.flush_undelivered(user.id) == []
    # other people's messages untouched
    assert Message.objects.get(pk=not_mine.pk).is_delivered is False
    assert Message.objects.get(pk=already.pk).is_delivered is True


def test_home_inbox_heals_stale_ghost_presence(db, user, other_user):
    """A crashed-socket ghost (is_online=True, no heartbeat) must not be
    shown as online in the inbox — presence reads heal instead of trusting
    the raw flag."""
    MessageFactory(sender=user, receiver=other_user)  # puts user in the inbox
    type(user).objects.filter(pk=user.pk).update(is_online=True, presence_heartbeat=None)

    client = Client()
    client.force_login(other_user)
    entry = next(u for u in client.get(reverse("chat:home-inbox-api")).json()["users"] if u["id"] == user.id)

    assert entry["is_online"] is False
    assert type(user).objects.get(pk=user.pk).is_online is False  # healed server-side


# --------------------------------------------------------------------------- #
# View wiring: every send path pushes a delivery hint to the recipient's
# personal group — and with no live socket the message stays single-ticked
# --------------------------------------------------------------------------- #
def test_send_message_api_pushes_delivery_hint(auth_client, user, other_user, monkeypatch):
    layer = RecordingLayer()
    monkeypatch.setattr("chat.views.get_channel_layer", lambda: layer)

    resp = auth_client.post(
        reverse("chat:send-message-api", kwargs={"username": other_user.username}),
        {"text": "hi"},
    )
    message_id = resp.json()["message"]["id"]
    room = delivery.room_group_name(user.id, other_user.id)

    hints = [(g, e) for g, e in layer.sent if e["type"] == "deliver_incoming"]
    assert len(hints) == 1
    group, event = hints[0]
    assert group == presence.user_group(other_user.id)  # recipient's personal group
    assert event["message_id"] == message_id
    assert event["room_group"] == room
    # the live chat_message broadcast still goes to the conversation room
    assert any(g == room and e["type"] == "chat_message" for g, e in layer.sent)
    # the layer only recorded the hint: no socket processed it -> honest single tick
    assert Message.objects.get(pk=message_id).is_delivered is False


def test_conversation_post_pushes_delivery_hint(auth_client, user, other_user, monkeypatch):
    layer = RecordingLayer()
    monkeypatch.setattr("chat.views.get_channel_layer", lambda: layer)

    resp = auth_client.post(
        reverse("chat:conversation-view", kwargs={"username": other_user.username}),
        {"text": "page form fallback"},
    )
    assert resp.status_code == 302

    hints = [e for _, e in layer.sent if e["type"] == "deliver_incoming"]
    assert len(hints) == 1
    assert hints[0]["room_group"] == delivery.room_group_name(user.id, other_user.id)
    assert Message.objects.get(text="page form fallback").is_delivered is False


# --------------------------------------------------------------------------- #
# Scenario: both online — recipient on ANOTHER page (presence socket only).
# THE core fix: the sender's tick flips without the recipient opening the DM.
# --------------------------------------------------------------------------- #
@pytest.mark.django_db(transaction=True)
async def test_recipient_on_another_page_gets_live_delivery_receipt(user, other_user, user_cookie, other_user_cookie):
    sender = ws_connect(f"/ws/chat/{other_user.username}/", cookie=user_cookie)
    recipient_presence = ws_connect("/ws/presence/", cookie=other_user_cookie)
    assert (await sender.connect())[0] is True
    assert (await recipient_presence.connect())[0] is True
    await _drain(sender), await _drain(recipient_presence)

    await sender.send_json_to({"type": "message", "text": "you home?"})

    echo = await sender.receive_json_from()
    assert echo["type"] == "message"
    assert echo["message"]["is_delivered"] is False  # sent state first
    receipt = await sender.receive_json_from()
    assert receipt == {"type": "message_delivered", "message_ids": [echo["message"]["id"]]}

    # delivery must not leak chat content to the presence socket, and it
    # must NOT count as reading: they never opened the DM
    assert await recipient_presence.receive_nothing(timeout=0.3) is True
    row = await sync_to_async(Message.objects.get)(pk=echo["message"]["id"])
    assert (row.is_delivered, row.is_read) == (True, False)
    await sender.disconnect()
    await recipient_presence.disconnect()


# --------------------------------------------------------------------------- #
# Scenario: recipient offline at send -> single tick; logging in (or any
# reconnect) flushes the backlog immediately — delivered, never read.
# --------------------------------------------------------------------------- #
@pytest.mark.django_db(transaction=True)
async def test_offline_messages_flushed_on_login_without_marking_read(user, other_user, user_cookie, other_user_cookie):
    pending = await sync_to_async(
        lambda: [MessageFactory(sender=user, receiver=other_user, text=f"offline {i}") for i in range(3)]
    )()

    sender = ws_connect(f"/ws/chat/{other_user.username}/", cookie=user_cookie)
    assert (await sender.connect())[0] is True
    await _drain(sender)
    # recipient has no live sockets at all: everything stays single-ticked
    assert await sender.receive_nothing(timeout=0.3) is True
    undelivered = await sync_to_async(
        lambda: Message.objects.filter(id__in=[m.id for m in pending], is_delivered=False).count()
    )()
    assert undelivered == 3

    # recipient logs in -> presence socket opens -> immediate delivery sync
    recipient = ws_connect("/ws/presence/", cookie=other_user_cookie)
    assert (await recipient.connect())[0] is True
    await _drain(recipient)

    receipt = await sender.receive_json_from()
    assert receipt == {"type": "message_delivered", "message_ids": [m.id for m in pending]}
    flags = await sync_to_async(
        lambda: list(Message.objects.filter(id__in=[m.id for m in pending]).values_list("is_read", "is_delivered"))
    )()
    assert flags == [(False, True), (False, True), (False, True)]  # login never marks read
    await sender.disconnect()
    await recipient.disconnect()


@pytest.mark.django_db(transaction=True)
async def test_reconnect_after_interruption_syncs_messages_missed_while_away(
    user, other_user, user_cookie, other_user_cookie
):
    recipient = ws_connect("/ws/presence/", cookie=other_user_cookie)
    assert (await recipient.connect())[0] is True
    await _drain(recipient)
    await recipient.disconnect()  # network interruption

    missed = await sync_to_async(lambda: MessageFactory(sender=user, receiver=other_user, text="while you were away"))()

    sender = ws_connect(f"/ws/chat/{other_user.username}/", cookie=user_cookie)
    assert (await sender.connect())[0] is True
    await _drain(sender)
    assert await sender.receive_nothing(timeout=0.3) is True  # still single-ticked

    # network returns -> the recipient's presence socket reconnects
    reborn = ws_connect("/ws/presence/", cookie=other_user_cookie)
    assert (await reborn.connect())[0] is True
    await _drain(reborn)

    receipt = await sender.receive_json_from()
    assert receipt == {"type": "message_delivered", "message_ids": [missed.id]}
    row = await sync_to_async(Message.objects.get)(pk=missed.pk)
    assert (row.is_delivered, row.is_read) == (True, False)
    await sender.disconnect()
    await reborn.disconnect()


# --------------------------------------------------------------------------- #
# Scenario: multiple sessions per recipient -> exactly ONE receipt (no dupes)
# --------------------------------------------------------------------------- #
@pytest.mark.django_db(transaction=True)
async def test_multiple_recipient_sessions_produce_exactly_one_receipt(
    user, other_user, user_cookie, other_user_cookie
):
    tab1 = ws_connect("/ws/presence/", cookie=other_user_cookie)
    tab2 = ws_connect("/ws/presence/", cookie=other_user_cookie)
    dm = ws_connect(f"/ws/chat/{user.username}/", cookie=other_user_cookie)
    for ws in (tab1, tab2, dm):
        assert (await ws.connect())[0] is True
    await _drain(tab1), await _drain(tab2), await _drain(dm)

    sender = ws_connect(f"/ws/chat/{other_user.username}/", cookie=user_cookie)
    assert (await sender.connect())[0] is True
    await _drain(sender)

    await sender.send_json_to({"type": "message", "text": "one receipt only"})
    echo = await sender.receive_json_from()
    assert echo["type"] == "message"
    receipt = await sender.receive_json_from()
    assert receipt == {"type": "message_delivered", "message_ids": [echo["message"]["id"]]}
    # the other two sessions must not announce it again
    assert await sender.receive_nothing(timeout=0.4) is True

    row = await sync_to_async(Message.objects.get)(pk=echo["message"]["id"])
    assert (row.is_delivered, row.is_read) == (True, False)
    for ws in (tab1, tab2, dm, sender):
        await ws.disconnect()


# --------------------------------------------------------------------------- #
# Honesty rule: an "online" DB flag with no live socket must NOT deliver
# --------------------------------------------------------------------------- #
@pytest.mark.django_db(transaction=True)
async def test_online_flag_alone_never_delivers(user, other_user, user_cookie):
    await sync_to_async(type(other_user).objects.filter(pk=other_user.pk).update)(is_online=True)

    sender = ws_connect(f"/ws/chat/{other_user.username}/", cookie=user_cookie)
    assert (await sender.connect())[0] is True
    await _drain(sender)

    await sender.send_json_to({"type": "message", "text": "flag says online"})
    echo = await sender.receive_json_from()
    assert echo["message"]["is_delivered"] is False
    assert await sender.receive_nothing(timeout=0.3) is True  # no receipt
    row = await sync_to_async(Message.objects.get)(pk=echo["message"]["id"])
    assert row.is_delivered is False
    await sender.disconnect()


# --------------------------------------------------------------------------- #
# Scenario: recipient opens the DM after reconnecting -> read receipt
# --------------------------------------------------------------------------- #
@pytest.mark.django_db(transaction=True)
async def test_opening_conversation_after_reconnect_reads_and_receipts(
    user, other_user, user_cookie, other_user_cookie
):
    pending = await sync_to_async(lambda: MessageFactory(sender=user, receiver=other_user, text="read me later"))()

    sender = ws_connect(f"/ws/chat/{other_user.username}/", cookie=user_cookie)
    assert (await sender.connect())[0] is True
    await _drain(sender)
    assert await sender.receive_nothing(timeout=0.3) is True  # single tick while away

    # 1) reconnect (login): delivered, NOT read
    recipient_presence = ws_connect("/ws/presence/", cookie=other_user_cookie)
    assert (await recipient_presence.connect())[0] is True
    await _drain(recipient_presence)
    receipt = await sender.receive_json_from()
    assert receipt == {"type": "message_delivered", "message_ids": [pending.id]}
    assert (await sync_to_async(Message.objects.get)(pk=pending.pk)).is_read is False

    # 2) open the DM -> the conversation really is read now
    dm = ws_connect(f"/ws/chat/{user.username}/", cookie=other_user_cookie)
    assert (await dm.connect())[0] is True
    await _drain(dm)
    await _drain(sender)  # consume the dm-connect user_status announce

    await dm.send_json_to({"type": "read", "message_ids": [pending.id]})
    read_receipt = await sender.receive_json_from()
    assert read_receipt == {"type": "messages_read", "message_ids": [pending.id]}
    row = await sync_to_async(Message.objects.get)(pk=pending.pk)
    assert (row.is_delivered, row.is_read) == (True, True)
    await recipient_presence.disconnect()
    await dm.disconnect()
    await sender.disconnect()


# --------------------------------------------------------------------------- #
# Scenario: older accounts — legacy rows behave exactly like new ones
# --------------------------------------------------------------------------- #
@pytest.mark.django_db(transaction=True)
async def test_legacy_rows_and_live_exchange_tick_like_new_accounts(user, other_user, user_cookie, other_user_cookie):
    # messages sent long ago that never got their delivery flag backfilled
    # (pre-is_delivered schema): one read, one still unread
    legacy_read = await sync_to_async(
        lambda: MessageFactory(
            sender=user,
            receiver=other_user,
            text="legacy read",
            is_read=True,
            is_delivered=False,
        )
    )()
    legacy_unread = await sync_to_async(
        lambda: MessageFactory(
            sender=user,
            receiver=other_user,
            text="legacy unread",
            is_read=False,
            is_delivered=False,
        )
    )()

    sender = ws_connect(f"/ws/chat/{other_user.username}/", cookie=user_cookie)
    assert (await sender.connect())[0] is True
    await _drain(sender)

    # recipient comes online (presence only — the reported "older accounts
    # both online but single tick" situation) -> backlog heals instantly
    recipient = ws_connect("/ws/presence/", cookie=other_user_cookie)
    assert (await recipient.connect())[0] is True
    await _drain(recipient)
    flush_receipt = await sender.receive_json_from()
    assert flush_receipt == {
        "type": "message_delivered",
        "message_ids": [legacy_read.id, legacy_unread.id],
    }

    # a fresh live message also ticks instantly, exactly like on new accounts
    await sender.send_json_to({"type": "message", "text": "fresh on old ids"})
    echo = await sender.receive_json_from()
    assert echo["type"] == "message"
    live_receipt = await sender.receive_json_from()
    assert live_receipt == {"type": "message_delivered", "message_ids": [echo["message"]["id"]]}
    assert await sender.receive_nothing(timeout=0.3) is True  # and nothing duplicated

    flags = await sync_to_async(
        lambda: {
            "read": (
                Message.objects.get(pk=legacy_read.pk).is_read,
                Message.objects.get(pk=legacy_read.pk).is_delivered,
            ),
            "unread": (
                Message.objects.get(pk=legacy_unread.pk).is_read,
                Message.objects.get(pk=legacy_unread.pk).is_delivered,
            ),
        }
    )()
    assert flags["read"] == (True, True)  # read state untouched by the flush
    assert flags["unread"] == (False, True)  # delivered, still unread

    # recipient opens the DM -> read receipt for the unread legacy row
    dm = ws_connect(f"/ws/chat/{user.username}/", cookie=other_user_cookie)
    assert (await dm.connect())[0] is True
    await _drain(dm)
    await _drain(sender)  # dm-connect user_status announce

    await dm.send_json_to({"type": "read", "message_ids": [legacy_unread.id]})
    read_receipt = await sender.receive_json_from()
    assert read_receipt == {"type": "messages_read", "message_ids": [legacy_unread.id]}
    assert (await sync_to_async(Message.objects.get)(pk=legacy_unread.pk)).is_read is True

    await recipient.disconnect()
    await dm.disconnect()
    await sender.disconnect()


# --------------------------------------------------------------------------- #
# Clients must not be able to inject delivery state
# --------------------------------------------------------------------------- #
@pytest.mark.django_db(transaction=True)
async def test_client_cannot_inject_delivery_state(user, other_user, user_cookie):
    ws = ws_connect(f"/ws/chat/{other_user.username}/", cookie=user_cookie)
    assert (await ws.connect())[0] is True
    await _drain(ws)

    await ws.send_json_to({"type": "deliver_incoming", "message_id": 1, "room_group": "chat_1_2"})
    assert await ws.receive_nothing(timeout=0.3) is True  # unknown type: dropped

    # normal messaging still works afterwards
    await ws.send_json_to({"type": "message", "text": "still alive"})
    event = await ws.receive_json_from()
    assert event["message"]["text"] == "still alive"
    await ws.disconnect()
