#!/usr/bin/env python3
"""
Trigger WAV conversions on Suno Studio using your browser auth headers.
- Reads lines: "FILENAME|URL"
- Extracts UUID from cdn1.suno.ai URLs
- Calls: https://studio-api.prod.suno.com/api/gen/{UUID}/convert_wav/
- If response includes a direct WAV URL, downloads it to {songfile_stem}_wav/
- NO unofficial third-party API usage. Uses your provided Authorization and optional headers.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Optional

import requests

STUDIO_BASE = "https://studio-api.prod.suno.com"


def extract_uuid(url: str) -> Optional[str]:
    m = re.search(r"https://cdn1\.suno\.ai/([^.]+)\.(mp3|mp4)", url.strip())
    return m.group(1) if m else None


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
        print(f"❌ Download failed: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Trigger Suno Studio WAV conversion with your auth headers")
    parser.add_argument("--songfile", required=True, help="Input file with lines: FILENAME|URL")
    parser.add_argument("--auth-bearer", required=True, help="Authorization Bearer token copied from browser")
    parser.add_argument("--session-id", help="Optional session-id header from browser")
    parser.add_argument("--browser-token", help="Optional browser-token JSON string from browser")
    parser.add_argument("--device-id", help="Optional device-id header from browser")
    parser.add_argument("--output-dir", help="Output directory (default: {songfile}_wav)")
    parser.add_argument("--save-response", action="store_true", help="Save API JSON responses alongside outputs")
    parser.add_argument("--wait", action="store_true", help="Wait for conversion to finish before moving to next song")
    parser.add_argument("--poll-interval", type=int, default=10, help="Seconds between status polls when waiting")
    parser.add_argument("--poll-timeout", type=int, default=300, help="Max seconds to wait per song when --wait is used")
    parser.add_argument("--wav-url-template", default="https://cdn1.suno.ai/{uuid}.wav", help="Template to probe for final WAV (use {uuid} placeholder)")
    parser.add_argument("--probe-http-method", choices=["HEAD", "GET"], default="HEAD", help="HTTP method to use when probing WAV URL")
    args = parser.parse_args()

    songfile = Path(args.songfile)
    if not songfile.exists():
        print(f"Error: File not found: {songfile}")
        sys.exit(1)

    base = songfile.stem
    out_dir = Path(args.output_dir) if args.output_dir else Path(f"{base}_wav")
    out_dir.mkdir(parents=True, exist_ok=True)

    # Build headers
    headers = {
        "Authorization": f"Bearer {args.auth_bearer}",
        "Accept": "*/*",
    }
    if args.session_id:
        headers["session-id"] = args.session_id
    if args.browser_token:
        headers["browser-token"] = args.browser_token
    if args.device_id:
        headers["device-id"] = args.device_id

    print("Starting WAV conversion trigger")
    print(f"Songfile: {songfile}")
    print(f"Output dir: {out_dir}")
    print(f"Wait mode: {'ON' if args.wait else 'OFF'} | Interval: {args.poll_interval}s | Timeout: {args.poll_timeout}s")
    print(f"Probe template: {args.wav_url_template} | Method: {args.probe_http_method}")

    with open(songfile, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or "|" not in line:
                continue
            filename, url = [s.strip() for s in line.split("|", 1)]
            if not filename.lower().endswith(('.mp3', '.mp4')):
                continue

            uuid = extract_uuid(url)
            if not uuid:
                print(f"⚠️ Could not extract UUID from: {url}")
                continue

            wav_name = Path(filename).with_suffix('.wav').name
            wav_path = out_dir / wav_name
            if wav_path.exists():
                print(f"⏭️ Skipping existing: {wav_name}")
                continue

            endpoint = f"{STUDIO_BASE}/api/gen/{uuid}/convert_wav/"
            print("\n------------------------------")
            print(f"🔄 Requesting WAV for: {filename}")
            print(f"UUID: {uuid}")
            print(f"Endpoint: {endpoint}")

            try:
                r = requests.post(endpoint, headers=headers, timeout=60)
                status = r.status_code
                body_text = r.text or ''
                # Save raw response if requested
                if args.save_response:
                    with open(out_dir / f"{uuid}_response.json", "w", encoding="utf-8") as jf:
                        jf.write(body_text)

                if status // 100 != 2:
                    print(f"❌ Initial API call returned {status}. Body (truncated): {body_text[:400]}")
                    continue

                # Try parse JSON and locate a direct URL if provided
                dl_url = None
                try:
                    body = r.json()
                    # Common possibilities
                    dl_url = (
                        body.get('audioWavUrl')
                        or body.get('data', {}).get('audioWavUrl')
                        or body.get('download_url')
                        or body.get('data', {}).get('download_url')
                    )
                except Exception:
                    print("ℹ️ Response was not JSON or parse failed; will rely on wait/poll if enabled")

                if dl_url:
                    print("⬇️ Download URL provided immediately; downloading...")
                    if download(dl_url, wav_path):
                        print(f"✅ Saved {wav_name}")
                    else:
                        print("❌ Download failed from provided URL")
                else:
                    if not args.wait:
                        print("⏳ Conversion triggered. No URL in response. Proceeding to next (use --wait to poll and download).")
                        continue

                    # Wait/poll loop: re-POST same endpoint and check for URL
                    import time
                    start = time.time()
                    attempt = 1
                    print("⏳ Waiting for conversion to complete...")
                    while time.time() - start < args.poll_timeout:
                        time.sleep(args.poll_interval)
                        try:
                            r2 = requests.post(endpoint, headers=headers, timeout=60)
                            status2 = r2.status_code
                            body2 = r2.text or ''
                            elapsed = int(time.time() - start)
                            print(f"[poll #{attempt}] +{elapsed}s status={status2}")
                            if args.save_response and status2 // 100 == 2:
                                with open(out_dir / f"{uuid}_poll_{attempt}.json", "w", encoding="utf-8") as jf:
                                    jf.write(body2)
                            if status2 // 100 != 2:
                                attempt += 1
                                # Even if non-2xx, still try a direct probe
                            # Try parse JSON for URL
                            try:
                                bodyj = r2.json()
                                dl_url = (
                                    bodyj.get('audioWavUrl')
                                    or bodyj.get('data', {}).get('audioWavUrl')
                                    or bodyj.get('download_url')
                                    or bodyj.get('data', {}).get('download_url')
                                )
                            except Exception:
                                dl_url = None
                            if dl_url:
                                print("⬇️ Download URL available; downloading...")
                                if download(dl_url, wav_path):
                                    print(f"✅ Saved {wav_name}")
                                else:
                                    print("❌ Download failed from provided URL")
                                break

                            # No URL in API response; probe a guessed CDN URL
                            probe_url = args.wav_url_template.format(uuid=uuid)
                            print(f"[probe +{elapsed}s] Checking {probe_url} via {args.probe_http_method}...")
                            try:
                                if args.probe_http_method == "HEAD":
                                    pr = requests.head(probe_url, timeout=15, allow_redirects=True)
                                else:
                                    pr = requests.get(probe_url, timeout=15, stream=True)
                                print(f"[probe] status={pr.status_code}")
                                if pr.status_code == 200:
                                    # Found! Download using GET to file
                                    print("[probe] URL exists; downloading WAV...")
                                    if download(probe_url, wav_path):
                                        print(f"✅ Saved {wav_name} (via probe)")
                                        break
                                    else:
                                        print("❌ Download failed from probe URL")
                                else:
                                    print("[probe] Not ready yet")
                            except requests.RequestException as pe:
                                print(f"[probe error] {pe}")
                            attempt += 1
                        except requests.RequestException as e:
                            elapsed = int(time.time() - start)
                            print(f"[poll error +{elapsed}s] {e}")
                    else:
                        print("⏰ Timeout reached without a download URL. Move to next.")
            except requests.RequestException as e:
                print(f"❌ Request failed: {e}")

    print(f"Done. Output dir: {out_dir}")


if __name__ == "__main__":
    main()
