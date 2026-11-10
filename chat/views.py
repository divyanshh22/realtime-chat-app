import logging
import redis
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.conf import settings
from django.contrib import messages as django_messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from login.models import CustomUser, ProfilePic
from login.utils import is_valid_image_upload

from . import delivery, presence
from .models import Message

logger = logging.getLogger(__name__)


def _redis_client():
    if not getattr(settings, 'USE_REDIS', False):
        return None
    return redis.Redis.from_url(
        settings.REDIS_URL,
        decode_responses=True,
        socket_connect_timeout=1,
        socket_timeout=1,
    )


def _build_home_inbox_context(request):
    ProfilePic.objects.get_or_create(user=request.user)

    all_messages = Message.objects.filter(
        Q(sender=request.user) | Q(receiver=request.user)
    ).select_related('sender', 'receiver').order_by('-timestamp')

    user_ids = set()
    latest_message_by_user = {}
    unread_count_by_user = {}

    for msg in all_messages:
        if msg.sender != request.user:
            user_ids.add(msg.sender.id)
            latest_message_by_user[msg.sender.id] = msg
            if not msg.is_read and msg.receiver == request.user:
                unread_count_by_user[msg.sender.id] = unread_count_by_user.get(msg.sender.id, 0) + 1
        if msg.receiver != request.user:
            user_ids.add(msg.receiver.id)
            if msg.receiver.id not in latest_message_by_user or msg.timestamp > latest_message_by_user[msg.receiver.id].timestamp:
                latest_message_by_user[msg.receiver.id] = msg

    chat_users = list(CustomUser.objects.filter(id__in=user_ids))

    for user in chat_users:
        ProfilePic.objects.get_or_create(user=user)
        last_message = latest_message_by_user.get(user.id)
        user.last_message = last_message
        user.last_message_preview = (last_message.text or 'Photo') if last_message else ''
        user.last_message_time = last_message.timestamp if last_message else None
        user.unread_count = unread_count_by_user.get(user.id, 0)

    chat_users.sort(key=lambda user: (user.last_message.timestamp if user.last_message else timezone.datetime.min.replace(tzinfo=timezone.utc)), reverse=True)

    online_users = {}
    last_seen = {}
    r = _redis_client()
    if r is not None:
        try:
            online_users = r.hgetall('online_users')
            last_seen = r.hgetall('last_seen')
        except Exception:
            pass

    for user in chat_users:
        # Redis (if configured) is the freshest signal and wins, exactly as
        # before; the DB flag is only trusted through effective_presence,
        # which heals stale ghosts (crashed sockets / legacy rows) instead of
        # showing them as online forever.
        db_online, db_last_seen = presence.effective_presence(user)
        user.is_online = online_users.get(str(user.id)) == 'online' or db_online
        user.last_seen_str = last_seen.get(str(user.id)) or db_last_seen or user.last_seen

    return {'chat_users': chat_users}


def get_room_group_name(user_id_1, user_id_2):
    """Canonical DM group name — delegates to chat.delivery (single source)."""
    return delivery.room_group_name(user_id_1, user_id_2)


def get_user_presence(user_id):
    """Effective presence: never trusts a stale ``is_online`` flag.

    ``presence.effective_presence`` self-heals ghosts (crashed sockets,
    legacy rows) so a viewer always gets the truth.
    """
    user = CustomUser.objects.filter(id=user_id).first()
    if user:
        return presence.effective_presence(user)

    r = _redis_client()
    if r is not None:
        try:
            is_online = r.hget('online_users', str(user_id)) == 'online'
            last_seen = r.hget('last_seen', str(user_id))
            return is_online, last_seen
        except Exception:
            pass
    return False, None


def _broadcast_receipts(user_id_1, user_id_2, message_ids):
    """Tell the whole room (especially the sender) about delivery + read.

    Used by the conversation GET: opening the page delivers and reads the
    incoming messages, and the sender must see the ticks update live
    instead of only after they reopen the DM themselves.
    """
    if not message_ids:
        return
    try:
        channel_layer = get_channel_layer()
        if channel_layer is None:
            return
        group = get_room_group_name(user_id_1, user_id_2)
        async_to_sync(channel_layer.group_send)(
            group, {'type': 'message_delivered', 'message_ids': message_ids}
        )
        async_to_sync(channel_layer.group_send)(
            group, {'type': 'messages_read', 'message_ids': message_ids}
        )
    except Exception:
        pass


