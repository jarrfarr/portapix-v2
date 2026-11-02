# fileserver_smb

Samba shares per event, tuned for large RAW/video files.

## Config (`fileserver_smb:` section)

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `true` | |
| `workgroup` | `EVENTHUB` | |
| `server_string` | `EventHub` | |
| `config_path` | `/etc/samba/smb.conf` | Full generated config. |
| `min_protocol` / `max_protocol` | `SMB2` / `SMB3_11` | |
| `socket_options` | `TCP_NODELAY IPTOS_LOWDELAY ...` | |
| `aio_read_size` / `aio_write_size` | `131072` | |
| `use_sendfile` / `strict_sync` | `true` / `false` | |
| `veto_files` | macOS/Windows junk | |
| `hide_dot_files` | `true` | |

## Shares

- `[Event_<slug>]` — event root; readers read-only, editors/admins write.
- `[Event_<slug>_Ingest]` — ingest folder; shooters/editors/admins upload.
- `[ActiveEvent]` — alias for the active event.
- `[Inbox]` — AirDrop/default upload inbox.

Samba usernames are `<system_user_prefix><eventhub username>` (for example
`eh_alice`).  Unix system users are created on demand in the
`eventhub-users` group; passwords are written only to Samba's tdbsam passdb.

## Limitations

- Samba grants write or read to a whole share.  Shooters are therefore given a
  write share only for `ingest`; they cannot browse the rest of the event.
  They can delete within that ingest share, unlike the copyparty `w`
  permission.  Use copyparty when delete prevention matters.
- `smbd.service` is used on Debian/Fedora; Arch's `smb.service` is detected
  when it is the only unit present.
- macOS metadata (AppleDouble files) is vetoed to keep event folders clean.
- Real client compatibility (macOS Finder, Windows Explorer) and transfer
  throughput can only be verified on the Pi.
