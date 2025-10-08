#!/usr/bin/env python3
"""
Unified Suno downloader/embedder
- Reads lines: "FILENAME|URL" (URL is usually https://cdn1.suno.ai/{uuid}.mp3 or .mp4)
- For each UUID, fetches song details from oEmbed (preferred) or the song page as fallback
- Downloads requested formats:
  - mp3: direct from CDN when available
  - mp4: requires Studio auth and may need generation; will probe/generate if direct URL is not available
  - wav: requires Studio auth and may need generation; will probe/generate if direct URL is not available
- Embeds into MP3 by default: cover, title, artist, comment (caption/prompt/tags/model info)
- Saves sidecars: details JSON
- WAV: sets only simple metadata feasible (INFO tags are limited); no cover embedding

Notes:
- Requires Python 3, requests, mutagen, and rich. ffmpeg is required for MP3 cover embedding and for simple metadata updates.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import builtins
from pathlib import Path
from typing import Dict, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import deque
from threading import Lock

import requests
try:
    from mutagen.id3 import ID3, TXXX
    MUTAGEN_AVAILABLE = True
except Exception:
    MUTAGEN_AVAILABLE = False

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.progress import (
    Progress,
    BarColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
    SpinnerColumn,
    TextColumn,
)
from rich.theme import Theme
from rich.live import Live
from rich.text import Text
from rich.console import Group
from rich.align import Align
from rich.layout import Layout
import concurrent.futures
from rich.layout import Layout
RICH_AVAILABLE = True
console = Console(theme=Theme({
    "info": "cyan",
    "warn": "yellow",
    "error": "red",
    "success": "green",
}))

CDN_AUDIO = "https://cdn1.suno.ai"
CDN_IMAGE = "https://cdn2.suno.ai"
STUDIO_BASE = "https://studio-api.prod.suno.com"


def ensure_dir(p: Path):
    p.mkdir(parents=True, exist_ok=True)


# Shared log storage for the Logs panel
LOG_LINES: deque[Text] = deque(maxlen=400)
LOG_LOCK = Lock()
LOG_TEXT = Text()

def _panel_print(*args, **kwargs):
    raw = " ".join(str(a) for a in args)
    # Build a styled Text line
    line = Text()
    # Detect leading [#TOKEN] prefix (e.g., [#3 MP3], [#Q1]) and color the token
    m = re.match(r"^\[#([^\]]+)\]\s?(.*)$", raw)
    if m:
        token, rest = m.group(1), m.group(2)
        # Style the token after #
        line.append("[")
        line.append("#", style="bold white")
        line.append(token, style="bold bright_cyan")
        line.append("] ")
        remaining = rest
    else:
        remaining = raw

    # Severity coloring heuristics
    low = remaining.lower()
    style = None
    if any(w in low for w in ["error", "failed", "exception", "timeout"]):
        style = "bold red"
    elif any(w in low for w in ["saved", "tagging complete", "metadata updated", "url ready", "probe ok"]):
        style = "bold green"
    elif any(w in low for w in ["skipping", "not available", "returned", "could not"]):
        style = "yellow"
    elif any(w in low for w in ["start (", "processing", "waiting..."]):
        style = "cyan"
    else:
        style = "white"

    line.append(remaining, style=style)
    with LOG_LOCK:
        LOG_LINES.append(line)
        # Rebuild text (cheap due to maxlen limit)
        LOG_TEXT.__init__("")
        first = True
        for t in LOG_LINES:
            if not first:
                LOG_TEXT.append("\n")
            LOG_TEXT.append_text(t)
            first = False

# Route all prints to the logs panel buffer
print = _panel_print  # type: ignore


def extract_uuid(url: str) -> Optional[str]:
    m = re.search(r"https://cdn1\.suno\.ai/([^.]+)\.(mp3|mp4|wav)", url.strip())
    return m.group(1) if m else None


def run_ffmpeg(cmd: list[str]) -> bool:
    try:
        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return res.returncode == 0
    except Exception:
        return False



def download_with_retries(url: str, dest: Path, attempts: int = 3, wait_seconds: int = 5, prefix: str = "", label: str = "DOWNLOAD") -> bool:
    """Attempt download multiple times with wait between attempts."""
    for i in range(1, max(1, attempts) + 1):
        ok = download(url, dest)
        if ok:
            return True
        if i < attempts:
            msg_prefix = f"{prefix} " if prefix else ""
            print(f"{msg_prefix}{label} failed (attempt {i}/{attempts}). Retrying in {wait_seconds}s...")
            try:
                time.sleep(wait_seconds)
            except Exception:
                pass
    return False

def probe_and_download(url: str, dest: Path, timeout: int = 15, prefix: str = "") -> bool:
    """Probe a URL with HEAD; if 200, download to dest with retries. Returns True if saved."""
    try:
        pr = requests.head(url, timeout=timeout, allow_redirects=True)
        if pr.status_code == 200:
            if prefix:
                print(f"{prefix} Probe OK -> download")
            else:
                print("[INFO] Probe succeeded; downloading...")
            # Fallback to single attempt; acquire_asset will pass retry args via kwargs
            if download(url, dest):
                return True
        else:
            if prefix:
                print(f"{prefix} Probe returned {pr.status_code}")
            else:
                print(f"[WARN] Probe returned {pr.status_code} for {url}")
    except requests.RequestException:
        pass
    return False


def acquire_asset(uuid: str,
                  dest: Path,
                  primary_url: str,
                  headers: Dict[str, str],
                  args,
                  trigger_fn=None,
                  label: str = "ASSET",
                  prefix: str = "") -> bool:
    """Common flow: probe CDN -> optionally trigger generation -> final direct download.
    Returns True if dest exists at end.
    """
    # 1) Probe CDN and download if available
    if not dest.exists():
        # Initial probe and attempt (single try); subsequent retries handled below
        if probe_and_download(primary_url, dest, prefix=prefix):
            print(f"{prefix} Saved {label}: {dest.name}")
    # 2) Trigger generation (if requested and supported)
    if not dest.exists() and headers and getattr(args, 'wait', False) and trigger_fn:
        ok_gen = trigger_fn(
            uuid,
            dest,
            headers,
            args.poll_interval,
            args.poll_timeout,
            prefix=prefix,
            retries=getattr(args, 'retries', 3),
            retry_wait=getattr(args, 'retry_wait', 5),
        )
        if ok_gen:
            print(f"{prefix} Saved {label}: {dest.name}")
    # 3) Final attempt: direct download in case it appeared in the meantime
    if not dest.exists():
        if download_with_retries(primary_url, dest, attempts=getattr(args, 'retries', 3), wait_seconds=getattr(args, 'retry_wait', 5), prefix=prefix, label=label):
            print(f"{prefix} Saved {label}: {dest.name}")
        else:
            print(f"{prefix} {label} download failed after retries")
    return dest.exists()




def embed_mp3_uuid_txxx(mp3_path: Path, uuid: str) -> bool:
    if not MUTAGEN_AVAILABLE:
        return False
    try:
        try:
            tags = ID3(str(mp3_path))
        except Exception:
            tags = ID3()
        # Use a vendor-specific TXXX frame name
        tags.add(TXXX(encoding=3, desc="SUNO_UUID", text=uuid))
        tags.save(str(mp3_path))
        return True
    except Exception:
        return False


def embed_simple_metadata(ffmpeg_path: str, src: Path, updates: Dict[str, str]) -> bool:
    """Use ffmpeg to write simple metadata (no cover) into any container by remuxing.
    Writes to temp file and replaces original.
    """
    temp = src.with_name(f"temp_{src.name}")
    cmd = [ffmpeg_path, "-i", str(src), "-c", "copy"]
    for k, v in updates.items():
        cmd += ["-metadata", f"{k}={v}"]
    cmd += ["-y", str(temp)]
    ok = run_ffmpeg(cmd)
    if not ok:
        if temp.exists():
            temp.unlink(missing_ok=True)
        return False
    try:
        os.replace(temp, src)
        return True
    except Exception:
        return False


def existing_uuid_in_mp3(mp3_path: Path) -> Optional[str]:
    """Try to read UUID from MP3 either from TXXX:SUNO_UUID or from comment 'uuid=...'."""
    # Try mutagen first
    if MUTAGEN_AVAILABLE:
        try:
            tags = ID3(str(mp3_path))
            for frame in tags.getall('TXXX'):
                if getattr(frame, 'desc', '').upper() == 'SUNO_UUID' and frame.text:
                    return str(frame.text[0])
            com = tags.get('COMM::eng') or tags.get('COMM')
            if com and hasattr(com, 'text') and com.text:
                m = re.search(r"uuid=([0-9a-f\-]{36})", com.text[0], re.I)
                if m:
                    return m.group(1)
        except Exception:
            pass
    # Fallback: quick read for ID3 comment via ffprobe would be heavy; skip
    return None


def any_file_with_uuid(out_dir: Path, uuid: str) -> bool:
    """Lightweight duplicate detection: scan MP3s for embedded UUID. """
    for p in out_dir.glob("*.mp3"):
        found = existing_uuid_in_mp3(p)
        if found and found.lower() == uuid.lower():
            return True
    return False


def download(url: str, dest: Path) -> bool:
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with requests.get(url, stream=True, timeout=120) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_content(8192):
                    if chunk:
                        f.write(chunk)
        return True
    except Exception as e:
        print(f"[ERROR] Download failed {url}: {e}")
        return False








def fetch_details_oembed(song_uuid: str) -> Optional[Dict]:
    # Official oEmbed advertised on page metadata
    url = f"{STUDIO_BASE}/api/oembed?url=https://suno.com/song/{song_uuid}"
    try:
        r = requests.get(url, timeout=20)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def fetch_details_metatags(song_uuid: str) -> Optional[Dict]:
    """Parse OpenGraph/Twitter meta tags from the public song page.
    Returns a dict shaped similarly to oEmbed: {source: 'metatags', data: {...}}.
    """
    try:
        html = requests.get(f"https://suno.com/song/{song_uuid}", timeout=20).text
        # Simple helpers
        def _meta_prop(prop: str) -> Optional[str]:
            m = re.search(rf'<meta[^>]+property=["\']{re.escape(prop)}["\'][^>]+content=["\']([^"\']+)["\']', html, re.IGNORECASE)
            return m.group(1) if m else None
        def _meta_name(name: str) -> Optional[str]:
            m = re.search(rf'<meta[^>]+name=["\']{re.escape(name)}["\'][^>]+content=["\']([^"\']+)["\']', html, re.IGNORECASE)
            return m.group(1) if m else None
        def _title_tag() -> Optional[str]:
            m = re.search(r"<title>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
            return m.group(1).strip() if m else None

        title = _meta_prop("og:title") or _meta_name("twitter:title") or _title_tag()
        desc = _meta_prop("og:description") or _meta_name("twitter:description")
        image = _meta_prop("og:image") or _meta_name("twitter:image")
        audio = _meta_prop("og:audio") or _meta_name("twitter:player:stream")

        # Try to infer author from the <title> form: "<song> by <author> | Suno"
        author_name = None
        t = _title_tag()
        if t and " by " in t:
            try:
                author_name = t.split(" by ", 1)[1].split("|")[0].strip()
            except Exception:
                author_name = None

        data = {
            "title": title,
            "author_name": author_name,
            "thumbnail_url": image,
            "description": desc,
            "audio_url": audio,
            "page_url": f"https://suno.com/song/{song_uuid}",
        }
        # Only accept if we got at least a title or image
        if any([title, image, audio, author_name]):
            return {"source": "metatags", "data": data}
        return None
    except Exception:
        return None


def fetch_details_page(song_uuid: str) -> Optional[Dict]:
    # Fallback: parse the NextJS hydration JSON for `clip`
    try:
        html = requests.get(f"https://suno.com/song/{song_uuid}", timeout=20).text
        # Find a snippet containing '"clip":{'...}
        m = re.search(r'\"clip\":\{.*?\}\},\"persona\":', html, re.DOTALL)
        if not m:
            return None
        # Extract JSON substring and try to balance braces (approx)
        blob = m.group(0)
        # Clean trailing ,"persona":
        blob = re.sub(r',\\"persona\\":$', '', blob)
        # Wrap into an object so we can parse
        jtext = '{' + blob + '}'
        # Unescape quotes
        jtext = jtext.encode('utf-8').decode('unicode_escape')
        data = json.loads(jtext)
        return data
    except Exception:
        return None


def collect_song_fields(song_uuid: str, details_dir: Optional[Path]) -> Dict:
    # If details JSON provided by user, prefer it
    if details_dir:
        for name in (f"{song_uuid}.json", f"{song_uuid}_details.json"):
            p = details_dir / name
            if p.exists():
                try:
                    return json.loads(p.read_text(encoding='utf-8'))
                except Exception:
                    pass
    # Try oEmbed (disabled)
    # data = fetch_details_oembed(song_uuid)
    # if isinstance(data, dict) and data:
    #     return {"source": "oembed", "data": data}
    # Try OpenGraph/Twitter meta tags as safer public fallback
    data_meta = fetch_details_metatags(song_uuid)
    if isinstance(data_meta, dict) and data_meta:
        return data_meta
    # Fallback parse of page (disabled)
    # data2 = fetch_details_page(song_uuid)
    # if isinstance(data2, dict) and data2:
    #     return {"source": "page", "data": data2}
    return {}


def build_cover_url(song_uuid: str) -> str:
    return f"{CDN_IMAGE}/image_large_{song_uuid}.jpeg"


def embed_mp3(ffmpeg_path: str, mp3_path: Path, cover_path: Optional[Path], title: str, artist: str, comment: str) -> bool:
    temp = mp3_path.with_name(f"temp_{mp3_path.name}")
    cmd = [ffmpeg_path, "-i", str(mp3_path)]
    if cover_path and cover_path.exists():
        cmd += ["-i", str(cover_path), "-map", "0", "-map", "1", "-c:v", "copy", "-disposition:v", "attached_pic",
                "-metadata:s:v", "title=Album cover", "-metadata:s:v", "comment=Cover (front)"]
    cmd += [
        "-c", "copy",
        "-metadata", f"title={title}",
        "-metadata", f"artist={artist}",
        "-metadata", f"album=Suno",
        "-metadata", f"comment={comment[:300]}",
        "-y", str(temp),
    ]
    ok = run_ffmpeg(cmd)
    if not ok:
        if temp.exists():
            temp.unlink(missing_ok=True)
        return False
    try:
        os.replace(temp, mp3_path)
        return True
    except Exception:
        return False


def trigger_wav_and_wait(uuid: str, out_path: Path, headers: Dict[str, str], poll_interval: int, poll_timeout: int, prefix: str = "", retries: int = 3, retry_wait: int = 5) -> bool:
    endpoint = f"{STUDIO_BASE}/api/gen/{uuid}/convert_wav/"
    try:
        r = requests.post(endpoint, headers=headers, timeout=60)
        if r.status_code // 100 != 2:
            print(f"{prefix} Trigger failed {r.status_code}: {r.text[:200]}")
            return False
    except requests.RequestException as e:
        print(f"{prefix} Trigger error: {e}")
        return False

    start = time.time()
    attempt = 1
    if prefix:
        print(f"{prefix} Waiting...")
    while time.time() - start < poll_timeout:
        time.sleep(poll_interval)
        # Try re-post (some sessions surface URL there)
        try:
            r2 = requests.post(endpoint, headers=headers, timeout=60)
            if r2.status_code // 100 == 2:
                try:
                    body = r2.json()
                    dl_url = (
                        body.get('audioWavUrl')
                        or body.get('data', {}).get('audioWavUrl')
                        or body.get('download_url')
                        or body.get('data', {}).get('download_url')
                    )
                except Exception:
                    dl_url = None
                if dl_url:
                    if prefix:
                        print(f"{prefix} URL ready -> download")
                    return download_with_retries(dl_url, out_path, attempts=retries, wait_seconds=retry_wait, prefix=prefix, label="WAV")
        except requests.RequestException:
            pass
        # Probe fixed CDN URL without exposing options in CLI
        probe_url = f"{CDN_AUDIO}/{uuid}.wav"
        try:
            pr = requests.head(probe_url, timeout=15, allow_redirects=True)
            if pr.status_code == 200:
                if prefix:
                    print(f"{prefix} Probe OK -> download")
                return download_with_retries(probe_url, out_path, attempts=retries, wait_seconds=retry_wait, prefix=prefix, label="WAV")
        except requests.RequestException:
            pass
        attempt += 1
    if prefix:
        print(f"{prefix} Timeout")
    return False


def trigger_mp4_and_wait(uuid: str, out_path: Path, headers: Dict[str, str], poll_interval: int, poll_timeout: int, prefix: str = "", retries: int = 3, retry_wait: int = 5) -> bool:
    endpoint = f"{STUDIO_BASE}/api/video/generate/{uuid}/"
    try:
        r = requests.post(endpoint, headers=headers, timeout=60)
        if prefix:
            print(f"{prefix} Generate -> {r.status_code}")
        if r.status_code // 100 not in (2,):
            # 204 No Content is typical on success
            return False
    except requests.RequestException as e:
        print(f"{prefix} Trigger error: {e}")
        return False

    start = time.time()
    if prefix:
        print(f"{prefix} Waiting...")
    while time.time() - start < poll_timeout:
        time.sleep(poll_interval)
        # Probe fixed CDN URL
        probe_url = f"{CDN_AUDIO}/{uuid}.mp4"
        try:
            pr = requests.head(probe_url, timeout=15, allow_redirects=True)
            if pr.status_code == 200:
                if prefix:
                    print(f"{prefix} Probe OK -> download")
                return download_with_retries(probe_url, out_path, attempts=retries, wait_seconds=retry_wait, prefix=prefix, label="MP4")
        except requests.RequestException:
            pass
    if prefix:
        print(f"{prefix} Timeout")
    return False


def process_one(uuid: str, left: str, url: str, idx: int, total_unique: int,
                args, out_dir: Path, details_dir: Optional[Path], headers: Dict[str, str],
                want_mp3: bool, want_mp4: bool, want_wav: bool,
                progress: Progress, task_id) -> Dict[str, bool]:
    # Decide filename according to --name-mode
    # Determine extension from url
    ext = os.path.splitext(url.split('?')[0])[-1].lower() or '.mp3'
    # Gather details for potential title
    details = collect_song_fields(uuid, details_dir)
    title_from_details = None
    if details:
        data = details.get('data', details)
        clip = data.get('clip') if isinstance(data, dict) else None
        if clip and isinstance(clip, dict):
            title_from_details = clip.get('title')
        else:
            title_from_details = data.get('title')

    if args.name_mode == 'input' and left:
        filename = left
        if not filename.lower().endswith(ext):
            filename += ext
    elif args.name_mode == 'details' and title_from_details:
        filename = f"{title_from_details}{ext}"
    elif args.name_mode == 'uuid' or not left:
        filename = f"{uuid}{ext}"
    else:
        filename = (left or uuid) + ext

    stem = Path(filename).stem
    base_stem = stem
    print(f"[#{idx}] Start ({idx}/{total_unique})")

    # Check for existing MP3 with same UUID, but don't skip other formats
    already_have_uuid = any_file_with_uuid(out_dir, uuid)
    if already_have_uuid:
        print(f"[#{idx} MP3] Existing UUID found in library; skipping MP3")

    # Gather details (already fetched above if needed)
    if not details:
        details = collect_song_fields(uuid, details_dir)
    # Normalize fields for tagging
    title = stem
    artist = ''
    cover_url = build_cover_url(uuid)
    comment_parts = []

    if details:
        src = details.get('source')
        data = details.get('data', {}) if 'data' in details else details
        # Page clip path
        clip = data.get('clip') if 'clip' in data else None
        if clip:
            title = clip.get('title') or title
            artist = f"{clip.get('display_name','')} (@{clip.get('handle','')})".strip()
            # Always prefer the large image; NEVER use smaller thumbnail/image fields
            cover_url = clip.get('image_large_url') or build_cover_url(uuid)
            md = clip.get('metadata', {})
            prompt = md.get('prompt')
            tags_long = md.get('tags')
            display_tags = clip.get('display_tags')
            model = f"{clip.get('major_model_version','')} {clip.get('model_name','')}".strip()
            dur = md.get('duration')
            if prompt:
                comment_parts.append(f"prompt: {prompt}")
            if display_tags:
                comment_parts.append(f"tags: {display_tags}")
            elif tags_long:
                comment_parts.append(f"tags: {tags_long[:120]}")
            if model:
                comment_parts.append(f"model: {model}")
            if dur:
                comment_parts.append(f"duration: {dur}s")
        else:
            # oEmbed may have title/author_name/thumbnail_url
            title = data.get('title') or title
            artist = data.get('author_name') or artist
            # Do NOT use oEmbed thumbnails for cover art; keep using large image URL

    # Prepend UUID to comment for broad containers
    base_comment = ' | '.join([p for p in comment_parts if p])
    comment = f"uuid={uuid}" + (f" | {base_comment}" if base_comment else "")

    # Prepare canonical URLs for formats based on UUID
    url_is_mp3 = url.lower().endswith('.mp3')
    url_is_mp4 = url.lower().endswith('.mp4')
    mp3_url = url if url_is_mp3 else f"{CDN_AUDIO}/{uuid}.mp3"
    mp4_url = url if url_is_mp4 else f"{CDN_AUDIO}/{uuid}.mp4"

    # Tracking for summary
    result = {
        "mp3_saved": False,
        "mp3_failed": False,
        "mp3_skipped_existing": False,
        "mp3_skipped_uuid": False,
        "mp4_saved": False,
        "mp4_failed": False,
        "mp4_skipped_existing": False,
        "wav_saved": False,
        "wav_failed": False,
        "wav_skipped_existing": False,
    }

    # MP3
    if want_mp3:
        if already_have_uuid:
            print(f"[#{idx} MP3] Skipping due to existing file with same embedded UUID")
            result["mp3_skipped_uuid"] = True
        mp3_name = f"{base_stem}.mp3"
        mp3_dest = out_dir / mp3_name
        mp3_preexists = mp3_dest.exists()
        mp3_just_saved = False
        if mp3_preexists:
            print(f"[#{idx} MP3] Skipping existing file: {mp3_dest.name}")
            result["mp3_skipped_existing"] = True
            # Advance all MP3 steps when skipping existing
            progress.advance(task_id, 1)  # download step
            if not args.no_metadata:
                progress.advance(task_id, 1)  # metadata step
            if not args.no_embed_uuid:
                progress.advance(task_id, 1)  # uuid embed step
        elif not already_have_uuid:
            if download(mp3_url, mp3_dest):
                print(f"[#{idx} MP3] Saved: {mp3_dest.name}")
                result["mp3_saved"] = True
                mp3_just_saved = True
                progress.advance(task_id, 1)
            else:
                print(f"[#{idx} MP3] Download failed; continuing")
                result["mp3_failed"] = True
                progress.advance(task_id, 1)
        # Only tag/embed when newly saved this run
        if mp3_just_saved and not args.no_metadata:
            cover_path = out_dir / f"{uuid}.jpeg"
            # Always fetch the preferred large image to avoid cached small thumbnails
            download(cover_url, cover_path)
            print(f"[#{idx} MP3] Embedding metadata and cover...")
            if embed_mp3(args.ffmpeg_path, mp3_dest, cover_path if cover_path.exists() else None, title, artist or 'Suno', comment):
                print(f"[#{idx} MP3] Tagging complete")
            else:
                print(f"[#{idx} MP3] Tagging failed")
            progress.advance(task_id, 1)
        if mp3_just_saved and not args.no_embed_uuid:
            if embed_mp3_uuid_txxx(mp3_dest, uuid):
                print(f"[#{idx} MP3] Embedded UUID (TXXX:SUNO_UUID)")
            else:
                print(f"[#{idx} MP3] Could not embed UUID TXXX; comment still contains uuid=")
            progress.advance(task_id, 1)

    # MP4
    if want_mp4:
        mp4_name = f"{base_stem}.mp4"
        mp4_dest = out_dir / mp4_name
        mp4_preexists = mp4_dest.exists()
        mp4_just_saved = False
        if mp4_preexists:
            print(f"[#{idx} MP4] Skipping existing file: {mp4_dest.name}")
            result["mp4_skipped_existing"] = True
            # Advance MP4 steps on skip
            progress.advance(task_id, 1)
            if not args.no_embed_uuid:
                progress.advance(task_id, 1)
        else:
            got_mp4 = acquire_asset(
                uuid=uuid,
                dest=mp4_dest,
                primary_url=mp4_url,
                headers=headers,
                args=args,
                trigger_fn=trigger_mp4_and_wait,
                label="MP4",
                prefix=f"[#{idx} MP4]",
            )
            if not got_mp4 and not (headers and args.wait):
                print(f"[#{idx} MP4] Not available on CDN; no auth/wait provided to trigger generation")
            mp4_just_saved = bool(got_mp4)
            result["mp4_saved"] = mp4_just_saved
            if not got_mp4:
                result["mp4_failed"] = True
            progress.advance(task_id, 1)
        # Only update metadata when newly saved
        if mp4_just_saved and not args.no_embed_uuid:
            print(f"[#{idx} MP4] Updating metadata...")
            if embed_simple_metadata(args.ffmpeg_path, mp4_dest, {"title": title, "artist": artist or 'Suno', "comment": comment}):
                print(f"[#{idx} MP4] Metadata updated with UUID")
            else:
                print(f"[#{idx} MP4] Metadata update failed")
            progress.advance(task_id, 1)

    # WAV
    if want_wav:
        wav_name = f"{stem}.wav"
        wav_dest = out_dir / wav_name
        wav_preexists = wav_dest.exists()
        wav_just_saved = False
        if wav_preexists:
            print(f"[#{idx} WAV] Skipping existing file: {wav_dest.name}")
            result["wav_skipped_existing"] = True
            # Advance WAV steps on skip
            progress.advance(task_id, 1)
            if not args.no_embed_uuid:
                progress.advance(task_id, 1)
        else:
            wav_url = f"{CDN_AUDIO}/{uuid}.wav"
            got_wav = acquire_asset(
                uuid=uuid,
                dest=wav_dest,
                primary_url=wav_url,
                headers=headers,
                args=args,
                trigger_fn=trigger_wav_and_wait,
                label="WAV",
                prefix=f"[#{idx} WAV]",
            )
            if not got_wav and not (headers and args.wait):
                print(f"[#{idx} WAV] Not available on CDN; no auth/wait provided to trigger generation")
            wav_just_saved = bool(got_wav)
            result["wav_saved"] = wav_just_saved
            if not got_wav:
                result["wav_failed"] = True
            progress.advance(task_id, 1)
        # Only update metadata when newly saved
        if wav_just_saved and not args.no_embed_uuid:
            print(f"[#{idx} WAV] Updating metadata...")
            if embed_simple_metadata(args.ffmpeg_path, wav_dest, {"title": title, "artist": artist or 'Suno', "comment": comment}):
                print(f"[#{idx} WAV] Metadata updated with UUID")
            else:
                print(f"[#{idx} WAV] Metadata update failed")
            progress.advance(task_id, 1)

    # Save details JSON sidecar for traceability
    if details:
        try:
            (out_dir / 'metadata').mkdir(exist_ok=True)
            with open(out_dir / 'metadata' / f"{uuid}.json", 'w', encoding='utf-8') as jf:
                json.dump(details, jf, indent=2, ensure_ascii=False)
        except Exception:
            pass
        # Sidecar saved (or attempted)
        progress.advance(task_id, 1)

    return result

def main():
    parser = argparse.ArgumentParser(description="Unified Suno downloader/embedder")
    parser.add_argument("--songfile", required=True, help="[required] Input file with lines: FILENAME|URL")
    parser.add_argument("--formats", default="mp3", help="[optional] Comma list: mp3,mp4,wav (default: mp3)")
    parser.add_argument("--output-dir", help="[optional] Destination directory (default: {songfile}_files)")
    parser.add_argument("--wait", action="store_true", help="[optional] When WAV or MP4 requested, wait until downloadable (trigger and poll if needed)")
    parser.add_argument("--no-metadata", action="store_true", help="[optional] Skip metadata writing (cover/art and tags)")
    parser.add_argument("--auth", help="[optional] Authorization Bearer token for WAV/MP4 generation")

    parser.add_argument("--details-dir", help="[optional] Directory with per-uuid JSON details to enrich tags")

    parser.add_argument("--poll-interval", type=int, default=5, help="[optional] Poll interval (seconds) for WAV/MP4 generation (default: 5)")
    parser.add_argument("--poll-timeout", type=int, default=60, help="[optional] Poll timeout (seconds) for WAV/MP4 generation (default: 60)")
    parser.add_argument("--retries", type=int, default=3, help="[optional] Number of retry attempts for MP4/WAV downloads (default: 3)")
    parser.add_argument("--retry-wait", type=int, default=5, help="[optional] Seconds to wait between retries (default: 5)")

    parser.add_argument("--ffmpeg-path", default="ffmpeg", help="[optional] Path to ffmpeg (used for tagging and metadata)")
    parser.add_argument("--name-mode", choices=["input", "details", "uuid"], default="input", help="[optional] How to choose base filename when title missing or to override (default: input)")
    parser.add_argument("--no-embed-uuid", action="store_true", help="[optional] Do not embed UUID into metadata (embeds by default)")
    parser.add_argument("--logs-height", type=int, default=12, help="[optional] Max height (rows) for the Logs panel (default: 12)")
    parser.add_argument("--downloads-height", type=int, default=20, help="[optional] Max height (rows) for the Downloads panel (default: 20)")
    parser.add_argument("--progress-visible", type=int, default=10, help="[optional] Max number of song progress rows to display at once (default: 10)")

    parser.add_argument("--session-id", help="[optional] Studio session-id header for WAV/MP4 generation")
    parser.add_argument("--browser-token", help="[optional] Studio browser-token header for WAV/MP4 generation")
    parser.add_argument("--device-id", help="[optional] Studio device-id header for WAV/MP4 generation")


    args = parser.parse_args()

    in_path = Path(args.songfile)
    if not in_path.exists():
        print(f"Error: songfile not found: {in_path}")
        sys.exit(1)

    out_dir = Path(args.output_dir) if args.output_dir else Path(f"{in_path.stem}_files")
    ensure_dir(out_dir)

    details_dir = Path(args.details_dir) if args.details_dir else None

    want = {s.strip().lower() for s in args.formats.split(',')}
    want_mp3 = 'mp3' in want
    want_mp4 = 'mp4' in want
    want_wav = 'wav' in want

    # Prepare auth headers if provided (used for WAV and MP4 triggers)
    headers = {}
    if args.auth:
        headers = {"Authorization": f"Bearer {args.auth}", "Accept": "*/*"}
        if args.session_id:
            headers["session-id"] = args.session_id
        if args.browser_token:
            headers["browser-token"] = args.browser_token
        if args.device_id:
            headers["device-id"] = args.device_id

    # First pass: collect unique UUIDs preserving first occurrence for naming
    unique_map = {}
    order = []
    total_lines = 0
    with open(in_path, 'r', encoding='utf-8') as f:
        for raw in f:
            total_lines += 1
            line = raw.strip()
            if not line:
                continue
            if '|' in line:
                left, url = [s.strip() for s in line.split('|', 1)]
            else:
                # URL-only mode
                left, url = "", line.strip()
            uuid = extract_uuid(url)
            if not uuid:
                print(f"Skipping line (cannot extract uuid): {line}")
                continue
            if uuid not in unique_map:
                unique_map[uuid] = (left, url)
                order.append(uuid)

    # Write temp file with unique cleaned UUIDs (one per line)
    tmp_uuid_path = in_path.with_name(f"{in_path.stem}.unique_uuids.tmp")
    try:
        tmp_uuid_path.write_text("\n".join(order) + ("\n" if order else ""), encoding='utf-8')
        print(f"Unique UUID list written: {tmp_uuid_path} ({len(order)} unique of {total_lines} lines)")
    except Exception as e:
        print(f"Failed to write unique UUID temp file: {e}")

    if not order:
        print("No valid UUIDs found. Exiting.")
        sys.exit(0)

    total_unique = len(order)
    print(f"Processing {total_unique} unique UUID(s)...")

    # Timing start
    t_start = time.time()

    # Processing in parallel (max 5 workers) with optional Rich progress
    index_map = {u: i for i, u in enumerate(order, 1)}
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = []
        # Per-song progress
        def calc_steps() -> int:
            steps = 0
            if want_mp3:
                steps += 1  # download
                if not args.no_metadata:
                    steps += 1  # metadata
                if not args.no_embed_uuid:
                    steps += 1  # uuid
            if want_mp4:
                steps += 1  # download
                if not args.no_embed_uuid:
                    steps += 1  # metadata
            if want_wav:
                steps += 1  # download
                if not args.no_embed_uuid:
                    steps += 1  # metadata
            steps += 1  # sidecar
            return steps

        # Build a Progress instance to embed in Live (no separate Progress live)
        progress = Progress(
            SpinnerColumn(style="info"),
            TextColumn("[info]{task.fields[desc]}"),
            BarColumn(bar_width=None),
            TextColumn("{task.completed}/{task.total}"),
            TimeElapsedColumn(),
            TimeRemainingColumn(),
            console=console,
        )

        # Seed logs with queue listing
        print("Queue:")
        for i, u in enumerate(order, 1):
            print(f"[#Q{i}] {u}")

        # Compute effective panel heights so they don't exceed the window
        term_h = console.size.height or 40
        # Leave some margin for borders/titles
        logs_h_eff = max(5, min(args.logs_height, term_h // 2))
        downloads_h_eff = max(5, min(args.downloads_height, term_h - logs_h_eff - 4))

        # Use Layout to ensure same width panels and separate Active vs Queue
        layout = Layout()
        layout.split_column(
            Layout(name="top", size=logs_h_eff),
            Layout(name="bottom", size=downloads_h_eff),
        )
        layout["bottom"].split_row(
            Layout(name="left", ratio=3),
            Layout(name="queue", ratio=1),
        )
        # Left side: split into Stats (top) and Active (bottom)
        stats_height = max(5, min(8, downloads_h_eff // 3))
        layout["left"].split_column(
            Layout(name="stats", size=stats_height),
            Layout(name="active"),
        )

        queue_text = Text()
        def refresh_queue_text(pending_list: list[str]):
            queue_text.__init__("\n".join(pending_list))

        # Render only the most recent log lines that can fit into the logs panel height.
        def build_logs_panel(max_rows: int) -> Panel:
            # Deduct some space for panel title/borders and keep a safe minimum
            visible_rows = max(3, max_rows - 2)
            text = Text()
            with LOG_LOCK:
                # Take the last N lines for display
                recent = list(LOG_LINES)[-visible_rows:]
            first = True
            for t in recent:
                if not first:
                    text.append("\n")
                text.append_text(t)
                first = False
            return Panel(Align(text, vertical="bottom"), title="Logs", expand=True)

        layout["top"].update(build_logs_panel(logs_h_eff))
        layout["active"].update(Panel(progress, title="Active", expand=True))
        layout["queue"].update(Panel(Align(queue_text, vertical="top"), title="Queue", expand=True))

        with Live(layout, refresh_per_second=8, console=console):
            # Aggregates
            agg = {
                "mp3_saved": 0,
                "mp3_failed": 0,
                "mp3_skipped_existing": 0,
                "mp3_skipped_uuid": 0,
                "mp4_saved": 0,
                "mp4_failed": 0,
                "mp4_skipped_existing": 0,
                "wav_saved": 0,
                "wav_failed": 0,
                "wav_skipped_existing": 0,
            }

            # Helper to build compact stats panel (live updating)
            def style_count(n: int, kind: str) -> str:
                if kind == "ok":
                    return f"[green]{n}[/]" if n > 0 else f"[dim]{n}[/]"
                if kind == "fail":
                    return f"[red]{n}[/]" if n > 0 else f"[dim]{n}[/]"
                if kind == "skip":
                    return f"[yellow]{n}[/]" if n > 0 else f"[dim]{n}[/]"
                return str(n)

            def build_stats_panel() -> Panel:
                t = Text()
                def add_line(label: str, kind: str, mp3v=None, mp4v=None, wavv=None):
                    # Label
                    t.append(label + ": ", style="bold")
                    parts = []
                    if want_mp3 and mp3v is not None:
                        parts.append(("MP3 ", style_count(mp3v or 0, kind)))
                    if want_mp4 and mp4v is not None:
                        parts.append(("MP4 ", style_count(mp4v or 0, kind)))
                    if want_wav and wavv is not None:
                        parts.append(("WAV ", style_count(wavv or 0, kind)))
                    # Render parts: label each format with fixed color tag and colored count
                    first = True
                    for tag, count_str in parts:
                        if not first:
                            t.append("  ")
                        t.append(tag, style="cyan")
                        # count_str contains markup like [green]X[/] -> parse as Rich Text
                        t.append_text(Text.from_markup(count_str))
                        first = False
                    t.append("\n")

                add_line("New", "ok",
                         agg["mp3_saved"] if want_mp3 else None,
                         agg["mp4_saved"] if want_mp4 else None,
                         agg["wav_saved"] if want_wav else None)
                add_line("Fail", "fail",
                         agg["mp3_failed"] if want_mp3 else None,
                         agg["mp4_failed"] if want_mp4 else None,
                         agg["wav_failed"] if want_wav else None)
                add_line("Skip exist", "skip",
                         agg["mp3_skipped_existing"] if want_mp3 else None,
                         agg["mp4_skipped_existing"] if want_mp4 else None,
                         agg["wav_skipped_existing"] if want_wav else None)
                if want_mp3:
                    add_line("Skip dup UUID", "skip", agg["mp3_skipped_uuid"], None, None)
                return Panel(Align(t, vertical="top"), title="Stats", title_align="left", expand=True)

            # Submit up to max_workers; keep the rest in a pending queue displayed at right
            pending = deque(order)
            running: dict[concurrent.futures.Future, int] = {}

            def submit_next(slots: int = 1):
                nonlocal pending
                count = 0
                while pending and count < slots and len(running) < executor._max_workers:
                    u = pending.popleft()
                    left, url = unique_map[u]
                    steps = calc_steps()
                    desc = f"#{index_map[u]} ({u})"
                    task_id = progress.add_task("song", total=steps, desc=desc)
                    fut = executor.submit(
                        process_one, u, left, url, index_map[u], total_unique,
                        args, out_dir, details_dir, headers, want_mp3, want_mp4, want_wav,
                        progress, task_id
                    )
                    running[fut] = task_id
                    count += 1
                # Refresh queue panel text
                refresh_queue_text([f"{i+1}. {u}" for i, u in enumerate(pending)])

            # Initial submissions and initial stats render
            submit_next(slots=executor._max_workers)
            layout["stats"].update(build_stats_panel())

            # Drive the event loop while there are running tasks
            while running:
                done, _ = concurrent.futures.wait(list(running.keys()), timeout=0.25, return_when=concurrent.futures.FIRST_COMPLETED)
                # Refresh logs panel to keep view pinned to latest entries
                layout["top"].update(build_logs_panel(logs_h_eff))
                for fut in done:
                    tid = running.pop(fut)
                    try:
                        res = fut.result()
                        for k in agg:
                            agg[k] += 1 if res.get(k) else 0
                    except Exception:
                        pass
                    # Remove finished task from Active panel
                    try:
                        progress.remove_task(tid)
                    except Exception:
                        progress.update(tid, visible=False)
                    # Submit one more from queue
                    submit_next(slots=1)
                    # Update stats panel
                    layout["stats"].update(build_stats_panel())
                    # Refresh logs after updates too
                    layout["top"].update(build_logs_panel(logs_h_eff))

    # Timing end
    elapsed = time.time() - t_start
    mins = int(elapsed // 60)
    secs = int(elapsed % 60)
    # Summary output (color-coded, per-format columns)
    total_songs = total_unique
    mp3_ok = agg.get("mp3_saved", 0)
    mp3_fail = agg.get("mp3_failed", 0)
    mp3_skip_exist = agg.get("mp3_skipped_existing", 0)
    mp3_skip_uuid = agg.get("mp3_skipped_uuid", 0)
    mp4_ok = agg.get("mp4_saved", 0)
    mp4_fail = agg.get("mp4_failed", 0)
    mp4_skip_exist = agg.get("mp4_skipped_existing", 0)
    wav_ok = agg.get("wav_saved", 0)
    wav_fail = agg.get("wav_failed", 0)
    wav_skip_exist = agg.get("wav_skipped_existing", 0)

    def style_count(n: int, kind: str) -> str:
        if kind == "ok":
            return f"[green]{n}[/]" if n > 0 else f"[dim]{n}[/]"
        if kind == "fail":
            return f"[red]{n}[/]" if n > 0 else f"[dim]{n}[/]"
        if kind == "skip":
            return f"[yellow]{n}[/]" if n > 0 else f"[dim]{n}[/]"
        return str(n)

    tbl = Table(title="Run Summary", show_edge=True, header_style="bold", title_style="bold cyan")
    tbl.add_column("Metric", justify="left", style="bold")
    if want_mp3:
        tbl.add_column("MP3", justify="right")
    if want_mp4:
        tbl.add_column("MP4", justify="right")
    if want_wav:
        tbl.add_column("WAV", justify="right")

    # Utility to add a row with only selected formats
    def add_row(metric: str, mp3v: Optional[int] = None, mp4v: Optional[int] = None, wavv: Optional[int] = None, kind: str = "ok"):
        row = [metric]
        if want_mp3:
            row.append(style_count(mp3v or 0, kind) if mp3v is not None else "")
        if want_mp4:
            row.append(style_count(mp4v or 0, kind) if mp4v is not None else "")
        if want_wav:
            row.append(style_count(wavv or 0, kind) if wavv is not None else "")
        tbl.add_row(*row)

    console.print(Panel(f"Duration: [bold]{mins}m {secs}s[/] ([dim]{elapsed:.1f}s[/])\nSongs processed: [bold]{total_songs}[/]", title="Run Info", expand=False))

    add_row("Newly saved", mp3_ok if want_mp3 else None, mp4_ok if want_mp4 else None, wav_ok if want_wav else None, kind="ok")
    add_row("Failed (this run)", mp3_fail if want_mp3 else None, mp4_fail if want_mp4 else None, wav_fail if want_wav else None, kind="fail")
    add_row("Skipped: existing file", mp3_skip_exist if want_mp3 else None, mp4_skip_exist if want_mp4 else None, wav_skip_exist if want_wav else None, kind="skip")
    # MP3-only extra skip reason
    if want_mp3:
        add_row("Skipped: duplicate UUID in library", mp3_skip_uuid, None, None, kind="skip")

    console.print(tbl)

    print(f"\nDone. Output: {out_dir}")


if __name__ == "__main__":
    main()
