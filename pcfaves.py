#!/usr/bin/env python3
"""Download starred (favourited) episodes from Pocket Casts.

Auth: reads a long-lived refresh token from the macOS Keychain and trades it for
short-lived access tokens. Nothing secret is ever written into this folder. See
README.md for the one-time setup that puts the refresh token in the Keychain.

Usage:
    python3 pcfaves.py                 # sync: fetch list + download new audio
    python3 pcfaves.py --no-audio      # metadata only
    python3 pcfaves.py --limit 5       # try a few episodes first
    python3 pcfaves.py --relogin       # force a token refresh
"""

import argparse
import base64
import csv
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

API = "https://api.pocketcasts.com"
CACHE_API = "https://podcast-api.pocketcasts.com"
UA = "PocketCasts/1.0 (pcfaves)"
KEYCHAIN_ACCESS = "pcfaves-pocketcasts-token"
KEYCHAIN_REFRESH = "pcfaves-pocketcasts-refresh"
ACCOUNT = "default"

HERE = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------- http

def post(path, payload, token=None, base=API):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(base + path, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", UA)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def get_json(url):
    req = urllib.request.Request(url)
    req.add_header("User-Agent", UA)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


# ---------------------------------------------------------------- auth

SETUP_HELP = """
No Pocket Casts refresh token stored yet (or the stored one has expired).

This account signs in with Google, so there is no password to prompt for.
Pocket Casts keeps its session in a JS-readable `auth` cookie; the refresh
token inside it lasts about a year. Grab it once:

  1. Open https://pocketcasts.com in Chrome, logged in.
  2. DevTools (Cmd-Option-J) -> Console -> paste the contents of get-token.js
     (sitting next to this script). It copies the token to your clipboard.
  3. In Terminal:

       security add-generic-password -U -s %s -a %s -w "$(pbpaste)"

  4. Re-run this script. It mints its own hourly access tokens from then on.
""" % (KEYCHAIN_REFRESH, ACCOUNT)


def keychain_get(service, account=ACCOUNT):
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", service,
             "-a", account, "-w"],
            capture_output=True, text=True, check=True)
        return out.stdout.strip() or None
    except subprocess.CalledProcessError:
        return None


def keychain_set(service, secret, account=ACCOUNT):
    subprocess.run(
        ["security", "add-generic-password", "-U", "-s", service,
         "-a", account, "-w", secret],
        capture_output=True, text=True, check=True)


