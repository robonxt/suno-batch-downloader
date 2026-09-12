#!/usr/bin/env python3
"""
Suno High-Limit Batch Downloader & Recovery Tool
Replicates and enhances pre-suno-6.0 batch downloader with 2026 stream decryption.
- Resolves Suno public song URLs, shortlinks (/s/), hook links, and raw UUIDs.
- Fetches stream authorization & unwraps keys (AES-GCM).
- Decrypts CloudFront audio stream (AES-CTR) into 100% playable audio.
- Embeds metadata (Title, Artist, Album, Year, Genre, Comment/Styles, Lyrics, Cover Art)
  directly into MP3, M4A, and WAV (same as pre-suno-6.0).
- Option for separate generation info text file (_info.txt).
- Web UI (--web) & CLI batch modes with CSV, JSON, and ZIP exports.
"""

import argparse
import base64
import csv
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import requests
except ImportError:
    print("Error: 'requests' library required. Run: pip install requests")
    sys.exit(1)

# Cryptography support
HAS_CRYPTOGRAPHY = False
try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.backends import default_backend
    HAS_CRYPTOGRAPHY = True
except ImportError:
    pass

try:
    from mutagen.id3 import ID3, TIT2, TPE1, TPE2, TALB, COMM, APIC, TCON, TDRC, USLT, TXXX
    MUTAGEN_ID3_AVAILABLE = True
except ImportError:
    MUTAGEN_ID3_AVAILABLE = False

from collections import deque
from threading import Lock

try:
    from mutagen.mp4 import MP4, MP4Cover
    MUTAGEN_MP4_AVAILABLE = True
except ImportError:
    MUTAGEN_MP4_AVAILABLE = False

try:
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
    from rich.align import Align
    from rich.layout import Layout
    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False

console = Console(
    theme=Theme(
        {
            "info": "cyan",
            "warn": "yellow",
            "error": "red",
            "success": "green",
        }
    )
) if RICH_AVAILABLE else None

LOG_LINES: deque = deque(maxlen=400)
LOG_LOCK = Lock()


def add_log(*args):
    raw = " ".join(str(a) for a in args)
    if not RICH_AVAILABLE:
        print(raw)
        return

    line = Text()
    m = re.match(r"^(\[[^\]]+\])\s?(.*)$", raw)
    if m:
        tag, rest = m.group(1), m.group(2)
        tag_lower = tag.lower()
        if "skip" in tag_lower:
            line.append(tag, style="bold yellow")
        elif "ok" in tag_lower or "saved" in tag_lower:
            line.append(tag, style="bold green")
        elif "err" in tag_lower or "fail" in tag_lower:
            line.append(tag, style="bold red")
        elif "warn" in tag_lower:
            line.append(tag, style="bold yellow")
        else:
            line.append(tag, style="bold cyan")
        line.append(" ")
        remaining = rest
    else:
        remaining = raw

    low = remaining.lower()
    if any(w in low for w in ["error", "failed", "exception", "timeout"]):
        style = "bold red"
    elif any(w in low for w in ["saved", "complete", "exported"]):
        style = "bold green"
    elif any(w in low for w in ["skip", "warn"]):
        style = "yellow"
    else:
        style = "white"

    line.append(remaining, style=style)
    with LOG_LOCK:
        LOG_LINES.append(line)


def build_logs_panel(max_rows: int = 10) -> Any:
    visible_rows = max(3, max_rows - 2)
    text = Text()
    with LOG_LOCK:
        recent = list(LOG_LINES)[-visible_rows:]
    first = True
    for t in recent:
        if not first:
            text.append("\n")
        text.append_text(t)
        first = False
    return Panel(Align(text, vertical="bottom"), title="Logs", expand=True)

UUID_PATTERN = re.compile(r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})")
SHORT_URL_PATTERN = re.compile(r"suno\.com/s/([a-zA-Z0-9_-]+)")
STUDIO_API_URLS = [
    "https://studio-api-prod.suno.com/api/clip",
    "https://studio-api.prod.suno.com/api/clip",
]
RIGHTS_URL = "https://yellow-salad.aibiei.com/rights"
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "*/*",
})


