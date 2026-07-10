# VaultBridge

Self-hosted NAS backup panel for pulling website files from a remote Linux server, syncing them with `rsync`, and keeping every backup as a Git history point.

VaultBridge is built for small server operators who host websites on a VPS, aaPanel/BaoTa, or another Linux box and want simple scheduled backups on a NAS. The source server is treated as read-only: VaultBridge connects over SSH, copies files into a local NAS folder, commits the snapshot to Git, and lets you download any historical version as a zip archive.

```mermaid
flowchart LR
  A["Remote Linux server<br/>/www/wwwroot"] -->|"SSH + rsync"| B["NAS backup folder"]
  B --> C["Git repository<br/>one commit per backup"]
  C --> D["Web panel<br/>versions + zip downloads"]
```

## Highlights

- Web UI for creating, editing, pausing, resuming, stopping, and deleting backup runs.
- Daily or weekly schedules with configurable time.
- Fast incremental transfer with `rsync` over SSH; unchanged files are not sent again.
- Automatic resume after interrupted `rsync` transfers, keeping partial files locally.
- Git-backed backup history, so repeated unchanged files are stored efficiently.
- Version browser with one-click zip download for any Git commit.
- Read-only source workflow: VaultBridge does not write to, move, or delete files on the remote server.
- Encrypted SSH passwords at rest using a local Fernet key.

## Quick Start With Docker

Docker is the recommended deployment path for a NAS or a small always-on machine.

```bash
cp .env.example .env
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Put the generated key into `.env` as `BACKUP_SECRET_KEY`, then edit `VAULTBRIDGE_BACKUP_HOST_DIR` so it points to the NAS folder where backups should live.

```bash
docker compose up -d --build
```

Open the panel:

```text
http://<nas-ip>:8728
```

The Docker image includes `rsync`, `sshpass`, `openssh-client`, and `git`, so password-based SSH backups work without installing extra packages inside the container.

## First Backup Job

In the web panel, create a job similar to this:

| Field | Example |
| --- | --- |
| SSH host | `203.0.113.10` |
| SSH user | `root` or a dedicated read-only SSH user |
| SSH port | `22` |
| Backup paths | `/www/wwwroot` |
| Target path | `/server-backups/203.0.113.10` |
| Schedule | Daily at `03:00` |

For the bundled Docker Compose setup, map your NAS folder into the container and use a target path under that mount, such as `/server-backups/example-site`.

## Local Development

```bash
python -m venv .venv
.\.venv\Scripts\activate
pip install -e .[dev]
python -m app.main
```

Open `http://127.0.0.1:8728`.

On Windows, install cwRsync for the fastest transfer path:

```powershell
choco install rsync -y
```

VaultBridge detects the Chocolatey cwRsync package automatically. When password-based SSH is used, the password is supplied through OpenSSH `SSH_ASKPASS`, not placed directly in the visible `rsync` command line.

## Configuration

| Variable | Default | Description |
| --- | --- | --- |
| `VAULTBRIDGE_HOST` | `0.0.0.0` | Bind address for the web app. |
| `VAULTBRIDGE_PORT` | `8728` | Web panel port. |
| `VAULTBRIDGE_DATA_DIR` | `/app/data` in Docker | Stores the SQLite database, encryption key, and app state. |
| `BACKUP_SECRET_KEY` | auto-generated if absent | Fernet key used to encrypt stored SSH passwords. Set this explicitly in production. |
| `VAULTBRIDGE_BACKUP_HOST_DIR` | `./backups` | Host folder mounted into Docker as `/server-backups`. |
| `VAULTBRIDGE_RSYNC_WORKERS` | `3` | Number of parallel `rsync` workers, clamped between 1 and 8. |
| `VAULTBRIDGE_RSYNC_RETRIES` | `2` | Retries per `rsync` shard after a dropped connection. |
| `VAULTBRIDGE_RSYNC_RESUME_RETRIES` | `10` | Whole-run automatic resume attempts before the run is marked failed. |

## Backup Behavior

VaultBridge prefers this transfer order:

1. `rsync` over SSH for incremental syncs.
2. Remote `tar` streaming when local `rsync` is unavailable.
3. SFTP recursion as the compatibility fallback.

Each job creates this layout under its target folder:

```text
<target path>/
  repository/
    .git/
    snapshot/
      www/wwwroot/...
  archives/
    vaultbridge-<commit>.zip
```

Only `repository/snapshot` is versioned. Generated zip files live outside the Git history.

## Default Excludes

Git is a good fit for website code, templates, config, and uploaded assets that change incrementally. To keep repositories practical, VaultBridge excludes noisy or heavy runtime paths by default, including:

- `.git`, `node_modules`, `.venv`, `venv`, `env`, `site-packages`
- `__pycache__`, `*.pyc`, `.cache`, `cache`, `tmp`
- `logs`, `*.log`
- common archives and dumps such as `*.tar.gz`, `*.zip`, `*.7z`, `*.bak`, `*.dump`, `*.sql.gz`
- SQLite sidecar files such as `*.sqlite-shm`, `*.sqlite-wal`, `*.db-shm`, `*.db-wal`

Linux symlinks are skipped by default to avoid duplicate release directories and Windows Git indexing problems.

## Restoring A Version

Open the Versions page, choose a backup task, select a commit, and download the generated zip. The archive is built from the Git commit, so it represents the snapshot exactly as it existed at that backup time.

You can also inspect the repository directly on disk:

```bash
cd <target path>/repository
git log --oneline
git checkout <commit> -- snapshot
```

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| `Error reading SSH protocol banner` | The port is probably not the SSH port. Confirm the server SSH port, firewall, and security group. |
| Transfer feels slow on the first run | The first backup must copy real file contents. Later runs should only transfer changed files. |
| Run stops during rsync | VaultBridge keeps partial files and retries automatically. Start the run again if all retries are exhausted. |
| Git commit takes a while | Large first-time snapshots can take time while Git writes object data. |
| Too few files are backed up | Review the default excludes and confirm the configured source path is the directory you expect. |

## Project Layout

```text
app/
  main.py          FastAPI routes
  backup.py        backup orchestration, Git commits, run control
  rsync_client.py  rsync discovery, command building, progress parsing
  ssh_client.py    SSH, SFTP, tar fallback helpers
  static/          single-page web UI
scripts/
  install_nas.sh   optional systemd install helper
tests/             regression tests for paths, rsync, Docker config
```

## Status

VaultBridge is a focused self-hosted backup tool. It is useful today for SSH-accessible Linux website folders, but you should still test restore downloads before relying on it for production recovery.

Planned improvements include backup verification, retention policies, notifications, and committed UI screenshots for the README.

## Security Notes

- Do not commit real SSH passwords, server keys, panel API keys, or `.env` files.
- Prefer a dedicated SSH user with read access to the folders you need to back up.
- Keep `BACKUP_SECRET_KEY` stable after deployment; changing it prevents existing encrypted passwords from being decrypted.

## License

No license file is included yet.
