"""Application-wide presence: connection counting + heartbeats.

Root cause this module fixes
----------------------------
Online status used to be written by two uncoordinated code paths — the
per-DM ``ChatConsumer`` and the global ``PresenceConsumer`` — each flipping
a plain ``is_online`` boolean on connect/disconnect. Consequences:

* closing ONE of several open tabs/DMs marked the user offline while other
  connections were still alive (the "switching DMs shows offline" bug),
* a socket that died without a clean disconnect left the flag stale forever,
* last_seen was only updated by whichever path happened to run last.

Rules implemented here:

* ``connection_count`` counts every open WebSocket that represents an active
  session (the presence socket open on each authenticated page + each
  per-conversation chat socket).
* A user is online iff ``connection_count > 0`` **and** the newest heartbeat
  is younger than ``STALE_AFTER``. Dead sockets self-heal: they are detected
  and cleared instead of ghosting the user as online.
* When the count reaches 0 we wait ``PRESENCE_OFFLINE_GRACE_SECONDS`` before
  declaring the user offline, so navigating between pages (which closes and
  re-opens sockets) never flashes "offline" to other users. last_seen is
  stamped with the actual departure time, not the post-grace time.
* ``is_online`` / ``last_seen`` remain the denormalized cache every existing
  query reads; these helpers keep them accurate and announce transitions on
  the global ``global_presence`` group.
"""

import asyncio
from datetime import timedelta

from asgiref.sync import async_to_sync
from channels.db import database_sync_to_async
from channels.layers import get_channel_layer
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db.models import Case, F, Q, Value, When
from django.utils import timezone

PRESENCE_GROUP = "global_presence"
# Client sends {"type": "ping"} every HEARTBEAT_INTERVAL seconds (see
# base.html). Three missed heartbeats => the connection is considered dead.
HEARTBEAT_INTERVAL = 30
STALE_AFTER = timedelta(seconds=HEARTBEAT_INTERVAL * 3)


# --------------------------------------------------------------------------- #
# Synchronous DB primitives — call via database_sync_to_async (or directly
# from sync views).
# --------------------------------------------------------------------------- #
def open_connection(user_id):
    """Register a new socket: count += 1, mark online, refresh heartbeat."""
    CustomUser = get_user_model()
    CustomUser.objects.filter(id=user_id).update(
        connection_count=F("connection_count") + 1,
        is_online=True,
        last_seen=None,
        presence_heartbeat=timezone.now(),
    )


def close_connection(user_id):
    """Register a closed socket; return the remaining count (floored at 0)."""
    CustomUser = get_user_model()
    CustomUser.objects.filter(id=user_id).update(
        connection_count=Case(
            When(connection_count__gt=0, then=F("connection_count") - 1),
            default=Value(0),
        )
    )
    return get_connection_count(user_id)


def get_connection_count(user_id):
    CustomUser = get_user_model()
    return CustomUser.objects.filter(id=user_id).values_list(
        "connection_count", flat=True
    ).first() or 0


def touch_heartbeat(user_id, min_interval=10):
    """Refresh the heartbeat, but at most once per ``min_interval`` seconds.

    Called on presence pings and on any chat-socket traffic, so a page whose
    presence socket died while the chat socket lives still stays fresh.
    """
    now = timezone.now()
    threshold = now - timedelta(seconds=min_interval)
    get_user_model().objects.filter(
        id=user_id,
        connection_count__gt=0,
    ).filter(
        Q(presence_heartbeat__lt=threshold) | Q(presence_heartbeat__isnull=True)
    ).update(presence_heartbeat=now)