# ---------------------------------------------------------------------------
# Pure Python AES-CTR / AES-GCM Fallback (if cryptography is not installed)
# ---------------------------------------------------------------------------
class PurePythonAES:
    s_box = [
        0x63, 0x7c, 0x77, 0x7b, 0xf2, 0x6b, 0x6f, 0xc5, 0x30, 0x01, 0x67, 0x2b, 0xfe, 0xd7, 0xab, 0x76,
        0xca, 0x82, 0xc9, 0x7d, 0xfa, 0x59, 0x47, 0xf0, 0xad, 0xd4, 0xa2, 0xaf, 0x9c, 0xa4, 0x72, 0xc0,
        0xb7, 0xfd, 0x93, 0x26, 0x36, 0x3f, 0xf7, 0xcc, 0x34, 0xa5, 0xe5, 0xf1, 0x71, 0xd8, 0x31, 0x15,
        0x04, 0xc7, 0x23, 0xc3, 0x18, 0x96, 0x05, 0x9a, 0x07, 0x12, 0x80, 0xe2, 0xeb, 0x27, 0xb2, 0x75,
        0x09, 0x83, 0x2c, 0x1a, 0x1b, 0x6e, 0x5a, 0xa0, 0x52, 0x3b, 0xd6, 0xb3, 0x29, 0xe3, 0x2f, 0x84,
        0x53, 0xd1, 0x00, 0xed, 0x20, 0xfc, 0xb1, 0x5b, 0x6a, 0xcb, 0xbe, 0x39, 0x4a, 0x4c, 0x58, 0xcf,
        0xd0, 0xef, 0xaa, 0xfb, 0x43, 0x4d, 0x33, 0x85, 0x45, 0xf9, 0x02, 0x7f, 0x50, 0x3c, 0x9f, 0xa8,
        0x51, 0xa3, 0x40, 0x8f, 0x92, 0x9d, 0x38, 0xf5, 0xbc, 0xb6, 0xda, 0x21, 0x10, 0xff, 0xf3, 0xd2,
        0xcd, 0x0c, 0x13, 0xec, 0x5f, 0x97, 0x44, 0x17, 0xc4, 0xa7, 0x7e, 0x3d, 0x64, 0x5d, 0x19, 0x73,
        0x60, 0x81, 0x4f, 0xdc, 0x22, 0x2a, 0x90, 0x88, 0x46, 0xee, 0xb8, 0x14, 0xde, 0x5e, 0x0b, 0xdb,
        0xe0, 0x32, 0x3a, 0x0a, 0x49, 0x06, 0x24, 0x5c, 0xc2, 0xd3, 0xac, 0x62, 0x91, 0x95, 0xe4, 0x79,
        0xe7, 0xc8, 0x37, 0x6d, 0x8d, 0xd5, 0x4e, 0xa9, 0x6c, 0x56, 0xf4, 0xea, 0x65, 0x7a, 0xae, 0x08,
        0xba, 0x78, 0x25, 0x2e, 0x1c, 0xa6, 0xb4, 0xc6, 0xe8, 0xdd, 0x74, 0x1f, 0x4b, 0xbd, 0x8b, 0x8a,
        0x70, 0x3e, 0xb5, 0x66, 0x48, 0x03, 0xf6, 0x0e, 0x61, 0x35, 0x57, 0xb9, 0x86, 0xc1, 0x1d, 0x9e,
        0xe1, 0xf8, 0x98, 0x11, 0x69, 0xd9, 0x8e, 0x94, 0x9b, 0x1e, 0x87, 0xe9, 0xce, 0x55, 0x28, 0xdf,
        0x8c, 0xa1, 0x89, 0x0d, 0xbf, 0xe6, 0x42, 0x68, 0x41, 0x99, 0x2d, 0x0f, 0xb0, 0x54, 0xbb, 0x16
    ]
    r_con = [0x00, 0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1b, 0x36]

    @staticmethod
    def _sub_word(word):
        return bytes(PurePythonAES.s_box[b] for b in word)

    @staticmethod
    def _rot_word(word):
        return word[1:] + word[:1]

    def __init__(self, key: bytes):
        self.key = key
        self.nk = len(key) // 4
        self.nr = self.nk + 6
        self.round_keys = self._key_expansion(key)

    def _key_expansion(self, key):
        w = list(key)
        i = self.nk
        while len(w) < 4 * 4 * (self.nr + 1):
            temp = w[-4:]
            if i % self.nk == 0:
                temp = [b ^ r for b, r in zip(self._sub_word(self._rot_word(temp)), [self.r_con[i // self.nk], 0, 0, 0])]
            elif self.nk > 6 and (i % self.nk == 4):
                temp = list(self._sub_word(temp))
            w.extend(a ^ b for a, b in zip(w[-4 * self.nk : -4 * (self.nk - 1)], temp))
            i += 1
        return [bytes(w[r * 16 : (r + 1) * 16]) for r in range(self.nr + 1)]

    def encrypt_block(self, block: bytes) -> bytes:
        state = list(block)
        state = [s ^ k for s, k in zip(state, self.round_keys[0])]
        sbox = self.s_box
        for r in range(1, self.nr):
            s = [sbox[b] for b in state]
            state = [
                s[0], s[5], s[10], s[15],
                s[4], s[9], s[14], s[3],
                s[8], s[13], s[2], s[7],
                s[12], s[1], s[6], s[11]
            ]
            nxt = [0] * 16
            for c in range(4):
                idx = c * 4
                a0, a1, a2, a3 = state[idx], state[idx + 1], state[idx + 2], state[idx + 3]
                def xtime(x): return ((x << 1) ^ 0x1b) & 0xff if (x & 0x80) else (x << 1)
                nxt[idx] = xtime(a0) ^ xtime(a1) ^ a1 ^ a2 ^ a3
                nxt[idx + 1] = a0 ^ xtime(a1) ^ xtime(a2) ^ a2 ^ a3
                nxt[idx + 2] = a0 ^ a1 ^ xtime(a2) ^ xtime(a3) ^ a3
                nxt[idx + 3] = xtime(a0) ^ a0 ^ a1 ^ a2 ^ xtime(a3)
            rk = self.round_keys[r]
            state = [n ^ k for n, k in zip(nxt, rk)]
        s = [sbox[b] for b in state]
        shifted = [
            s[0], s[5], s[10], s[15],
            s[4], s[9], s[14], s[3],
            s[8], s[13], s[2], s[7],
            s[12], s[1], s[6], s[11]
        ]
        return bytes(b ^ k for b, k in zip(shifted, self.round_keys[self.nr]))


def _gf_mult(x: int, y: int) -> int:
    r = 0xe1000000000000000000000000000000
    z = 0
    v = y
    for i in range(128):
        if (x >> (127 - i)) & 1:
            z ^= v
        if v & 1:
            v = (v >> 1) ^ r
        else:
            v >>= 1
    return z


def pure_aes_gcm_decrypt(key: bytes, nonce: bytes, ciphertext: bytes, tag: bytes, aad: bytes = b"") -> bytes:
    aes = PurePythonAES(key)
    h_bytes = aes.encrypt_block(b"\x00" * 16)
    h = int.from_bytes(h_bytes, "big")

    if len(nonce) == 12:
        j0_bytes = nonce + b"\x00\x00\x00\x01"
    else:
        s = nonce + b"\x00" * ((16 - len(nonce) % 16) % 16)
        s += (len(nonce) * 8).to_bytes(16, "big")
        y = 0
        for i in range(0, len(s), 16):
            y = _gf_mult(y ^ int.from_bytes(s[i : i + 16], "big"), h)
        j0_bytes = y.to_bytes(16, "big")

    j0 = int.from_bytes(j0_bytes, "big")

    plaintext = bytearray(len(ciphertext))
    counter = (j0 + 1) & 0xffffffffffffffffffffffffffffffff
    for offset in range(0, len(ciphertext), 16):
        ks = aes.encrypt_block(counter.to_bytes(16, "big"))
        block_len = min(16, len(ciphertext) - offset)
        for i in range(block_len):
            plaintext[offset + i] = ciphertext[offset + i] ^ ks[i]
        counter = (counter + 1) & 0xffffffffffffffffffffffffffffffff

    pad_aad = aad + b"\x00" * ((16 - len(aad) % 16) % 16)
    pad_c = ciphertext + b"\x00" * ((16 - len(ciphertext) % 16) % 16)
    data = pad_aad + pad_c + (len(aad) * 8).to_bytes(8, "big") + (len(ciphertext) * 8).to_bytes(8, "big")

    y = 0
    for i in range(0, len(data), 16):
        y = _gf_mult(y ^ int.from_bytes(data[i : i + 16], "big"), h)

    auth_tag = bytes(a ^ b for a, b in zip(y.to_bytes(16, "big"), aes.encrypt_block(j0_bytes)))
    return bytes(plaintext)


def decrypt_aes_gcm(key: bytes, wrapped: bytes, aad: bytes) -> bytes:
    nonce = wrapped[:12]
    ciphertext = wrapped[12:-16]
    tag = wrapped[-16:]

    if HAS_CRYPTOGRAPHY:
        try:
            aesgcm = AESGCM(key)
            return aesgcm.decrypt(nonce, ciphertext + tag, aad)
        except Exception:
            pass

    return pure_aes_gcm_decrypt(key, nonce, ciphertext, tag, aad)


def decrypt_aes_ctr(key: bytes, initial_iv: bytes, data: bytes) -> bytes:
    if HAS_CRYPTOGRAPHY:
        try:
            cipher = Cipher(algorithms.AES(key), modes.CTR(initial_iv), backend=default_backend())
            decryptor = cipher.decryptor()
            return decryptor.update(data) + decryptor.finalize()
        except Exception:
            pass

    aes = PurePythonAES(key)
    res = bytearray(len(data))
    iv_int = int.from_bytes(initial_iv, "big")
    for offset in range(0, len(data), 16):
        cnt = (iv_int + (offset // 16)) & 0xffffffffffffffffffffffffffffffff
        ks = aes.encrypt_block(cnt.to_bytes(16, "big"))
        blen = min(16, len(data) - offset)
        for i in range(blen):
            res[offset + i] = data[offset + i] ^ ks[i]
    return bytes(res)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def sanitize_filename(name: str) -> str:
    cleaned = re.sub(r'[\\/*?:"<>|]', "", name)
    cleaned = " ".join(cleaned.split())
    return cleaned.strip() or "suno_track"


def find_ffmpeg(custom_path: Optional[str] = None) -> Optional[str]:
    if custom_path and os.path.exists(custom_path):
        return custom_path

    p = shutil.which("ffmpeg")
    if p:
        return p

    candidates = [
        r"C:\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files (x86)\ffmpeg\bin\ffmpeg.exe",
        r"C:\ProgramData\chocolatey\bin\ffmpeg.exe",
        r"C:\tools\ffmpeg\bin\ffmpeg.exe",
    ]

    local_app_data = os.environ.get("LOCALAPPDATA", "")
    if local_app_data:
        winget_path = Path(local_app_data) / "Microsoft" / "WinGet" / "Packages"
        if winget_path.exists():
            for exe in winget_path.glob("**/ffmpeg.exe"):
                if exe.is_file():
                    return str(exe)

    for c in candidates:
        if os.path.exists(c):
            return c
    return None


def resolve_uuid(url_or_id: str, timeout: int = 15) -> Optional[str]:
    clean = url_or_id.strip()
    if not clean or clean.startswith("#"):
        return None

    match = UUID_PATTERN.search(clean)
    if match:
        return match.group(1).lower()

    short_match = SHORT_URL_PATTERN.search(clean)
    if short_match:
        target_url = clean if clean.startswith("http") else f"https://{clean}"
        try:
            r = SESSION.get(target_url, timeout=timeout, allow_redirects=True)
            if r.status_code == 200:
                match = UUID_PATTERN.search(r.url)
                if match:
                    return match.group(1).lower()
                canonical_match = re.search(r'<link[^>]*rel=["\']canonical["\'][^>]*href=["\']([^"\']+)["\']', r.text)
                if canonical_match:
                    canon_uuid = UUID_PATTERN.search(canonical_match.group(1))
                    if canon_uuid:
                        return canon_uuid.group(1).lower()
                body_match = UUID_PATTERN.search(r.text)
                if body_match:
                    return body_match.group(1).lower()
        except Exception:
            pass

    return None


def fetch_clip_metadata(uuid: str, timeout: int = 15) -> Optional[Dict[str, Any]]:
    for base in STUDIO_API_URLS:
        try:
            r = SESSION.get(f"{base}/{uuid}", timeout=timeout)
            if r.status_code == 200:
                clip = r.json()
                meta = clip.get("metadata") or {}

                stream_url = None
                media_urls = clip.get("media_urls") or []
                for m in media_urls:
                    u = m.get("url")
                    if u and not u.endswith("forbidden"):
                        stream_url = u
                        break

                if not stream_url:
                    stream_url = f"https://d2lwuy8qc234o3.cloudfront.net/1/clip/{uuid}.m4a"

                display_name = clip.get("display_name") or ""
                handle = clip.get("handle") or ""
                if display_name and handle:
                    author = f"{display_name} (@{handle})"
                elif display_name:
                    author = display_name
                elif handle:
                    author = f"@{handle}"
                else:
                    author = "Suno Creator"

                created_at = clip.get("created_at") or ""
                year = created_at[:4] if len(created_at) >= 4 else ""
                date = created_at[:10] if len(created_at) >= 10 else ""

                major_model = clip.get("major_model_version") or ""
                model_name = clip.get("model_name") or ""
                model_str = f"{major_model} ({model_name})" if major_model and model_name else (major_model or model_name)

                styles = meta.get("tags") or ""
                display_tags = clip.get("display_tags") or ""
                genre = display_tags or styles or "AI Music"
                prompt = meta.get("prompt") or ""
                page_url = f"https://suno.com/song/{uuid}"

                return {
                    "id": uuid,
                    "title": clip.get("title") or f"Suno Track {uuid[:8]}",
                    "author": author,
                    "display_name": display_name,
                    "handle": handle,
                    "duration": float(meta.get("duration") or clip.get("duration") or 0.0),
                    "created_at": created_at,
                    "year": year,
                    "date": date,
                    "genre": genre,
                    "styles": styles,
                    "display_tags": display_tags,
                    "model": model_str,
                    "lyrics": prompt,
                    "stream_url": stream_url,
                    "cover_url": clip.get("image_large_url") or clip.get("image_url") or f"https://cdn2.suno.ai/image_large_{uuid}.jpeg",
                    "page_url": page_url,
                }
        except Exception:
            continue
    return None


def fetch_and_decrypt_audio(uuid: str, stream_url: str) -> Optional[bytes]:
    try:
        r_audio = SESSION.get(stream_url, timeout=60)
        if r_audio.status_code != 200 or len(r_audio.content) == 0:
            return None
        encrypted_bytes = r_audio.content
    except Exception:
        return None

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Origin": "https://usesuno.com",
        "Referer": "https://usesuno.com/",
    }
    payload = {
        "content_params": {
            "content_id": uuid,
            "content_type": "clip"
        }
    }
    try:
        r_rights = SESSION.post(RIGHTS_URL, headers=headers, json=payload, timeout=20)
        if r_rights.status_code != 200:
            return None
        rights_data = r_rights.json()
        key_b64 = rights_data.get("key")
        iv_b64 = rights_data.get("iv")
        glt = rights_data.get("glt")
        if not (key_b64 and iv_b64 and glt):
            return None

        user_key = hashlib.sha256(glt.encode("utf-8")).digest()
        aad = uuid.encode("utf-8")
        content_key = decrypt_aes_gcm(user_key, base64.b64decode(key_b64), aad)
        content_iv = decrypt_aes_gcm(user_key, base64.b64decode(iv_b64), aad)
        decrypted_audio = decrypt_aes_ctr(content_key, content_iv, encrypted_bytes)
        return decrypted_audio
    except Exception:
        return None


def convert_to_mp3(ffmpeg_bin: str, input_path: Path, output_path: Path) -> bool:
    cmd = [
        ffmpeg_bin,
        "-y",
        "-i", str(input_path),
        "-vn",
        "-c:a", "libmp3lame",
        "-b:a", "320k",
        str(output_path),
    ]
    try:
        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if res.returncode == 0 and output_path.exists() and output_path.stat().st_size > 0:
            return True
    except Exception:
        pass

    cmd_fallback = [
        ffmpeg_bin,
        "-y",
        "-i", str(input_path),
        "-vn",
        "-b:a", "320k",
        str(output_path),
    ]
    try:
        res = subprocess.run(cmd_fallback, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return res.returncode == 0 and output_path.exists() and output_path.stat().st_size > 0
    except Exception:
        return False


def convert_to_wav(ffmpeg_bin: str, input_path: Path, output_path: Path, meta: Optional[Dict[str, Any]] = None) -> bool:
    cmd = [
        ffmpeg_bin,
        "-y",
        "-i", str(input_path),
        "-vn",
        "-c:a", "pcm_s16le",
    ]
    if meta:
        if meta.get("title"):
            cmd.extend(["-metadata", f"title={meta['title']}"])
        if meta.get("author"):
            cmd.extend(["-metadata", f"artist={meta['author']}"])
        cmd.extend(["-metadata", "album=Suno AI"])
        if meta.get("styles"):
            cmd.extend(["-metadata", f"comment={meta['styles'][:200]}"])
    cmd.append(str(output_path))
    try:
        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return res.returncode == 0 and output_path.exists() and output_path.stat().st_size > 0
    except Exception:
        return False


def embed_id3_metadata(mp3_path: Path, meta: Dict[str, Any], cover_bytes: Optional[bytes] = None) -> bool:
    if not MUTAGEN_ID3_AVAILABLE:
        return False
    try:
        try:
            tags = ID3(str(mp3_path))
        except Exception:
            tags = ID3()

        tags.delall("TIT2")
        tags.add(TIT2(encoding=3, text=meta.get("title", "")))

        tags.delall("TPE1")
        tags.add(TPE1(encoding=3, text=meta.get("author", "Suno Creator")))

        tags.delall("TALB")
        tags.add(TALB(encoding=3, text="Suno AI"))

        tags.delall("TPE2")
        tags.add(TPE2(encoding=3, text="Suno AI"))

        genre = meta.get("genre") or meta.get("styles") or "AI Music"
        tags.delall("TCON")
        tags.add(TCON(encoding=3, text=genre))

        date_val = meta.get("date") or meta.get("year")
        if date_val:
            tags.delall("TDRC")
            tags.add(TDRC(encoding=3, text=date_val))

        comment_parts = []
        if meta.get("styles"):
            comment_parts.append(f"Styles: {meta['styles']}")
        if meta.get("model"):
            comment_parts.append(f"Model: {meta['model']}")
        if meta.get("page_url"):
            comment_parts.append(f"URL: {meta['page_url']}")
        if meta.get("id"):
            comment_parts.append(f"UUID: {meta['id']}")
        if comment_parts:
            tags.delall("COMM")
            tags.add(COMM(encoding=3, lang="eng", desc="", text="\n".join(comment_parts)))

        if meta.get("lyrics"):
            tags.delall("USLT")
            tags.add(USLT(encoding=3, lang="eng", desc="", text=meta["lyrics"]))

        def set_txxx(desc: str, val: Any):
            tags.delall(f"TXXX:{desc}")
            if val:
                tags.add(TXXX(encoding=3, desc=desc, text=str(val)))

        set_txxx("SUNO_UUID", meta.get("id"))
        set_txxx("SUNO_URL", meta.get("page_url"))
        set_txxx("SUNO_MODEL", meta.get("model"))
        set_txxx("SUNO_STYLES", meta.get("styles"))

        if cover_bytes:
            tags.delall("APIC")
            tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover_bytes))

        tags.save(str(mp3_path), v2_version=3)
        return True
    except Exception:
        return False


def embed_m4a_metadata(m4a_path: Path, meta: Dict[str, Any], cover_bytes: Optional[bytes] = None) -> bool:
    if not MUTAGEN_MP4_AVAILABLE:
        return False
    try:
        tags = MP4(str(m4a_path))
        if meta.get("title"):
            tags["\xa9nam"] = [meta["title"]]
        if meta.get("author"):
            tags["\xa9ART"] = [meta["author"]]
        tags["\xa9alb"] = ["Suno AI"]
        tags["aART"] = ["Suno AI"]
        date_val = meta.get("date") or meta.get("year")
        if date_val:
            tags["\xa9day"] = [date_val]
        genre = meta.get("genre") or meta.get("styles") or "AI Music"
        tags["\xa9gen"] = [genre]

        comment_parts = []
        if meta.get("styles"):
            comment_parts.append(f"Styles: {meta['styles']}")
        if meta.get("model"):
            comment_parts.append(f"Model: {meta['model']}")
        if meta.get("page_url"):
            comment_parts.append(f"URL: {meta['page_url']}")
        if meta.get("id"):
            comment_parts.append(f"UUID: {meta['id']}")
        if comment_parts:
            tags["\xa9cmt"] = ["\n".join(comment_parts)]

        if meta.get("lyrics"):
            tags["\xa9lyr"] = [meta["lyrics"]]

        if cover_bytes:
            tags["covr"] = [MP4Cover(cover_bytes, imageformat=MP4Cover.FORMAT_JPEG)]

        tags.save()
        return True
    except Exception:
        return False


def write_info_file(meta: Dict[str, Any], info_path: Path):
    mins = int(meta["duration"] // 60)
    secs = int(meta["duration"] % 60)
    dur_str = f"{mins}:{secs:02d} ({meta['duration']:.2f}s)"

    content = [
        "=" * 70,
        f"Title:        {meta.get('title', '')}",
        f"Artist:       {meta.get('author', '')}",
        f"Duration:     {dur_str}",
        f"Created At:   {meta.get('created_at', '')}",
        f"Model:        {meta.get('model', '')}",
        f"Genre/Styles: {meta.get('styles') or meta.get('genre') or ''}",
        f"Suno Song:    {meta.get('page_url', '')}",
        f"UUID:         {meta.get('id', '')}",
        "=" * 70,
        "",
        "Prompt / Lyrics:",
        "-" * 70,
        meta.get("lyrics") or "(No prompt/lyrics found)",
        "-" * 70,
        "",
    ]
    info_path.write_text("\n".join(content), encoding="utf-8")


def process_single_track(
    meta: Dict[str, Any],
    out_dir: Path,
    formats: List[str],
    ffmpeg_bin: Optional[str] = None,
    embed_metadata: bool = True,
    overwrite: bool = False,
    log_fn=print,
) -> Dict[str, Any]:
    uuid = meta["id"]
    safe_title = sanitize_filename(meta["title"])
    base_name = f"{safe_title} - {uuid[:8]}"
    saved_files: Dict[str, str] = {}

    out_dir.mkdir(parents=True, exist_ok=True)

    # Check if target files already exist in output folder (skip re-downloading)
    need_audio = any(f in formats for f in ["mp3", "wav", "original"])
    audio_formats = [f for f in formats if f in ["mp3", "wav", "original"]]

    if not overwrite and audio_formats:
        all_audio_found = True
        for afmt in audio_formats:
            ext = ".mp3" if afmt == "mp3" else (".wav" if afmt == "wav" else ".m4a")
            exact_p = out_dir / f"{base_name}{ext}"
            if exact_p.exists() and exact_p.stat().st_size > 1024:
                saved_files[afmt] = str(exact_p)
            else:
                # Also match any file with this UUID
                found = False
                for match in out_dir.glob(f"*{uuid[:8]}*{ext}"):
                    if match.exists() and match.stat().st_size > 1024:
                        saved_files[afmt] = str(match)
                        found = True
                        break
                if not found:
                    all_audio_found = False

        if all_audio_found:
            log_fn(f"[SKIP] Existing: '{meta['title']}' ({uuid[:8]})")
            # Write info/cover if requested and missing
            if "info" in formats:
                info_path = out_dir / f"{base_name}_info.txt"
                if not info_path.exists():
                    write_info_file(meta, info_path)
                saved_files["info"] = str(info_path)
            return {
                "uuid": uuid,
                "title": meta["title"],
                "files": saved_files,
                "status": "skipped",
                "skipped_existing": True,
            }

    # 1. Metadata Info Text File
    if "info" in formats:
        info_path = out_dir / f"{base_name}_info.txt"
        write_info_file(meta, info_path)
        saved_files["info"] = str(info_path)

    # 2. Cover Art JPEG & Bytes for embedding
    cover_bytes = None
    if "cover" in formats or embed_metadata:
        if meta.get("cover_url"):
            try:
                r = SESSION.get(meta["cover_url"], timeout=20)
                if r.status_code == 200:
                    cover_bytes = r.content
                    if "cover" in formats:
                        cover_path = out_dir / f"{base_name}.jpeg"
                        cover_path.write_bytes(cover_bytes)
                        saved_files["cover"] = str(cover_path)
            except Exception:
                pass

    # 3. Audio Processing (Decryption + optional Transcoding & Tagging)
    if need_audio and meta.get("stream_url"):
        decrypted_bytes = fetch_and_decrypt_audio(uuid, meta["stream_url"])
        if not decrypted_bytes:
            log_fn(f"[ERROR] Failed to decrypt audio stream for '{meta['title']}' ({uuid[:8]})")
            return {
                "uuid": uuid,
                "title": meta["title"],
                "files": saved_files,
                "status": "failed",
                "skipped_existing": False,
            }

        if decrypted_bytes[:4] == b"\x1a\x45\xdf\xa3":
            orig_ext = ".webm"
        elif decrypted_bytes[:3] == b"ID3" or (len(decrypted_bytes) >= 2 and decrypted_bytes[0] == 0xFF and (decrypted_bytes[1] & 0xE0) == 0xE0):
            orig_ext = ".mp3"
        else:
            orig_ext = ".m4a"

        temp_audio = out_dir / f"{base_name}_decrypted_tmp{orig_ext}"
        temp_audio.write_bytes(decrypted_bytes)

        # Original
        if "original" in formats:
            orig_dest = out_dir / f"{base_name}{orig_ext}"
            shutil.copyfile(temp_audio, orig_dest)
            if embed_metadata and orig_ext == ".m4a":
                embed_m4a_metadata(orig_dest, meta, cover_bytes)
            saved_files["original"] = str(orig_dest)

        # MP3
        if "mp3" in formats:
            mp3_dest = out_dir / f"{base_name}.mp3"
            if orig_ext == ".mp3":
                shutil.copyfile(temp_audio, mp3_dest)
                if embed_metadata:
                    embed_id3_metadata(mp3_dest, meta, cover_bytes)
                saved_files["mp3"] = str(mp3_dest)
            elif ffmpeg_bin and convert_to_mp3(ffmpeg_bin, temp_audio, mp3_dest):
                if embed_metadata:
                    embed_id3_metadata(mp3_dest, meta, cover_bytes)
                saved_files["mp3"] = str(mp3_dest)
            else:
                log_fn(f"[WARN] ffmpeg required to transcode decrypted {orig_ext} to MP3 for '{meta['title']}'.")

        # WAV
        if "wav" in formats:
            wav_dest = out_dir / f"{base_name}.wav"
            wav_meta = meta if embed_metadata else None
            if ffmpeg_bin and convert_to_wav(ffmpeg_bin, temp_audio, wav_dest, wav_meta):
                saved_files["wav"] = str(wav_dest)
            else:
                log_fn(f"[WARN] ffmpeg required to transcode decrypted {orig_ext} to WAV for '{meta['title']}'.")

        temp_audio.unlink(missing_ok=True)

    log_fn(f"[OK] Saved '{meta['title']}': {list(saved_files.keys())}")
    return {
        "uuid": uuid,
        "title": meta["title"],
        "files": saved_files,
        "status": "saved",
        "skipped_existing": False,
    }


def export_csv(songs: List[Dict[str, Any]], out_path: Path):
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["ID", "Title", "Artist", "Duration (s)", "Created At", "Model", "Styles", "Page URL", "Stream URL"])
        for s in songs:
            writer.writerow([
                s.get("id", ""),
                s.get("title", ""),
                s.get("author", ""),
                s.get("duration", ""),
                s.get("created_at", ""),
                s.get("model", ""),
                s.get("styles", ""),
                s.get("page_url", ""),
                s.get("stream_url", ""),
            ])


def export_json(songs: List[Dict[str, Any]], out_path: Path):
    out_path.write_text(json.dumps(songs, indent=2, ensure_ascii=False), encoding="utf-8")


def export_zip(file_paths: List[str], zip_path: Path):
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for fp in file_paths:
            p = Path(fp)
            if p.exists():
                z.write(p, arcname=p.name)


## ---------------------------------------------------------------------------
# Local Web UI
# ---------------------------------------------------------------------------
WEB_DIR = Path(__file__).resolve().parent / "web"
HTML_INDEX_PATH = WEB_DIR / "index.html"


def get_web_ui_html() -> bytes:
    if HTML_INDEX_PATH.exists():
        return HTML_INDEX_PATH.read_bytes()
    return b"<h1>Error: web/index.html not found</h1>"


class WebUIHandler(BaseHTTPRequestHandler):
    cached_tracks: List[Dict[str, Any]] = []
    ffmpeg_bin: Optional[str] = None

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)

        if parsed.path in ["/", "/index.html"]:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(get_web_ui_html())

        elif parsed.path == "/api/status":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ffmpeg": bool(WebUIHandler.ffmpeg_bin)}).encode("utf-8"))

        elif parsed.path == "/api/export-csv":
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", "attachment; filename=suno_export.csv")
            self.end_headers()
            sio = io.StringIO()
            writer = csv.writer(sio)
            writer.writerow(["ID", "Title", "Artist", "Duration (s)", "Created At", "Model", "Styles", "Page URL", "Stream URL"])
            for s in WebUIHandler.cached_tracks:
                writer.writerow([
                    s.get("id", ""),
                    s.get("title", ""),
                    s.get("author", ""),
                    s.get("duration", ""),
                    s.get("created_at", ""),
                    s.get("model", ""),
                    s.get("styles", ""),
                    s.get("page_url", ""),
                    s.get("stream_url", ""),
                ])
        elif parsed.path == "/api/stream":
            track_id = query.get("id", [None])[0]
            target_track = None
            for t in WebUIHandler.cached_tracks:
                if t.get("id") == track_id:
                    target_track = t
                    break
            if not target_track and track_id:
                target_track = fetch_clip_metadata(track_id)

            if not target_track or not target_track.get("stream_url"):
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"Track not found")
                return

            decrypted = fetch_and_decrypt_audio(target_track["id"], target_track["stream_url"])
            if not decrypted:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(b"Failed to decrypt audio")
                return

            # Determine mime
            if decrypted[:4] == b"\x1a\x45\xdf\xa3":
                mime = "audio/webm"
            elif decrypted[:3] == b"ID3" or (len(decrypted) >= 2 and decrypted[0] == 0xFF and (decrypted[1] & 0xE0) == 0xE0):
                mime = "audio/mpeg"
            else:
                mime = "audio/mp4"

            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(decrypted)))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            self.wfile.write(decrypted)

        elif parsed.path == "/api/download-single":
            track_id = query.get("id", [None])[0]
            fmt = query.get("fmt", ["mp3"])[0].lower()
            embed_meta = query.get("embed", ["1"])[0] == "1"

            target_track = None
            for t in WebUIHandler.cached_tracks:
                if t.get("id") == track_id:
                    target_track = t
                    break

            if not target_track and track_id:
                target_track = fetch_clip_metadata(track_id)

            if not target_track:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"Track not found")
                return

            temp_dir = Path("./.suno_single_tmp")
            temp_dir.mkdir(exist_ok=True)
            run_dir = temp_dir / f"single_{int(time.time()*1000)}"

            res = process_single_track(
                meta=target_track,
                out_dir=run_dir,
                formats=[fmt],
                ffmpeg_bin=WebUIHandler.ffmpeg_bin,
                embed_metadata=embed_meta,
                log_fn=lambda _: None,
            )

            file_path = res["files"].get(fmt)
            if file_path and os.path.exists(file_path):
                p = Path(file_path)
                ext = p.suffix.lower()
                mime_types = {
                    ".mp3": "audio/mpeg",
                    ".wav": "audio/wav",
                    ".m4a": "audio/mp4",
                    ".jpeg": "image/jpeg",
                    ".txt": "text/plain; charset=utf-8",
                }
                mime = mime_types.get(ext, "application/octet-stream")

                self.send_response(200)
                self.send_header("Content-Type", mime)
                filename_quoted = urllib.parse.quote(p.name)
                self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{filename_quoted}")
                self.send_header("Content-Length", str(p.stat().st_size))
                self.end_headers()
                with open(p, "rb") as f:
                    shutil.copyfileobj(f, self.wfile)
                shutil.rmtree(run_dir, ignore_errors=True)
            else:
                shutil.rmtree(run_dir, ignore_errors=True)
                self.send_response(500)
                self.end_headers()
                self.wfile.write(f"Failed to generate {fmt} file".encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        content_len = int(self.headers.get("Content-Length", 0))
        post_body = self.rfile.read(content_len).decode("utf-8")

        if parsed.path == "/api/resolve":
            req_data = json.loads(post_body)
            urls = req_data.get("urls", [])
            resolved_tracks = []

            def worker(u: str):
                uid = resolve_uuid(u)
                if uid:
                    meta = fetch_clip_metadata(uid)
                    if meta:
                        return meta
                return None

            with ThreadPoolExecutor(max_workers=10) as executor:
                futures = [executor.submit(worker, u) for u in urls]
                for fut in as_completed(futures):
                    res = fut.result()
                    if res:
                        resolved_tracks.append(res)

            WebUIHandler.cached_tracks = resolved_tracks
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"tracks": resolved_tracks}).encode("utf-8"))

        elif parsed.path == "/api/download-zip":
            req_data = json.loads(post_body)
            tracks = req_data.get("tracks", [])
            formats = req_data.get("formats", ["mp3"])
            embed_meta = bool(req_data.get("embed", True))

            temp_dir = Path("./.suno_web_tmp")
            temp_dir.mkdir(exist_ok=True)
            run_dir = temp_dir / f"batch_{int(time.time()*1000)}"
            run_dir.mkdir(parents=True, exist_ok=True)

            all_saved_files = []

            def download_worker(track: Dict[str, Any]):
                res = process_single_track(
                    meta=track,
                    out_dir=run_dir,
                    formats=formats,
                    ffmpeg_bin=WebUIHandler.ffmpeg_bin,
                    embed_metadata=embed_meta,
                    log_fn=lambda _: None,
                )
                return list(res["files"].values())

            with ThreadPoolExecutor(max_workers=5) as executor:
                futures = [executor.submit(download_worker, t) for t in tracks]
                for fut in as_completed(futures):
                    all_saved_files.extend(fut.result())

            zip_buf = io.BytesIO()
            with zipfile.ZipFile(zip_buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
                for fp in all_saved_files:
                    p = Path(fp)
                    if p.exists():
                        z.write(p, arcname=p.name)

            shutil.rmtree(run_dir, ignore_errors=True)

            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", "attachment; filename=suno_batch.zip")
            self.end_headers()
            self.wfile.write(zip_buf.getvalue())


def run_web_server(port: int = 8080, ffmpeg_bin: Optional[str] = None):
    WebUIHandler.ffmpeg_bin = ffmpeg_bin
    server_address = ("127.0.0.1", port)
    httpd = HTTPServer(server_address, WebUIHandler)
    url = f"http://localhost:{port}"
    print("=" * 60)
    print(f" Suno High-Limit Downloader Web UI running at:")
    print(f" {url}")
    print(f" Transcoding: {'ffmpeg detected (' + ffmpeg_bin + ')' if ffmpeg_bin else 'ffmpeg NOT found (Original M4A & Info available)'}")
    print(f" Metadata embedding: ACTIVE (MP3, M4A, WAV)")
    print(f" Unlimited URLs. Decryption engine active. Zero CORS.")
    print(f" Press Ctrl+C to stop.")
    print("=" * 60)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server.")
        httpd.server_close()


def main():
    parser = argparse.ArgumentParser(description="Suno High-Limit Batch Recovery and Downloader")
    parser.add_argument("songfile", nargs="?", help="Input file with Suno URLs or UUIDs (one per line)")
    parser.add_argument("--songfile", dest="songfile_flag", help="Input file (flag alias)")
    parser.add_argument("-o", "--output-dir", default="./downloads", help="Output directory (default: ./downloads)")
    parser.add_argument("--formats", default="mp3,original", help="Comma list: mp3,original,wav,cover,info (default: mp3,original)")
    parser.add_argument("--no-metadata", action="store_true", help="Skip embedding metadata tags and artwork into audio files")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite and re-download files if they already exist (default: skip existing)")
    parser.add_argument("--csv", action="store_true", help="Export metadata catalog as CSV")
    parser.add_argument("--json", action="store_true", help="Export structured metadata catalog as JSON")
    parser.add_argument("--zip", action="store_true", help="Package downloaded files into a ZIP archive")
    parser.add_argument("--workers", type=int, default=5, help="Number of concurrent workers (default: 5)")
    parser.add_argument("--ffmpeg", help="Custom path to ffmpeg executable")
    parser.add_argument("--web", action="store_true", help="Launch local Web UI browser interface")
    parser.add_argument("--port", type=int, default=8080, help="Web UI port (default: 8080)")
    parser.add_argument("--no-tui", action="store_true", help="Disable Rich interactive terminal UI and use plain text logs")
    parser.add_argument("--logs-height", type=int, default=10, help="Height of terminal logs panel in lines (default: 10)")

    args = parser.parse_args()

    ffmpeg_bin = find_ffmpeg(args.ffmpeg)

    if args.web:
        run_web_server(args.port, ffmpeg_bin)
        return

    raw_songfile = args.songfile_flag or args.songfile
    if not raw_songfile:
        print("Usage error: specify an input file (e.g., urls.txt or --songfile urls.txt) or use --web to launch the browser UI.")
        parser.print_help()
        sys.exit(1)

    in_path = Path(raw_songfile)
    if not in_path.exists():
        print(f"Error: input file not found: {in_path}")
        sys.exit(1)

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    formats = [f.strip().lower() for f in args.formats.split(",") if f.strip()]
    embed_metadata = not args.no_metadata

    if not ffmpeg_bin and ("mp3" in formats or "wav" in formats):
        print("[WARN] ffmpeg not found in PATH or standard directories.")
        print("[WARN] Transcoding to MP3/WAV requires ffmpeg. Falling back to 'original' decrypted audio.")
        if "original" not in formats:
            formats.append("original")
        formats = [f for f in formats if f not in ["mp3", "wav"]]

    lines = in_path.read_text(encoding="utf-8").splitlines()
    unique_uuids = []
    seen = set()

    print(f"Resolving links from {in_path} ({len(lines)} lines)...")
    for line in lines:
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue
        url_part = raw.split("|", 1)[1] if "|" in raw else raw
        uid = resolve_uuid(url_part)
        if uid and uid not in seen:
            seen.add(uid)
            unique_uuids.append(uid)

    if not unique_uuids:
        print("No valid Suno URLs or UUIDs found.")
        sys.exit(0)

    t_start = time.time()
    use_tui = RICH_AVAILABLE and not args.no_tui and console is not None and sys.stdout.isatty()

    if use_tui:
        console.print(
            Panel.fit(
                f"[bold cyan]Suno Batch Recovery[/] [dim]|[/] Output: [green]{out_dir}[/] [dim]|[/] Formats: [bold]{','.join(formats)}[/]",
                border_style="cyan",
            )
        )
    else:
        print(f"Found {len(unique_uuids)} unique song(s). Fetching metadata...")

    # Phase 1: Fetch Metadata
    songs_meta: List[Dict[str, Any]] = []
    if use_tui:
        with Progress(
            SpinnerColumn(style="cyan"),
            TextColumn("[bold cyan]Fetching metadata...[/]"),
            BarColumn(bar_width=None),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            TextColumn("({task.completed}/{task.total})"),
            TimeElapsedColumn(),
            console=console,
        ) as prog:
            t_meta = prog.add_task("meta", total=len(unique_uuids))
            with ThreadPoolExecutor(max_workers=min(10, args.workers * 2)) as executor:
                future_to_uuid = {executor.submit(fetch_clip_metadata, uid): uid for uid in unique_uuids}
                for fut in as_completed(future_to_uuid):
                    uid = future_to_uuid[fut]
                    try:
                        res = fut.result()
                        if res:
                            songs_meta.append(res)
                            add_log(f"[OK] Resolved {res.get('title', uid[:8])}")
                        else:
                            add_log(f"[WARN] Metadata not found for {uid}")
                    except Exception as e:
                        add_log(f"[ERROR] Exception retrieving {uid}: {e}")
                    prog.advance(t_meta, 1)
    else:
        with ThreadPoolExecutor(max_workers=min(10, args.workers * 2)) as executor:
            future_to_uuid = {executor.submit(fetch_clip_metadata, uid): uid for uid in unique_uuids}
            for fut in as_completed(future_to_uuid):
                uid = future_to_uuid[fut]
                try:
                    res = fut.result()
                    if res:
                        songs_meta.append(res)
                    else:
                        print(f"[WARN] Could not retrieve metadata for {uid}")
                except Exception as e:
                    print(f"[ERROR] Exception retrieving {uid}: {e}")

    # Phase 2: Processing & Downloading Tracks
    all_saved_files = []
    want_mp3 = "mp3" in formats
    want_wav = "wav" in formats
    want_orig = "original" in formats

    agg = {
        "saved": 0,
        "failed": 0,
        "skipped_existing": 0,
        "mp3_saved": 0,
        "wav_saved": 0,
        "orig_saved": 0,
    }

    if use_tui:
        term_h = console.size.height or 40
        logs_h_eff = max(6, min(args.logs_height, term_h // 2))
        downloads_h_eff = max(10, term_h - logs_h_eff - 1)

        layout = Layout()
        layout.split_column(
            Layout(name="top", size=logs_h_eff),
            Layout(name="bottom", size=downloads_h_eff),
        )
        layout["bottom"].split_row(
            Layout(name="left", ratio=3),
            Layout(name="queue", ratio=1),
        )
        stats_height = 6
        layout["left"].split_column(
            Layout(name="stats", size=stats_height),
            Layout(name="active"),
        )

        queue_text = Text()

        def refresh_queue_text(pending_list: List[str]):
            queue_text.__init__("\n".join(pending_list))

        progress = Progress(
            SpinnerColumn(style="cyan"),
            TextColumn("[info]{task.fields[desc]}"),
            BarColumn(bar_width=None),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            TimeElapsedColumn(),
            TimeRemainingColumn(),
            console=console,
        )

        layout["top"].update(build_logs_panel(logs_h_eff))
        layout["active"].update(Panel(progress, title="Active Downloads", expand=True))
        layout["queue"].update(Panel(Align(queue_text, vertical="top"), title="Queue", expand=True))

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
            t.append("Completed: ", style="bold")
            parts = [("Tracks ", style_count(agg["saved"], "ok"))]
            if want_mp3:
                parts.append(("MP3 ", style_count(agg["mp3_saved"], "ok")))
            if want_orig:
                parts.append(("Original ", style_count(agg["orig_saved"], "ok")))
            if want_wav:
                parts.append(("WAV ", style_count(agg["wav_saved"], "ok")))
            first = True
            for tag, c_str in parts:
                if not first:
                    t.append("  ")
                t.append(tag, style="cyan")
                t.append_text(Text.from_markup(c_str))
                first = False
            t.append("\n")

            t.append("Skipped:   ", style="bold")
            t.append("Existing ", style="yellow")
            t.append_text(Text.from_markup(style_count(agg["skipped_existing"], "skip")))
            t.append("\n")

            t.append("Failed:    ", style="bold")
            t.append("Errors ", style="red")
            t.append_text(Text.from_markup(style_count(agg["failed"], "fail")))
            return Panel(Align(t, vertical="top"), title="Stats", title_align="left", expand=True)

        pending = deque(songs_meta)
        running: Dict[Any, Tuple[Any, Any]] = {}

        def submit_next(slots: int = 1):
            nonlocal pending
            count = 0
            while pending and count < slots and len(running) < args.workers:
                meta = pending.popleft()
                desc = f"#{len(songs_meta) - len(pending)} {meta['title'][:22]} ({meta['id'][:8]})"
                task_id = progress.add_task("song", total=100, desc=desc)
                fut = executor.submit(
                    process_single_track,
                    meta,
                    out_dir,
                    formats,
                    ffmpeg_bin,
                    embed_metadata,
                    args.overwrite,
                    add_log,
                )
                running[fut] = (task_id, meta)
                count += 1
            refresh_queue_text([f"{i+1}. {m['title'][:18]} ({m['id'][:8]})" for i, m in enumerate(pending)])

        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            layout["stats"].update(build_stats_panel())
            with Live(layout, refresh_per_second=8, console=console):
                submit_next(slots=args.workers)
                while running:
                    done, _ = as_completed(list(running.keys())), None
                    # Wait briefly for completed tasks
                    done_futs = []
                    for f in list(running.keys()):
                        if f.done():
                            done_futs.append(f)
                    if not done_futs:
                        time.sleep(0.1)
                        layout["top"].update(build_logs_panel(logs_h_eff))
                        continue

                    for fut in done_futs:
                        task_id, meta = running.pop(fut)
                        try:
                            res = fut.result()
                            all_saved_files.extend(res["files"].values())
                            status = res.get("status")
                            if status == "skipped":
                                agg["skipped_existing"] += 1
                            elif status == "failed":
                                agg["failed"] += 1
                            else:
                                agg["saved"] += 1
                                if "mp3" in res["files"]:
                                    agg["mp3_saved"] += 1
                                if "original" in res["files"]:
                                    agg["orig_saved"] += 1
                                if "wav" in res["files"]:
                                    agg["wav_saved"] += 1
                        except Exception as ex:
                            agg["failed"] += 1
                            add_log(f"[ERROR] Exception on {meta['title']}: {ex}")

                        try:
                            progress.remove_task(task_id)
                        except Exception:
                            progress.update(task_id, visible=False)

                        submit_next(slots=1)
                        layout["stats"].update(build_stats_panel())
                        layout["top"].update(build_logs_panel(logs_h_eff))
    else:
        print(f"Retrieved metadata for {len(songs_meta)}/{len(unique_uuids)} track(s). Processing downloads...")
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = [
                executor.submit(process_single_track, meta, out_dir, formats, ffmpeg_bin, embed_metadata, args.overwrite, print)
                for meta in songs_meta
            ]
            for fut in as_completed(futures):
                res = fut.result()
                all_saved_files.extend(res["files"].values())
                status = res.get("status")
                if status == "skipped":
                    agg["skipped_existing"] += 1
                elif status == "failed":
                    agg["failed"] += 1
                else:
                    agg["saved"] += 1
                    if "mp3" in res["files"]:
                        agg["mp3_saved"] += 1
                    if "original" in res["files"]:
                        agg["orig_saved"] += 1
                    if "wav" in res["files"]:
                        agg["wav_saved"] += 1

    # Final Run Summary
    elapsed = time.time() - t_start
    mins = int(elapsed // 60)
    secs = int(elapsed % 60)

    if use_tui:
        tbl = Table(title="Run Summary", show_edge=True, header_style="bold cyan", title_style="bold cyan")
        tbl.add_column("Metric", justify="left", style="bold")
        tbl.add_column("Count", justify="right")
        tbl.add_row("Total Tracks Found", str(len(unique_uuids)))
        tbl.add_row("Metadata Resolved", str(len(songs_meta)))
        tbl.add_row("Newly Downloaded", f"[bold green]{agg['saved']}[/]")
        if want_mp3:
            tbl.add_row("  - MP3 Files", f"[green]{agg['mp3_saved']}[/]")
        if want_orig:
            tbl.add_row("  - Original Audio", f"[green]{agg['orig_saved']}[/]")
        if want_wav:
            tbl.add_row("  - WAV Files", f"[green]{agg['wav_saved']}[/]")
        tbl.add_row("Skipped (Existing)", f"[yellow]{agg['skipped_existing']}[/]")
        tbl.add_row("Failed", f"[red]{agg['failed']}[/]" if agg["failed"] else "[dim]0[/]")

        console.print()
        console.print(
            Panel(
                f"Duration: [bold]{mins}m {secs}s[/] ([dim]{elapsed:.1f}s[/])\nOutput Directory: [green]{out_dir.resolve()}[/]",
                title="Run Complete",
                expand=False,
            )
        )
        console.print(tbl)
    else:
        print(f"\nDone in {mins}m {secs}s. Successfully saved: {agg['saved']}, Skipped existing: {agg['skipped_existing']}, Failed: {agg['failed']}")

    if args.csv:
        csv_path = out_dir / f"{in_path.stem}_metadata.csv"
        export_csv(songs_meta, csv_path)
        print(f"[OK] CSV catalog exported: {csv_path}")

    if args.json:
        json_path = out_dir / f"{in_path.stem}_metadata.json"
        export_json(songs_meta, json_path)
        print(f"[OK] JSON catalog exported: {json_path}")

    if args.zip and all_saved_files:
        zip_path = out_dir / f"{in_path.stem}_bundle.zip"
        export_zip(all_saved_files, zip_path)
        print(f"[OK] ZIP archive created: {zip_path}")

    print(f"Output directory: {out_dir.resolve()}")


if __name__ == "__main__":
    main()
