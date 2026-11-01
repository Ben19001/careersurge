# Contributing

Thanks for helping. Set the app up as the README describes, then run the checks below before you open a pull
request. CI runs the same ones on Windows, macOS and Linux.

## Backend tests

```
cd backend
python -m unittest discover -s tests
```

- Bare `python -m unittest` finds nothing: the `discover -s tests` part is needed.
- Set `CAREERSURGE_DATA_DIR` to a scratch folder first, so no test can reach your real data:
  `CAREERSURGE_DATA_DIR=/tmp/cs-test python -m unittest discover -s tests`.
- The end-to-end UI tests (`test_local_ui_e2e.py`, `test_local_ui_onboarding.py`) drive headless Chromium
  against `frontend/dist`, and skip when the UI isn't built.
- On a machine low on memory, run one file at a time with `-p test_local_apply.py`.
- No test reaches Apify, TypeSafe or a real employer, and none submits an application. Keep it that way.

## Frontend checks

```
cd frontend
npm run build
npm run lint
```

`npm run format` applies Prettier.

## Rules for changes

- **No real personal data** in fixtures, screenshots, logs or issues: use invented names, `example.com`
  addresses and synthetic resumes.
- **Never fabricate an answer.** Every value the app puts on a form comes from the profile, an answer the user
  saved, a consent the user gave, or the form's own decline option. Anything else goes to Needs you.
- **No evasion features**: no captcha solving, stealth, proxy rotation or email aliases.
- Migrations in `backend/app/store/migrations/` run once each, in number order, tracked by SQLite's
  `user_version`. An installed copy never runs a released file again, so a schema change is always a new file.
- Keep SQL to schema and plain reads and writes. Normalizing, scoring and classifying belong in Python.
