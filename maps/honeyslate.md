# Journey map — which user journeys depend on which symbols

Target: `/home/ericm/personal_projects/honeyslate/main` · index schema 8 · generated from commit `1cd0385`

Look up the symbols you changed **by name**. Every journey listed for them may have changed behavior and is worth verifying. This is **recall-first**: a shared symbol legitimately fans out to many journeys.

**Line numbers are frozen at the commit above and are a hint only** — your own edit has already shifted them, so an insertion higher up the file makes the ranges point at the wrong symbol (issue #24). Match the symbol name; use the range only to disambiguate two symbols sharing one.

`!` marks a journey reached only through weak or synthesized graph edges — treat it as *verify manually*, not as *probably fine*.

## Journeys

- **J1** submit task — entry: `create_task`
- **J2** browse tasks — entry: `list_tasks`, `get_task`, `list_task_types`
- **J3** edit task — entry: `patch_task`
- **J4** reschedule — entry: `reschedule_task`
- **J5** comments — entry: `list_comments`, `add_comment`
- **J6** auth / sign-in — entry: `mint_magic_link`, `request_link`, `consume`, `logout`, `me`
- **J7** gcal sync — entry: `gcal_push`
- **J8** auto-scheduler — entry: `sweep`, `start`

## Symbols by file

### `backend/app/auth_service.py`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `sqlalchemy.orm` | J6 | 7–7 |
| `_settings` | J6 | 13–13 |
| `create_magic_link` | J6 | 16–30 |
| `consume_magic_link` | J6 | 33–53 |

### `backend/app/config.py`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `ENV_FILE` | J1 J2 J3 J4 J5 J8 | 13–13 |
| `Settings` | J1 J2 J3 J4 J5 J6 J7 J8 | 16–78 |
| `get_settings` | J1 J2 J3 J4 J5 J6 J7 J8 | 84–89 |
| `load_type_windows` | J1 J2 J3 J4 J5 J8 | 92–109 |

### `backend/app/db.py`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `collections.abc` | J1 J2 J3 J4 J5 J6 J7 J8 | 4–4 |
| `sqlalchemy.orm` | J1 J2 J3 J4 J5 J6 J7 J8 | 7–7 |
| `Base` | J1 J2 J3 J4 J5 J6 J7 J8 | 12–13 |
| `SessionLocal` | J1 J2 J3 J4 J5 J6 J7 J8 | 22–22 |
| `get_db` | J1 J2 J3 J4 J5 J6 | 25–31 |

### `backend/app/deps.py`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `sqlalchemy.orm` | J1 J2 J3 J4 J5 J6 | 6–6 |
| `require_user` | J1 J2 J3 J4 J5 J6 | 34–37 |
| `require_operator` | J1 J2 J3 J4 J5 J6 | 40–43 |

### `backend/app/gcal_sync.py`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `sync_channel` | J8 | 36–91 |
| `reconcile` | J7 | 94–133 |
| `_parse_gcal_time` | J7 | 136–146 |
| `_apply_event` | J7 | 149–187 |

### `backend/app/google_calendar.py`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `_settings` | J1 J2 J3 J4 J5 J7 J8 | 25–25 |
| `SCOPES` | J1 J2 J3 J4 J5 J7 J8 | 28–28 |
| `CALENDAR_SUMMARY` | J1 J2 J3 J4 J5 J7 J8 | 29–29 |
| `calendar_configured` | J1 J2 J3 J4 J5 J7 J8 | 33–36 |
| `_build_service` | J1 J2 J3 J4 J5 J7 J8 | 49–66 |
| `_resolve_calendar` | J1 J2 J3 J4 J5 J7 J8 | 69–105 |
| `_event_time` | J8 | 108–109 |
| `delete_event` | J1 J2 J3 J4 J5 | 112–121 |
| `push_block` | J8 | 124–146 |
| `get_managed_calendar_id` | J7 J8 | 149–152 |
| `SyncTokenExpiredError` | J7 J8 | 160–161 |
| `watch_calendar` | J7 J8 | 164–198 |
| `stop_watch` | J7 J8 | 201–211 |
| `initial_sync` | J7 J8 | 214–234 |
| `list_events_incremental` | J7 J8 | 237–274 |

### `backend/app/models.py`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `sqlalchemy.orm` | J1 J2 J3 J4 J5 J6 J7 J8 | 22–22 |
| `STATUSES` | J1 J2 J3 J4 J5 | 28–28 |
| `_ts` | J1 J2 J3 J4 J5 J6 J7 J8 | 32–33 |
| `User` | J1 J2 J3 J4 J5 J6 | 36–45 |
| `Session` | J1 J2 J3 J4 J5 J6 J7 J8 | 48–60 |
| `MagicLink` | J6 J8 | 63–73 |
| `TaskType` | J1 J2 J3 J4 J5 | 76–81 |
| `Task` | J1 J2 J3 J4 J5 J6 J8 | 84–121 |
| `Comment` | J1 J2 J3 J4 J5 | 124–141 |
| `CalendarBlock` | J1 J2 J3 J4 J5 J7 J8 | 144–160 |
| `GcalChannel` | J7 J8 | 163–181 |

### `backend/app/notifications.py`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `email.message` | J1 J2 J3 J4 J5 J6 J8 | 11–11 |
| `_settings` | J1 J2 J3 J4 J5 J6 J8 | 21–21 |
| `digest_configured` | J1 J2 J3 J4 J5 J6 J8 | 24–25 |
| `_send_email` | J1 J2 J3 J4 J5 J6 J8 | 46–55 |
| `send_scheduled` | J3 J8 | 58–75 |
| `send_login_link` | J6 | 89–97 |
| `send_task_update` | J1 J2 J3 J4 J5 | 100–117 |

### `backend/app/placement.py`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `sqlalchemy.orm` | J1 J2 J3 J4 J5 J8 | 15–15 |
| `Busy` | J1 J2 J3 J4 J5 J8 | 22–22 |
| `_settings` | J1 J2 J3 J4 J5 J8 | 24–24 |
| `_DAYS` | J1 J2 J3 J4 J5 J8 | 26–26 |
| `_OVERRUN_DAYS` | J1 J2 J3 J4 J5 J8 | 28–28 |
| `Window` | J1 J2 J3 J4 J5 J8 | 32–37 |
| `_parse_days` | J1 J2 J3 J4 J5 J8 | 40–50 |
| `parse_window` | J1 J2 J3 J4 J5 J8 | 53–59 |
| `parse_windows` | J1 J2 J3 J4 J5 J8 | 62–68 |
| `_first_free` | J1 J2 J3 J4 J5 J8 | 71–90 |
| `find_slot` | J1 J2 J3 J4 J5 J8 | 93–125 |
| `find_earliest_slot` | J1 J2 J3 J4 J5 J8 | 128–137 |
| `place_task` | J1 J2 J3 J4 J5 J8 | 140–170 |

### `backend/app/routers/auth.py`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `fastapi.responses` | J6 | 7–7 |
| `sqlalchemy.orm` | J6 | 10–10 |
| `_settings` | J6 | 23–23 |
| `MagicLinkResponse` | J6 | 30–31 |
| `RequestLinkResponse` | J6 | 34–35 |
| `_GENERIC_REQUEST_MESSAGE` | J6 | 38–38 |
| `_set_session_cookie` | J6 | 48–57 |
| `mint_magic_link` | J6 | 61–70 |
| `request_link` | J6 | 74–108 |
| `consume` | J6 | 112–119 |
| `logout` | J6 | 123–137 |
| `me` | J6 | 141–142 |

### `backend/app/routers/tasks.py`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `sqlalchemy.orm` | J1 J2 J3 J4 J5 | 16–16 |
| `_settings` | J4 | 37–37 |
| `_fmt_dt` | J4 | 40–43 |
| `_notify_others` | J3 J4 J5 | 46–57 |
| `_SUBMITTER_FIELDS` | J3 | 62–62 |
| `_validate_type` | J1 J3 | 65–67 |
| `_get_task_or_404` | J2 J3 J4 J5 | 70–74 |
| `list_task_types` | J2 | 78–79 |
| `list_tasks` | J2 | 83–93 |
| `get_task` | J2 | 97–98 |
| `create_task` | J1 | 102–120 |
| `patch_task` | J3 | 124–175 |
| `reschedule_task` | J4 | 193–215 |
| `list_comments` | J5 | 219–227 |
| `add_comment` | J5 | 231–245 |

### `backend/app/routers/webhooks.py`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `gcal_push` | J7 | 19–40 |

### `backend/app/scheduler.py`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `apscheduler.jobstores.sqlalchemy` | J8 | 17–17 |
| `apscheduler.schedulers.background` | J8 | 18–18 |
| `_settings` | J8 | 28–28 |
| `sweep` | J8 | 32–81 |
| `start` | J8 | 84–120 |

### `backend/app/scheduling.py`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `_settings` | J1 J2 J3 J4 J5 | 8–8 |
| `compute_due_date` | J1 J2 J3 J4 J5 | 11–22 |

### `backend/app/schemas.py`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `TaskCreate` | J1 J2 J3 J4 J5 | 11–17 |
| `TaskPatch` | J1 J2 J3 J4 J5 | 20–34 |
| `status_is_valid` | J3 | 33–34 |
| `TaskOut` | J1 J2 J3 J4 J5 | 37–53 |
| `TaskReschedule` | J1 J2 J3 J4 J5 | 56–60 |
| `TaskTypeOut` | J1 J2 J3 J4 J5 | 63–67 |
| `CommentCreate` | J1 J2 J3 J4 J5 | 70–71 |
| `CommentOut` | J1 J2 J3 J4 J5 | 74–82 |

### `backend/app/security.py`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `_settings` | J1 J2 J3 J4 J5 J6 | 18–18 |
| `_serializer` | J1 J2 J3 J4 J5 J6 | 19–19 |
| `now` | J1 J2 J3 J4 J5 J6 J7 J8 | 22–23 |
| `new_token` | J6 | 26–28 |
| `hash_token` | J6 | 31–32 |
| `sign_session` | J6 | 35–36 |
| `unsign_session` | J1 J2 J3 J4 J5 J6 | 39–44 |

### `frontend/src/lib/api.js`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `api` | J8! | 20–33 |

### `frontend/src/lib/components/TaskDetail.svelte`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `TaskDetail` | J8! | 1–210 |
| `onClose` | J8! | 7–7 |
| `toLocalInput` | J8! | 22–27 |
| `loadComments` | J8! | 29–35 |
| `fmt` | J8! | 82–88 |

### `frontend/src/lib/queue.js`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `QUEUE_KEY` | J8! | 4–4 |
| `TYPES_KEY` | J8! | 5–5 |
| `readJSON` | J8! | 7–13 |
| `getQueue` | J8! | 15–17 |
| `enqueue` | J8! | 19–27 |
| `dequeue` | J8! | 29–37 |
| `cacheTypes` | J8! | 39–41 |
| `getCachedTypes` | J8! | 43–45 |

### `frontend/src/lib/stores.js`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `user` | J8! | 4–4 |

### `frontend/src/routes/+page.svelte`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `queueCount` | J8! | 23–23 |
| `refreshQueueCount` | J8! | 48–50 |
| `load` | J8! | 52–67 |
| `flushQueue` | J8! | 69–97 |
| `handleOnline` | J8! | 99–101 |
| `patch` | J8! | 158–165 |

_137 symbols across 21 files reach at least one journey. Symbols reaching none are omitted._
