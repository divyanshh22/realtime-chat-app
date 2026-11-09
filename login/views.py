from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth import login, logout, authenticate
from django.contrib.auth.decorators import login_required
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
import logging
from .models import CustomUser, ProfilePic
from .utils import is_valid_image_upload

logger = logging.getLogger(__name__)


def login_view(request):
    if request.user.is_authenticated:
        return redirect('chat:home-view')

    if request.method == 'POST':
        username = request.POST.get('username', '').strip()
        password = request.POST.get('password', '')

        if not username or not password:
            messages.error(request, "Username and password are required!")
            return redirect('login-view')

        user = authenticate(request, username=username, password=password)

        if user is not None:
            login(request, user)
            ProfilePic.objects.get_or_create(user=user)
            messages.success(request, f"Welcome, {user.first_name}!")
            return redirect('chat:home-view')
        else:
            messages.error(request, "Invalid username or password!")
            return redirect('login-view')
    return render(request, 'login/login.html')


def home_redirect_view(request):
    return redirect('chat:home-view')


def register_view(request):
    if request.user.is_authenticated:
        return redirect('chat:home-view')

    if request.method == 'POST':
        username   = request.POST.get('username', '').strip()
        first_name = request.POST.get('first_name', '').strip()
        last_name  = request.POST.get('last_name', '').strip()
        email      = request.POST.get('email', '').strip()
        password1  = request.POST.get('password1', '')
        password2  = request.POST.get('password2', '')

        if not all([username, first_name, last_name, email, password1, password2]):
            messages.error(request, "All fields are required!")
            return redirect('register-view')

        if password1 != password2:
            messages.error(request, "Password Don't Match")
            return redirect('register-view')

        try:
            validate_email(email)
        except ValidationError:
            messages.error(request, "Enter a valid email address!")
            return redirect('register-view')

        try:
            validate_password(password1)
        except ValidationError as e:
            messages.error(request, " ".join(e.messages))
            return redirect('register-view')

        if CustomUser.objects.filter(username=username).exists():
            messages.error(request, "Username is already Taken")
            return redirect('register-view')

        if CustomUser.objects.filter(email=email).exists():
            messages.error(request, "Email is already registered!")
            return redirect('register-view')

        try:
            CustomUser.objects.create_user(
                username=username,
                first_name=first_name,
                last_name=last_name,
                email=email,
                password=password1,
            )
            messages.success(request, "Account created successfully!")
            return redirect('login-view')
        except Exception as e:
            messages.error(request, f"Error: {str(e)}")
            return redirect('register-view')

    return render(request, 'login/register.html')


@login_required(login_url='login-view')
def logout_view(request):
    # Capture identity BEFORE logout() clears request.user.
    uid, uname = request.user.id, request.user.username

    # Presence cleanup is done here instead of relying on the browser to
    # close its WebSockets (which is delayed by proxies and can be skipped
    # entirely by a lingering/background socket). Logout must, for everyone:
    #   1. mark this account offline in the DB right now,
    #   2. stamp last_seen at the actual logout moment,
    #   3. broadcast OFFLINE over the global presence group, and
    #   4. force-close every open socket of this account (all tabs/devices).
    from chat import presence as chat_presence

    chat_presence.force_offline_and_announce(uid, uname)
    chat_presence.close_user_sockets(uid)
    _clear_redis_presence(uid)

    logout(request)
    messages.success(request, "Logged out successfully!")
    return redirect('login-view')


def _clear_redis_presence(user_id):
    """Drop any Redis presence leftovers for this account (defensive).

    The database is now the source of truth for presence; the Redis
    ``online_users`` / ``last_seen`` hashes are only read as a fallback and
    nothing in this codebase writes them. A stale entry left behind by an
    older deployment could still make the inbox show the user online after
    logout (the inbox ORs the Redis flag), so clear it when Redis is enabled.
    """
    try:
        from chat.views import _redis_client

        r = _redis_client()
        if r is None:
            return
        r.hdel('online_users', str(user_id))
        r.hdel('last_seen', str(user_id))
    except Exception:
        pass  # presence is already correct in the DB; Redis cleanup is best-effort


@login_required(login_url='login-view')
def profile_view(request):
    profile, _ = ProfilePic.objects.get_or_create(user=request.user)

    if request.method == 'POST':
        pic = request.FILES.get('profile_pic')
        if pic and not is_valid_image_upload(pic):
            messages.error(request, "Please upload a valid image (JPG, PNG, WEBP) under 5 MB.")
            return redirect('profile-view')

        pic_saved = True
        if pic:
            try:
                # Write the file to storage FIRST, outside Model.save():
                # Model.save() wraps the file write in
                # mark_for_rollback_on_error, so a storage failure there
                # would poison the surrounding transaction and take every
                # later query down with it.
                profile.profile_pic.save(pic.name, pic, save=False)
                profile.save()  # DB only — the file is already stored
            except Exception:
                # Storage failure (e.g. Cloudinary rejecting credentials)
                # must surface as a friendly message, not an HTTP 500.
                logger.exception("Profile picture upload failed for %s", request.user.username)
                pic_saved = False
                profile.refresh_from_db()  # drop the failed in-memory file

        request.user.first_name = request.POST.get('first_name', '')
        request.user.last_name = request.POST.get('last_name', '')
        request.user.email = request.POST.get('email', '')
        request.user.save()

        if pic_saved:
            messages.success(request, 'Profile updated!')
        else:
            messages.error(
                request,
                'Profile details saved, but the picture could not be uploaded — '
                'the file storage service is unavailable. Please try again later.'
            )
        return redirect('profile-view')

    return render(request, 'login/profile.html', {'profile': profile})


@login_required(login_url='login-view')
def user_profile_view(request, username):
    user = get_object_or_404(CustomUser, username=username)
    profile, _ = ProfilePic.objects.get_or_create(user=user)
    try:
        from chat.views import get_user_presence
        user.is_online, _ = get_user_presence(user.id)
    except Exception:
        pass
    return render(request, 'login/user_profile.html', {
        'profile_user': user,
        'profile': profile
    })