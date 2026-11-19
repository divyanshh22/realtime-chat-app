(function () {
  const list = document.getElementById('conversation-list');
  if (!list) {
    return;
  }

  const inboxApiUrl = '{% url "chat:home-inbox-api" %}';
  const searchUrl = '{% url "chat:search-view" %}';
  const activeUserId = String(list.dataset.activeUserId || '');
  const activeUsername = list.dataset.activeUsername || '';
  const activeDisplayName = list.dataset.activeDisplayName || activeUsername;
  const activeInitial = list.dataset.activeInitial || '';
  const activeAvatar = list.dataset.activeAvatar || '';
  const activeUrl = list.dataset.activeUrl || '#';
  const activeOnline = list.dataset.activeOnline === 'true';

  let loading = false;

  function escapeHtml(str) {
    if (!str) return '';
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
  }

  function displayName(user) {
    return user.display_name || [user.first_name, user.last_name].filter(Boolean).join(' ') || user.username || '';
  }

  function toInitials(name) {
    return (name || '').split(' ').map(part => part[0]).join('').slice(0, 2).toUpperCase();
  }

  function formatTime(isoString) {
    if (!isoString) return '';
    const d = new Date(isoString);
    if (isNaN(d.getTime())) return '';
    return d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  }

  function rowMarkup(user, isActive, synthetic) {
    const name = escapeHtml(displayName(user));
    const username = escapeHtml(user.username || '');
    const preview = escapeHtml(
      synthetic ? 'Start the conversation' : (user.preview || 'Start a conversation')
    );
    const time = synthetic ? '' : formatTime(user.last_message_time);
    const avatarUrl = user.profile_pic_url || '';
    const isOnline = !!user.is_online;
    const unread = !synthetic && user.unread_count > 0;
    const href = user.conversation_url || activeUrl;

    const avatarMarkup = avatarUrl
      ? `<img src="${avatarUrl}" class="avatar" alt="">`
      : `<span class="avatar-placeholder" aria-hidden="true">${escapeHtml(toInitials(name))}</span>`;

    return `
      <a href="${href}" data-user-id="${user.id}" data-username="${username}" data-display-name="${name}" class="conversation-item${isActive ? ' is-active' : ''}${unread ? ' has-unread' : ''}"${isActive ? ' aria-current="page"' : ''}>
        <span class="conversation-avatar">
          ${avatarMarkup}
          <span class="status-dot chat-presence-dot" id="dot-${user.id}" style="background:${isOnline ? 'var(--online)' : 'var(--offline)'};" title="${isOnline ? 'Online' : 'Offline'}"></span>
        </span>
        <span class="conversation-main">
          <span class="conversation-top">
            <span class="conversation-name">${name}</span>
            ${time ? `<span class="conversation-time">${time}</span>` : ''}
          </span>
          <span class="conversation-bottom">
            <span class="conversation-preview">${preview}</span>
            ${unread ? `<span class="chat-unread-badge">${user.unread_count}</span>` : ''}
          </span>
        </span>
      </a>`;
  }

  function requestListStatuses() {
    const socket = window.appPresenceSocket;
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      return;
    }
    const ids = Array.from(list.querySelectorAll('[data-user-id]'))
      .map(el => parseInt(el.dataset.userId, 10))
      .filter(id => Number.isInteger(id) && id > 0);
    if (ids.length) {
      socket.send(JSON.stringify({ type: 'get_status', user_ids: ids }));
    }
  }

  function applyStatus(userId, status) {
    const dot = list.querySelector('#dot-' + String(userId));
    if (!dot) return;
    dot.style.background = status === 'online' ? 'var(--online)' : 'var(--offline)';
    dot.title = status === 'online' ? 'Online' : 'Offline';
  }

  window.addEventListener('presence-message', function (e) {
    const data = e.detail;
    if (!data) return;

    if (data.type === 'presence_update') {
      applyStatus(data.user_id, data.status);
    } else if (data.type === 'bulk_status' && data.statuses) {
      Object.entries(data.statuses).forEach(([userId, info]) => {
        applyStatus(userId, info.status);
      });
    }
  });

  window.addEventListener('presence-open', requestListStatuses);
  if (window.appPresenceSocket && window.appPresenceSocket.readyState === WebSocket.OPEN) {
    requestListStatuses();
  }

  function render(users) {
    const countEl = document.getElementById('chat-count');
    const activeInUsers = users.some(user => String(user.id) === activeUserId);
    const total = users.length + (activeInUsers ? 0 : 1);

    if (countEl) {
      countEl.textContent = total || '';
    }

    const scrollTop = list.scrollTop;
    let html = '';

    if (!activeInUsers && activeUserId) {
      html += rowMarkup({
        id: activeUserId,
        username: activeUsername,
        display_name: activeDisplayName,
        preview: '',
        last_message_time: null,
        unread_count: 0,
        is_online: activeOnline,
        profile_pic_url: activeAvatar,
        conversation_url: activeUrl,
      }, true, true);
    }

    html += users.map(user => rowMarkup(user, String(user.id) === activeUserId, false)).join('');
    list.innerHTML = html;
    list.scrollTop = scrollTop;
    requestListStatuses();
  }

  function showLoadError() {
    list.querySelectorAll('.conversation-skeleton').forEach(el => el.remove());
    if (list.querySelector('.conversation-item')) {
      return;
    }
    list.innerHTML = `
      <div class="conversation-list-state">
        Couldn’t load conversations.<br>
        <a href="${searchUrl}">Find people</a>
      </div>`;
  }

  async function loadConversations() {
    if (loading) return;
    loading = true;

    try {
      const response = await fetch(inboxApiUrl, { cache: 'no-store' });
      if (!response.ok) {
        throw new Error(`Inbox request failed (HTTP ${response.status})`);
      }
      const data = await response.json();
      render(data.users || []);
    } catch (error) {
      console.error('Conversation list refresh failed', error);
      showLoadError();
    } finally {
      loading = false;
    }
  }

  loadConversations();

  // Trigger an immediate refresh when a message is sent or received on this
  // page (via the WebSocket), so the conversation re-sorts to the top.
  window.addEventListener('inbox-refresh', function () {
    loadConversations();
  });

  // Only refresh while the desktop conversation pane is actually visible.
  setInterval(function () {
    if (!document.hidden && window.matchMedia('(min-width: 901px)').matches) {
      loadConversations();
    }
  }, 5000);

  let wasWide = window.matchMedia('(min-width: 901px)').matches;
  window.addEventListener('resize', function () {
    const isWide = window.matchMedia('(min-width: 901px)').matches;
    if (isWide && !wasWide) {
      loadConversations();
    }
    wasWide = isWide;
  });

  // Conversation search: filter existing conversations only (WhatsApp-style)
  var searchInput = document.getElementById('conversation-search');
  if (searchInput) {
    searchInput.addEventListener('input', function() {
      var query = this.value.toLowerCase().trim();
      var items = list.querySelectorAll('.conversation-item');
      var visibleCount = 0;

      items.forEach(function(item) {
        var nameEl = item.querySelector('.conversation-name');
        var previewEl = item.querySelector('.conversation-preview');
        if (!nameEl) return;

        var name = (nameEl.textContent || '').toLowerCase();
        var username = (item.dataset.username || '').toLowerCase();
        var displayName = (item.dataset.displayName || '').toLowerCase();
        var preview = (previewEl ? previewEl.textContent || '' : '').toLowerCase();

        var matches = !query || name.indexOf(query) >= 0 || username.indexOf(query) >= 0 || displayName.indexOf(query) >= 0 || preview.indexOf(query) >= 0;
        item.style.display = matches ? '' : 'none';
        if (matches) visibleCount++;
      });

      var countEl = document.getElementById('chat-count');
      if (countEl) {
        countEl.textContent = visibleCount || '';
      }
    });
  }
})();
