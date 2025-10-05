import argparse
import os
import random
import re
import sys
import time

import requests
from colorama import Fore
from colorama import init
from mutagen.id3 import ID3, APIC, error, TIT2, TPE1, TCON, TPUB, WOAR
from mutagen.mp3 import MP3

# Initialize colorama
init(autoreset=True)

FILENAME_BAD_CHARS = r'[<>:"/\\|?*\x00-\x1F]'


def sanitize_filename(name, maxlen=200):
    safe = re.sub(FILENAME_BAD_CHARS, "_", name)
    # avoid trailing dots/spaces (Windows)
    safe = safe.strip(" .")
    return (safe[:maxlen]) if len(safe) > maxlen else safe


def ensure_unique_filename(directory, filename):
    """
    Returns a filename that does not currently exist in `directory`.
    If `filename` exists, appends ' (2)', ' (3)', ... before the extension.
    Also normalizes titles that already end with ' (n)' to avoid duplication.
    """
    name, ext = os.path.splitext(filename)

    # Normalize existing counters like "Title (2)" -> base "Title"
    m = re.search(r"\s*\((\d+)\)$", name)
    if m:
        name = re.sub(r"\s*\(\d+\)$", "", name)

    candidate = os.path.join(directory, f"{name}{ext}")
    if not os.path.exists(candidate):
        return f"{name}{ext}"

    i = 2
    while True:
        candidate = os.path.join(directory, f"{name} ({i}){ext}")
        if not os.path.exists(candidate):
            return f"{name} ({i}){ext}"
        i += 1


def pick_proxy_dict(proxies_list):
    if not proxies_list:
        return None
    proxy = random.choice(proxies_list)
    return {"http": proxy, "https": proxy}


def embed_metadata(mp3_path, image_url=None, title=None, artist=None,
                   genre=None, publisher=None, author_url=None,
                   proxies_list=None, timeout=15):
    image_bytes = None
    mime = None

    # Try fetching artwork if provided
    if image_url:
        try:
            proxy_dict = pick_proxy_dict(proxies_list)
            r = requests.get(image_url, proxies=proxy_dict, timeout=timeout)
            r.raise_for_status()
            image_bytes = r.content
            ct = r.headers.get("Content-Type", "")
            mime = (ct.split(";")[0].strip() if ct else None) or "image/jpeg"
        except Exception as e:
            print(f"{Fore.YELLOW}Skipping artwork download: {e}")

    audio = MP3(mp3_path, ID3=ID3)
    try:
        audio.add_tags()
    except error:
        pass  # tags already exist

    # Title
    if title:
        audio.tags["TIT2"] = TIT2(encoding=3, text=title)

    # Artist
    if artist:
        audio.tags["TPE1"] = TPE1(encoding=3, text=artist)

    # Genre
    if genre:
        audio.tags["TCON"] = TCON(encoding=3, text=genre)

    # Publisher
    if publisher:
        audio.tags["TPUB"] = TPUB(encoding=3, text=publisher)

    # Author URL
    if author_url:
        audio.tags["WOAR"] = WOAR(url=author_url)

    # Remove old covers if any, then add ours
    if image_bytes:
        for key in list(audio.tags.keys()):
            if key.startswith("APIC"):
                del audio.tags[key]

        audio.tags.add(APIC(
            encoding=3,  # UTF-8
            mime=mime,  # e.g. "image/jpeg" or "image/png"
            type=3,  # 3 = front cover
            desc="Cover",
            data=image_bytes
        ))

    audio.save(v2_version=3)  # ID3v2.3 works well with Windows Explorer