def commit_offline(user_id, when=None):
    """Count is (still) 0: flip the cached flags.

    Returns ``(username, last_seen_iso)`` on success or ``None`` when the
    user reconnected in the meantime (the ``connection_count=0`` guard in
    the UPDATE prevents clobbering a fresh connection).
    """
    when = when or timezone.now()
    CustomUser = get_user_model()
    # ``is_online=True`` is part of the guard on purpose: after an intentional
    # logout (``force_offline``) the row is already offline, so a socket that
    # closes a moment later must not re-stamp ``last_seen`` or re-announce a
    # transition that was already broadcast.
    updated = CustomUser.objects.filter(
        id=user_id, connection_count=0, is_online=True
    ).update(
        is_online=False,
        last_seen=when,
    )
    if not updated:
        return None
    username = CustomUser.objects.filter(id=user_id).values_list(
        "username", flat=True
    ).first()
    if username is None:
        return None
    return username, when.isoformat()


def _stale_filter(cutoff):
    """Rows flagged online whose heartbeat is missing or too old."""
    return Q(presence_heartbeat__lt=cutoff) | Q(presence_heartbeat__isnull=True)


def user_group(user_id):
    """Channel-layer group that holds every open socket of one account.

    Both consumers join it on connect and leave on disconnect, so logout can
    fan a single ``force_disconnect`` out to all of the user's tabs/devices at
    once (see ``close_user_sockets``).
    """
    return f"user_presence_{user_id}"


def force_offline(user_id, when=None):
    """Intentional logout: clear ALL presence state for the account at once.

    Unlike ``commit_offline`` (which decrements one socket and waits for the
    grace period) this is authoritative: it zeroes ``connection_count`` for
    every tab/device, flips ``is_online`` off, and stamps ``last_seen`` at the
    logout moment. A socket that closes a moment later is a no-op — its
    ``connection_closed`` floors the count at 0 and ``commit_offline`` no-ops
    because the row is already offline.

    Returns ``(username, last_seen_iso)`` or ``None`` if the user is missing.
    """
    when = when or timezone.now()
    CustomUser = get_user_model()
    updated = CustomUser.objects.filter(id=user_id).update(
        connection_count=0,
        is_online=False,
        last_seen=when,
    )
    if not updated:
        return None
    username = CustomUser.objects.filter(id=user_id).values_list(
        "username", flat=True
    ).first()
    if username is None:
        return None
    return username, when.isoformat()


def sweep_stale():
    """Heal rows whose sockets died without disconnecting.

    Crashed sockets leave ``connection_count > 0`` behind, so unlike
    ``commit_offline`` this path forces the count back to 0. The UPDATE
    re-checks staleness in its WHERE clause, so a user who reconnected
    since we noticed is never clobbered.

    Returns ``[(user_id, username, last_seen_iso), ...]`` for every user
    flipped offline by this sweep.
    """
    now = timezone.now()
    cutoff = now - STALE_AFTER
    CustomUser = get_user_model()
    stale = list(
        CustomUser.objects.filter(is_online=True)
        .filter(_stale_filter(cutoff))
        .values("id", "username", "presence_heartbeat")
    )
    healed = []
    for row in stale:
        when = row["presence_heartbeat"] or now
        updated = CustomUser.objects.filter(id=row["id"], is_online=True).filter(
            _stale_filter(cutoff)
        ).update(is_online=False, connection_count=0, last_seen=when)
        if updated:
            healed.append((row["id"], row["username"], when.isoformat()))
    return healed


def effective_presence(user):
    """Return ``(is_online, last_seen_iso)`` for a user row, healing stale state.

    Reads never trust the cached flag alone: an "online" row must have a
    heartbeat younger than ``STALE_AFTER`` (freshness also covers the
    offline-grace window right after the last socket closed). Anything else
    is a ghost from a crashed socket or a legacy pre-migration row, and is
    corrected here with a conditional UPDATE that re-checks staleness.
    """
    if not user.is_online:
        return False, user.last_seen.isoformat() if user.last_seen else None

    now = timezone.now()
    cutoff = now - STALE_AFTER
    CustomUser = get_user_model()
    CustomUser.objects.filter(id=user.id, is_online=True).filter(
        _stale_filter(cutoff)
    ).update(is_online=False, connection_count=0,
             last_seen=user.presence_heartbeat or now)

    row = CustomUser.objects.filter(id=user.id).values(
        "is_online", "last_seen"
    ).first()
    if row is None:
        return False, None
    if row["is_online"]:
        return True, None
    last_seen = row["last_seen"]
    return False, last_seen.isoformat() if last_seen else None


