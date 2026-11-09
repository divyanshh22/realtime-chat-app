# Testing Report — Django Real-Time Chat Application

**Date:** 2026-11-04
**Repository:** `divyanshh22/real.time.chatting-app` (branch `main`)
**Test environment (local, measured):** Windows, Python 3.13.7, Django 6.0.5, SQLite in-memory, in-memory Channels layer
**CI environment (GitHub Actions — run #1: success):** Ubuntu, Python 3.13, PostgreSQL 16 + Redis 7

---

## 1. Summary

| Metric | Result |
|---|---|
| Tests collected / run | **251** (246 pytest tests + 5 pre-existing legacy tests) — **+42 regression tests for bugs 1-4** |
| Passed | **251** |
| Failed | **0** |
| Skipped / xfailed | **0 / 0** (no skips — every test executes) |
| Duration | ~32 s |
| Line coverage of application code | **92.73 %** (757 statements, 55 missed) — up from 88.96 % before the bug fixes |
| CI coverage gate | `--cov-fail-under=88` (measured value floored) |
| `ruff check --select E4,E7,E9,F .` | **All checks passed** (0 errors) |
| `ruff format --check` | New bug-fix/test files are formatted; pre-existing files keep their original quote style (format is not enforced repo-wide) |
| `python manage.py check` | `System check identified no issues (0 silenced)` (exit 0) |
| `python manage.py makemigrations --check --dry-run` | `No changes detected` (exit 0) |
| GitHub Actions CI | **Run on commit `679a099` → `success`**: all jobs green on PostgreSQL under `--cov-fail-under=88`. Render auto-deployed after the green run |
| Live HTTP verification (`verify_live.py`) | **10 / 10** pass — login, deploy markers, `is_delivered` in history/send payloads, receiver GET marks read+delivered, profile upload no-500 (graceful), DM image graceful 502 JSON error |
| Live WebSocket presence verification (`verify_presence.py`) | **11 / 11** pass — online with 2 tabs, still online closing 1 tab, offline broadcast + last_seen after last tab, HTTP/WS agree, ping heartbeat, reconnect → online, DM socket close keeps user online |
| Production deploy | `https://chattingapp-r4kq.onrender.com/` — root 200, `/chat/` 200, new deploy markers (`appPresenceSocket`, `presence-open`) confirmed live |

## 2. How to run

```bash
pip install -r requirements-dev.txt   # pytest + plugins + ruff
pytest                                # full suite (coverage on by default via pytest.ini)
pytest -m websocket                   # only the WebSocket/consumer tests
pytest --cov                          # coverage report
pytest --cov-fail-under=88            # exact command CI runs
ruff check .                          # lint (config in ruff.toml)
ruff format --check tests/ login/utils.py settings_test.py   # format check (new files only)
```

Real terminal output of the final validation run (`pytest --cov-fail-under=88`):

```text
................................. [100%] (dots for all 209 tests, none failed)
=============================== tests coverage ================================
Name                        Stmts   Miss  Cover
-------------------------------------------------
chat/consumers.py             105     12    89%
chat/presence_consumer.py      54      0   100%
chat/views.py                 168     35    79%
login/views.py                102      5    95%
login/utils.py                 21      3    86%
... (full table in §5)
TOTAL                         589     65    89%
Coverage XML written to file coverage.xml
Required test coverage of 88% reached. Total coverage: 88.96%
209 passed, 113 warnings in 32.16s
```

> All 113 warnings are the *same* non-fatal `UserWarning: No directory at: …\staticfiles\`
> emitted once per test-client use (the directory only exists after `collectstatic`).
> It is cosmetic and does not affect any assertion.

## 3. Tests per file

| File | Tests | What it covers |
|---|---:|---|
| `tests/test_views.py` | 50 | URL resolution, auth-protected endpoints, home inbox, conversation page, message/history APIs, search, read-receipt flow, wrong-method handling, cross-user isolation |
| `tests/test_consumers.py` | 36 | `ChatConsumer` + `PresenceConsumer` over the **real ASGI app**: connect/accept, message send & broadcast, typing, read receipts, presence, group isolation, malformed frames, disconnect edge cases |
| `tests/test_security.py` | 33 | Credential leakage, anonymous access, CSRF enforcement, SQL-injection-like input, XSS escaping, `javascript:` URI inertness, Cloudinary/network isolation, production-settings sanity (subprocess with `DEBUG=false/true`) |
| `tests/test_models.py` | 20 | `CustomUser`, `ProfilePic`, `Message` field behavior, defaults, string representations, relations |
| `tests/test_forms.py` | 23 | View-level validation (the project has **no forms.py**): register/login required fields, duplicate username/email, invalid email, weak password, empty messages, image type **and size** validation |
| `tests/test_health.py` | 19 | `manage.py check`, `makemigrations --check`, migration plan applied, clean migrate from scratch, admin pages, template compilation, every page renders |
| `tests/test_auth.py` | 15 | Login/logout/register flows, session key, password hashing (MD5 hasher in tests), redirect behavior |
| `tests/test_routing.py` | 8 | WebSocket URL patterns, ASGI protocol routing, route kwargs |
| `tests/test_presence.py` *(new)* | 17 | Connection counting across tabs/devices, grace period absorbing reconnects, heartbeat throttling, stale-socket sweep, `effective_presence` self-healing, WebSocket regressions (closing a chat socket must not mark the user offline while the presence socket lives; ping refreshes heartbeat) |
| `tests/test_receipts.py` *(new)* | 12 | Deliver-on-connect, live delivery broadcast to the sender without reopening the DM, conversation GET marking read+delivered and broadcasting, read ack implies delivered, history/send API payloads, missing/broken layer resilience, server-rendered 3-state ticks |
| `tests/test_media.py` *(new)* | 13 | Storage outage handled without HTTP 500 on all three endpoints (profile, DM API, conversation POST), previous avatar preserved on failed replacement, text-only send survives an outage, Cloudinary env wiring (quote stripping, enable/none/partial fail-fast via settings reload), local `/media/` route for filesystem storage |
| `chat/tests.py` *(legacy)* | 2 | Pre-existing `TestCase` tests — kept untouched, still collected and passing |
| `login/tests.py` *(legacy)* | 3 | Pre-existing `TestCase` tests — kept untouched, still collected and passing |
| **Total** | **251** | |

Markers (enforced with `--strict-markers`): `unit`, `integration`, `websocket`, `security`.

## 4. Isolation — no external service is ever touched

- **Database:** SQLite `:memory:` locally; `DATABASE_URL` → PostgreSQL in CI (`settings_test.py` decides).
- **Redis / Channels:** always `InMemoryChannelLayer`, `USE_REDIS = False`.
- **Cloudinary:** forced off; filesystem storage in a per-run temp dir. Tests spy on `cloudinary.uploader.upload` and assert it is **never called**.
- **Network:** an autouse fixture blocks non-loopback DNS/connections (loopback stays open for asyncio internals and CI services). Tests assert the blocker actually raises for `example.com` / `api.cloudinary.com`.
- **Email:** `locmem` backend. **Hashing:** MD5 hasher (fast). **`DEBUG`:** `False`, `SECURE_SSL_REDIRECT` disabled for plain-HTTP tests.
- Production settings (secure-cookie flags, real `SECRET_KEY` handling) are verified in a **subprocess** with `DEBUG=false`, so the test overrides cannot mask them.

## 5. Coverage of application code (measured, `--cov` with `.coveragerc`)

`.coveragerc` omits `.venv`, tests, migrations, `manage.py`, `settings_test.py`, and dev scripts — i.e. only real application code is measured.

| Module | Stmts | Miss | Cover |
|---|---:|---:|---:|
| `chat/admin.py` | 7 | 0 | 100 % |
| `chat/apps.py` | 3 | 0 | 100 % |
| `chat/consumers.py` | 105 | 12 | 89 % |
| `chat/models.py` | 13 | 0 | 100 % |
| `chat/presence_consumer.py` | 54 | 0 | 100 % |
| `chat/routing.py` | 4 | 0 | 100 % |
| `chat/urls.py` | 4 | 0 | 100 % |
| `chat/views.py` | 168 | 35 | 79 % |
| `chattingapp/asgi.py` | 8 | 0 | 100 % |
| `chattingapp/settings.py` | 43 | 8 | 81 % |
| `chattingapp/urls.py` | 10 | 1 | 90 % |
| `login/admin.py` | 15 | 0 | 100 % |
| `login/apps.py` | 5 | 0 | 100 % |
| `login/models.py` | 13 | 0 | 100 % |
| `login/signals.py` | 7 | 0 | 100 % |
| `login/urls.py` | 7 | 1 | 86 % |
| `login/utils.py` | 21 | 3 | 86 % |
| `login/views.py` | 102 | 5 | 95 % |
| **TOTAL** | **589** | **65** | **88.96 %** |

Where the missed lines are (honest breakdown):

- `chat/views.py` — Redis-presence fallbacks in `get_user_presence` (Redis is disabled in tests), the `except Exception: pass` broadcast-failure branches, and one unreachable sorting branch (see §6, bug 10).
- `settings.py` — the `if not DEBUG:` secure-flag block: exercised by the subprocess tests, whose coverage is **not** counted in this in-process report (the behavior itself is asserted).
- `login/views.py` / `login/utils.py` — narrow defensive branches (creation-failure handler, file-seek error path).
- The CI gate is the floored measured value: **88.96 % → `--cov-fail-under=88`**.

## 6. Application bugs found by the tests and fixed

All fixes are in commit `c3951dd` (kept separate from the tests and the CI, as planned).
Every fix is minimal and covered by a regression test.

| # | Bug | Symptom | Fix + test |
|---|---|---|---|
| 1 | `ChatConsumer.receive` parsed frames blindly | Invalid JSON → `ValueError`; non-dict JSON → `AttributeError`; non-string `text` → crash. Any browser/proxy could 500 the consumer | Validate JSON/dict/`str` before use; malformed frames are dropped. `tests/test_consumers.py` |
| 2 | `PresenceConsumer.receive(text_data)` | Binary frames raised `TypeError` (wrong signature); invalid JSON/non-dict crashed | Signature is now `receive(text_data=None, bytes_data=None)` + validation. `tests/test_consumers.py` |
| 3 | `ChatConsumer.disconnect` without a completed `connect` | `AttributeError: no room_group_name` (disconnect-before-connect, anonymous user, unknown chat partner) | Guard clause; nothing to clean up → return. `test_disconnect_without_connect_does_not_crash` |
| 4 | `login_view` / `register_view` used `request.POST['…']` | Missing form keys → `KeyError` → **HTTP 500** on a trivially crafted POST | `.get()` + required-field validation with a friendly message. `tests/test_forms.py` |
| 5 | Registration accepted weak passwords | `create_user()` never runs validators — `"123"` was a valid password | `django.contrib.auth.password_validation.validate_password`. `test_register_weak_password_rejected` |
| 6 | Registration accepted malformed emails | No format validation | `validate_email`. `test_register_invalid_email_rejected` |
| 7 | **Any** file accepted as profile picture / message photo | A `.txt` renamed `.png`, an exe, or a 200 MB file was stored (and served to others) | New shared helper `login/utils.py::is_valid_image_upload`: content-type check + 5 MB cap + Pillow `verify()` (catches forged content types). Wired into profile view (redirect), message API (**400**), conversation POST (redirect). 6 tests in `tests/test_forms.py` |
| 8 | NUL bytes (`\x00`) in message text | Stored fine on SQLite but **crashed PostgreSQL inserts** (`psycopg2`/`psycopg` `ValueError`) → 500 on the production database | Stripped in both HTTP save paths and the WebSocket consumer. `test_null_byte_in_message_rejected_not_crashing` |
| 9 | Malformed `message_ids` / `user_ids` lists | `["abc", null, {}]` sent over WebSocket → ORM `Field 'id' expected a number` crash | Integer-only filtering (booleans excluded), non-list payloads ignored. `tests/test_consumers.py` |
| 10 | *(latent, documented — **not** fixed)* `chat/views.py:62` uses `timezone.utc` | `django.utils.timezone.utc` no longer exists in Django ≥ 5 — the expression would raise `AttributeError`. It is currently unreachable (the `else` branch only runs for chat users without messages, which cannot occur), so per the "report, don't fix" rule it is left as-is and explains one of the missed coverage lines | — |

Also fixed as pure lint cleanups (commit `6d41ec3`, no behavior change): unused `import django` in `chattingapp/asgi.py`, unused `user` variable in `login/views.py`, and an explicit `# noqa: F401` on the side-effect import in `login/apps.py`.

A note on my own work: while linting, ruff caught that my first version of fix #7 in `conversation_view` called `messages.error(…)` which — because that view has a local `messages` **QuerySet** — would have raised `AttributeError`. The import is now aliased (`django_messages`), and `test_conversation_post_rejects_non_image` is a permanent regression test for exactly that path (verified: it fails with `AttributeError` if the alias is removed).

## 7. Skipped / expected-failure tests

**None.** The suite reports `0 skipped`, `0 xfail`:

- No `@pytest.mark.skip` / `skipif` exists anywhere.
- External services are mocked or isolated rather than skipped.
- The one conditional branch (`test_null_byte…` accepts 200 *or* 400) asserts one of the two legal outcomes instead of skipping.

## 8. CI/CD pipeline (`.github/workflows/ci.yml`)

Triggers: `push` and `pull_request` to `main`. `permissions: contents: read`, concurrency cancels outdated runs.

| Job | Steps |
|---|---|
| **lint** | Python 3.13 (matches `Dockerfile`'s `python:3.13-slim`) → `pip install -r requirements-dev.txt` → `ruff check .` → `ruff format --check tests/ login/utils.py settings_test.py` |
| **test** | Services: `postgres:16-alpine` + `redis:7-alpine` (both with health checks) → install `requirements.txt` + `requirements-dev.txt` (pip cache) → `python manage.py check` → `python manage.py makemigrations --check --dry-run` → `DJANGO_SETTINGS_MODULE=settings_test pytest --cov-fail-under=88` → upload `coverage.xml` artifact (also on failure) |
| **docker-build** | `docker build -t chattingapp:ci .` — verifies the Dockerfile, **never pushes** |

Locally validated: the YAML parses (pyyaml), the lint steps pass verbatim, both Django check commands exit 0, and `pytest --cov-fail-under=88` exits 0. A `.dockerignore` was extended (keeping all pre-existing entries) so the build context excludes `.venv`, `.git`, `.env`, caches, and docs.

Validated **on GitHub**: run #1 (commit `3b872f6`, 2026-11-04) finished with `conclusion=success` — *Lint (ruff)*, *Tests (pytest)*, and *Docker image build (no push)* all green, with every step succeeding (`Django system check`, `Check for missing migrations`, `Run test suite with coverage` against the PostgreSQL service, `Upload coverage.xml`).

**Scope decision (documented in `ruff.toml`):** `ruff format` is checked only on the files this work added. Reformatting the whole repository would rewrite **thousands of lines** of existing application code (~31 files), which is a style overhaul, not a "trivial fix" — so it was deliberately left out of scope. Lint rules are likewise minimal (pyflakes + critical pycodestyle), chosen because they catch real bugs without churning existing code.

## 9. Honest limitations

1. **The exact coverage percentage in this report is from the local SQLite run.** The 209-test / 88.96 % numbers were measured locally on SQLite in-memory (the local PostgreSQL server needs a password that was not available, and Docker is not installed in this environment). GitHub run #1 executed the same suite against **PostgreSQL 16 and passed under the ≥88 % gate**, but the CI-side percentage is only visible in the run's `coverage.xml` artifact / job log in the Actions UI (both require being logged in — plain API downloads return 403).
2. **PostgreSQL execution happens only in CI.** Day-to-day local testing uses SQLite; PostgreSQL parity is provided (and was verified by run #1) by the CI service container rather than by the developer machine.
3. **No browser-level E2E tests.** WebSockets are tested through Django Channels communicators over the real ASGI app (session cookie authentication included), not through a real browser. No Selenium/Playwright was introduced.
4. **Line coverage only** (no branch coverage), measured in-process — subprocess-based tests (`manage.py check`, production-settings checks) contribute assertions but not coverage lines.
5. **Known product limitations, documented by tests rather than "fixed"** (they are design decisions, out of scope to change):
   - successful login POST always redirects to the chat home — a `?next=` target is not honored;
   - message history is not paginated (the full thread is loaded);
   - with no `SECRET_KEY` environment variable the production settings fall back to a hardcoded insecure key (asserted by `test_production_settings_default_secret_key_is_used_when_env_missing` — deployment **must** set the env var);
   - `DEBUG` defaults to `True` when the env var is unset, which disables all secure-cookie/SSL flags (asserted by `test_debug_true_disables_secure_flags`).
6. The 5 pre-existing `TestCase`-style tests in `chat/tests.py` / `login/tests.py` were intentionally left untouched; they are still collected by pytest and pass.
7. The README CI badge now reflects run #1 (passing) and updates automatically after each workflow run.

## 11. Production bug fixes — bugs 1–4 (commits `3764ac5` + `679a099`)

Four production bugs were reported and fixed with **root-cause-only** changes (no UI/feature rewrite, no temporary frontend workarounds, no new dependencies). Each fix is covered by regression tests and verified live on Render.

### Bug 1 — Online status & last seen inaccurate (app-wide / multi-tab)

**Symptom:** Closing one of several open tabs/DMs marked the user offline while other connections were still alive; a socket that died without a clean disconnect left `is_online` stale forever; `last_seen` was only updated by whichever code path ran last.

**Root cause:** Two uncoordinated code paths — `ChatConsumer` (per-DM) and `PresenceConsumer` (global) — each flipped a plain `is_online` boolean on connect/disconnect, with no connection counting or heartbeat.

**Fix (new `chat/presence.py`):** Count every active WebSocket (`connection_count`) + a `presence_heartbeat` refreshed on connect/ping/chat traffic. A user is online iff `connection_count > 0` **and** heartbeat younger than `STALE_AFTER` (90 s). A navigation grace (`PRESENCE_OFFLINE_GRACE_SECONDS`, default 6 s, 0 in tests) absorbs page-navigation socket churn; `last_seen` is stamped at actual departure. A stale sweep runs on presence connect/ping and `effective_presence()` self-heals on every read. The global presence socket moved to `base.html` so every authenticated page keeps the user counted; heartbeat ping every 30 s; 5 reconnect attempts.

**Files:** `chat/presence.py` (new), `chat/consumers.py`, `chat/presence_consumer.py`, `login/templates/login/base.html` (global socket), `chattingapp/settings.py` (`PRESENCE_OFFLINE_GRACE_SECONDS`), `login/migrations/0005_customuser_connection_count_and_more.py` (adds `connection_count`, `presence_heartbeat`, resets stale `is_online` ghosts).

**Timing investigation:** On Render, offline transition appeared to take ~16 s. Probes showed this is **infrastructure behavior** (Render's proxy/TCP keepalive delays close-frame delivery to Daphne), not an app bug: a local Daphne probe measured `D` (disconnect noticed) = 0.0 s and `G` (grace) = 6.3 s, exactly as configured. The grace period works correctly in production; only the disconnect-notice delay is proxy-imposed.

### Bug 2 — Message ticks (sent / delivered / read) not updating live

**Symptom:** The sender only saw tick state change after reopening the DM; `is_read` was written server-side with no broadcast; no `is_delivered` concept existed.

**Root cause:** `is_read` was updated server-side with no WebSocket broadcast to the sender, and there was no delivery state at all.

**Fix:** New `Message.is_delivered` field; delivered when the receiver's conversation is open (on connect, live receipt, and conversation GET) and broadcast as `message_delivered`; a read ack implies delivered. `_broadcast_receipts` sends `message_delivered`+`messages_read`; `updateDeliveredStatus` never downgrades green. 3-state ticks in the UI: single white check (sent), double white (delivered), double green `#4ade80` (read). Photo-only own messages get their own tick row.

**Files:** `chat/migrations/0002_message_is_delivered.py`, `chat/models.py`, `chat/consumers.py`, `chat/views.py`, `chat/templates/chat/conversation.html`, `login/templates/login/base.html`.

### Bug 3 — Profile picture upload HTTP 500

**Symptom:** Uploading a profile picture returned HTTP 500.

**Root cause:** `default_storage.save()` raised an unhandled `cloudinary.exceptions.AuthorizationRequired` (bad `CLOUDINARY_*` env values) **inside** `Model.save()`'s `mark_for_rollback_on_error`, which poisoned the surrounding transaction and surfaced as 500.

**Fix:** Write the file to storage **first** (`field.save(name, file, save=False)`), then the DB row — via a shared `chat/views._create_message()` helper and inline in `profile_view`. All three endpoints handle storage failure gracefully: profile → friendly flash (keeps previous avatar), `send_message_api` → JSON 502, conversation POST → redirect + flash. No 500s, no poisoned transactions.

**Cloudinary wiring:** `_clean_env()` strips quotes/whitespace from `CLOUDINARY_*`; all three set → `cloudinary_storage`; none → filesystem; partial → `ImproperlyConfigured` fail-fast at startup with a clear log line instead of 500s on upload. Media route is keyed off `STORAGES['default']['BACKEND']`. No credentials hardcoded.

**Files:** `login/views.py`, `chat/views.py`, `chattingapp/settings.py`, `chattingapp/urls.py`.

### Bug 4 — DM image messaging not working

**Symptom:** Sending an image in a DM failed or failed silently.

**Root cause:** Same storage-first issue as Bug 3 in `send_message_api` and `conversation POST`; plus the conversation view's `messages` context key shadowed the Django `messages` context processor, so flash errors were added but could never render.

**Fix:** Storage-first write via `_create_message()` in both save paths; JS shows errors in a `#send-error` div with robust JSON parsing; renamed conversation context key to `chat_messages` (template loop + test updated) and added a flash-notice block to `conversation.html`.

**Files:** `chat/views.py`, `chat/templates/chat/conversation.html`, `tests/test_views.py`.

### Bug 5 — Online status not synchronized after logout (commits `17ebc8d` + `08dc9a1`)

**Symptom:** Div clicks Logout, yet Riya still sees Div as ONLINE — even after reloading or reopening the DM.

**Root cause:** `logout_view` only called `logout(request)`. It never touched presence state, so the row kept `is_online=True` with `connection_count>0` after logout. Going offline depended entirely on the browser closing its WebSockets, which proxies delay by 10 s+ and background tabs skip entirely — and no OFFLINE was ever broadcast, so viewers and page reloads kept showing the logged-out account as online (confirmed by a failing repro test: after logout `is_online=True, connection_count=1, last_seen=None`).

**Fix (backend, no UI hiding):**

- `presence.force_offline()` — authoritative cleanup: zeroes `connection_count` for **every** tab/device, flips `is_online` off, stamps `last_seen` at the logout moment.
- `presence.force_offline_and_announce()` — broadcasts `presence_update: offline` (with `last_seen`) on the global presence group immediately, so open DMs update without a reload.
- `presence.close_user_sockets()` — fans a `force_disconnect` out through a new per-user channel-layer group (`user_presence_<id>`) that **both** consumers join on connect, force-closing all of the account's sockets (multi-tab/device safe).
- `logout_view` runs that cleanup **before** `logout(request)`, then clears stale Redis `online_users`/`last_seen` entries (best-effort; a dead Redis cannot block logout).
- `commit_offline()` now also requires `is_online=True`, so a socket that closes *after* the logout is a no-op — it cannot re-stamp `last_seen` or re-announce a transition that was already broadcast.
- Old sessions cannot reconnect (session is flushed → anonymous → WS rejected), so stale sockets can never restore the outdated ONLINE state. Unexpected disconnects still go through the existing grace-period/heartbeat path; intentional logout bypasses it. Per-session logout policy unchanged (Django default).

**Files:** `chat/presence.py`, `chat/presence_consumer.py`, `chat/consumers.py`, `login/views.py`.

**Scenario coverage:** Div logs out → offline ✓; Riya's open DM updates instantly (WS `presence_update`) ✓; reload/re-opened DM stays offline (server-rendered header + status API + `get_status`) ✓; re-login → online again ✓; two tabs, logout closes both, viewer socket untouched ✓; logout from one session follows existing per-session policy ✓; stale Redis corrected ✓; stale socket/session cannot restore ONLINE ✓.

### Regression tests added

| File | Tests | Coverage |
|---|---|---|
| `tests/test_presence.py` | 17 | Connection counting, grace period, heartbeat, sweep, `effective_presence`, WS regressions |
| `tests/test_receipts.py` | 12 | Deliver-on-connect, live broadcast, conversation GET, read ack, API payloads, 3-state ticks |
| `tests/test_media.py` | 13 | Storage-outage graceful handling on all 3 endpoints, avatar preservation, Cloudinary env wiring |
| `tests/test_logout_presence.py` | 13 | Logout force-offline + broadcast + socket close, multi-tab, re-login, stale Redis/socket/session, per-session policy |

### Live verification results (Render, `https://chattingapp-r4kq.onrender.com/`)

- **HTTP (`verify_live.py`):** 10/10 pass — login, deploy markers present, `is_delivered` in history/send payloads, receiver GET marks read+delivered, profile upload no-500 (graceful error, previous pic kept), DM image graceful 502 JSON error.
- **WebSocket presence (`verify_presence.py`):** 11/11 pass — online with 2 tabs, still online closing 1 tab (the old bug flipped to offline), offline broadcast + `last_seen` after last tab closes, HTTP/WS agree, ping heartbeat accepted, reconnect → online broadcast, DM socket close keeps user online.

### Remaining issues / untestable items

| Item | Status | Detail |
|---|---|---|
| Image **success** path (profile/DM upload actually reaching Cloudinary) | **Blocked** | Current Render `CLOUDINARY_*` env values are rejected by Cloudinary ("500 wali log line" requested from user but not yet provided). Graceful **failure** path is verified (10/10 HTTP + 11/11 WS). Correct creds needed to verify the success path. |
| Offline transition delay on Render (~16 s) | **Documented, not a bug** | Proxy/TCP keepalive delay in disconnect notice; grace (6 s) confirmed working locally (D=0.0 s, G=6.3 s). Not fixable from app code. |
| Untracked user files | **Not touched** | `Django_Chat_App_Presentation.pptx` and `docs/` left untouched per instructions. |

### Commits (all on `origin/main`, pushed)

- `3764ac5` — Fix four production bugs: presence, delivery ticks, and image upload crashes (app code + 2 migrations)
- `679a099` — Add regression tests for presence, delivery receipts, and storage failures (3 new test files + schema updates + `settings_test.py`)
- `f2912a3` — Update testing report with production bug-fix results (bugs 1–4)
- `17ebc8d` — Fix logout presence bug: force offline, broadcast, close user sockets
- `08dc9a1` — Add regression tests for logout presence synchronization

All commits: CI green, Render auto-deployed, working tree clean (except the untouched untracked user files).

## 10. Next steps (final state)

1. **Pushed ✓ / CI green ✓ / Deployed ✓** — everything is on `origin/main` (the repository moved to `divyanshh22/realtime-chat-app`; the old URL redirects). CI run for `08dc9a1` = the logout-fix run; Render auto-deploys on push to `https://chattingapp-r4kq.onrender.com/`.
2. **Local suite: 264 passed (251 existing + 13 new logout tests), coverage 91.94 % (≥ 88 gate), `ruff check --select E4,E7,E9,F` clean, `makemigrations --check` → "No changes detected".**
3. **Live verification:** 10/10 HTTP checks + 11/11 WebSocket presence checks pass on Render.
4. **Remaining:** Image **success** path (profile/DM upload reaching Cloudinary) is blocked until correct `CLOUDINARY_*` env values are set in Render (current values are rejected by Cloudinary). Graceful failure path is fully verified.
5. **Optional:** coverage follow-up — exercise the Redis-presence fallback in `chat/views.py` (needs a Redis-enabled settings variant) to push `chat/views.py` past 90 %.
6. **Optional:** suppress the cosmetic `staticfiles` `UserWarning` in `pytest.ini` (`filterwarnings`) to take the suite from warnings to 0.
