# storage

Detects removable drives, mounts them by UUID at boot, reports free space and
provides safe eject and a heavily-confirmed format wizard hook.

## Config (`storage:` section)

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `true` | |
| `required` | `false` | Fail apply if the drive is missing. |
| `mount_root` | `/media/eventhub` | Parent for mounts. |
| `drive.uuid` | `""` | Preferred selector. |
| `drive.label` / `drive.device` | `""` | Fallbacks when no UUID exists. |
| `drive.fstype` | `auto` | `auto`, `ext4`, `exfat`, `vfat`, `xfs`, `btrfs`. |
| `drive.mount_point` | `/media/eventhub/data` | Mount target. |
| `drive.options` | `noatime,nofail,...` | fstab-only options are stripped for the unit. |
| `drive.auto_mount` | `true` | Generate/enable a systemd `.mount` unit. |
| `warn_percent` / `critical_percent` | `80` / `90` | Dashboard and health thresholds. |
| `allow_format` | `true` | Formatting still requires the `FORMAT` token. |
| `minimum_free_gb` | `2` | Pre-ingest free-space check. |

When `drive.uuid` is set, `events.root` resolves to
`drive.mount_point/events`, so selecting a drive in the wizard moves event data
onto it automatically.

## Behaviour

- `render()` emits an escaped systemd `.mount` unit (for example
  `media-eventhub-data.mount`) with `ConditionPathExists`, so a missing drive
  never blocks boot.
- `apply()` enables and starts the unit, or reports an optional-drive warning.
- `unmount()` stops the unit, runs `sync`, then `umount`; `--force` passes `-f`.
- `format()` requires the exact `FORMAT` token, a removable detected drive,
  and only supports ext4/exfat. It never formats a mounted device.
- If a UUID is configured but the drive is absent, event creation is refused
  instead of writing into an unmounted mount point (which would hide files
  after the real drive is attached).

Real mounts, USB hotplug and filesystem formatting can only be verified on the
Pi; tests use a fake mount table and fake runner.
