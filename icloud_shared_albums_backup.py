#!/usr/bin/env python3
"""
Download photos and videos from PRIVATE (non-public) iCloud Shared Albums.

Background
----------
icloudpd / pyicloud currently only support the "Personal Library" and
"Shared Library" (iOS 16+). Classic Shared Albums (shared with specific
invited people, as opposed to a public "Shared Album website" link) are
not supported - see:
https://github.com/icloud-photos-downloader/icloud_photos_downloader/issues/1019

This script fills that gap. It reuses pyicloud for authentication (Apple ID
login + 2FA) and then talks directly to the same `sharedstreams` endpoints
that icloud.com/photos uses in the browser, discovered by inspecting a HAR
capture of the web UI.

How it works
------------
1. POST .../sharedstreams/getchanges
   Lists all shared albums (owned + subscribed). Each album has an
   `albumguid` and an `albumlocation` URL containing a per-album TOKEN -
   that token (not the albumguid) is the URL path segment used for all
   further requests about that specific album.

2. POST .../sharedstreams/webgetalbumview
   Fetches the human-readable album name (not included in `getchanges`).
   Supports batching, but large batches (>~15-20 guids) were unreliable in
   testing, so this falls back to small batches with adaptive splitting on
   failure.

3. POST .../sharedstreams/webgetassets
   Lists (and gives signed download URLs for) the photos/videos in an
   album. IMPORTANT: `offset`/`limit` describe a photo-index RANGE
   [offset, limit), not "start + page size". Sending a constant `limit`
   value on every request (as if it were a page size) makes the second
   request ask for an empty range and silently truncates every album
   over ~100 photos. See `get_album_assets()` below.

Requirements
------------
    pip install pyicloud requests

Usage
-----
Fill in APPLE_ID and TARGET_DIR below, then run:
    python icloud_shared_albums_backup.py

Status
------
Works against a real account as of testing in July 2026, but this relies
on undocumented, unofficial endpoints that Apple could change at any time.
Contributions / PRs adapting this into pyicloud proper are welcome.
"""

import sys
import json
import uuid
import re
import getpass
import logging
import base64
import threading
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import datetime
from pathlib import Path
from pyicloud import PyiCloudService

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
APPLE_ID = "your_apple_id@icloud.com"          # <-- change this
TARGET_DIR = r"D:\Backup\SharedAlbumsPrivate"   # <-- change this
LOG_FILE = "icloud_shared_albums.log"           # written to the current working directory
NAME_CACHE_FILE = "icloud_album_names_cache.json"  # avoids re-fetching names on every run

# Optional GUID -> folder name overrides, in case you want a DIFFERENT name
# than the one iCloud has for the album (which is fetched automatically via
# the webgetalbumview endpoint). Entries here take priority over the
# automatically discovered name.
ALBUM_NAME_OVERRIDES = {
    # "966FA777-16E8-44C7-B632-EC11464DAE7F": "Custom name instead of iCloud's",
}

# These two just need to be *some* plausible-looking client version string;
# taken verbatim from a real browser session, but any reasonably current
# value seems to work.
CLIENT_BUILD_NUMBER = "2624Build18"
CLIENT_MASTERING_NUMBER = "2624Build18"

NAME_FETCH_MAX_WORKERS = 6
SCAN_MAX_WORKERS = 8
PAGE_SIZE = 100  # photos per webgetassets request


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def sanitize_foldername(name: str) -> str:
    """Strip characters that are illegal in Windows folder names."""
    for ch in '<>:"/\\|?*':
        name = name.replace(ch, "_")
    return name.strip().rstrip(".") or "Unnamed"


def format_hms(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def format_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} PB"


