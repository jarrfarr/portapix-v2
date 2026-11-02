# Security

## Defaults

- The hotspot is never open; a WPA2/WPA3 passphrase is required.
- First-boot secrets (wifi password, dashboard session key, copyparty salt) are
  generated with `secrets` and written with mode `0600`.
- No shared default password exists in the final state.  The generated admin
  password is written to `/var/lib/eventhub/first-boot-admin.txt` (mode `0600`)
  and must be changed on first login.
- Dashboard passwords are argon2id; copyparty passwords are argon2 hashes with a
  dedicated salt; SMB passwords live in Samba's tdbsam passdb.

## Network exposure

- nftables input policy is drop.  Only the AP subnet and wired LAN may reach
  dashboard, copyparty, SMB, DNS/DHCP, mDNS and SSH.
- WAN input allows only replies to outbound traffic and DHCP.
- Forwarding is limited to AP/LAN → uplink when internet sharing is enabled;
  masquerading prevents clients from reaching the uplink's own LAN.
- The dashboard binds all interfaces but is unreachable from WAN because of the
  firewall.  If you add another firewall, preserve that default-drop policy.
- Avahi is bound to AP/LAN, not WAN.  AirDrop uses its own AWDL interface.

## Privilege boundary

- The dashboard runs as `eventhub-web` with `CAP_NET_BIND_SERVICE`, a strict
  systemd sandbox and no root.
- It can read/write only its state DB (sessions/login) and logs; everything else
  goes through the helper socket.
- The helper runs as root and exposes a fixed allow-list of actions.  It never
  accepts shell commands, executable paths, arbitrary devices or arbitrary
  filesystem paths.  Socket access is `SO_PEERCRED` + group/mode restricted.
- Every helper action is audited in SQLite.

## Authentication and sessions

- Login is rate-limited per IP; failed attempts are audited.
- Session IDs are random and stored server-side; the cookie is signed with
  itsdangerous, `HttpOnly`, `SameSite=Lax`, and `Secure` when configured.
- Every POST requires a per-session CSRF token; logout revokes the session.
- Dashboard admin pages and all mutations require the `admin` role.
- Host header guard allows configured names and IP literals.

## Files and secrets

- Config is YAML mode `0600` in a `0750 root:eventhub` directory, so only root
  reads it.  Dashboard settings are a separate `0640 root:eventhub-web` file.
- Rendered hostapd config (contains the WPA passphrase) is `0600`.
- Snapshots of rendered configs are `0600`; prune them if they contain secrets.
- Backups (`/var/lib/eventhub/backups`) contain config and the user DB; protect
  and delete them as carefully as the originals.
- The helper never returns secrets except the deliberate one-time admin reset
  result to an authenticated dashboard admin action.

## Updates and upstream code

- EventHub requires an explicit update action; it backs up config/DB and rolls
  back on failure.
- AirDrop pins `t4t5/owly` at `v0.4.2`; dependencies are pinned in
  `pyproject.toml`.  `bootstrap.sh` is the only shell entry point in this repo.
- Review upstream owly build scripts before enabling AirDrop on a production
  hub; the installer runs `packaging/install.sh` from the pinned checkout.

## Recommendations

- Change the admin password immediately; delete the first-boot file.
- Keep the hub offline unless you need internet; sharing is optional.
- Use a unique wifi password, and do not expose the dashboard over the internet.
- Use the official power supply; undervoltage is a reliability risk.
- Keep the OS and EventHub updated between shoots, not during one.
