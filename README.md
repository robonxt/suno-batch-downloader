# Suno Batch Recovery Tool

Batch recovery tool for songs from Suno with embedded metadata (title, artist, cover art, styles, lyrics). Extracts progressive audio stream directly from public clip metadata.

## Requirements
- Python 3.8+
- ffmpeg (in PATH)

## Install
```bash
pip install requests mutagen rich cryptography
```
*(Note: `cryptography` provides hardware-accelerated AES decryption for large batches. If omitted, pure-Python fallback is used automatically).*

## Usage

Use `suno_batch_recovery.py`.

### 1. Browser Web UI Mode
Launch via double-clicking `start_web_ui.bat` or run:
```bash
python suno_batch_recovery.py --web
```
Open `http://localhost:8080` in your browser. Paste unlimited URLs (`suno.com/song/...`, `suno.com/s/...`, UUIDs), preview tracks, and download MP3/original audio/cover/lyrics or export CSV/JSON/ZIP.

### 2. CLI Batch Mode
```bash
python suno_batch_recovery.py urls.txt -o ./downloads --formats mp3,original --csv --json
```

Options:
```
--formats        Comma-separated list: mp3,original,wav,cover,info (default: mp3,original)
--no-metadata    Skip embedding metadata tags and artwork into audio files
--overwrite      Re-download and overwrite existing files (default: skip existing)
--csv            Export catalog table as CSV
--json           Export structured metadata JSON
--zip            Package downloaded tracks into a ZIP file
--workers        Concurrent workers (default: 5)
--ffmpeg         Path to ffmpeg executable (if not in PATH)
--web            Launch local Web UI on http://localhost:8080
--no-tui         Disable Rich terminal UI and use plain text output
--logs-height    Height of terminal logs panel in lines (default: 10)
```

## Legacy Downloader
The legacy pre-September 2026 downloader (`unified_downloader.py`) has been archived on branch `pre-suno-6.0-sept-2026`.
```bash
git checkout pre-suno-6.0-sept-2026
```