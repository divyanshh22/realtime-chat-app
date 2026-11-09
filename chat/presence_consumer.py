import json
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async

from . import presence


class PresenceConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.user = self.scope["user"]

        if not self.user.is_authenticated:
            await self.close()
            return

        await self.channel_layer.group_add(presence.PRESENCE_GROUP, self.channel_name)
        # Per-user group so logout can force-close every socket of this
        # account at once (all tabs/devices) — see presence.close_user_sockets.
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
