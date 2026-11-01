"""The launcher's startup order, single instance, lifespan and close-window guard.

Runs against a throwaway data dir. uvicorn serves on a real 127.0.0.1 port and the requests go over
a real socket (`urllib`), but the queues, Chromium and Apify are replaced by recorders: nothing here
launches a browser or leaves the machine.
"""
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import time
import types
import unittest
import urllib.error
import urllib.request
from unittest import mock

import conftest  # noqa: E402,F401 - shared sys.path + heavy-dep stubs, installed before app imports
import test_local_apply as tla  # noqa: E402 - the store-backed base class

from app.store import instance_lock  # noqa: E402

_STUBBED = {}
local_main = local_queues = local_sourcing = local_apply = local_profile = None


def setUpModule():
    """The launcher needs real FastAPI and pydantic, which `conftest` stubs suite-wide (see test_local_api)."""
    global local_main, local_queues, local_sourcing, local_apply, local_profile
    for name in ("httpx", "pydantic"):
        if name in sys.modules:
            _STUBBED[name] = sys.modules.pop(name)
    try:
        from app import local_main as launcher
        from app.services import (local_apply as apply_module, local_profile as profile, local_queues as queues,
                                  local_sourcing as sourcing)
    except ImportError as exc:
        sys.modules.update(_STUBBED)
        raise unittest.SkipTest(f"the launcher's dependencies are not installed: {exc}")
    local_main, local_queues, local_sourcing, local_apply, local_profile = launcher, queues, sourcing, apply_module, profile


def tearDownModule():
    sys.modules.update(_STUBBED)


def chromium_at(path):
    """Patch the launcher's Chromium check to look for the browser at `path`, without starting Playwright."""
    playwright = mock.MagicMock()
    playwright.__enter__.return_value.chromium.executable_path = str(path)
    return mock.patch.object(local_main, "sync_playwright", return_value=playwright)


def fetch(url, token=None):
    """GET `url`; returns `(status, body)` without raising on an HTTP error."""
    request = urllib.request.Request(url, headers={"X-CareerSurge-Token": token} if token else {})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()


