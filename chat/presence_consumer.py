import json

from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async

from . import delivery, presence


class PresenceConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.user = self.scope["user"]

        if not self.user.is_authenticated:
            await self.close()
            return

        await self.channel_layer.group_add(presence.PRESENCE_GROUP, self.channel_name)
        # Per-user group so logout can force-close every socket of this
        # account at once (all tabs/devices) — see presence.close_user_sockets.
        # It is also the delivery channel: senders push deliver_incoming here
        # so any live session of this account (presence page or open DM)
        # flags incoming messages delivered.
        try:
            await self.channel_layer.group_add(
                presence.user_group(self.user.id), self.channel_name
            )
        except Exception:
            pass

        # Heal sockets that died without disconnecting BEFORE announcing, so
        # a connecting client immediately sees corrected statuses.
        await presence.sweep_and_announce()

        # Counted, not blindly set: other tabs/devices may already be online.
        await presence.connection_opened(self.user.id, self.user.username)

        await self.accept()

        # Login/reconnect synchronisation: hand the whole inbox backlog to
        # this newly live session — every undelivered message is flagged and
        # each conversation gets its receipt immediately, without the user
        # opening the DM. Only is_delivered changes: reading still requires
        # actually opening the conversation.
        backlog = await database_sync_to_async(delivery.flush_undelivered)(self.user.id)
        for room_group, message_ids in backlog:
            try:
                await self.channel_layer.group_send(
                    room_group,
                    {"type": "message_delivered", "message_ids": message_ids},
                )
            except Exception:
                pass

    async def disconnect(self, close_code):
        if hasattr(self, "user") and self.user.is_authenticated:
            try:
                await self.channel_layer.group_discard(presence.PRESENCE_GROUP, self.channel_name)
            except Exception:
                pass
            try:
                await self.channel_layer.group_discard(
                    presence.user_group(self.user.id), self.channel_name
                )
            except Exception:
                pass
            # Marks offline + announces only when this was the last
            # connection (after the navigation grace period). A logout has
            # already forced the row offline, so this becomes a no-op there.
            await presence.connection_closed(self.user.id, self.user.username)

    async def force_disconnect(self, event):
        # The account logged out (or was invalidated) — close this socket.
        # The DB presence was already forced offline by the logout view.
        await self.close()

    async def deliver_incoming(self, event):
        """A sender pushed a delivery hint to this account's personal group.

        The presence socket (open on every authenticated page) is a live
        session too: receiving the hint means the message really was handed
        to the user's device, so it counts as delivered. Only the socket
        that wins the conditional UPDATE announces the receipt — N open
        sessions still produce exactly one ``message_delivered`` frame.
        """
        message_id = event.get("message_id")
        room_group = event.get("room_group")
        if not isinstance(message_id, int) or isinstance(message_id, bool):
            return
        if not isinstance(room_group, str) or not room_group:
            return
        delivered_ids = await database_sync_to_async(delivery.mark_delivered)(self.user.id, message_id)
        if not delivered_ids:
            return
        try:
            await self.channel_layer.group_send(
                room_group,
                {"type": "message_delivered", "message_ids": delivered_ids},
            )
        except Exception:
            pass

    async def receive(self, text_data=None, bytes_data=None):
        if text_data is None:
            return  # binary frames are ignored

        try:
            data = json.loads(text_data)
        except (ValueError, TypeError):
            return  # malformed JSON is dropped instead of crashing
        if not isinstance(data, dict):
            return

        message_type = data.get("type")

        # Heartbeat from the page: proves this tab is alive, and gives us a
        # regular beat to sweep other users' dead sockets too.
        if message_type == "ping":
            await database_sync_to_async(presence.touch_heartbeat)(self.user.id)
            await presence.sweep_and_announce()
            return

        # Client can ask "who's online right now" when the page loads
        if message_type == "get_status":
            user_ids = data.get("user_ids", [])
            if not isinstance(user_ids, (list, tuple)):
                return
            # keep only real integer ids — strings/None/bools would crash the ORM
            valid_ids = [
                uid for uid in user_ids
                if isinstance(uid, int) and not isinstance(uid, bool)
            ]
            statuses = await database_sync_to_async(presence.get_statuses)(valid_ids)
            await self.send(text_data=json.dumps({
                "type": "bulk_status",
                "statuses": statuses
            }))

    async def presence_update(self, event):
        await self.send(text_data=json.dumps(event))