def token_expired(token, skew=300):
    """Best-effort JWT expiry check; if it isn't a JWT, treat as expired."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload)).get("exp")
        if not exp:
            return False
        return time.time() > exp - skew
    except Exception:
        return False


def need_setup():
    sys.exit(SETUP_HELP)


def refresh_access_token(refresh_token):
    """Trade the long-lived refresh token for a fresh access token."""
    body = {"grantType": "refresh_token", "refreshToken": refresh_token,
            "scope": "webplayer"}
    try:
        res = post("/user/token", body)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.loads(e.read().decode()).get("error", "")
        except Exception:
            pass
        if e.code in (400, 401, 403):
            print(f"Refresh token rejected ({detail or e.code}).", file=sys.stderr)
            need_setup()
        raise

    access = (res.get("accessToken") or res.get("access_token")
              or res.get("token"))
    if not access:
        sys.exit(f"Unexpected refresh response, no access token in: "
                 f"{list(res.keys())}")
    new_refresh = res.get("refreshToken") or res.get("refresh_token")
    if new_refresh and new_refresh != refresh_token:
        keychain_set(KEYCHAIN_REFRESH, new_refresh)
    keychain_set(KEYCHAIN_ACCESS, access)
    return access


def get_token(force=False):
    """Cached access token, refreshed automatically when stale."""
    if not force:
        access = keychain_get(KEYCHAIN_ACCESS)
        if access and not token_expired(access):
            return access
    refresh_token = keychain_get(KEYCHAIN_REFRESH)
    if not refresh_token:
        need_setup()
    return refresh_access_token(refresh_token)


# ---------------------------------------------------------------- data

def fetch_starred(token):
    return post("/user/starred", {}, token=token).get("episodes", [])


def fetch_podcast_titles(token):
    """uuid -> title for everything the account subscribes to."""
    try:
        res = post("/user/podcast/list", {"v": 1}, token=token)
    except urllib.error.HTTPError:
        return {}
    out = {}
    for p in res.get("podcasts", []):
        if p.get("uuid"):
            out[p["uuid"]] = p.get("title") or ""
    return out


def lookup_podcast(uuid):
    """Fallback for starred episodes from unsubscribed shows."""
    try:
        res = get_json(f"{CACHE_API}/podcast/full/{uuid}/0/3/0")
        return (res.get("podcast") or {}).get("title") or ""
    except Exception:
        return ""


SAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize(name, limit=120):
    name = SAFE.sub("-", (name or "").strip())
    name = re.sub(r"\s+", " ", name).strip(" .")
    return (name[:limit].strip(" .") or "untitled")


def ext_for(ep):
    url = (ep.get("url") or "").split("?")[0]
    m = re.search(r"\.(mp3|m4a|mp4|aac|ogg|opus|wav|m4b)$", url, re.I)
    if m:
        return "." + m.group(1).lower()
    ft = (ep.get("fileType") or "").lower()
    return {"audio/mpeg": ".mp3", "audio/mp4": ".m4a", "audio/x-m4a": ".m4a",
            "audio/aac": ".aac", "audio/ogg": ".ogg",
            "video/mp4": ".mp4"}.get(ft, ".mp3")


def to_int(v):
    """The API sends numbers as strings, and sometimes as empty strings."""
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def pub_date(ep):
    raw = ep.get("published") or ""
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ",
                "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return raw[:10] or "0000-00-00"


def normalize(episodes, titles):
    rows, missing = [], {}
    for ep in episodes:
        puuid = ep.get("podcastUuid") or ""
        show = ep.get("podcastTitle") or titles.get(puuid) or ""
        if not show and puuid:
            if puuid not in missing:
                missing[puuid] = lookup_podcast(puuid)
            show = missing[puuid]
        rows.append({
            "uuid": ep.get("uuid"),
            "show": show or "Unknown Show",
            "title": ep.get("title") or "",
            "published": pub_date(ep),
            "duration_sec": to_int(ep.get("duration")),
            "size_bytes": to_int(ep.get("size")),
            "audio_url": ep.get("url") or "",
            "podcast_uuid": puuid,
        })
    rows.sort(key=lambda r: (r["published"], r["show"]), reverse=True)
    return rows


# ---------------------------------------------------------------- download

def target_path(out_dir, row, ep):
    show_dir = os.path.join(out_dir, sanitize(row["show"]))
    fname = sanitize(f'{row["published"]} - {row["title"]}') + ext_for(ep)
    return os.path.join(show_dir, fname)


def download(url, path, expected=0):
    if os.path.exists(path):
        have = os.path.getsize(path)
        # Feeds routinely disagree with the bytes actually served, so only a
        # wildly short file (a truncated earlier run) is worth re-fetching.
        if have > 0 and (not expected or have >= expected * 0.5):
            return "skip"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".part"
    cmd = ["curl", "-fsSL", "--retry", "3", "--retry-delay", "2",
           "--connect-timeout", "30", "--max-time", "3600",
           "-C", "-", "-A", UA, "-o", tmp, url]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode == 33:  # server refused byte-range resume; restart clean
        if os.path.exists(tmp):
            os.remove(tmp)
        cmd.remove("-C")
        cmd.remove("-")
        res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        if os.path.exists(tmp):
            os.remove(tmp)
        return "fail: " + (res.stderr.strip().splitlines() or ["curl exit %d" % res.returncode])[-1]
    os.replace(tmp, path)
    return "ok"


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="Download Pocket Casts starred episodes")
    ap.add_argument("--out", default=os.path.join(HERE, "downloads"))
    ap.add_argument("--no-audio", action="store_true", help="metadata only")
    ap.add_argument("--limit", type=int, default=0, help="only the N newest")
    ap.add_argument("--jobs", type=int, default=3, help="parallel downloads")
    ap.add_argument("--relogin", action="store_true",
                help="force a token refresh before fetching")
    args = ap.parse_args()

    token = get_token(force=args.relogin)

    try:
        episodes = fetch_starred(token)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            token = get_token(force=True)
            episodes = fetch_starred(token)
        else:
            raise

    print(f"{len(episodes)} starred episodes.")
    if not episodes:
        return

    titles = fetch_podcast_titles(token)
    rows = normalize(episodes, titles)
    by_uuid = {e.get("uuid"): e for e in episodes}

    os.makedirs(args.out, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")

    with open(os.path.join(args.out, "starred.json"), "w") as f:
        json.dump({"fetched_at": stamp, "count": len(rows), "episodes": rows},
                  f, indent=2, ensure_ascii=False)
    with open(os.path.join(args.out, "starred.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"Wrote starred.json / starred.csv to {args.out}")

    if args.no_audio:
        return

    # --limit caps what we download; the manifest above always covers everything
    if args.limit:
        rows = rows[:args.limit]

    total = sum(r["size_bytes"] for r in rows)
    print(f"Downloading audio (~{human(total)} if nothing is cached) "
          f"with {args.jobs} parallel jobs...\n")

    jobs = []
    for r in rows:
        if not r["audio_url"]:
            continue
        jobs.append((r, target_path(args.out, r, by_uuid.get(r["uuid"], {}))))

    done = skipped = failed = 0
    failures = []
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futs = {pool.submit(download, r["audio_url"], p, r["size_bytes"]): (r, p)
                for r, p in jobs}
        for i, fut in enumerate(as_completed(futs), 1):
            r, p = futs[fut]
            try:
                status = fut.result()
            except Exception as e:
                status = f"fail: {e}"
            label = f'{r["show"]} — {r["title"]}'[:70]
            if status == "ok":
                done += 1
                print(f"[{i}/{len(jobs)}] ok      {label}")
            elif status == "skip":
                skipped += 1
                print(f"[{i}/{len(jobs)}] have    {label}")
            else:
                failed += 1
                failures.append((label, status, r["audio_url"]))
                print(f"[{i}/{len(jobs)}] FAILED  {label}  ({status})")

    print(f"\nDownloaded {done}, already had {skipped}, failed {failed}.")
    if failures:
        log = os.path.join(args.out, "failures.txt")
        with open(log, "w") as f:
            for label, status, url in failures:
                f.write(f"{label}\n  {status}\n  {url}\n\n")
        print(f"Failure details: {log}")
    print(f"Files are in {args.out}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
