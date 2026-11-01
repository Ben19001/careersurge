# Security

## Reporting a vulnerability

Please report it privately through GitHub's **private vulnerability reporting**: the "Report a vulnerability"
button under this repository's **Security** tab. Don't open a public issue for it. Include what an attacker
can do, the steps to reproduce it, and the OS and version you saw it on.

## Threat model

CareerSurge runs on your own computer, for one user. It defends against **other websites and other local
users**, not against malware already running as you: anything that runs as your user can read your data
folder and your keychain.

- **Loopback only.** The server listens on `127.0.0.1` at a random port, so other machines can't reach it.
- **Every API call is checked** (`backend/app/local_api.py`):
  1. `Host` must be `127.0.0.1` or `localhost`, which refuses DNS rebinding (a hostile site's own domain
     pointed at your loopback address).
  2. `Origin`, when the browser sends one, must be the app's own origin, which refuses a form or `fetch` from
     another site.
  3. `X-CareerSurge-Token` must equal a token made fresh for each run. It reaches the window in the URL
     fragment (`#token=...`), which the browser never sends to a server, and a page from another site can't
     read it.
  There are no cookies and no CORS headers, so another site's page has no ambient credential to use.
- **Keys** are stored in the OS keychain. Where there is none, they go to `secrets.json` in the data folder,
  created with mode `0600` (readable by your user only). Windows does not enforce that mode, so there the
  file is protected only by your user folder's permissions. The API never returns a key: only whether it is
  set and its last four characters.
- **The database is not encrypted.** It holds your profile and application history, protected only by the
  data folder's permissions (`0700` on macOS and Linux).
- **The browser opens only application forms.** A listing's URL comes from Apify, so the apply worker opens it
  only when its host belongs to that listing's applicant tracking system (Greenhouse, Lever or Ashby).
