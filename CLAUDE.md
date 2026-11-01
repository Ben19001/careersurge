# CareerSurge

A free desktop app that finds job postings and applies to them for you, on your own machine.
It runs as a local FastAPI server with a pywebview window, keeps everything in SQLite, drives plain headless
Chromium, and uses keys you supply: Apify (job listings, billed to your account) and Jev (TypeSafe, for
picking among values the app already found). It supports Greenhouse, Lever and Ashby.
There is no captcha solving, stealth, proxy rotation or alias mail: a blocked application is handed back to you.

## Environment
Everything runs on `127.0.0.1` at a random port. Hardcoded loopback addresses are the design, not a defect.

## Repo layout
- `backend/`: Python 3.12. The FastAPI shell is `app/local_api.py`, launched by `app/local_main.py`.
- `frontend/`: the React 19 + Vite SPA that `local_main` serves (see Frontend).

## Backend (`backend/`)
- **Deps:** `requirements.txt` (lower bounds, not a lockfile; see its header). Then `python -m playwright install chromium`.
- **Run:** `python -m app.local_main` from `backend/` opens the window; `--no-window` prints the URL instead.
  `run.sh` / `run.ps1` at the repo root install everything on the first run (marker `backend/venv/.installed`;
  `--update` reinstalls), then start it the same way. They assume the README's prerequisites and don't check them.
  The data folder is the OS user data dir (`platformdirs`), or `CAREERSURGE_DATA_DIR` when set.
- **Tests:** `cd backend && python -m unittest discover -s tests` (bare `python -m unittest` discovers nothing;
  `tests/conftest.py` holds the shared bootstrap and stubs). Point `CAREERSURGE_DATA_DIR` at a scratch folder so
  no test can reach your real data.
- **Structure:** `app/` is a package.
  - `app/local_api.py`: the routes. `app/local_main.py`: lock, migrate, recover, uvicorn, window.
  - `app/services/`: the workers. `local_sourcing` (Apify runs and ingest), `local_profile` (resume parse,
    profile), `local_batch` (the batch you approve), `local_apply` and `local_formfill` (claim, fill, submit),
    `local_answers` (saved answers, label mapping), `local_handoff` (captcha, one-time codes, email links),
    `local_queues` (per-ATS queues and the sourcing schedule), `jev` (the TypeSafe client), `us_states`.
    Cross-service calls go through the module object rather than direct imports, so tests can patch one seam.
  - `app/store/`: SQLite. `db.py` (data dir, connections, `migrate()`, settings), `secrets.py` (OS keychain, or
    `secrets.json` when there is none), `instance_lock.py`, `migrations/NNNN_*.sql` applied in order by
    `PRAGMA user_version`.
  - `app/__init__.py` stays EMPTY: tests import `app.services.*` without the web stack.
- **Conventions:**
  - Routes answer `{"ok": True, ...}`; errors go through `_error(message, status)` in `local_api.py`.
  - Private helpers `_`-prefixed. Non-trivial helpers carry docstrings (what/who/why).
  - Keys never leave the keychain except into the request that needs them. Routes return only `set`, `last4` and `rejected`.

## Frontend (`frontend/`)
- **Stack:** React 19, TypeScript, Vite, Tailwind v4, shadcn/ui primitives copied into `src/components/ui/`
  as needed, TanStack Router (code-based, `src/router.tsx`). npm.
- **Build:** `npm run build` (`tsc --noEmit` then `vite build`) writes `frontend/dist`, which `python -m app.local_main`
  serves. There is no dev server: Vite's origin would fail the API's Origin check, so develop with `npm run dev`
  (`vite build --watch`) and reload the launcher's window. `npm run lint` = eslint, `npm run format` = prettier.
  No JS tests; the end-to-end tests are `backend/tests/test_local_ui_e2e.py` and `test_local_ui_onboarding.py`
  (headless Playwright, need `dist`).
- **Screens:** `src/screens/` (`onboarding`, `approve`, `progress`, `needs-you`, `profile`, `settings`); layout and
  nav in `src/layout.tsx`.
- **Conventions:**
  - API: `src/lib/api.ts` uses relative URLs and sends the per-run token header on every `/api/` call. snake_case JSON
    bodies. Gate on `response.ok`, surface `payload.error` (the `ApiError` message).
  - The token arrives in the window URL fragment (`#token=...`), is read once into `sessionStorage` and removed from the
    address bar. An `<img>` cannot send a header, so screenshots are fetched as blobs.
  - Polling: `usePoll` (`src/lib/poll.ts`); the Needs you tray polls `/api/blocked` every 3 s through `BlockedProvider`.
  - `cn()` from `src/lib/utils.ts` for classes. Alias `@/*` -> `src/*`. No router codegen file.
  - Imperative `fetch` + `useState`; no TanStack Query or form library.

## Backend ↔ frontend
One origin. `app.local_main` serves the built SPA and the `/api/` routes from the same uvicorn server on `127.0.0.1` at a
random port, and opens a pywebview window at `http://127.0.0.1:<port>/#token=<token>` (`--no-window` prints the URL).
Every `/api/` request must carry the per-run token in `X-CareerSurge-Token`, a loopback `Host`, and, when present, an own
`Origin` (`app/local_api.py`); pages outside `/api/` need Host and Origin only. No CORS, no cookies.

## Product rules
- **UI/UX is the top priority; integrity is the second.** Friction-free flows beat feature count,
  especially onboarding (intentionally multi-step and bite-sized). Integrity means:
  - never fabricate a factual answer: every value on a form comes from the profile, an answer the user saved,
    a consent the user authorized, or the form's own decline option. Otherwise the field goes to Needs you;
  - never apply to the same listing twice; a likely repost gets a soft warning;
  - pricing is transparent: the app shows the Apify price and asks before the first charge, and never charges past
    the monthly budget the user set.
- **SQL is CRUD, not business logic.** Migrations hold schema DDL, indexes, constraints, seed-data inserts
  and plain CRUD only. Domain transforms (normalization, canonicalization, scoring, classification) live in Python;
  SQL stores the results. Carve-out: a single-statement atomic guard (a claim or compare-and-set) stays SQL,
  because its atomicity is the point.
- Prefer few helpers over many tiny single-use abstractions.
- **Write for the reviewer.** Never wrap a DB call in a comprehension or nest queries inline. Name each query's
  result: three plain lines beat one dense one, and any multi-join or subquery carries a plain-SQL comment
  so its shape is readable at a glance.
- **Recap the schema before changing it**: table columns, CHECK values, and for each status value whether it is
  actually *written* and actually *read*. Status vocabularies drift from their constraints when nobody checks.

## Keys
Stored in the OS keychain by the onboarding and Settings screens, never in a file in the repo. An env var of
the same name wins, for dev and CI: `APIFY_TOKEN` (the only listing source; absent means no sourcing) and
`JEV_API_KEY` (absent or refused means the app falls back without Jev).
