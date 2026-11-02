# EventHub

A portable Raspberry Pi NAS and transfer hub for event photo/video shoots.

EventHub creates its own wifi network, presents a web dashboard at
`http://eventhub.local`, serves files over SMB and copyparty, and receives
AirDrop transfers — all without needing the internet.

Full documentation is in [`docs/`](docs/), with the architecture in
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) and the build plan in
[`plan.md`](plan.md).

## Quick start (on a Raspberry Pi)

```sh
curl -fsSL https://raw.githubusercontent.com/OWNER/eventhub/main/bootstrap.sh | sudo bash
```

or, from a checkout:

```sh
sudo ./bootstrap.sh
```

The installer opens a terminal UI. See [`docs/INSTALL.md`](docs/INSTALL.md).

## Development

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
.venv/bin/ruff check .
.venv/bin/mypy
```

Everything except `bootstrap.sh` and thin systemd/udev glue is Python.