class MainTests(tla._StoreTestCase):
    def test_a_second_launch_says_already_running_and_exits_1(self):
        first = instance_lock.acquire()
        self.addCleanup(first.release)
        self.addCleanup(setattr, instance_lock, "_held", None)
        (self.tmp / "index.html").write_text("<div id=root></div>")
        err = io.StringIO()
        with mock.patch.object(local_apply, "recover") as recover, chromium_at(sys.executable), \
                contextlib.redirect_stderr(err):
            code = local_main.main(["--no-window", "--static-dir", str(self.tmp)])

        self.assertEqual(code, 1)
        self.assertIn("already running", err.getvalue())
        recover.assert_not_called()

    def test_startup_order_is_lock_migrate_recover_then_serve_with_the_token_in_the_fragment(self):
        order = []
        stopped = mock.Mock()
        (self.tmp / "index.html").write_text("<div id=root></div>")

        class _Server:
            started = True
            should_exit = False

        def serve(app):
            order.append("serve")
            return _Server(), mock.Mock(join=lambda: order.append("joined")), 4321

        out = io.StringIO()
        with mock.patch.object(instance_lock, "acquire", lambda: order.append("lock")), \
                mock.patch.object(local_main.db, "migrate", lambda: order.append("migrate")), \
                mock.patch.object(local_apply, "recover", lambda: order.append("recover")), \
                mock.patch.object(local_main, "build_app", lambda token, static: order.append(("app", len(token)))), \
                mock.patch.object(local_main, "start_server", serve), \
                mock.patch.object(local_main.signal, "signal", stopped), \
                chromium_at(sys.executable), \
                contextlib.redirect_stdout(out):
            code = local_main.main(["--no-window", "--static-dir", str(self.tmp)])

        self.assertEqual(code, 0)
        self.assertEqual(order[:3], ["lock", "migrate", "recover"])
        self.assertEqual(order[3][0], "app")
        self.assertGreaterEqual(order[3][1], 43)       # token_urlsafe(32): 32 random bytes
        self.assertEqual(order[4], "serve")
        self.assertIn("joined", order[5:])      # the server thread is waited on, then again after shutdown
        printed = out.getvalue().strip()
        self.assertRegex(printed, r"^http://127\.0\.0\.1:4321/#token=[A-Za-z0-9_-]{43,}$")

    def test_the_server_serves_the_page_without_a_token_the_api_with_one_and_runs_the_queues(self):
        static = self.tmp / "dist"
        static.mkdir()
        (static / "index.html").write_text("<div id=root></div>")
        calls = []

        async def run_all(should_stop=lambda: False):
            calls.append("run_all")
            while not local_queues._stop_requested:
                await local_queues.asyncio.sleep(0.01)

        async def run_schedule(should_stop=lambda: False):
            calls.append("schedule")

        async def stop_all():
            calls.append("stop_all")
            local_queues._stop_requested = True

        with mock.patch.object(local_queues, "run_all", run_all), \
                mock.patch.object(local_queues, "run_sourcing_schedule", run_schedule), \
                mock.patch.object(local_queues, "stop_all", stop_all), \
                mock.patch.object(local_profile, "build_prepared", lambda: calls.append("build_prepared")):
            app = local_main.build_app("tok", static)
            server, thread, port = local_main.start_server(app)
            base = f"http://127.0.0.1:{port}"
            self.assertEqual(fetch(f"{base}/"), (200, "<div id=root></div>"))
            self.assertEqual(fetch(f"{base}/api/blocked")[0], 401)
            status, body = fetch(f"{base}/api/blocked", "tok")
            self.assertEqual((status, json.loads(body)), (200, {"ok": True, "blocked": []}))
            deadline = time.monotonic() + 3
            while len(calls) < 3 and time.monotonic() < deadline:
                time.sleep(0.02)
            server.should_exit = True
            thread.join(10)

        self.assertEqual(sorted(calls[:3]), ["build_prepared", "run_all", "schedule"])    # no started run: the launch only builds
        self.assertIn("stop_all", calls)


class MissingUiTests(unittest.TestCase):
    def test_a_missing_build_says_to_build_it_and_exits_1_before_taking_the_lock(self):
        err = io.StringIO()
        with mock.patch.object(instance_lock, "acquire") as acquire, contextlib.redirect_stderr(err):
            code = local_main.main(["--no-window", "--static-dir", "/nonexistent/dist"])
        self.assertEqual(code, 1)
        self.assertIn("npm run build", err.getvalue())
        acquire.assert_not_called()


