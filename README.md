# Suno Batch Downloader

Batch download songs from Suno with embedded metadata (title, artist, cover art, styles, lyrics).

## Requirements
- Python 3.8+
- ffmpeg (in PATH)

## Install
```bash
pip install requests mutagen rich
```

## High-Limit Batch Recovery (September 2026 & Beyond)

To bypass Suno's 403 download blocks and `usesuno.com`'s 20-URL limit, use `suno_batch_recovery.py`. It extracts the progressive audio stream directly from public clip metadata, supports unlimited URLs, and avoids browser CORS issues:

### 1. Browser Web UI Mode
Launch the local web dashboard:
```bash
python suno_batch_recovery.py --web
```
Open `http://localhost:8080` in your browser. Paste unlimited URLs (`suno.com/song/...`, `suno.com/s/...`, UUIDs), preview tracks, and download MP3/original audio/cover/lyrics or export CSV/JSON/ZIP.

### 2. CLI Batch Mode
```bash
python suno_batch_recovery.py urls.txt -o ./downloads --formats original,info --csv --json
```

Options:
```
--formats    Comma-separated list: mp3,wav,original,info,cover (default: original,info)
--csv        Export catalog table as CSV
--json       Export structured metadata JSON
--zip        Package downloaded tracks into a ZIP file
--workers    Concurrent workers (default: 5)
--ffmpeg     Path to ffmpeg executable (if not in PATH)
--web        Launch local Web UI on http://localhost:8080
```

## Legacy Downloader
The legacy pre-September 2026 downloader (`unified_downloader.py`) has been archived on branch `pre-suno-6.0-sept-2026`.
```bash
git checkout pre-suno-6.0-sept-2026
```