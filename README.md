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

That is the on-demand command. Re-runs are incremental: an episode already on
disk is skipped, so only newly starred ones get fetched. Matching is by
filename, because the byte size a feed advertises often disagrees with what its
CDN actually serves; size is used only to spot a badly truncated file and
re-fetch it. If the refresh token expires or is revoked, the script stops and
reprints the setup steps.

## Options

| Flag | Effect |
|---|---|
| `--no-audio` | Manifest only, no downloads |
| `--limit N` | Only the N most recent starred episodes |
| `--out DIR` | Where to put files (default `./downloads`) |
| `--jobs N` | Parallel downloads (default 3) |
| `--relogin` | Force a token refresh before fetching |

## Output

```
downloads/
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