class ProgressDisplay:
    """
    Self-overwriting progress line on the console (via \\r), independent of
    the logging setup below. Driven by totals gathered upfront in a scan
    pass (album count, photo count, total size) rather than a runtime/ETA
    estimate, since album sizes vary too much for a time-based estimate to
    be meaningful.
    """

    def __init__(self, total_albums: int, total_photos: int, total_bytes: int):
        self.total_albums = total_albums
        self.total_photos = total_photos
        self.total_bytes = total_bytes

        self.album_index = 0
        self.current_album_name = ""
        self.current_album_total_photos = 0
        self.current_album_done_photos = 0

        self.done_photos = 0
        self.done_bytes = 0

        self._last_line_len = 0
        self._line_active = False

    def start_album(self, index: int, name: str, album_total_photos: int):
        self.album_index = index
        self.current_album_name = name
        self.current_album_total_photos = album_total_photos
        self.current_album_done_photos = 0
        self._render()

    def tick(self, photo_done: bool = False, bytes_downloaded: int = 0):
        if photo_done:
            self.current_album_done_photos += 1
            self.done_photos += 1
        self.done_bytes += bytes_downloaded
        self._render()

    def end_line(self):
        """End the current progress line with a newline so subsequent
        logger.info() output doesn't get written into the middle of it."""
        if self._line_active:
            sys.stdout.write("\n")
            sys.stdout.flush()
            self._line_active = False

    def _render(self):
        line = (
            f"[Album {self.album_index}/{self.total_albums}] {self.current_album_name} "
            f"({self.current_album_done_photos}/{self.current_album_total_photos} photos) | "
            f"Total: {self.done_photos}/{self.total_photos} photos, "
            f"{format_bytes(self.done_bytes)}/{format_bytes(self.total_bytes)}"
        )
        pad = max(self._last_line_len - len(line), 0)
        sys.stdout.write("\r" + line + " " * pad)
        sys.stdout.flush()
        self._last_line_len = len(line)
        self._line_active = True


# --------------------------------------------------------------------------
# Logging setup
# --------------------------------------------------------------------------
logger = logging.getLogger("icloud_backup")
logger.setLevel(logging.DEBUG)

# File: everything (DEBUG and up), timestamped - for sharing/debugging
file_handler = logging.FileHandler(LOG_FILE, mode="a", encoding="utf-8")
file_handler.setLevel(logging.DEBUG)
file_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
))

# Console: INFO and up only, compact - for a live overview while it runs
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(logging.Formatter("%(message)s"))

logger.addHandler(file_handler)
logger.addHandler(console_handler)


# --------------------------------------------------------------------------
# Album name cache
# --------------------------------------------------------------------------
def load_name_cache() -> dict:
    p = Path(NAME_CACHE_FILE)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"Could not read name cache: {e}")
    return {}


