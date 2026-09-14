# Pocket Casts starred-episode downloader

Pulls every starred episode from your Pocket Casts account, writes a manifest,
and downloads the audio into per-show folders.

## How auth works

Pocket Casts accounts using Google (or Apple) sign-in have no password to give
a script. The web player instead stores its session in a JS-readable `auth`
cookie holding two JWTs:

| Token | Lifetime | Role |
|---|---|---|
| `accessToken` | 1 hour | Sent as `Authorization: Bearer` on API calls |
| `refreshToken` | ~365 days | Trades for new access tokens via `POST /user/token` |

So you hand the script the **refresh token once a year**, and it mints its own
hourly access tokens after that. Both live in your macOS Keychain, never in
this folder. Treat the refresh token like a password: it carries `webplayer`
scope over your account for its full lifetime.

## One-time setup

1. Open <https://pocketcasts.com> in Chrome, logged in.
2. DevTools (Cmd-Option-J) → Console → paste all of [`get-token.js`](get-token.js).
   It copies the refresh token to your clipboard and prints its expiry date.
3. In Terminal, move it clipboard → Keychain (the value never appears on screen):

```bash
security add-generic-password -U -s pcfaves-pocketcasts-refresh -a default -w "$(pbpaste)"
```

## Normal use

```bash
python3 pcfaves.py
```

That is the on-demand command. Re-runs are incremental: only episodes that have
never been downloaded get fetched. If the refresh token expires or is revoked,
the script stops and reprints the setup steps.

## Staging, and how re-runs stay incremental

`downloads/` is a staging area. Move, rename, and reorganize what lands there
however you like — the script never looks at the folder to decide what to skip.

What it reads instead is a ledger at `.pcfaves-state.json`, which records the
episode IDs already fetched plus the date of each run. That is deliberately
*not* kept inside `downloads/`, so emptying the staging folder never makes the
script forget its work, and you never end up maintaining a second copy of your
library just to track what you have.

```bash
python3 pcfaves.py --status     # last run, counts, and what keeps failing
```

A note on why the ledger tracks IDs rather than just a cutoff date: the API
reports *that* an episode is starred, never *when* it was starred. Filtering on
a date alone would silently skip an old episode you starred today, because its
publish date predates the last run. The ledger has no such blind spot.

Episodes that fail keep their place in a retry list and are attempted on every
run, so a temporary outage fixes itself. `--status` shows the attempt count for
each, which is how you spot a permanently dead URL.

## Options

| Flag | Effect |
|---|---|
| `--no-audio` | Manifest only, no downloads |
| `--limit N` | Only the N most recent starred episodes |
| `--out DIR` | Where to put files (default `./downloads`) |
| `--jobs N` | Parallel downloads (default 3) |
| `--relogin` | Force a token refresh before fetching |
| `--status` | Show the ledger and exit; makes no network calls |
| `--redownload` | Ignore the ledger and fetch everything again |
| `--state PATH` | Use a different ledger file |

## Output

```
.pcfaves-state.json     the ledger: what has been fetched, and when
downloads/              staging; safe to empty after you file things away
  starred.json          normalized list + fetch timestamp
  starred.csv           same, spreadsheet-friendly
  failures.txt          only if something failed
  <Show Name>/
    2026-08-14 - Episode Title.mp3
```

## Notes

- Audio comes from each podcast's own CDN — the same file the app downloads.
  Shows that expire old episodes will fail; those land in `failures.txt`.
- Clear stored tokens with:

```bash
security delete-generic-password -s pcfaves-pocketcasts-refresh
security delete-generic-password -s pcfaves-pocketcasts-token
```
