import json
from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.core.exceptions import ObjectDoesNotExist
from django.utils import timezone

from . import presence
from .models import Message
from login.models import CustomUser


class ChatConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.other_username = self.scope['url_route']['kwargs']['username']
        self.me = self.scope['user']

        if not self.me or not self.me.is_authenticated:
            await self.close()
            return

        try:
            self.other_user = await database_sync_to_async(CustomUser.objects.get)(username=self.other_username)
        except ObjectDoesNotExist:
            await self.close()
            return

        self.room_group_name = self._get_room_group_name(self.me.id, self.other_user.id)
        try:
            await self.channel_layer.group_add(self.room_group_name, self.channel_name)
        except Exception:
            pass

        # Per-user group so logout can force-close every socket of this
        # account (this DM + the presence socket) in one shot.
        try:
            await self.channel_layer.group_add(
                presence.user_group(self.me.id), self.channel_name
            )
        except Exception:
            pass

        # Presence is counted (not blindly set): the user may have other
        # tabs/devices connected — see chat/presence.py.
        await presence.connection_opened(self.me.id, self.me.username)
        await self.accept()
        await self._announce_status('online')

        # Messages that arrived while this conversation was closed become
        # "delivered" the moment the receiver opens it (double tick).
        delivered_ids = await database_sync_to_async(self._deliver_pending)()
        if delivered_ids:
            await self._broadcast_delivered(delivered_ids)

    async def disconnect(self, close_code):
        if not hasattr(self, 'room_group_name'):
            # connect() never completed (no prior connect, anonymous user, or
            # unknown chat partner) — there is nothing to clean up.
            return

        try:
            await self.channel_layer.group_discard(self.room_group_name, self.channel_name)
        except Exception:
            pass
        try:
            await self.channel_layer.group_discard(
                presence.user_group(self.me.id), self.channel_name
            )
        except Exception:
            pass

        # Only announce "offline" to this room when the user is really gone
        # (last connection closed and the grace period expired). After a
        # logout the row is already offline, so this is a harmless no-op.
        last_seen = await presence.connection_closed(self.me.id, self.me.username)
        if last_seen is not None:
            await self._announce_status('offline', last_seen)

    async def force_disconnect(self, event):
        # The account logged out (or was invalidated) — close this socket.
        # DB presence was already forced offline by the logout view, so the
        # disconnect below will not re-stamp or re-announce anything.
        await self.close()

    async def receive(self, text_data=None, bytes_data=None):
        if text_data is None:
            return  # binary frames are ignored

        try:
            payload = json.loads(text_data)
        except (ValueError, TypeError):
            return  # malformed JSON is dropped instead of crashing the consumer
        if not isinstance(payload, dict):
            return

        # Any traffic proves this socket is alive (throttled server-side).
        await database_sync_to_async(presence.touch_heartbeat)(self.me.id)

        message_type = payload.get('type')

        if message_type == 'message':
            await self._handle_message(payload)
        elif message_type == 'typing':
            await self._broadcast_typing(payload.get('is_typing', False))
        elif message_type == 'read':
            await self._mark_messages_read(payload.get('message_ids', []))

    async def chat_message(self, event):
        await self.send(text_data=json.dumps({
            'type': 'message',
            'message': event['message'],
        }))

        # The receiver just got the message on an OPEN conversation => it is
        # delivered (single tick -> double tick), announced to the room so
        # the sender's client updates instantly without a refresh.
        message = event.get('message') or {}
        if message.get('sender_id') != getattr(self.me, 'id', None):
            delivered_ids = await database_sync_to_async(self._mark_delivered)(message.get('id'))
            if delivered_ids:
                await self._broadcast_delivered(delivered_ids)

    async def message_delivered(self, event):
        await self.send(text_data=json.dumps({
            'type': 'message_delivered',
            'message_ids': event['message_ids'],
        }))

    async def user_status(self, event):
        await self.send(text_data=json.dumps({
            'type': 'user_status',
            'status': event['status'],
            'last_seen': event.get('last_seen'),
            'username': event.get('username'),
        }))

    async def typing_status(self, event):
        await self.send(text_data=json.dumps({
            'type': 'typing',
            'username': event['username'],
            'is_typing': event['is_typing'],
        }))

    async def messages_read(self, event):
        await self.send(text_data=json.dumps({
            'type': 'messages_read',
            'message_ids': event['message_ids'],
        }))

    @staticmethod
    def _get_room_group_name(user_id_1, user_id_2):
        return f"chat_{min(user_id_1, user_id_2)}_{max(user_id_1, user_id_2)}"

    async def _announce_status(self, status, last_seen=None):
        now = timezone.now().isoformat()
        try:
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'user_status',
                    'status': status,
                    'last_seen': last_seen or (now if status == 'offline' else None),
                    'username': self.me.username,
                }
            )
        except Exception:
            pass

    async def _broadcast_delivered(self, message_ids):
        try:
            await self.channel_layer.group_send(
                self.room_group_name,
                {'type': 'message_delivered', 'message_ids': message_ids},
            )
        except Exception:
            pass

    async def _broadcast_typing(self, is_typing):
        try:
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'typing_status',
                    'username': self.me.username,
                    'is_typing': is_typing,
                }
            )
        except Exception:
            pass

    async def _handle_message(self, payload):
        text = payload.get('text')
        if not isinstance(text, str):
            return  # non-string text (int, list, dict, ...) is dropped
        # NUL bytes would crash PostgreSQL inserts (psycopg2 ValueError)
        text = text.replace('\x00', '').strip()
        if not text:
            return

        message = await database_sync_to_async(self._create_message)(text=text)
        try:
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'chat_message',
                    'message': {
                        'id': message.id,
                        'sender_id': message.sender.id,
                        'sender_username': message.sender.username,
                        'text': message.text,
                        'photo_url': message.photo.url if message.photo else None,
                        'timestamp': message.timestamp.isoformat(),
                        'is_read': message.is_read,
                        'is_delivered': message.is_delivered,
                    },
                }
            )
        except Exception:
            pass

    def _create_message(self, text):
        return Message.objects.create(
            sender=self.me,
            receiver=self.other_user,
            text=text,
        )

    def _deliver_pending(self):
        """Mark messages that waited in the inbox as delivered (ids returned)."""
        ids = list(
            Message.objects.filter(
                sender=self.other_user, receiver=self.me, is_delivered=False,
            ).values_list('id', flat=True)
        )
        if ids:
            Message.objects.filter(id__in=ids, is_delivered=False).update(is_delivered=True)
        return ids

    def _mark_delivered(self, message_id):
        """Atomically deliver one incoming message; only the connection that
        actually flips the flag broadcasts, so multi-tab receivers don't
        duplicate the announcement."""
        if not isinstance(message_id, int) or isinstance(message_id, bool):
            return []
        updated = Message.objects.filter(
            id=message_id, receiver=self.me, is_delivered=False,
        ).update(is_delivered=True)
        return [message_id] if updated else []

    async def _mark_messages_read(self, message_ids):
        if not isinstance(message_ids, (list, tuple)):
            return
        # keep only real integer ids — strings/None/bools would crash the ORM
        ids = [mid for mid in message_ids if isinstance(mid, int) and not isinstance(mid, bool)]
        if not ids:
            return

        updated = await database_sync_to_async(self._mark_ids_as_read)(ids)
        if not updated:
            return

        try:
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'messages_read',
                    'message_ids': ids,
                }
            )
        except Exception:
            pass

    def _mark_ids_as_read(self, message_ids):
        # read implies delivered — keep the two flags consistent even when
        # the read ack races ahead of the delivery marking
        return Message.objects.filter(
            id__in=message_ids, receiver=self.me,
        ).update(is_read=True, is_delivered=True)
