# events

Event lifecycle: create, activate, archive/restore, per-event access and the
ingest layout.

## Config (`events:` section)

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `true` | |
| `root` | auto | Explicit event root; otherwise storage mount/events or data_root/events. |
| `active_event` | `null` | Slug of the active event (AirDrop/default upload target). |
| `layout` | `ingest/{user}`, `raw`, `edit`, `exports`, `_eventhub` | Relative layout created per event. |
| `archive_is_read_only` | `true` | Fileservers render archived events read-only. |
| `auto_activate_new_event` | `true` | First event becomes active automatically. |
| `keep_metadata` | `true` | Writes `_eventhub/metadata.json`. |
| `default_ingest_user` | `anonymous` | Destination for uploads without a user. |
| `slug_max_length` | `48` | Slug length limit. |

## Behaviour

- Slugs are ASCII, lowercase, hyphenated and collision-suffixed (`wedding-2`).
- Creating an event inserts a DB row, creates the layout with group-writable
  permissions, writes metadata and optionally activates it.
- Activation is atomic: the previous active event becomes `open`, the new one
  `active`, and `events.active_event` is updated. Callers persist the config
  and re-apply so file-server volumes update.
- Archiving sets status, clears active if needed and writes an `ARCHIVED`
  marker.  The module never deletes event data.
- `ingest_target(user)` resolves the active event and creates
  `ingest/<user>`; unsafe usernames fall back to `anonymous`.
- `permission_for()` uses the explicit `access` row, otherwise the user's role
  default (`admin`→admin, `editor`→write, `shooter`→upload, `viewer`→read).
- Event creation is refused when a configured storage drive is not mounted.
