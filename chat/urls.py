from django.urls import path
from . import views

app_name = 'chat'

urlpatterns = [
    path('', views.home_view, name='home-view'),
    path('search/', views.search_view, name='search-view'),
    path('api/inbox/', views.home_inbox_api, name='home-inbox-api'),
    path('api/messages/<str:username>/', views.send_message_api, name='send-message-api'),
    path('api/messages/<str:username>/history/', views.get_messages_api, name='messages-api'),
    path('<str:username>/', views.conversation_view, name='conversation-view'),
    path('api/status/<int:user_id>/', views.get_user_status_api, name='user-status-api'),
    path('api/storage-probe/', views.storage_probe, name='storage-probe'),
]