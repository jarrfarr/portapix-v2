# fileserver_copyparty

Renders copyparty's INI-style config and controls the service.

## Config (`fileserver_copyparty:` section)

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `true` | |
| `port` / `bind` | `3923` / `0.0.0.0` | HTTP listener. |
| `base_url` | `/` | Set `rp-loc` when mounted under a path. |
| `config_path` | `/etc/eventhub/copyparty.conf` | Mode `0600`. |
| `home_dir` | `/var/lib/eventhub/copyparty` | Hist/thumbnails/landing. |
| `password_salt` | generated | Must match the stored copyparty hashes. |
| `thumbnails` | `auto` | `auto`, `pyvips`, `pil`, `off`. |
| `thumbnail_age_days` | `30` | `th-maxage`. |
| `extra_args` | `[]` | Extra global copyparty arguments. |
| `extra_volumes` | `{}` | Additional volumes with an absolute `path`. |

## Generated volumes

- `/` → a landing folder (admins writable, all users read).
- `/inbox` → AirDrop/default inbox; all users can write, editors/admins read.
- `/events/<slug>` → per-event folder, permissions derived from the access
  matrix; archived events are read-only.
- `/active` → alias for the active event.
- `extra_volumes` as configured.

Uploads are resumable (copyparty up2k), thumbnails are enabled unless
configured off, and accounts use copyparty's argon2 verification with the stored
per-user `+hash` values.

## Limitations

- The `shooter` permission maps to copyparty `w` on the event volume, so a
  shooter can upload anywhere inside that event volume.  SMB restricts shooters
  to the ingest share.  This is documented rather than pretending the two
  protocols have identical semantics.
- Running copyparty and Samba against the same files is safe (both are just
  file access), but deleting through one is immediately visible in the other.
- Real thumbnails/HEIC/RAW decoding depend on installed optional codecs and can
  only be verified on the Pi.
