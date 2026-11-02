# core

Shared foundations, deliberately unaware of any individual service:

- `config.py` — pydantic `AppConfig` with one section per module, generated
  secrets, strict validation, YAML atomic save/load and deep merge.
- `db.py` — SQLite wrapper, migrations, transactions, settings, audit,
  sessions/users/events/access tables.
- `adapters.py` — `Runner`, `FileSystem`, `Systemd`, `Clock` protocols and real
  implementations.  Modules never call `subprocess`/`pathlib` directly.
- `context.py` — `Runtime`, the injected dependency container.
- `bus.py` — synchronous typed-ish event bus.
- `logging.py` — rotating file/console logs and install-log markers.
- `passwords.py` — argon2id dashboard hashing and copyparty-compatible hashes.
- `templates.py` — Jinja2 environment discoverable from checkout or wheel.
- `mounts.py` — `/proc/self/mounts` helpers.
- `metrics.py` — CPU/load/memory/throttle/UPS collection.
- `orchestrator.py` — validate → render → snapshot → atomic write → recharge
  with rollback.
- `types.py` — shared dataclasses/enums (`ServiceStatus`, `RenderedFile`, …).

## Testing

`tests/core/` covers config validation/round-trips, secret hashing, database
migrations/foreign keys/audit, the event bus, the apply engine (success,
validation/render failure, rollback, dry-run, snapshots) and the CLI skeleton.
