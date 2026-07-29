# Journey map — which user journeys depend on which symbols

Target: `/home/ericm/personal_projects/honeyslate/main` · index schema 8 · generated from commit `1cd0385`

Look up the symbols you changed. Every journey listed for them may have changed behavior and is worth verifying. This is **recall-first**: a shared symbol legitimately fans out to many journeys.

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

| lines | symbol | journeys |
|---|---|---|
| 7–7 | `sqlalchemy.orm` | J6 |
| 13–13 | `_settings` | J6 |
| 16–30 | `create_magic_link` | J6 |
| 33–53 | `consume_magic_link` | J6 |

### `backend/app/config.py`

| lines | symbol | journeys |
|---|---|---|
| 13–13 | `ENV_FILE` | J1 J2 J3 J4 J5 J8 |
| 16–78 | `Settings` | J1 J2 J3 J4 J5 J6 J7 J8 |
| 84–89 | `get_settings` | J1 J2 J3 J4 J5 J6 J7 J8 |
| 92–109 | `load_type_windows` | J1 J2 J3 J4 J5 J8 |

### `backend/app/db.py`

| lines | symbol | journeys |
|---|---|---|
| 4–4 | `collections.abc` | J1 J2 J3 J4 J5 J6 J7 J8 |
| 7–7 | `sqlalchemy.orm` | J1 J2 J3 J4 J5 J6 J7 J8 |
| 12–13 | `Base` | J1 J2 J3 J4 J5 J6 J7 J8 |
| 22–22 | `SessionLocal` | J1 J2 J3 J4 J5 J6 J7 J8 |
| 25–31 | `get_db` | J1 J2 J3 J4 J5 J6 |

### `backend/app/deps.py`

| lines | symbol | journeys |
|---|---|---|
| 6–6 | `sqlalchemy.orm` | J1 J2 J3 J4 J5 J6 |
| 34–37 | `require_user` | J1 J2 J3 J4 J5 J6 |
| 40–43 | `require_operator` | J1 J2 J3 J4 J5 J6 |

### `backend/app/gcal_sync.py`

| lines | symbol | journeys |
|---|---|---|
| 36–91 | `sync_channel` | J8 |
| 94–133 | `reconcile` | J7 |
| 136–146 | `_parse_gcal_time` | J7 |
| 149–187 | `_apply_event` | J7 |

### `backend/app/google_calendar.py`

| lines | symbol | journeys |
|---|---|---|
| 25–25 | `_settings` | J1 J2 J3 J4 J5 J7 J8 |
| 28–28 | `SCOPES` | J1 J2 J3 J4 J5 J7 J8 |
| 29–29 | `CALENDAR_SUMMARY` | J1 J2 J3 J4 J5 J7 J8 |
| 33–36 | `calendar_configured` | J1 J2 J3 J4 J5 J7 J8 |
| 49–66 | `_build_service` | J1 J2 J3 J4 J5 J7 J8 |
| 69–105 | `_resolve_calendar` | J1 J2 J3 J4 J5 J7 J8 |
| 108–109 | `_event_time` | J8 |
| 112–121 | `delete_event` | J1 J2 J3 J4 J5 |
| 124–146 | `push_block` | J8 |
| 149–152 | `get_managed_calendar_id` | J7 J8 |
| 160–161 | `SyncTokenExpiredError` | J7 J8 |
| 164–198 | `watch_calendar` | J7 J8 |
| 201–211 | `stop_watch` | J7 J8 |
| 214–234 | `initial_sync` | J7 J8 |
| 237–274 | `list_events_incremental` | J7 J8 |

### `backend/app/models.py`

