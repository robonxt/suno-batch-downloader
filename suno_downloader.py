#!/usr/bin/env python3
"""
Python port of suno-downloader.sh (no unofficial APIs).
- Reads lines in SONGFILE as: "FILENAME|URL"
- Downloads MP3 files and associated JPEG covers using UUID from the URL
- Embeds cover art into MP3 using ffmpeg (Front Cover)
- Optionally downloads MP4 files when --include-mp4 is provided
- Avoids duplicates by URL and existing files
- Minimal, clean console output (ffmpeg silenced)
"""

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional, Set

import requests

CDN_IMAGE = "https://cdn2.suno.ai"


def extract_uuid_from_audio_url(url: str) -> Optional[str]:
    m = re.search(r"https://cdn1\.suno\.ai/([^.]+)\.(mp3|mp4)", url.strip())
    return m.group(1) if m else None


def download_to(url: str, dest: Path) -> bool:
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with requests.get(url, stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
        return True
    except Exception as e:
        print(f"Failed to download: {url} -> {e}")
        return False


def embed_cover_with_ffmpeg(ffmpeg_path: str, mp3_path: Path, image_path: Path) -> bool:
    temp_path = mp3_path.with_name(f"temp_{mp3_path.name}")
    cmd = [
        ffmpeg_path,
        "-i", str(mp3_path),
        "-i", str(image_path),
        "-c", "copy",
        "-map", "0",
        "-map", "1",
        "-c:v", "copy",
        "-metadata:s:v", "title=Album cover",
        "-metadata:s:v", "comment=Cover (front)",
        "-disposition:v", "attached_pic",
        "-y", str(temp_path),
    ]
    try:
        # Silence ffmpeg output
        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if res.returncode != 0:
            print("Failed to embed album art (ffmpeg error)")
            if temp_path.exists():
                temp_path.unlink(missing_ok=True)
            return False
        # Replace original with temp
        os.replace(temp_path, mp3_path)
        return True
    except Exception as e:
        print(f"Failed to embed album art: {e}")
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)
        return False


def main():
    parser = argparse.ArgumentParser(description="Download Suno MP3/MP4 with optional MP3 cover embedding")
    parser.add_argument("--songfile", required=True, help="Input file with lines: FILENAME|URL")
    parser.add_argument("--include-mp4", action="store_true", help="Also download MP4 entries")
    parser.add_argument("--ffmpeg-path", default="ffmpeg", help="Path to ffmpeg executable")
    parser.add_argument("--no-embed", action="store_true", help="Skip embedding cover art into MP3 files")
    args = parser.parse_args()

    songfile = Path(args.songfile)
    if not songfile.exists():
        print(f"Error: File '{songfile}' does not exist.")
        sys.exit(1)

    basename = songfile.stem
    dest_dir = Path(f"{basename}_files")
    dest_dir.mkdir(parents=True, exist_ok=True)

    downloaded_urls: Set[str] = set()

    with open(songfile, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or "|" not in line:
                continue

            filename, url = [s.strip() for s in line.split("|", 1)]

            # Type filter
            if filename.lower().endswith(".mp3"):
                pass
            elif filename.lower().endswith(".mp4"):
                if not args.include_mp4:
                    continue
            else:
                # Unknown type; skip
                continue

            uuid = extract_uuid_from_audio_url(url)
            if not uuid:
                print(f"Warning: Could not extract UUID from {url}")
                continue

            dest_file = dest_dir / filename

            # Skip if already exists
            if dest_file.exists():
                print(f"Skipping {filename} (already exists)")
                continue

            # Skip duplicate URL lines
            if url in downloaded_urls:
                print(f"Skipping {filename} (duplicate URL already downloaded)")
                continue

            print(f"Downloading {filename}...")
            if download_to(url, dest_file):
                downloaded_urls.add(url)
                print(f"Successfully downloaded: {dest_file.name}")

                # MP3: fetch image and embed
                if filename.lower().endswith(".mp3"):
                    if args.no_embed:
                        # User chose not to embed; still try to download cover image alongside for reference
                        image_url = f"{CDN_IMAGE}/image_large_{uuid}.jpeg"
                        image_path = dest_dir / f"{uuid}.jpeg"
                        if not image_path.exists():
                            download_to(image_url, image_path)
                        print("Skipping album art embedding (--no-embed)")
                        continue
                    image_url = f"{CDN_IMAGE}/image_large_{uuid}.jpeg"
                    image_path = dest_dir / f"{uuid}.jpeg"

                    if not image_path.exists():
                        print(f"Downloading image: {image_path.name}")
                        if not download_to(image_url, image_path):
                            print("Failed to download image; skipping embedding")
                            continue
                        else:
                            print(f"Successfully downloaded image: {image_path.name}")

                    # Embed cover
                    print(f"Embedding album art in {filename}...")
                    if embed_cover_with_ffmpeg(args.ffmpeg_path, dest_file, image_path):
                        print("File ready with embedded album art")
                    else:
                        print("Failed to embed album art")
                else:
                    # MP4: just confirm
                    print(f"MP4 file ready: {dest_file.name}")
            else:
                print(f"Failed to download file: {filename}")

    print(f"All files downloaded to: {dest_dir}")


if __name__ == "__main__":
    main()