def get_statuses(user_ids):
    """Bulk presence lookup for the ``get_status`` WebSocket request."""
    CustomUser = get_user_model()
    result = {}
    for user in CustomUser.objects.filter(id__in=user_ids):
        is_online, last_seen = effective_presence(user)
        result[str(user.id)] = {
            "status": "online" if is_online else "offline",
            "last_seen": last_seen,
        }
    return result


# --------------------------------------------------------------------------- #
# Announcements on the global presence group
# --------------------------------------------------------------------------- #
async def announce(user_id, username, status, last_seen=None):
    layer = get_channel_layer()
    if layer is None:
        return
    try:
        await layer.group_send(
            PRESENCE_GROUP,
            {
                "type": "presence_update",
                "user_id": user_id,
                "username": username,
                "status": status,
                "last_seen": last_seen,
            },
        )
    except Exception:  # never let presence break a socket
        pass


def announce_sync(user_id, username, status, last_seen=None):
    """Synchronous wrapper for use from views."""
    async_to_sync(announce)(user_id, username, status, last_seen)


def force_offline_and_announce(user_id, username, when=None):
    """Synchronous logout path: mark offline in the DB, then broadcast it.

    Returns the ``(username, last_seen_iso)`` written, or ``None`` if the row
    vanished. Called from ``logout_view`` so every connected viewer learns the
    account left **immediately**, instead of waiting for socket teardown.
    """
    result = force_offline(user_id, when)
    if result is None:
        return None
    _, last_seen = result
    announce_sync(user_id, username, "offline", last_seen)
    return result


def close_user_sockets(user_id):
    """Tell every open socket of this account to close (best effort).

    Sends ``force_disconnect`` to the per-user group both consumers join. The
    group layer drops the message when the layer is missing or the user has no
    open sockets, so this is always safe to call.
    """
    layer = get_channel_layer()
    if layer is None:
        return

    async def _close():
        group = user_group(user_id)
        try:
            await layer.group_send(group, {"type": "force_disconnect"})
        except Exception:
            pass  # no sockets / no layer: nothing to close

    try:
        async_to_sync(_close)()
    except Exception:
        pass  # closing sockets is best-effort; the DB already says offline


async def sweep_and_announce():
    """Run the stale-socket sweep and announce everyone it healed offline."""
    healed = await database_sync_to_async(sweep_stale)()
    for user_id, username, last_seen in healed:
        await announce(user_id, username, "offline", last_seen)
    return healed


# --------------------------------------------------------------------------- #
# Async orchestration used by both consumers
# --------------------------------------------------------------------------- #
async def connection_opened(user_id, username):
    """A socket opened: count it and announce the user online (idempotent)."""
    await database_sync_to_async(open_connection)(user_id)
    await announce(user_id, username, "online")


async def connection_closed(user_id, username):
    """A socket closed: uncount it; if it was the last one (and stays closed
    for the grace period), mark offline and announce.

    Returns the ``last_seen`` ISO string when the user actually went
    offline, otherwise ``None``.
    """
    count = await database_sync_to_async(close_connection)(user_id)
    if count > 0:
        return None  # other tabs/devices are still connected

    offline_at = timezone.now()
    grace = getattr(settings, "PRESENCE_OFFLINE_GRACE_SECONDS", 0)
    if grace > 0:
        # Give page navigation / reconnects a chance to re-open a socket
        # before telling everyone the user left.
        await asyncio.sleep(grace)
        count = await database_sync_to_async(get_connection_count)(user_id)
        if count > 0:
            return None  # reconnected during the grace period

    result = await database_sync_to_async(commit_offline)(user_id, offline_at)
    if result is None:
        return None
    _, last_seen = result
    await announce(user_id, username, "offline", last_seen)
    return last_seen
