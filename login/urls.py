from django.urls import path
from . import views
from django.conf import settings
from django.conf.urls.static import static

urlpatterns = [
    path('', views.login_view, name='login-view'),
    path('home/', views.home_redirect_view, name='home-view'),
    path('about/', views.about_view, name='about-view'),
    path('profile/', views.profile_view, name='profile-view'),
    path('profile/<str:username>/', views.user_profile_view, name='user_profile'),
    path('register/', views.register_view, name='register-view'),
    path('logout/', views.logout_view, name='logout-view'),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

