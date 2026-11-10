"""Server-side delivery-state synchronisation (the ✓ -> ✓✓ -> green ✓✓ ticks).

Root cause this module fixes
----------------------------
``Message.is_delivered`` used to be written only by code paths tied to ONE
specific conversation socket:

* ``ChatConsumer.chat_message`` — runs only on the recipient's open DM,
* ``ChatConsumer.connect`` / ``_deliver_pending`` — only when the recipient
  opens THAT conversation,
* ``conversation_view`` GET — only when the recipient loads the DM page.

A recipient who was online anywhere else (home page, another DM, second
tab) received nothing, so the sender kept a single tick until the recipient
happened to open that exact conversation. Long-lived accounts looked
"broken" because their accumulated flags were never re-synced — the data
was fine, the event routing was.

Rules implemented here:

* As soon as a message is saved, a ``deliver_incoming`` event is pushed to
  the RECIPIENT's personal group (``user_presence_<id>``) — the group that
  both their presence socket and every open chat socket join on connect.
* Whichever live socket receives the event flips ``is_delivered`` with a
  conditional UPDATE and — only for the socket that won the flip —
  broadcasts ``message_delivered`` to the conversation group, producing
  exactly one receipt no matter how many tabs the recipient has open.
* Delivery therefore means "handed to an actively connected session of the
  receiver": no open socket => the group is empty, the event is dropped and
  the message honestly stays single-ticked. A stale ``is_online`` DB flag
  alone never delivers anything.
* ``flush_undelivered`` runs when a presence socket (re)connects — login,
  refresh, network recovery — and pushes the whole inbox backlog to its
  rooms. It touches ONLY ``is_delivered``; reading still requires the
  conversation to actually be opened.
"""

from .models import Message


def room_group_name(user_id_1, user_id_2):
    """Canonical channel-layer group for the DM between two users.

    Single source of truth: ``views.get_room_group_name`` and
    ``ChatConsumer._get_room_group_name`` both delegate here, so HTTP and
    WebSocket paths can never drift apart (a drift would mean receipts
    broadcast to a group the sender is not listening on).
    """
    return f"chat_{min(user_id_1, user_id_2)}_{max(user_id_1, user_id_2)}"


def mark_delivered(user_id, message_id):
    """Flag one incoming message as delivered for ``user_id``.

    Returns ``[message_id]`` only for the caller that actually flipped the
    flag — every other concurrent caller (second tab, the chat_message
    handler, the presence flush) gets ``[]``, so the ``message_delivered``
    receipt is broadcast exactly once no matter how many of the receiver's
    sockets process the same event.

    Never touches ``is_read``: reading requires opening the conversation.
    """
    if not isinstance(message_id, int) or isinstance(message_id, bool):
        return []
    flipped = Message.objects.filter(
        id=message_id, receiver_id=user_id, is_delivered=False,
    ).update(is_delivered=True)
    return [message_id] if flipped else []


def flush_undelivered(user_id):
    """Deliver this account's whole inbox backlog; return its receipts.

    Marks every still-undelivered message addressed to ``user_id`` and
    returns ``[(room_group, [message_ids]), ...]`` so the caller can push a
    ``message_delivered`` receipt into each conversation. Rooms whose rows
    were all flipped by a concurrent session are omitted (the UPDATE is
    conditional), so a reconnecting user never re-announces deliveries twice.

    Only ``is_delivered`` is ever written — never ``is_read``.
    """
    pending = list(
        Message.objects.filter(receiver_id=user_id, is_delivered=False)
        .values_list("id", "sender_id")
    )
    if not pending:
        return []

    by_room = {}
    for message_id, sender_id in pending:
        by_room.setdefault(room_group_name(sender_id, user_id), []).append(message_id)

    receipts = []
    for room, ids in sorted(by_room.items()):
        flipped = Message.objects.filter(
            id__in=ids, receiver_id=user_id, is_delivered=False,
        ).update(is_delivered=True)
        if flipped:
            receipts.append((room, ids))
    return receipts
