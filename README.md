# Suno Batch Downloader

Batch download songs from Suno with embedded metadata (title, artist, cover art, styles, lyrics).

## Requirements
- Python 3.8+
- ffmpeg (in PATH)

## Install
```bash
pip install requests mutagen rich
```

## Usage

**1. Get song URLs** - Run `console.js` in browser console on `https://suno.com/me` (aka `Your Library`), copy output to `urls.txt`.

**2. Download**
```bash
python unified_downloader.py urls.txt -o ./downloads
```

## Options
Common options:
```
-o, --output DIR     Output directory (default: ./downloads)
--mp4               Also download MP4
--wav               Also download WAV (requires auth)
--no-metadata       Skip metadata embedding
--name-mode         Filename: uuid|details|input (default: details)
```
Run `python unified_downloader.py --help` for more info on optional arguments.


## Auth (optional, for WAV/private songs)
Set `SUNO_COOKIE` env var with your browser cookie.