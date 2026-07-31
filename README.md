# icloud-shared-albums-backup

Download photos and videos from **private (non-public) iCloud Shared Albums** — the kind shared with specific invited people, as opposed to a public "Shared Album website" link.

`icloudpd`/`pyicloud` currently support the Personal Library and Shared Library (iOS 16+), but not classic Shared Albums ([see this open issue](https://github.com/icloud-photos-downloader/icloud_photos_downloader/issues/1019)). This script fills that gap by reusing `pyicloud` for authentication and talking directly to the same `sharedstreams` endpoints the iCloud.com web UI uses.

**Status:** works against a real account, but relies on undocumented, unofficial Apple endpoints that could change at any time. Use at your own risk, and keep an independent backup of anything irreplaceable.

## What it does

- Logs in with your Apple ID (handles 2FA)
- Lists all your private shared albums and fetches their real names
- Scans each album first (photo count + total size) before downloading anything
- Downloads everything with resumability — safe to re-run, already-downloaded files are skipped
- Shows live progress (album X/Y, photos done, data transferred) and writes a full log file

## Requirements

- **Python 3.9 or newer**
- Python packages:
  - [`pyicloud`](https://pypi.org/project/pyicloud/) — handles Apple ID authentication (incl. 2FA)
  - [`requests`](https://pypi.org/project/requests/) — HTTP calls (installed automatically as a `pyicloud` dependency, but listed explicitly since the script imports it directly)
- An Apple ID with at least one private Shared Album you're a member of or own
- Windows, macOS, or Linux — the Python script itself is platform-independent (the optional `.ps1` wrapper is Windows-only)

## Installation

```bash
pip install pyicloud requests
```

## Usage

1. Download `icloud_shared_albums_backup.py`
2. Open it and edit the two settings near the top:
   ```python
   APPLE_ID = "your_apple_id@icloud.com"
   TARGET_DIR = r"D:\Backup\SharedAlbumsPrivate"   # or e.g. "/home/you/backup/shared_albums"
   ```
3. Run it:
   ```bash
   python icloud_shared_albums_backup.py
   ```
4. Enter your Apple ID password and 2FA code when prompted.

The script will scan all your shared albums, then download everything into `TARGET_DIR/<album name>/`. Re-running it later only downloads new photos.

### Windows convenience wrapper (optional)

`run_icloud_backup.ps1` is an optional PowerShell wrapper that checks for Python, installs missing packages automatically, and runs the script. Place it in the same folder as the `.py` file and run:

```powershell
.\run_icloud_backup.ps1
```

## Output

- `<TARGET_DIR>/<album name>/` — one folder per shared album, containing the original photos/videos
- `icloud_shared_albums.log` — full run log (every file, every error, useful for troubleshooting)
- `icloud_album_names_cache.json` — caches album names so repeat runs don't re-fetch them

## Known limitations

- **No unattended/scheduled runs**: the script prompts interactively for your password and 2FA code. It works fine for a manual, periodic backup, but isn't currently suitable for a fully automated cron/Task Scheduler job — the cached Apple session eventually expires and needs a fresh interactive login.
- Relies on reverse-engineered, undocumented endpoints — Apple could change these without notice.
- Only downloads the highest available resolution per photo; doesn't preserve every piece of metadata iCloud stores (e.g. comments/likes on shared photos).

## Contributing

Happy to see this cleaned up further or adapted into `pyicloud`/`icloudpd` proper — see the [background issue](https://github.com/icloud-photos-downloader/icloud_photos_downloader/issues/1019) for context. PRs welcome.

## License

MIT (or add whichever license you prefer here)
