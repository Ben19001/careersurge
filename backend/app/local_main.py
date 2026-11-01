"""The local app's launcher: `python -m app.local_main` from `backend/`.

Startup order, each step needing the one before it: check that the UI is built and Chromium is
installed (a missing one exits with the command that fixes it, before anything starts), take the
single-instance lock (a second copy would reset the first copy's live job in `recover`), apply the
migrations, settle rows a crash left mid-flight, then serve the API and UI with uvicorn on a random
127.0.0.1 port and start the queues inside the app's lifespan. The pywebview window opens
`http://127.0.0.1:<port>/#token=<token>`; the fragment never reaches the server or its logs.
`--no-window` prints that URL instead (this server has no display; the end-to-end tests drive it
with headless Playwright). There is no installer yet.
"""
import argparse
import asyncio
import contextlib
import logging
import secrets
import signal
import socket
import sys
import threading
import time
from pathlib import Path

import uvicorn
from playwright.sync_api import sync_playwright

from app import local_api
from app.services import local_apply, local_queues
from app.store import db, instance_lock

logger = logging.getLogger("careersurge.local_main")

# `backend/app/local_main.py` -> the repo's `frontend/dist`, which `vite build` writes.
DEFAULT_STATIC_DIR = Path(__file__).resolve().parents[2] / "frontend" / "dist"
SERVER_START_TIMEOUT_S = 10
# Printed when pywebview finds no GUI toolkit: `import webview` failed, or `webview.start()` raised.
NO_WINDOW_ADVICE = (
    "Cannot open the window: {error}\n"
    "Install a window toolkit for pywebview: on Linux `pip install \"pywebview[qt]\"` (or GTK with PyGObject),\n"
    "on Windows the Microsoft Edge WebView2 runtime, on macOS `pip install pyobjc`.\n"
    "Or run with --no-window and open the printed URL in your browser."
)


def in_flight_count() -> int:
    """Applications that are mid-run (`in_progress` or `submitting`), for the close-window warning."""
    conn = db.get_db()
    try:
        return conn.execute("SELECT COUNT(*) FROM job_applications "
                            "WHERE status IN ('in_progress', 'submitting')").fetchone()[0]
    finally:
        conn.close()


def close_guard(confirm, stop) -> bool:
    """Whether the window may close now; the pywebview `closing` handler returns this.

    With nothing running it is True at once. Otherwise `confirm(message)` asks the user; on "no" the
    window stays open (False). On "yes" `stop()` winds the queues down (an `in_progress` row goes back
    to `pending`, a `submitting` row reaches its outcome first) and the window closes.
    """
    running = in_flight_count()
    if running == 0:
        return True
    noun = "application" if running == 1 else "applications"
    if not confirm(f"{running} {noun} in progress. Quit when they reach a safe point?"):
        return False
    stop()
    return True


def build_app(token: str, static_dir: Path):
    """The API app with its lifespan: the queues run while the server runs.

    On startup it launches `local_queues.run_all` and `run_sourcing_schedule` as tasks on the server's
    loop, and `local_queues.resume_sourcing`, which gives each run a quit left `started` the same ingest
    and batch build as Run now (retries, failure notice), so a run paid for before the last quit is read
    instead of billed again and its listings reach the approval screen without a later Run now. On shutdown it calls `local_queues.stop_all` and
    waits for the tasks. `app.state.stop_queues()` is the same wind-down for the window-close path.
    """
    app = local_api.create_app(token, static_dir)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        loop = asyncio.get_running_loop()

        def stop_queues():
            """Wind the queues down from another thread (the window's close handler) and wait for it."""
            asyncio.run_coroutine_threadsafe(local_queues.stop_all(), loop).result()

        tasks = [asyncio.ensure_future(local_queues.run_all()),
                 asyncio.ensure_future(local_queues.run_sourcing_schedule()),
                 asyncio.ensure_future(local_queues.resume_sourcing())]
        app.state.stop_queues = stop_queues
        try:
            yield
        finally:
            await local_queues.stop_all()
            await asyncio.gather(*tasks, return_exceptions=True)

    app.router.lifespan_context = lifespan
    return app


def start_server(app) -> tuple[uvicorn.Server, threading.Thread, int]:
    """Serve `app` on 127.0.0.1 at a port the OS picks; returns `(server, thread, port)` once it accepts requests.

    The socket is bound here and handed to uvicorn, so the port is known before the window opens.
    Stop it with `server.should_exit = True` and `thread.join()`.
    """
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))

    def serve():
        server.run(sockets=[sock])

    thread = threading.Thread(target=serve, name="uvicorn", daemon=True)
    thread.start()
    deadline = time.monotonic() + SERVER_START_TIMEOUT_S
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError("the local server did not start")
        time.sleep(0.05)
    return server, thread, sock.getsockname()[1]


def main(argv=None) -> int:
    """Start the app; returns the process exit code.

    1, with what to do printed to stderr, when the UI is not built, Chromium is not installed, another copy
    is already running, or (without `--no-window`) pywebview finds no window toolkit.
    """
    parser = argparse.ArgumentParser(prog="python -m app.local_main")
    parser.add_argument("--no-window", action="store_true", help="print the URL instead of opening a window")
    parser.add_argument("--static-dir", type=Path, default=DEFAULT_STATIC_DIR, help="the built UI (frontend/dist)")
    args = parser.parse_args(argv)

    if not (args.static_dir / "index.html").is_file():
        print(f"No built UI in {args.static_dir}. Run `npm run build` in frontend/ first.", file=sys.stderr)
        return 1
    # Checked at launch so a missing browser shows up now, not as a failed first application.
    with sync_playwright() as playwright:
        chromium = Path(playwright.chromium.executable_path)
    if not chromium.exists():
        print("Chromium is missing. Run `python -m playwright install chromium`.", file=sys.stderr)
        return 1
    try:
        instance_lock.acquire()
    except instance_lock.AlreadyRunning as exc:
        print(f"{exc}. Close the other window first.", file=sys.stderr)
        return 1
    db.migrate()
    local_apply.recover()

    token = secrets.token_urlsafe(32)
    app = build_app(token, args.static_dir)
    server, thread, port = start_server(app)
    url = f"http://127.0.0.1:{port}/#token={token}"
    try:
        if args.no_window:
            print(url, flush=True)
            def on_sigterm(signum, frame):
                server.should_exit = True    # the join below returns and the lifespan winds the queues down

            signal.signal(signal.SIGTERM, on_sigterm)
            thread.join()
        else:
            try:
                import webview    # imported here: a headless machine (and the tests) have no GUI toolkit
            except ImportError as exc:
                print(NO_WINDOW_ADVICE.format(error=exc), file=sys.stderr)
                return 1    # the finally below stops the server

            window = webview.create_window("CareerSurge", url)

            def ask(message):
                return window.create_confirmation_dialog("CareerSurge", message)

            def on_closing():
                return close_guard(ask, app.state.stop_queues)

            window.events.closing += on_closing
            try:
                webview.start()    # where pywebview looks for a toolkit; `import webview` succeeds without one
            except webview.WebViewException as exc:
                print(NO_WINDOW_ADVICE.format(error=exc), file=sys.stderr)
                return 1
    except KeyboardInterrupt:
        pass
    finally:
        server.should_exit = True
        thread.join()
    return 0


if __name__ == "__main__":
    sys.exit(main())