def extract_song_info_from_suno(profile, proxies_list=None):
    print(f"{Fore.CYAN}Extracting songs from profile: {profile}")

    username = profile.lstrip("@")
    base_url = (
        f"https://studio-api.prod.suno.com/api/profiles/{username}"
        f"?playlists_sort_by=created_at&clips_sort_by=created_at&page="
    )

    song_info = {}
    page = 1
    MAX_SLEEP = 60
    INITIAL_SLEEP = 10
    INCREMENT = 5

    while True:
        api_url = f"{base_url}{page}"
        retry_sleep = INITIAL_SLEEP

        while True:
            try:
                proxy_dict = pick_proxy_dict(proxies_list)
                response = requests.get(api_url, proxies=proxy_dict, timeout=10)

                # Handle 429 without raising to catch status code cleanly
                if response.status_code == 429:
                    if retry_sleep > MAX_SLEEP:
                        print(f"{Fore.RED}Hit 429 too many times on page {page}. "
                              f"Exceeded {MAX_SLEEP}s backoff. Exiting.")
                        return song_info
                    print(f"{Fore.YELLOW}429 received on page {page}. "
                          f"Sleeping {retry_sleep}s then retrying…")
                    time.sleep(retry_sleep)
                    retry_sleep += INCREMENT
                    continue  # retry same page

                response.raise_for_status()
                data = response.json()
                break  # successful fetch, exit retry loop

            except requests.exceptions.HTTPError as e:
                # Non-429 HTTP errors: log and exit
                status = getattr(e.response, "status_code", "Unknown")
                print(f"{Fore.RED}HTTP error on page {page} (status {status}): {e}")
                return song_info
            except Exception as e:
                print(f"{Fore.RED}Failed to retrieve data from API (page {page}): {e}")
                return song_info

        clips = data.get("clips", [])
        if not clips:
            break

        for clip in clips:
            uuid = clip.get("id")
            title = clip.get("title")
            audio_url = clip.get("audio_url")
            image_url = clip.get("image_url")
            video_url = clip.get("video_url")
            genre = clip.get("display_tags")
            publisher = clip.get("display_name")
            url = "https://suno.com/song/" + clip.get("id")
            if (uuid and title and audio_url) and uuid not in song_info:
                song_info[uuid] = {
                    "title": title,
                    "audio_url": audio_url,
                    "video_url": video_url,
                    "image_url": image_url,
                    "genre": genre,
                    "publisher": publisher,
                    "url": url
                }

        page += 1
        # Light delay between pages to reduce chance of 429s
        time.sleep(5)

    return song_info


def fetch_song_info_by_uuid(uuid, proxies_list=None, timeout=15):
    """
    Directly query the clip endpoint for a single UUID.
    Returns dict compatible with songs map used elsewhere, or None on failure.
    """
    api_url = f"https://studio-api.prod.suno.com/api/clip/{uuid}"
    try:
        proxy_dict = pick_proxy_dict(proxies_list)
        r = requests.get(api_url, proxies=proxy_dict, timeout=timeout)
        if r.status_code == 404:
            print(f"{Fore.YELLOW}UUID not found: {uuid}")
            return None
        r.raise_for_status()
        j = r.json()

        # Fall back fields if some are missing
        title = j.get("title") or uuid
        audio_url = j.get("audio_url")
        image_url = j.get("image_url") or j.get("image_large_url")
        video_url = j.get("video_url")
        genre = j.get("display_tags")
        publisher = j.get("display_name") or j.get("persona", {}).get("name")
        url = "https://suno.com/song/" + (j.get("id") or uuid)

        if not audio_url:
            print(f"{Fore.YELLOW}No audio_url for UUID {uuid}, skipping.")
            return None

        return {
            "title": title,
            "audio_url": audio_url,
            "video_url": video_url,
            "image_url": image_url,
            "genre": genre,
            "publisher": publisher,
            "url": url
        }
    except requests.exceptions.HTTPError as e:
        status = getattr(e.response, "status_code", "Unknown")
        print(f"{Fore.RED}HTTP error for UUID {uuid} (status {status}): {e}")
    except Exception as e:
        print(f"{Fore.RED}Failed to fetch UUID {uuid}: {e}")
    return None


def refresh_song_info(profile, proxies_list):
    all_songs = extract_song_info_from_suno(profile, proxies_list)
    if not all_songs:
        return {}
    return all_songs


