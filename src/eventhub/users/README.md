# users

Users, groups, roles, guest accounts/links and credential synchronisation.

## Roles

| Role | Default permission | Dashboard | copyparty | SMB |
| --- | --- | --- | --- | --- |
| `admin` | admin | full | `rwmda` | write |
| `editor` | write | full | `rwmd` | write |
| `shooter` | upload | uploads only | `w` | ingest share write |
| `viewer` | read | read | `r` | read |

Per-event access overrides live in the `access` table and take precedence over
the role default.  `eventhub.events.EventsService.permission_for()` resolves
them.

## Config (`users:` section)

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `true` | |
| `default_role` | `viewer` | Applied by callers that do not specify a role. |
| `min_password_length` | `12` | Enforced on create/set-password. |
| `guest_default_hours` | `24` | Guest account/link lifetime. |
| `system_user_prefix` | `eh_` | Unix/Samba username prefix. |
| `allow_guest_accounts` | `true` | |
| `default_group` | `eventhub-users` | Unix group for SMB users. |

## Passwords

- Dashboard passwords are argon2id (`argon2-cffi`), rehashed on login when the
  parameters change.
- copyparty needs a verifiable hash, so each user also stores a
  copyparty-compatible argon2 hash made with
  `fileserver_copyparty.password_salt`.  No plaintext password is stored.
- SMB passwords live in Samba's tdbsam passdb, synced with `smbpasswd` when a
  password is created or changed.

## Guest access

`create_guest_account()` generates a random password and a temporary user.
`create_guest_link()` returns an opaque token exactly once; only its SHA-256
hash is stored, with expiry and optional maximum uses.  `verify_guest_link()`
consume-checks the token.

## Safety

The last enabled administrator cannot be deleted, disabled or demoted.
Expired guest users are purged during apply.