def save_name_cache(cache: dict):
    try:
        Path(NAME_CACHE_FILE).write_text(
            json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except Exception as e:
        logger.warning(f"Could not save name cache: {e}")


# --------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------
def login() -> PyiCloudService:
    password = getpass.getpass(f"Password for {APPLE_ID}: ")
    api = PyiCloudService(APPLE_ID, password)

    logger.debug(f"requires_2fa: {api.requires_2fa}")
    logger.debug(f"requires_2sa: {api.requires_2sa}")

    if api.requires_2fa:
        code = input("Enter the 2FA code from your device: ").strip()
        logger.debug(f"2FA code entered (length {len(code)})")
        try:
            result = api.validate_2fa_code(code)
            logger.debug(f"validate_2fa_code returned: {result}")
        except Exception as e:
            logger.error(f"Exception in validate_2fa_code: {type(e).__name__}: {e}")
            sys.exit(1)
        if not result:
            logger.error("2FA validation failed (returned False/None).")
            sys.exit(1)
        if not api.is_trusted_session:
            logger.debug("Session not yet trusted, calling trust_session()...")
            trust_result = api.trust_session()
            logger.debug(f"trust_session returned: {trust_result}")
    elif api.requires_2sa:
        logger.info("Account uses 2SA (older mechanism), not 2FA.")
        devices = api.trusted_devices
        for i, dev in enumerate(devices):
            print(f"  {i}: {dev.get('deviceName', 'SMS')}")
        idx = int(input("Choose a device (number): "))
        device = devices[idx]
        if not api.send_verification_code(device):
            logger.error("Failed to send verification code.")
            sys.exit(1)
        code = input("Enter 2SA code: ").strip()
        if not api.validate_verification_code(device, code):
            logger.error("2SA validation failed.")
            sys.exit(1)
    else:
        logger.debug("No 2FA/2SA required - session already trusted.")

    return api


def build_params(dsid: str) -> dict:
    return {
        "clientBuildNumber": CLIENT_BUILD_NUMBER,
        "clientMasteringNumber": CLIENT_MASTERING_NUMBER,
        "clientId": str(uuid.uuid4()),
        "dsid": dsid,
    }


# --------------------------------------------------------------------------
# Shared-album API calls
# --------------------------------------------------------------------------
def list_shared_albums(api: PyiCloudService, dsid: str, base_host="p108") -> dict:
    """Lists all shared albums (owned + subscribed) for the account."""
    url = f"https://{base_host}-sharedstreams.icloud.com/{dsid}/sharedstreams/getchanges"
    params = build_params(dsid)
    resp = api.session.post(url, params=params, json={"rootctag": None}, timeout=30)

    if resp.status_code == 330:
        data = resp.json()
        new_host = data.get("X-Apple-MMe-Host", "").split("-")[0]
        logger.debug(f"Redirected to partition {new_host}")
        return list_shared_albums(api, dsid, base_host=new_host or base_host)

    resp.raise_for_status()
    data = resp.json()
    logger.debug(f"Album list raw response: {json.dumps(data)[:5000]}")
    return data


def parse_album_location(albumlocation: str) -> tuple[str, str]:
    """
    Extracts (host, token) from an album's `albumlocation` URL, e.g.:
        https://p108-sharedstreams.icloud.com:443/v2qUECAE.../sharedstreams/
    -> ("p108", "v2qUECAE...")
    The token (not the albumguid) is the URL path segment for all
    subsequent requests about this specific album.
    """
    m = re.search(r"https://([a-z0-9]+)-sharedstreams\.icloud\.com", albumlocation)
    host = m.group(1) if m else "p108"
    m2 = re.search(r"icloud\.com(?::\d+)?/([^/]+)/sharedstreams", albumlocation)
    token = m2.group(1) if m2 else None
    return host, token


def get_album_assets(api: PyiCloudService, host: str, token: str, album_guid: str,
                      dsid: str, offset=0, end_index=100, albumctag=None) -> dict:
    """
    Fetch one page of assets for an album.

    IMPORTANT: `offset`/`end_index` describe a photo-index RANGE
    [offset, end_index), NOT "offset + page size"! Apple's own web UI sends
    e.g. offset=390, limit=420 to mean "the next 30 photos starting at
    index 390". Sending a constant `end_index` value on every call (as if
    it were a fixed page size) means the second call asks for an empty
    range and the album appears to end after ~100 photos.
    """
    url = f"https://{host}-sharedstreams.icloud.com/{token}/sharedstreams/webgetassets"
    params = build_params(dsid)
    body = {
        "albumguid": album_guid,
        "offset": str(offset),
        "limit": str(end_index),
    }
    if albumctag:
        body["albumctag"] = albumctag
    resp = api.session.post(url, params=params, json=body, timeout=30)
    resp.raise_for_status()
    return resp.json()


def extract_download_url(record: dict) -> tuple[str, str]:
    """Picks the best available resolution and returns (url, field_name)."""
    fields = record.get("fields", {})
    for key in ("resOriginalRes", "resJPEGMedRes", "resVidMedRes", "resVidSmallRes"):
        if key in fields:
            res = fields[key].get("value", {})
            url = res.get("downloadURL")
            if url:
                return url, key
    return None, None


def extract_size(record: dict, quality: str) -> int:
    """File size (bytes) for the same resolution extract_download_url picked."""
    fields = record.get("fields", {})
    size_key = quality.replace("Res", "FileSize") if quality else None
    if size_key and size_key in fields:
        try:
            return int(fields[size_key].get("value", 0))
        except (TypeError, ValueError):
            return 0
    return 0


def get_filename(record: dict) -> str:
    fields = record.get("fields", {})
    enc = fields.get("filenameEnc", {}).get("value")
    if enc:
        try:
            return base64.b64decode(enc).decode("utf-8")
        except Exception:
            pass
    return record.get("recordName", "unknown") + ".jpg"


# --------------------------------------------------------------------------
# Album names (webgetalbumview) - fetched in parallel batches with
# adaptive splitting, since large batches were unreliable in testing.
# --------------------------------------------------------------------------
def get_album_names(api: PyiCloudService, dsid: str, album_guids: list,
                     base_host="p108", batch_size=10) -> dict:
    """
    Calls webgetalbumview in parallel (several batches at once) to fetch
    human-readable names for all albums. A failed batch is halved and both
    halves are re-queued in the same thread pool, rather than retrying
    sequentially.
    """
    names = {}
    names_lock = threading.Lock()

    def make_batches(guids, size):
        return [guids[i:i + size] for i in range(0, len(guids), size)]

    with ThreadPoolExecutor(max_workers=NAME_FETCH_MAX_WORKERS) as executor:
        pending = set()
        for batch in make_batches(album_guids, batch_size):
            pending.add(executor.submit(_fetch_album_names_batch_once, api, dsid, batch, base_host))

        while pending:
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for fut in done:
                batch, result_names, error = fut.result()
                if error is None:
                    with names_lock:
                        names.update(result_names)
                elif len(batch) == 1:
                    logger.warning(f"Could not fetch name for {batch[0]}: {error}")
                else:
                    logger.debug(f"Batch of {len(batch)} GUIDs failed ({error}), splitting...")
                    mid = len(batch) // 2
                    pending.add(executor.submit(_fetch_album_names_batch_once, api, dsid, batch[:mid], base_host))
                    pending.add(executor.submit(_fetch_album_names_batch_once, api, dsid, batch[mid:], base_host))

    return names


def _fetch_album_names_batch_once(api: PyiCloudService, dsid: str, batch: list, base_host: str):
    """A single attempt (no retry/split here) - returns (batch, names_dict, error_str_or_None)."""
    url = f"https://{base_host}-sharedstreams.icloud.com/{dsid}/sharedstreams/webgetalbumview"
    params = build_params(dsid)
    try:
        resp = api.session.post(url, params=params, json={"albumguids": batch}, timeout=20)
        if resp.status_code == 330:
            data = resp.json()
            new_host = data.get("X-Apple-MMe-Host", "").split("-")[0]
            resp = api.session.post(
                f"https://{new_host}-sharedstreams.icloud.com/{dsid}/sharedstreams/webgetalbumview",
                params=build_params(dsid), json={"albumguids": batch}, timeout=20
            )
        resp.raise_for_status()
        data = resp.json()
        result = {}
        for album in data.get("albums", []):
            guid = album.get("albumguid")
            name = album.get("attributes", {}).get("name")
            if guid and name:
                result[guid] = name
        return batch, result, None
    except Exception as e:
        detail = ""
        resp_obj = getattr(e, "response", None)
        if resp_obj is not None:
            detail = f" (HTTP {resp_obj.status_code}: {resp_obj.text[:200]})"
        return batch, {}, f"{type(e).__name__}: {e}{detail}"


# --------------------------------------------------------------------------
# Scan pass: count photos + total size per album without downloading
# --------------------------------------------------------------------------
def scan_album(api: PyiCloudService, dsid: str, host: str, token: str, album_guid: str) -> tuple:
    """Counts photos and total size of an album without downloading anything."""
    offset = 0
    count = 0
    total_bytes = 0

    while True:
        end_index = offset + PAGE_SIZE
        try:
            data = get_album_assets(api, host, token, album_guid, dsid, offset, end_index)
        except Exception as e:
            logger.warning(f"Scan of album {album_guid} failed at offset {offset}: {e}")
            break

        records = data.get("records", [])
        if not records:
            break

        page_photos = 0
        for record in records:
            if record.get("recordType") != "CPLMaster":
                continue
            count += 1
            page_photos += 1
            _, quality = extract_download_url(record)
            total_bytes += extract_size(record, quality)

        if page_photos < PAGE_SIZE:
            break
        offset = end_index

    return count, total_bytes


def scan_all_albums(api: PyiCloudService, dsid: str, albums_info: list, max_workers=SCAN_MAX_WORKERS) -> dict:
    """
    albums_info: list of (album_guid, host, token)
    Returns {album_guid: (photo_count, size_bytes)}, fetched in parallel.
    """
    results = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_guid = {
            executor.submit(scan_album, api, dsid, host, token, guid): guid
            for guid, host, token in albums_info
        }
        done_count = 0
        for fut in list(future_to_guid.keys()):
            guid = future_to_guid[fut]
            try:
                results[guid] = fut.result()
            except Exception as e:
                logger.warning(f"Scan failed for album {guid}: {e}")
                results[guid] = (0, 0)
            done_count += 1
            sys.stdout.write(f"\rScanning albums... {done_count}/{len(albums_info)}")
            sys.stdout.flush()
    sys.stdout.write("\n")
    sys.stdout.flush()
    return results


# --------------------------------------------------------------------------
# Download
# --------------------------------------------------------------------------
def download_private_album(api: PyiCloudService, dsid: str, album_guid: str,
                            album_name: str, host: str, token: str,
                            progress: "ProgressDisplay" = None) -> dict:
    """Returns stats: {'downloaded': int, 'skipped': int, 'errors': int}"""
    target = Path(TARGET_DIR) / sanitize_foldername(album_name)
    target.mkdir(parents=True, exist_ok=True)
    logger.info(f"\n=== Album: {album_name} ({album_guid}) -> {target} ===")

    offset = 0
    stats = {"downloaded": 0, "skipped": 0, "errors": 0}
    dates_ms = []

    while True:
        end_index = offset + PAGE_SIZE
        try:
            data = get_album_assets(api, host, token, album_guid, dsid, offset, end_index)
        except Exception as e:
            logger.error(f"  Error fetching offset {offset}: {e}")
            stats["errors"] += 1
            break

        records = data.get("records", [])
        if not records:
            break

        page_photos = 0
        for record in records:
            if record.get("recordType") != "CPLMaster":
                continue
            page_photos += 1

            fields = record.get("fields", {})
            created = fields.get("originalCreationDate", {}).get("value")
            if isinstance(created, (int, float)):
                dates_ms.append(created)

            url, quality = extract_download_url(record)
            filename = get_filename(record)
            if not url:
                logger.debug(f"  skipped (no URL): {filename}")
                stats["skipped"] += 1
                if progress:
                    progress.tick(photo_done=True)
                continue

            dest = target / filename
            if dest.exists():
                stats["skipped"] += 1
                if progress:
                    progress.tick(photo_done=True)
                continue

            try:
                r = api.session.get(url, timeout=60)
                r.raise_for_status()
                dest.write_bytes(r.content)
                stats["downloaded"] += 1
                logger.debug(f"  [{quality}] downloaded: {filename}")
                if progress:
                    progress.tick(photo_done=True, bytes_downloaded=len(r.content))
            except Exception as e:
                logger.error(f"  Error downloading {filename}: {e}")
                stats["errors"] += 1
                if progress:
                    progress.tick(photo_done=True)

        if page_photos < PAGE_SIZE:
            break
        offset = end_index

    if progress:
        progress.end_line()

    if dates_ms:
        oldest = datetime.fromtimestamp(min(dates_ms) / 1000).strftime("%Y-%m-%d")
        newest = datetime.fromtimestamp(max(dates_ms) / 1000).strftime("%Y-%m-%d")
        logger.info(f"  Photo date range: {oldest} to {newest} (useful for ALBUM_NAME_OVERRIDES)")

    logger.info(
        f"  -> {stats['downloaded']} newly downloaded, "
        f"{stats['skipped']} skipped (already present), "
        f"{stats['errors']} errors."
    )
    return stats


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main():
    logger.info(f"===== Backup run started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} =====")

    try:
        api = login()
    except Exception as e:
        logger.error(f"Login failed: {type(e).__name__}: {e}")
        sys.exit(1)

    dsid = api.data["dsInfo"]["dsid"]
    logger.info(f"Logged in, dsid={dsid}")

    try:
        changes = list_shared_albums(api, dsid)
    except Exception as e:
        logger.error(f"Could not load album list: {type(e).__name__}: {e}")
        sys.exit(1)

    albums = changes.get("albums", [])
    if not albums:
        logger.error("No albums found. See the log file for the raw response.")
        return

    logger.info(f"{len(albums)} shared albums found.")

    all_guids = [a.get("albumguid") or a.get("albumGuid") for a in albums]

    name_cache = load_name_cache()
    missing_guids = [g for g in all_guids if g not in name_cache]

    if missing_guids:
        logger.info(f"Fetching album names ({len(missing_guids)} new, {len(all_guids) - len(missing_guids)} from cache)...")
        try:
            newly_found = get_album_names(api, dsid, missing_guids)
            logger.info(f"{len(newly_found)} of {len(missing_guids)} new album names found.")
            name_cache.update(newly_found)
            save_name_cache(name_cache)
        except Exception as e:
            logger.warning(f"Could not automatically fetch album names: {e}")
    else:
        logger.info(f"All {len(all_guids)} album names taken from cache (no request needed).")

    fetched_names = name_cache

    # Resolve host/token per album upfront (needed for both scan and download)
    album_infos = []  # list of dicts: guid, name, host, token
    totals = {"downloaded": 0, "skipped": 0, "errors": 0, "albums_failed": 0}
    for album in albums:
        album_guid = album.get("albumguid") or album.get("albumGuid")
        album_name = ALBUM_NAME_OVERRIDES.get(album_guid) or fetched_names.get(album_guid) or album_guid
        albumlocation = album.get("albumlocation")
        if not albumlocation:
            logger.warning(f"SKIPPED (no albumlocation): {album_name}")
            totals["albums_failed"] += 1
            continue
        host, token = parse_album_location(albumlocation)
        if not token:
            logger.warning(f"SKIPPED (could not parse token): {album_name} -> {albumlocation}")
            totals["albums_failed"] += 1
            continue
        album_infos.append({"guid": album_guid, "name": album_name, "host": host, "token": token})

    logger.info(f"Scanning {len(album_infos)} albums (determining photo count and data volume)...")
    scan_results = scan_all_albums(
        api, dsid, [(a["guid"], a["host"], a["token"]) for a in album_infos]
    )

    total_photos = sum(c for c, _ in scan_results.values())
    total_bytes = sum(b for _, b in scan_results.values())
    logger.info(
        f"Scan complete: {len(album_infos)} albums, {total_photos} photos, "
        f"{format_bytes(total_bytes)} total.\n"
    )

    progress = ProgressDisplay(
        total_albums=len(album_infos), total_photos=total_photos, total_bytes=total_bytes
    )

    run_start = datetime.now()

    for idx, info in enumerate(album_infos, start=1):
        album_guid, album_name = info["guid"], info["name"]
        album_total_photos, _ = scan_results.get(album_guid, (0, 0))

        progress.start_album(idx, album_name, album_total_photos)
        try:
            stats = download_private_album(
                api, dsid, album_guid, album_name, info["host"], info["token"], progress
            )
            totals["downloaded"] += stats["downloaded"]
            totals["skipped"] += stats["skipped"]
            totals["errors"] += stats["errors"]
        except Exception as e:
            progress.end_line()
            logger.error(f"Error in album {album_name}: {type(e).__name__}: {e}")
            totals["albums_failed"] += 1

    total_runtime = (datetime.now() - run_start).total_seconds()
    logger.info(
        f"\n===== SUMMARY =====\n"
        f"Total albums: {len(albums)}\n"
        f"Albums skipped due to errors: {totals['albums_failed']}\n"
        f"Files newly downloaded: {totals['downloaded']}\n"
        f"Files skipped (already present): {totals['skipped']}\n"
        f"Individual file errors: {totals['errors']}\n"
        f"Total runtime: {format_hms(total_runtime)}\n"
        f"Log file: {Path(LOG_FILE).resolve()}"
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.stdout.write("\n")
        sys.stdout.flush()
        logger.warning("Manually interrupted (Ctrl+C).")
