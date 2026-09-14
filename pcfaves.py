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

# ---------------------------------------------------------------- state

STATE_VERSION = 1


def iso_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_state(path):
    """The ledger of what has already been fetched.

    Kept outside the download folder on purpose: `downloads/` is a staging
    area you are expected to empty, so presence of a file there cannot be
    the record of what has been downloaded.
    """
    try:
        with open(path) as f:
            state = json.load(f)
    except FileNotFoundError:
        state = {}
    except json.JSONDecodeError:
        print(f"State file at {path} is unreadable; starting a fresh ledger.",
              file=sys.stderr)
        state = {}
    state.setdefault("version", STATE_VERSION)
    state.setdefault("last_run", None)
    state.setdefault("runs", 0)
    state.setdefault("downloaded", {})
    state.setdefault("failed", {})
    return state


def save_state(path, state):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def record_download(state, row, path, out_dir):
    state["downloaded"][row["uuid"]] = {
        "show": row["show"],
        "title": row["title"],
        "published": row["published"],
        "file": os.path.relpath(path, out_dir),
        "at": iso_now(),
        "bytes": os.path.getsize(path) if os.path.exists(path) else 0,
    }
    state["failed"].pop(row["uuid"], None)


def record_failure(state, row, reason):
    prev = state["failed"].get(row["uuid"], {})
    state["failed"][row["uuid"]] = {
        "show": row["show"],
        "title": row["title"],
        "url": row["audio_url"],
        "attempts": prev.get("attempts", 0) + 1,
        "last_error": reason,
        "at": iso_now(),
    }


def adopt_existing(state, out_dir, rows, by_uuid):
    """Seed a new ledger from audio already sitting in the staging folder.

    Only runs when there is no ledger yet, so upgrading from an earlier
    version does not re-download everything already on disk.
    """
    adopted = 0
    for row in rows:
        if row["uuid"] in state["downloaded"]:
            continue
        path = target_path(out_dir, row, by_uuid.get(row["uuid"], {}))
        if os.path.exists(path) and os.path.getsize(path) > 0:
            record_download(state, row, path, out_dir)
            state["downloaded"][row["uuid"]]["adopted"] = True
            adopted += 1
    return adopted


def print_status(state, path):
    if not os.path.exists(path):
        print(f"No ledger yet at {path}. The next run creates one.")
        return
    last = state["last_run"] or "never"
    print(f"Ledger      : {path}")
    print(f"Last run    : {last}   ({state['runs']} run(s) recorded)")
    print(f"Downloaded  : {len(state['downloaded'])} episodes")
    if state["failed"]:
        print(f"Failing     : {len(state['failed'])} episode(s), retried every run")
        for info in sorted(state["failed"].values(),
                           key=lambda i: -i.get("attempts", 0)):
            print(f"  {info.get('attempts', 0):2}x  {info['show']} - {info['title']}"[:78])
            print(f"        {info.get('last_error', '')}"[:78])


def main():
    ap = argparse.ArgumentParser(description="Download Pocket Casts starred episodes")
    ap.add_argument("--out", default=os.path.join(HERE, "downloads"),
                    help="staging folder for downloads (default ./downloads)")
    ap.add_argument("--state", default=os.path.join(HERE, ".pcfaves-state.json"),
                    help="ledger of what has been downloaded")
    ap.add_argument("--no-audio", action="store_true", help="metadata only")
    ap.add_argument("--limit", type=int, default=0, help="only the N newest")
    ap.add_argument("--jobs", type=int, default=3, help="parallel downloads")
    ap.add_argument("--relogin", action="store_true",
                    help="force a token refresh before fetching")
    ap.add_argument("--status", action="store_true",
                    help="show the ledger and exit, without contacting the API")
    ap.add_argument("--redownload", action="store_true",
                    help="ignore the ledger and fetch everything again")
    args = ap.parse_args()

    state = load_state(args.state)

    if args.status:
        print_status(state, args.state)
        return

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
    stamp = iso_now()

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

    first_ledger = not state["downloaded"] and not state["failed"]
    if first_ledger:
        adopted = adopt_existing(state, args.out, rows, by_uuid)
        if adopted:
            print(f"Adopted {adopted} episode(s) already in {args.out} "
                  f"into a new ledger.")
        save_state(args.state, state)

    print(f"Last run: {state['last_run'] or 'never'}")

    if args.redownload:
        pending = [r for r in rows if r["audio_url"]]
        print(f"--redownload: fetching all {len(pending)} again.")
    else:
        pending = [r for r in rows
                   if r["audio_url"] and r["uuid"] not in state["downloaded"]]
        retries = sum(1 for r in pending if r["uuid"] in state["failed"])
        fresh = len(pending) - retries
        print(f"{len(state['downloaded'])} already downloaded, "
              f"{fresh} new since last run, {retries} retrying after failure.")

    # --limit caps what we download; the manifest above always covers everything
    if args.limit:
        pending = pending[:args.limit]

    if not pending:
        state["last_run"] = stamp
        state["runs"] += 1
        save_state(args.state, state)
        print("Nothing to do.")
        return

    total = sum(r["size_bytes"] for r in pending)
    print(f"\nDownloading {len(pending)} episode(s), ~{human(total)}, "
          f"with {args.jobs} parallel jobs...\n")

    jobs = [(r, target_path(args.out, r, by_uuid.get(r["uuid"], {})))
            for r in pending]

    done = skipped = failed = 0
    try:
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            futs = {pool.submit(download, r["audio_url"], p, r["size_bytes"]):
                    (r, p) for r, p in jobs}
            for i, fut in enumerate(as_completed(futs), 1):
                r, p = futs[fut]
                try:
                    status = fut.result()
                except Exception as e:
                    status = f"fail: {e}"
                label = f'{r["show"]} - {r["title"]}'[:70]
                if status in ("ok", "skip"):
                    done += status == "ok"
                    skipped += status == "skip"
                    record_download(state, r, p, args.out)
                    word = "ok     " if status == "ok" else "have   "
                    print(f"[{i}/{len(jobs)}] {word} {label}")
                else:
                    failed += 1
                    record_failure(state, r, status)
                    print(f"[{i}/{len(jobs)}] FAILED {label}  ({status})")
                if i % 10 == 0:
                    save_state(args.state, state)
    finally:
        state["last_run"] = stamp
        state["runs"] += 1
        save_state(args.state, state)

    print(f"\nDownloaded {done}, already on disk {skipped}, failed {failed}.")
    if state["failed"]:
        log = os.path.join(args.out, "failures.txt")
        with open(log, "w") as f:
            for info in state["failed"].values():
                f.write(f"{info['show']} - {info['title']}\n"
                        f"  {info.get('last_error','')} "
                        f"(attempt {info.get('attempts',0)})\n"
                        f"  {info['url']}\n\n")
        print(f"{len(state['failed'])} episode(s) still failing; details: {log}")
    print(f"Staged in {args.out}; ledger at {args.state}")
    print("Move or rename those files freely - the ledger, not the folder, "
          "is what stops them downloading again.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
