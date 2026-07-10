# VaultBridge

VaultBridge is a Python web panel for backing up website files from a remote Linux server to a NAS folder. The source server is read only: VaultBridge prefers `rsync` over SSH for incremental transfer, then commits the local copy into a Git repository on the backup disk. If local `rsync` is not available, it falls back to remote `tar` streaming, then SFTP.

## What it does

- Configure one or more remote folders to back up.
- Schedule daily or weekly backups from the web panel.
- Transfer with `rsync` when available, so unchanged files are not sent again.
- Store backups in a local Git repository, so unchanged files are not duplicated every day.
- View historical commits and download any version as a zip file.
- Show run details with file count, received files, bytes, current path, and task status.
- Keep SSH passwords encrypted at rest with a local Fernet key.

## Quick start

```bash
python -m venv .venv
.\.venv\Scripts\activate
pip install -e .[dev]
python -m app.main
```

Open `http://127.0.0.1:8728`.

For faster backups on Windows, install cwRsync first:

```powershell
choco install rsync -y
```

VaultBridge detects the Chocolatey cwRsync package automatically. When password-based SSH is used, it supplies the password through OpenSSH `SSH_ASKPASS` instead of putting the password in the rsync command line.

## Recommended NAS deployment

Run VaultBridge on the NAS or on a machine where the NAS backup folder is mounted locally.

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Put the generated value in `.env` as `BACKUP_SECRET_KEY`, then:

```bash
docker compose up -d --build
```

The Docker image installs `rsync`, `sshpass`, `openssh-client`, and `git`, so password-based rsync works without extra manual setup. For a non-Docker Linux/NAS install, install those packages yourself if you want the fastest incremental transfer path.

The compose file maps:

```text
/storage/Files/服务器备份/147.189.128.208
```

into the container, matching the intended NAS folder.

If your NAS has systemd and Python available, you can also install it as a service:

```bash
sudo APP_DIR=/opt/vaultbridge bash scripts/install_nas.sh
```

Copy the project folder to `/opt/vaultbridge` first, or adjust `APP_DIR` to the folder where you placed it.

## Suggested first job

- Source host: your server IP
- Source user: `root` or a read-only SSH user if you create one later
- Source folder: `/www/wwwroot`
- Backup folder: `/storage/Files/服务器备份/147.189.128.208`
- Schedule: daily or weekly at the time you prefer

Do not commit real SSH passwords, panel API keys, or server secrets to GitHub. Configure them only through the web panel or local `.env`.

## Data layout

For each job, VaultBridge creates:

```text
<backup folder>/
  repository/
    .git/
    snapshot/
      www/wwwroot/...
  archives/
    <generated version zip files>
```

Only `repository/snapshot` is versioned. The source server is never modified.

## Notes

Git is convenient for website files, templates, and uploads that change incrementally. Very large binary media, dependency folders, caches, generated folders, and runtime SQLite sidecar files can still make a repository heavy or noisy, so VaultBridge includes default excludes such as `.git`, `node_modules`, `.venv`, `site-packages`, cache, log folders, `*.sqlite-shm`, `*.sqlite-wal`, and common archive/backup files like `.tar.gz`, `.zip`, `.7z`, and `.bak`.