| lines | symbol | journeys |
|---|---|---|
| 22–22 | `sqlalchemy.orm` | J1 J2 J3 J4 J5 J6 J7 J8 |
| 28–28 | `STATUSES` | J1 J2 J3 J4 J5 |
| 32–33 | `_ts` | J1 J2 J3 J4 J5 J6 J7 J8 |
| 36–45 | `User` | J1 J2 J3 J4 J5 J6 |
| 48–60 | `Session` | J1 J2 J3 J4 J5 J6 J7 J8 |
| 63–73 | `MagicLink` | J6 J8 |
| 76–81 | `TaskType` | J1 J2 J3 J4 J5 |
| 84–121 | `Task` | J1 J2 J3 J4 J5 J6 J8 |
| 124–141 | `Comment` | J1 J2 J3 J4 J5 |
| 144–160 | `CalendarBlock` | J1 J2 J3 J4 J5 J7 J8 |
| 163–181 | `GcalChannel` | J7 J8 |

### `backend/app/notifications.py`

| lines | symbol | journeys |
|---|---|---|
| 11–11 | `email.message` | J1 J2 J3 J4 J5 J6 J8 |
| 21–21 | `_settings` | J1 J2 J3 J4 J5 J6 J8 |
| 24–25 | `digest_configured` | J1 J2 J3 J4 J5 J6 J8 |
| 46–55 | `_send_email` | J1 J2 J3 J4 J5 J6 J8 |
| 58–75 | `send_scheduled` | J3 J8 |
| 89–97 | `send_login_link` | J6 |
| 100–117 | `send_task_update` | J1 J2 J3 J4 J5 |

### `backend/app/placement.py`

| lines | symbol | journeys |
|---|---|---|
| 15–15 | `sqlalchemy.orm` | J1 J2 J3 J4 J5 J8 |
| 22–22 | `Busy` | J1 J2 J3 J4 J5 J8 |
| 24–24 | `_settings` | J1 J2 J3 J4 J5 J8 |
| 26–26 | `_DAYS` | J1 J2 J3 J4 J5 J8 |
| 28–28 | `_OVERRUN_DAYS` | J1 J2 J3 J4 J5 J8 |
| 32–37 | `Window` | J1 J2 J3 J4 J5 J8 |
| 40–50 | `_parse_days` | J1 J2 J3 J4 J5 J8 |
| 53–59 | `parse_window` | J1 J2 J3 J4 J5 J8 |
| 62–68 | `parse_windows` | J1 J2 J3 J4 J5 J8 |
| 71–90 | `_first_free` | J1 J2 J3 J4 J5 J8 |
| 93–125 | `find_slot` | J1 J2 J3 J4 J5 J8 |
| 128–137 | `find_earliest_slot` | J1 J2 J3 J4 J5 J8 |
| 140–170 | `place_task` | J1 J2 J3 J4 J5 J8 |

### `backend/app/routers/auth.py`

| lines | symbol | journeys |
|---|---|---|
| 7–7 | `fastapi.responses` | J6 |
| 10–10 | `sqlalchemy.orm` | J6 |
| 23–23 | `_settings` | J6 |
| 30–31 | `MagicLinkResponse` | J6 |
| 34–35 | `RequestLinkResponse` | J6 |
| 38–38 | `_GENERIC_REQUEST_MESSAGE` | J6 |
| 48–57 | `_set_session_cookie` | J6 |
| 61–70 | `mint_magic_link` | J6 |
| 74–108 | `request_link` | J6 |
| 112–119 | `consume` | J6 |
| 123–137 | `logout` | J6 |
| 141–142 | `me` | J6 |

### `backend/app/routers/tasks.py`

| lines | symbol | journeys |
|---|---|---|
| 16–16 | `sqlalchemy.orm` | J1 J2 J3 J4 J5 |
| 37–37 | `_settings` | J4 |
| 40–43 | `_fmt_dt` | J4 |
| 46–57 | `_notify_others` | J3 J4 J5 |
| 62–62 | `_SUBMITTER_FIELDS` | J3 |
| 65–67 | `_validate_type` | J1 J3 |
| 70–74 | `_get_task_or_404` | J2 J3 J4 J5 |
| 78–79 | `list_task_types` | J2 |
| 83–93 | `list_tasks` | J2 |
| 97–98 | `get_task` | J2 |
| 102–120 | `create_task` | J1 |
| 124–175 | `patch_task` | J3 |
| 193–215 | `reschedule_task` | J4 |
| 219–227 | `list_comments` | J5 |
| 231–245 | `add_comment` | J5 |

