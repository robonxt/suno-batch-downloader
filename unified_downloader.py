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
- Requires Python 3 and requests. ffmpeg is required for MP3 cover embedding and for simple metadata updates.
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

import requests
try:
    from mutagen.id3 import ID3, TXXX
    MUTAGEN_AVAILABLE = True
except Exception:
    MUTAGEN_AVAILABLE = False

CDN_AUDIO = "https://cdn1.suno.ai"
CDN_IMAGE = "https://cdn2.suno.ai"
STUDIO_BASE = "https://studio-api.prod.suno.com"


def ensure_dir(p: Path):
    p.mkdir(parents=True, exist_ok=True)


# Global timestamped print
def _ts_print(*args, **kwargs):
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    try:
        builtins.print(f"[{ts}] ", end='')
        builtins.print(*args, **kwargs)
    except Exception:
        # Fallback to normal print if formatting fails for any reason
        builtins.print(*args, **kwargs)

# Override built-in print for this script to include timestamps
print = _ts_print  # type: ignore


def extract_uuid(url: str) -> Optional[str]:
    m = re.search(r"https://cdn1\.suno\.ai/([^.]+)\.(mp3|mp4|wav)", url.strip())
    return m.group(1) if m else None


def run_ffmpeg(cmd: list[str]) -> bool:
    try:
        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return res.returncode == 0
    except Exception:
        return False





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
        print(f"❌ Download failed {url}: {e}")
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
    # Try oEmbed
    data = fetch_details_oembed(song_uuid)
    if isinstance(data, dict) and data:
        return {"source": "oembed", "data": data}
    # Fallback parse of page
    data2 = fetch_details_page(song_uuid)
    if isinstance(data2, dict) and data2:
        return {"source": "page", "data": data2}
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


def trigger_wav_and_wait(uuid: str, out_path: Path, headers: Dict[str, str], poll_interval: int, poll_timeout: int) -> bool:
    endpoint = f"{STUDIO_BASE}/api/gen/{uuid}/convert_wav/"
    try:
        r = requests.post(endpoint, headers=headers, timeout=60)
        if r.status_code // 100 != 2:
            print(f"❌ WAV trigger failed {r.status_code}: {r.text[:400]}")
            return False
    except requests.RequestException as e:
        print(f"❌ WAV trigger error: {e}")
        return False

    start = time.time()
    attempt = 1
    print("⏳ Waiting for WAV...")
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
                    print("⬇️ WAV URL ready; downloading...")
                    return download(dl_url, out_path)
        except requests.RequestException:
            pass
        # Probe fixed CDN URL without exposing options in CLI
        probe_url = f"{CDN_AUDIO}/{uuid}.wav"
        try:
            pr = requests.head(probe_url, timeout=15, allow_redirects=True)
            if pr.status_code == 200:
                print("⬇️ WAV probe succeeded; downloading...")
                return download(probe_url, out_path)
        except requests.RequestException:
            pass
        attempt += 1
    print("⏰ WAV timeout")
    return False


def trigger_mp4_and_wait(uuid: str, out_path: Path, headers: Dict[str, str], poll_interval: int, poll_timeout: int) -> bool:
    endpoint = f"{STUDIO_BASE}/api/video/generate/{uuid}/"
    try:
        r = requests.post(endpoint, headers=headers, timeout=60)
        print(f"🎬 MP4 generate -> {r.status_code}")
        if r.status_code // 100 not in (2,):
            # 204 No Content is typical on success
            return False
    except requests.RequestException as e:
        print(f"❌ MP4 trigger error: {e}")
        return False

    start = time.time()
    print("⏳ Waiting for MP4...")
    while time.time() - start < poll_timeout:
        time.sleep(poll_interval)
        # Probe fixed CDN URL
        probe_url = f"{CDN_AUDIO}/{uuid}.mp4"
        try:
            pr = requests.head(probe_url, timeout=15, allow_redirects=True)
            if pr.status_code == 200:
                print("⬇️ MP4 probe succeeded; downloading...")
                return download(probe_url, out_path)
        except requests.RequestException:
            pass
    print("⏰ MP4 timeout")
    return False