@login_required(login_url='login-view')
def home_view(request):
    context = _build_home_inbox_context(request)
    return render(request, 'chat/home.html', context)


@login_required(login_url='login-view')
@require_http_methods(["GET"])
def home_inbox_api(request):
    context = _build_home_inbox_context(request)
    payload = []
    for user in context['chat_users']:
        payload.append({
            'id': user.id,
            'username': user.username,
            'preview': user.last_message_preview,
            'last_message_time': user.last_message_time.isoformat() if user.last_message_time else None,
            'unread_count': user.unread_count,
            'is_online': user.is_online,
            'profile_pic_url': user.profilepic.profile_pic.url if getattr(user.profilepic, 'profile_pic', None) else None,
            'conversation_url': f"/chat/{user.username}/",
        })
    return JsonResponse({'users': payload})



@login_required(login_url='login-view')
def conversation_view(request, username):
    other_user = get_object_or_404(CustomUser, username=username)
    ProfilePic.objects.get_or_create(user=other_user)

    # dono ke beech ke messages
    messages = Message.objects.filter(
        Q(sender=request.user, receiver=other_user) |
        Q(sender=other_user, receiver=request.user)
    ).order_by('timestamp')

    # Opening the conversation means the receiver has it open: mark incoming
    # messages delivered + read, and broadcast the transition so the sender's
    # open chat updates its ticks instantly (was: sender never found out
    # until they reopened the DM themselves).
    changed_ids = list(
        Message.objects.filter(sender=other_user, receiver=request.user)
        .filter(Q(is_read=False) | Q(is_delivered=False))
        .values_list('id', flat=True)
    )
    if changed_ids:
        Message.objects.filter(id__in=changed_ids).update(is_read=True, is_delivered=True)
        _broadcast_receipts(request.user.id, other_user.id, changed_ids)

    # POST - naya message save karo
    if request.method == 'POST':
        # NUL bytes would crash PostgreSQL inserts (psycopg2 ValueError)
        text = request.POST.get('text', '').replace('\x00', '').strip()
        photo = request.FILES.get('photo')

        if photo and not is_valid_image_upload(photo):
            # NB: aliased import — this function has a local `messages` QuerySet
            django_messages.error(request, "Please upload a valid image (JPG, PNG, WEBP) under 5 MB.")
            return redirect('chat:conversation-view', username=username)

        if text or photo:
            try:
                message = _create_message(
                    sender=request.user,
                    receiver=other_user,
                    text=text,
                    photo=photo,
                )
            except Exception:
                # Storage failure (e.g. Cloudinary rejecting credentials)
                # must not take the whole page down — log the real
                # traceback and tell the user plainly.
                logger.exception("Failed to save message photo for %s", request.user.username)
                django_messages.error(
                    request,
                    "Couldn't send that — the file storage service is unavailable. Please try again."
                )
                return redirect('chat:conversation-view', username=username)

            # Broadcast over WebSocket so the other user gets it in real time
            room_group = get_room_group_name(request.user.id, other_user.id)
            try:
                channel_layer = get_channel_layer()
                async_to_sync(channel_layer.group_send)(room_group, {
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
                })
                # ...and push a delivery hint to the RECIPIENT's personal
                # group: whichever of their live sockets (presence page,
                # open DMs, other tabs) receives it flags the message
                # delivered and announces the receipt to this room. With no
                # socket open the group is empty and nothing is marked.
                async_to_sync(channel_layer.group_send)(
                    presence.user_group(other_user.id),
                    {
                        'type': 'deliver_incoming',
                        'message_id': message.id,
                        'room_group': room_group,
                    },
                )
            except Exception:
                pass
        return redirect('chat:conversation-view', username=username)

    # Get online status from DB first, with Redis as a fallback
    other_user.is_online, other_user.last_seen_str = get_user_presence(other_user.id)

    return render(request, 'chat/conversation.html', {
        'other_user': other_user,
        # NB: NOT 'messages' — that name belongs to the Django messages
        # context processor (flash notices). Shadowing it meant storage/
        # validation errors were added but could never render.
        'chat_messages': messages,
    })