### `backend/app/routers/webhooks.py`

| lines | symbol | journeys |
|---|---|---|
| 19–40 | `gcal_push` | J7 |

### `backend/app/scheduler.py`

| lines | symbol | journeys |
|---|---|---|
| 17–17 | `apscheduler.jobstores.sqlalchemy` | J8 |
| 18–18 | `apscheduler.schedulers.background` | J8 |
| 28–28 | `_settings` | J8 |
| 32–81 | `sweep` | J8 |
| 84–120 | `start` | J8 |

### `backend/app/scheduling.py`

| lines | symbol | journeys |
|---|---|---|
| 8–8 | `_settings` | J1 J2 J3 J4 J5 |
| 11–22 | `compute_due_date` | J1 J2 J3 J4 J5 |

### `backend/app/schemas.py`

| lines | symbol | journeys |
|---|---|---|
| 11–17 | `TaskCreate` | J1 J2 J3 J4 J5 |
| 20–34 | `TaskPatch` | J1 J2 J3 J4 J5 |
| 33–34 | `status_is_valid` | J3 |
| 37–53 | `TaskOut` | J1 J2 J3 J4 J5 |
| 56–60 | `TaskReschedule` | J1 J2 J3 J4 J5 |
| 63–67 | `TaskTypeOut` | J1 J2 J3 J4 J5 |
| 70–71 | `CommentCreate` | J1 J2 J3 J4 J5 |
| 74–82 | `CommentOut` | J1 J2 J3 J4 J5 |

### `backend/app/security.py`

| lines | symbol | journeys |
|---|---|---|
| 18–18 | `_settings` | J1 J2 J3 J4 J5 J6 |
| 19–19 | `_serializer` | J1 J2 J3 J4 J5 J6 |
| 22–23 | `now` | J1 J2 J3 J4 J5 J6 J7 J8 |
| 26–28 | `new_token` | J6 |
| 31–32 | `hash_token` | J6 |
| 35–36 | `sign_session` | J6 |
| 39–44 | `unsign_session` | J1 J2 J3 J4 J5 J6 |

### `frontend/src/lib/api.js`

| lines | symbol | journeys |
|---|---|---|
| 20–33 | `api` | J8! |

### `frontend/src/lib/components/TaskDetail.svelte`

| lines | symbol | journeys |
|---|---|---|
| 1–210 | `TaskDetail` | J8! |
| 7–7 | `onClose` | J8! |
| 22–27 | `toLocalInput` | J8! |
| 29–35 | `loadComments` | J8! |
| 82–88 | `fmt` | J8! |

### `frontend/src/lib/queue.js`

| lines | symbol | journeys |
|---|---|---|
| 4–4 | `QUEUE_KEY` | J8! |
| 5–5 | `TYPES_KEY` | J8! |
| 7–13 | `readJSON` | J8! |
| 15–17 | `getQueue` | J8! |
| 19–27 | `enqueue` | J8! |
| 29–37 | `dequeue` | J8! |
| 39–41 | `cacheTypes` | J8! |
| 43–45 | `getCachedTypes` | J8! |

### `frontend/src/lib/stores.js`

| lines | symbol | journeys |
|---|---|---|
| 4–4 | `user` | J8! |

### `frontend/src/routes/+page.svelte`

| lines | symbol | journeys |
|---|---|---|
| 23–23 | `queueCount` | J8! |
| 48–50 | `refreshQueueCount` | J8! |
| 52–67 | `load` | J8! |
| 69–97 | `flushQueue` | J8! |
| 99–101 | `handleOnline` | J8! |
| 158–165 | `patch` | J8! |

_137 symbols across 21 files reach at least one journey. Symbols reaching none are omitted._