class LaunchCheckTests(unittest.TestCase):
    """A missing Chromium or window toolkit ends the launch with what to install, instead of a traceback."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.static = tmp.name
        with open(f"{self.static}/index.html", "w") as page:
            page.write("<div id=root></div>")

    def launch_with_window(self, webview_module):
        """Run `main` with a window and `webview_module` as `import webview`; returns `(code, stderr, server)`.

        The lock, migrations, recovery and server are recorders, so nothing touches a data dir or a port.
        """
        server = types.SimpleNamespace(should_exit=False)
        err = io.StringIO()
        with mock.patch.object(instance_lock, "acquire"), \
                mock.patch.object(local_main.db, "migrate"), \
                mock.patch.object(local_apply, "recover"), \
                mock.patch.object(local_main, "build_app"), \
                mock.patch.object(local_main, "start_server", lambda app: (server, mock.Mock(), 4321)), \
                mock.patch.dict(sys.modules, {"webview": webview_module}), \
                chromium_at(sys.executable), \
                contextlib.redirect_stderr(err):
            code = local_main.main(["--static-dir", self.static])
        return code, err.getvalue(), server

    def test_a_missing_chromium_says_how_to_install_it_and_exits_1_before_taking_the_lock(self):
        err = io.StringIO()
        with mock.patch.object(instance_lock, "acquire") as acquire, \
                chromium_at(f"{self.static}/no-such-chrome"), \
                contextlib.redirect_stderr(err):
            code = local_main.main(["--no-window", "--static-dir", self.static])
        self.assertEqual(code, 1)
        self.assertIn("python -m playwright install chromium", err.getvalue())
        acquire.assert_not_called()

    def test_no_pywebview_says_what_to_install_and_offers_no_window(self):
        code, err, server = self.launch_with_window(None)    # a None entry makes `import webview` raise ImportError
        self.assertEqual(code, 1)
        self.assertIn("pywebview[qt]", err)
        self.assertIn("--no-window", err)
        self.assertTrue(server.should_exit)

    def test_no_gui_toolkit_at_webview_start_says_what_to_install_and_stops_the_server(self):
        webview = types.ModuleType("webview")

        class WebViewException(Exception):
            pass

        def start():
            raise WebViewException("You must have either QT or GTK with Python extensions installed")

        webview.WebViewException = WebViewException
        webview.create_window = lambda title, url: mock.MagicMock()
        webview.start = start
        code, err, server = self.launch_with_window(webview)
        self.assertEqual(code, 1)
        self.assertIn("QT or GTK", err)
        self.assertIn("--no-window", err)
        self.assertTrue(server.should_exit)


class CloseGuardTests(tla._StoreTestCase):
    def setUp(self):
        super().setUp()
        self.add_profile()

    def test_nothing_running_closes_without_asking(self):
        asked = []
        self.add_row(status="pending", approved_at="2026-10-01T00:00:00.000Z")
        self.assertTrue(local_main.close_guard(asked.append, lambda: asked.append("stop")))
        self.assertEqual(asked, [])

    def test_one_running_asks_and_a_no_keeps_the_window_open(self):
        self.add_row(status="in_progress", claim_token="t")
        asked, stopped = [], []
        keep_open = local_main.close_guard(lambda message: asked.append(message) or False, lambda: stopped.append(1))
        self.assertFalse(keep_open)
        self.assertEqual(asked, ["1 application in progress. Quit when they reach a safe point?"])
        self.assertEqual(stopped, [])

    def test_a_yes_stops_the_queues_then_closes_and_the_count_is_plural(self):
        self.add_row(status="in_progress", claim_token="a")
        self.add_row(status="submitting", claim_token="b")
        asked, stopped = [], []
        closes = local_main.close_guard(lambda message: asked.append(message) or True, lambda: stopped.append(1))
        self.assertTrue(closes)
        self.assertEqual(asked, ["2 applications in progress. Quit when they reach a safe point?"])
        self.assertEqual(stopped, [1])


class NoSupabaseImportTests(unittest.TestCase):
    """The local app must import and launch on a machine without the `supabase` package."""

    def test_local_main_imports_with_supabase_blocked(self):
        """Runs in a fresh interpreter so the suite's own stubs and already-imported modules cannot hide a regression.

        A `None` entry in `sys.modules` makes `import supabase` raise ImportError, which is how a
        machine without the package behaves. Exit code 3 means a different dependency is missing
        (skip); any other failure is the regression this test exists for.
        """
        script = (
            "import sys\n"
            "try:\n"
            "    import fastapi, uvicorn, playwright.async_api, impit, apify_client\n"
            "except ImportError as exc:\n"
            "    print(exc); sys.exit(3)\n"
            "sys.modules['supabase'] = None\n"
            "sys.modules['app.supabase_client'] = None\n"
            "import app.local_main\n"
            "assert 'app.services.common' not in sys.modules, 'local code imported the hosted common module'\n"
        )
        result = subprocess.run([sys.executable, "-c", script], cwd=conftest.BACKEND, capture_output=True, text=True)

        if result.returncode == 3:
            self.skipTest(f"the launcher's dependencies are not installed: {result.stdout.strip()}")
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