@login_required(login_url='login-view')
def search_view(request):
    query = request.GET.get('q', '').strip()
    results = []

    if query:
        results = CustomUser.objects.filter(
            username__icontains=query
        ).exclude(id=request.user.id)  # khud ko exclude karo

    return render(request, 'chat/search.html', {
        'results': results,
        'query': query,
    })


def _create_message(sender, receiver, text, photo=None):
    """Persist a message, storing any photo BEFORE the DB row.

    ``Model.save()`` writes the file inside ``mark_for_rollback_on_error``,
    so a storage outage (e.g. Cloudinary rejecting credentials) would poison
    the surrounding transaction and turn a handled error into a 500. Writing
    the file first keeps the two steps independent: storage failure = no row,
    DB failure = clean exception with no half-stored file attached.
    """
    message = Message(sender=sender, receiver=receiver, text=text)
    if photo:
        message.photo.save(photo.name, photo, save=False)
    message.save()
    return message


@login_required(login_url='login-view')
@require_http_methods(["POST"])
def send_message_api(request, username):
    other_user = get_object_or_404(CustomUser, username=username)
    # NUL bytes would crash PostgreSQL inserts (psycopg2 ValueError)
    text = request.POST.get('text', '').replace('\x00', '').strip()
    photo = request.FILES.get('photo')

    if photo and not is_valid_image_upload(photo):
        return JsonResponse({'error': 'Invalid image file'}, status=400)

    if not text and not photo:
        return JsonResponse({'error': 'Message cannot be empty'}, status=400)

    try:
        message = _create_message(
            sender=request.user,
            receiver=other_user,
            text=text,
            photo=photo,
        )
    except Exception:
        # Storage failure (e.g. Cloudinary rejecting credentials) — log the
        # real traceback and return a clean JSON error the JS can display.
        logger.exception("Failed to save message for %s", request.user.username)
        return JsonResponse(
            {'error': 'Could not send: the file storage service is unavailable. Please try again.'},
            status=502,
        )

    room_group_name = get_room_group_name(request.user.id, other_user.id)
    payload = {
        'type': 'message',
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

    try:
        channel_layer = get_channel_layer()
        async_to_sync(channel_layer.group_send)(room_group_name, {
            'type': 'chat_message',
            'message': payload['message'],
        })
        # Delivery hint to the recipient's personal group (presence socket +
        # every open chat socket). If none of their sessions is live the
        # group doesn't exist and the message honestly stays single-ticked
        # until they reconnect (the presence flush then delivers it).
        async_to_sync(channel_layer.group_send)(
            presence.user_group(other_user.id),
            {
                'type': 'deliver_incoming',
                'message_id': message.id,
                'room_group': room_group_name,
            },
        )
    except Exception:
        pass

    return JsonResponse(payload)


@login_required(login_url='login-view')
@require_http_methods(["GET"])
def get_messages_api(request, username):
    """API endpoint to fetch messages for WebSocket initialization"""
    other_user = get_object_or_404(CustomUser, username=username)

    messages = Message.objects.filter(
        Q(sender=request.user, receiver=other_user) |
        Q(sender=other_user, receiver=request.user)
    ).order_by('timestamp')

    messages_data = []
    for msg in messages:
        messages_data.append({
            'id': msg.id,
            'sender_id': msg.sender.id,
            'sender_username': msg.sender.username,
            'text': msg.text,
            'photo_url': msg.photo.url if msg.photo else None,
            'timestamp': msg.timestamp.isoformat(),
            'is_read': msg.is_read,
            'is_delivered': msg.is_delivered,
        })

    return JsonResponse({'messages': messages_data})


@login_required(login_url='login-view')
@require_http_methods(["GET"])
def get_user_status_api(request, user_id):
    """API endpoint to get user online status"""
    try:
        user = CustomUser.objects.get(id=user_id)
        is_online = False
        last_seen = None
        is_online, last_seen = get_user_presence(user_id)

        return JsonResponse({
            'user_id': user.id,
            'username': user.username,
            'is_online': is_online,
            'last_seen': last_seen
        })
    except CustomUser.DoesNotExist:
        return JsonResponse({'error': 'User not found'}, status=404)