def main():
    parser = argparse.ArgumentParser(description="Unified Suno downloader/embedder")
    parser.add_argument("--songfile", required=True, help="[required] Input file with lines: FILENAME|URL")
    parser.add_argument("--formats", default="mp3", help="[optional] Comma list: mp3,mp4,wav (default: mp3)")
    parser.add_argument("--output-dir", help="[optional] Destination directory (default: {songfile}_files)")
    parser.add_argument("--wait", action="store_true", help="[optional] When WAV or MP4 requested, wait until downloadable (trigger and poll if needed)")
    parser.add_argument("--no-metadata", action="store_true", help="[optional] Skip metadata writing (cover/art and tags)")
    parser.add_argument("--auth", help="[optional] Authorization Bearer token for WAV/MP4 generation")

    parser.add_argument("--details-dir", help="[optional] Directory with per-uuid JSON details to enrich tags")

    parser.add_argument("--poll-interval", type=int, default=10, help="[optional] Poll interval (seconds) for WAV/MP4 generation (default: 10)")
    parser.add_argument("--poll-timeout", type=int, default=300, help="[optional] Poll timeout (seconds) for WAV/MP4 generation (default: 300)")

    parser.add_argument("--ffmpeg-path", default="ffmpeg", help="[optional] Path to ffmpeg (used for tagging and metadata)")
    parser.add_argument("--name-mode", choices=["input", "details", "uuid"], default="input", help="[optional] How to choose base filename when title missing or to override (default: input)")
    parser.add_argument("--no-embed-uuid", action="store_true", help="[optional] Do not embed UUID into metadata (embeds by default)")

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

    # Processing loop over unique UUIDs only
    for idx, uuid in enumerate(order, 1):
        left, url = unique_map[uuid]

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
        print(f"\n[{idx}/{total_unique}] === {filename} (uuid={uuid}) ===")

        # Check for existing MP3 with same UUID, but don't skip other formats
        already_have_uuid = any_file_with_uuid(out_dir, uuid)
        if already_have_uuid:
            print("Found existing MP3 with same UUID; will skip MP3 but still process other formats.")

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
                cover_url = clip.get('image_large_url') or clip.get('image_url') or cover_url
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
                cover_url = data.get('thumbnail_url') or cover_url

        # Prepend UUID to comment for broad containers
        base_comment = ' | '.join([p for p in comment_parts if p])
        comment = f"uuid={uuid}" + (f" | {base_comment}" if base_comment else "")

        # Prepare canonical URLs for formats based on UUID
        url_is_mp3 = url.lower().endswith('.mp3')
        url_is_mp4 = url.lower().endswith('.mp4')
        mp3_url = url if url_is_mp3 else f"{CDN_AUDIO}/{uuid}.mp3"
        mp4_url = url if url_is_mp4 else f"{CDN_AUDIO}/{uuid}.mp4"

        # MP3
        if want_mp3:
            if already_have_uuid:
                pass
            mp3_name = f"{base_stem}.mp3"
            mp3_dest = out_dir / mp3_name
            if not mp3_dest.exists() and not already_have_uuid:
                if download(mp3_url, mp3_dest):
                    print(f"MP3 saved: {mp3_dest.name}")
                else:
                    print("MP3 download failed; continuing")
                    continue
            if not args.no_metadata:
                # Cover sidecar
                cover_path = out_dir / f"{uuid}.jpeg"
                if not cover_path.exists():
                    download(cover_url, cover_path)
                print("Embedding MP3 metadata and cover...")
                if embed_mp3(args.ffmpeg_path, mp3_dest, cover_path if cover_path.exists() else None, title, artist or 'Suno', comment):
                    print("MP3 tagging complete")
                else:
                    print("MP3 tagging failed")

            # Embed UUID via TXXX for robust detection
            if not args.no_embed_uuid:
                if embed_mp3_uuid_txxx(mp3_dest, uuid):
                    print("Embedded UUID (TXXX:SUNO_UUID)")
                else:
                    print("Could not embed UUID TXXX; comment still contains uuid=")



        # MP4
        if want_mp4:
            mp4_name = f"{base_stem}.mp4"
            mp4_dest = out_dir / mp4_name
            # If we have auth and user requested waiting, try to generate MP4 like WAV
            if not mp4_dest.exists() and headers and args.wait:
                ok_gen = trigger_mp4_and_wait(uuid, mp4_dest, headers, args.poll_interval, args.poll_timeout)
                if ok_gen:
                    print(f"MP4 saved: {mp4_dest.name}")
            # If still not present, try direct download (in case it's already available publicly)
            if not mp4_dest.exists():
                if download(mp4_url, mp4_dest):
                    print(f"MP4 saved: {mp4_dest.name}")
                else:
                    print("MP4 download failed")
            # Write simple metadata including UUID in comment
            if not args.no_embed_uuid:
                if embed_simple_metadata(args.ffmpeg_path, mp4_dest, {"title": title, "artist": artist or 'Suno', "comment": comment}):
                    print("MP4 metadata updated with UUID")
                else:
                    print("MP4 metadata update failed")

        # WAV
        if want_wav:
            if not headers:
                print("WAV requested but no Studio auth provided; skipping")
            else:
                wav_name = f"{stem}.wav"
                wav_dest = out_dir / wav_name
                if wav_dest.exists():
                    print(f"Skipping existing WAV: {wav_dest.name}")
                else:
                    ok = trigger_wav_and_wait(uuid, wav_dest, headers, args.poll_interval, args.poll_timeout)
                    if ok:
                        print(f"WAV saved: {wav_dest.name}")
                        # Add simple metadata with UUID in comment
                        if not args.no_embed_uuid:
                            if embed_simple_metadata(args.ffmpeg_path, wav_dest, {"title": title, "artist": artist or 'Suno', "comment": comment}):
                                print("WAV metadata updated with UUID")
                            else:
                                print("WAV metadata update failed")
                    else:
                        print("WAV was not obtained")

        # Save details JSON sidecar for traceability
        if details:
            try:
                (out_dir / 'metadata').mkdir(exist_ok=True)
                with open(out_dir / 'metadata' / f"{uuid}.json", 'w', encoding='utf-8') as jf:
                    json.dump(details, jf, indent=2, ensure_ascii=False)
            except Exception:
                pass

    print(f"\nDone. Output: {out_dir}")


if __name__ == "__main__":
    main()