def download_file(url, filename, proxies_list=None, timeout=30):
    proxy_dict = pick_proxy_dict(proxies_list)
    with requests.get(url, stream=True, proxies=proxy_dict, timeout=timeout) as r:
        r.raise_for_status()
        with open(filename, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
    return filename


def read_uuid_file(path):
    uuids = set()
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    # Split on common delimiters and whitespace
    for token in re.split(r"[,\s]+", content.strip()):
        if token:
            uuids.add(token.strip())
    return uuids


def parse_uuid_list(raw):
    uuids = set()
    # Accept commas, spaces, or newlines
    for token in re.split(r"[,\s]+", raw.strip()):
        if token:
            uuids.add(token.strip())
    return uuids


def main():
    parser = argparse.ArgumentParser(description="Bulk download suno songs by profile or UUIDs")

    parser.add_argument("--profile", type=str,
                        help="Suno profile name (e.g., '@xxxx'). "
                             "NOT allowed together with --uuids/--uuids-file.")

    parser.add_argument("--proxy", type=str, required=False,
                        help="Proxy with protocol. Multiple proxies separated by commas.")
    parser.add_argument("--directory", type=str, required=False, default="suno-downloads",
                        help="Local directory for saving the files")
    parser.add_argument(
        "--with-thumbnail",
        action="store_true",
        help="Embed the song's thumbnail into the MP3 file (default: disabled)"
    )
    parser.add_argument(
        "--uuids",
        type=str,
        help="Only download these UUIDs (comma/space/newline separated). "
             "If set, --profile must NOT be provided."
    )
    parser.add_argument(
        "--uuids-file",
        type=str,
        help="Path to a file containing UUIDs to download (one per line or separated by commas/spaces). "
             "If set, --profile must NOT be provided."
    )

    args = parser.parse_args()

    # Proxies
    proxies_list = args.proxy.split(",") if args.proxy else None

    # Collect requested UUIDs (if any)
    requested_uuids = set()
    if args.uuids:
        requested_uuids.update(parse_uuid_list(args.uuids))
    if args.uuids_file:
        try:
            requested_uuids.update(read_uuid_file(args.uuids_file))
        except Exception as e:
            print(f"{Fore.RED}Failed to read --uuids-file: {e}")
            sys.exit(1)

    # Validate mode: profile vs uuid(s)
    if requested_uuids and args.profile:
        print(f"{Fore.RED}--profile is not allowed when using --uuids or --uuids-file.")
        sys.exit(2)

    if not requested_uuids and not args.profile:
        print(f"{Fore.RED}You must provide either --profile OR (--uuids/--uuids-file).")
        sys.exit(2)

    # Build songs map
    songs = {}

    if requested_uuids:
        print(f"{Fore.CYAN}UUID mode active. Fetching {len(requested_uuids)} clip(s) directly.")
        for uid in sorted(requested_uuids):
            info = fetch_song_info_by_uuid(uid, proxies_list=proxies_list)
            if info:
                songs[uid] = info

        if not songs:
            print(f"{Fore.RED}No valid clips fetched for provided UUIDs.")
            sys.exit(1)

    else:
        # Profile mode
        raw_input = args.profile.strip()
        profile_name = raw_input.lstrip("@")
        profile = f"@{profile_name}"
        songs = refresh_song_info(profile, proxies_list)
        if not songs:
            print(f"{Fore.RED}No songs found for profile: {profile}")
            sys.exit(1)

    # Create output directory
    if not os.path.exists(args.directory):
        os.makedirs(args.directory)

    # Download & tag
    for uuid, obj in songs.items():
        title = obj.get("title") or uuid
        base_fname = sanitize_filename(title) + ".mp3"
        unique_fname = ensure_unique_filename(args.directory, base_fname)
        out_path = os.path.join(args.directory, unique_fname)

        print(f"Downloading: {Fore.GREEN}🎵 {title} — {uuid}")
        try:
            saved = download_file(obj["audio_url"], out_path, proxies_list)
            if args.with_thumbnail and obj.get("image_url"):
                _artist, _title = None, None

                # This part is just something I needed for my own use case. It's not required.
                # It splits the title by the '-' and uses the first part as the artist and second as the song title
                # Then adds them to the metadata of the file
                # if " - " in title:
                #     parts = title.split(" - ", 1)
                #     if len(parts) == 2:
                #         _artist = parts[0]
                #         _title = parts[1]
                #     else:
                #         _title = title
                # else:
                #     _title = title

                embed_metadata(
                    saved,
                    image_url=obj.get("image_url"),
                    proxies_list=proxies_list,
                    genre=obj.get('genre'),
                    publisher=obj.get('publisher'),
                    author_url=obj.get('url'),
                    artist=_artist,
                    title=_title
                )
        except Exception as e:
            print(f"{Fore.RED}Failed on {title}: {e}")

    print(f"\n{Fore.BLUE}All songs have been downloaded and saved into {args.directory}.")
    sys.exit(0)


if __name__ == "__main__":
    main()