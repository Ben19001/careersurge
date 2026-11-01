"""Per-ATS pacing, the block breaker, pause and stop, the shared browser, the sourcing schedule.

No browser and no network. Time is simulated: `local_queues._sleep` parks the caller on a timer and
a driver task advances the fake clock to the earliest timer once every task is blocked, so a
15-minute back-off costs milliseconds and three queues share one consistent clock. `apply_one` is
replaced by a scripted fake, except in the tests that need its real cancellation and settle logic
(those replace `_attempt`). Chromium is replaced by `_FakePlaywright`.
"""
import asyncio
import datetime
import heapq
import itertools
import sys
import sqlite3
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import conftest  # noqa: E402,F401 - shared sys.path + heavy-dep stubs, installed before app imports
import test_local_apply as tla  # noqa: E402 - its store-backed base class

# conftest stubs `pydantic`, and `apify_client` (imported by local_sourcing, hence local_queues) needs the
# real one. Swap the stub out for the import and put it back, as test_local_sourcing does.
_pydantic_stub = sys.modules.pop("pydantic", None)
try:
    from apify_client.errors import ApifyApiError  # noqa: E402
    from app.services import local_answers, local_apply, local_handoff, local_profile, local_queues, local_sourcing  # noqa: E402
finally:
    if _pydantic_stub is not None:
        sys.modules["pydantic"] = _pydantic_stub

T0 = 1_800_000_000.0
REAL_APPLY_ONE = local_apply.apply_one    # captured before any test patches it
REAL_BUILD_PREPARED = local_profile.build_prepared
# Apify rejecting the token: something the user can fix, so the ingest waits and tries again.
TOKEN_REJECTED = ApifyApiError(SimpleNamespace(status_code=403, text="", json=lambda: {}), 1)


class _FakeContext:
    def __init__(self, browser):
        self.browser = browser

    async def close(self):
        self.browser.open -= 1


class _FakeBrowser:
    def __init__(self, clock):
        self.clock = clock
        self.contexts = self.open = self.max_open = 0
        self.connected, self.closed_at = True, None

    def is_connected(self):
        return self.connected and self.closed_at is None

    async def new_context(self):
        self.contexts += 1
        self.open += 1
        self.max_open = max(self.max_open, self.open)
        return _FakeContext(self)

    async def close(self):
        self.closed_at = self.clock()


class _FakePlaywright:
    """Stands in for `async_playwright()`: `.start()` gives an object whose Chromium launches fake browsers."""

    def __init__(self, clock):
        self.clock, self.launched, self.stopped = clock, [], False
        self.chromium = self

    async def start(self):
        return self

    async def launch(self, **kwargs):
        browser = _FakeBrowser(self.clock)
        self.launched.append(browser)
        return browser

    async def stop(self):
        self.stopped = True


class _QueueCase(tla._StoreTestCase):
    def setUp(self):
        super().setUp()
        self.now = T0
        self.timers, self._seq = [], itertools.count()
        self.calls = []           # (ats, fake time) per fake apply_one
        self.script = {}          # ats -> list of outcomes: "ok", "slow", "blocked"
        self.always_blocked = set()    # application ids whose page is a block page on every visit
        self.uniform_args = []
        self.playwright = _FakePlaywright(lambda: self.now)

        async def fake_sleep(seconds):
            future = asyncio.get_running_loop().create_future()
            heapq.heappush(self.timers, (self.now + seconds, next(self._seq), future))
            await future

        def fake_uniform(low, high):
            self.uniform_args.append((low, high))
            return low + 40

        for target, name, value in ((local_queues, "_clock", lambda: self.now),
                                    (local_queues, "_sleep", fake_sleep),
                                    (local_queues, "POLL_S", 30),
                                    (local_queues.random, "uniform", fake_uniform),
                                    (local_queues, "async_playwright", lambda: self.playwright),
                                    (local_apply, "apply_one", self.fake_apply)):
            patch = mock.patch.object(target, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(setattr, local_queues, "_stop_requested", False)

    async def asyncSetUp(self):
        driver = asyncio.ensure_future(self._drive())
        self.addAsyncCleanup(self._end, driver)

    @staticmethod
    async def _end(driver):
        driver.cancel()
        await asyncio.gather(driver, return_exceptions=True)

    async def _drive(self):
        """Advance the fake clock to the earliest timer once every other task is blocked on one."""
        while True:
            for _ in range(20):
                await asyncio.sleep(0)
            if self.timers:
                wake, _, future = heapq.heappop(self.timers)
                self.now = max(self.now, wake)
                if not future.done():
                    future.set_result(None)
            else:
                await asyncio.sleep(0.001)

    async def fake_apply(self, browser, row):
        """Record the call, open a context like the real one, and settle the row as scripted."""
        self.calls.append((row["ats"], self.now))
        context = await browser.new_context()
        await context.close()
        steps = self.script.get(row["ats"])
        step = "blocked" if row["id"] in self.always_blocked else (steps.pop(0) if steps else "ok")
        if step == "blocked":
            local_apply._transition_application(row["id"], row["claim_token"], "in_progress", "pending",
                                                refund_attempt=True)
        else:
            local_apply._transition_application(row["id"], row["claim_token"], "in_progress", "applied")
        return {"blocked": step == "blocked", "slow": step == "slow"}

    def use_real_apply_one(self):
        patch = mock.patch.object(local_apply, "apply_one", REAL_APPLY_ONE)
        patch.start()
        self.addCleanup(patch.stop)

    async def run_all(self, until):
        await asyncio.wait_for(local_queues.run_all(until), 60)

    def times(self, ats):
        return [at for name, at in self.calls if name == ats]

    def iso(self, seconds_ago):
        stamp = datetime.datetime.fromtimestamp(self.now - seconds_ago, datetime.timezone.utc)
        return stamp.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class CooldownTests(_QueueCase):
    async def test_the_next_claim_waits_the_cooldown_after_the_first_settles(self):
        self.add_profile()
        self.add_row()
        self.add_row()
        await self.run_all(lambda: len(self.calls) >= 2)

        first, second = self.times("greenhouse")
        self.assertEqual(self.uniform_args[0], (60, 180))
        self.assertGreaterEqual(second - first, 100)    # the fake draw is 40 s over the floor
        self.assertLess(second - first, 100 + local_queues.POLL_S)

    async def test_the_floor_setting_sets_the_range_and_never_goes_under_30_seconds(self):
        self.add_profile()
        for setting, expected in ((10, (30, 150)), (None, (60, 180)), (45, (45, 165))):
            with self.subTest(setting=setting):
                local_queues.db.set_setting("apply_cooldown_floor_s", setting)
                self.uniform_args.clear()
                self.calls.clear()
                self.add_row()
                await self.run_all(lambda: len(self.calls) >= 1)
                await asyncio.sleep(0)
                self.assertEqual(self.uniform_args[0], expected)

    async def test_a_slow_page_doubles_the_next_gap_then_it_resets(self):
        self.add_profile()
        for _ in range(3):
            self.add_row()
        self.script["greenhouse"] = ["slow", "ok"]
        await self.run_all(lambda: len(self.calls) >= 3)

        first, second, third = self.times("greenhouse")
        self.assertGreaterEqual(second - first, 200)
        self.assertLess(second - first, 200 + local_queues.POLL_S)
        self.assertGreaterEqual(third - second, 100)
        self.assertLess(third - second, 100 + local_queues.POLL_S)

    async def test_consecutive_slow_pages_stop_doubling_at_four_times(self):
        self.add_profile()
        for _ in range(4):
            self.add_row()
        self.script["greenhouse"] = ["slow", "slow", "slow"]
        await self.run_all(lambda: len(self.calls) >= 4)

        times = self.times("greenhouse")
        gaps = [later - earlier for earlier, later in zip(times, times[1:])]
        self.assertEqual([round(gap) // 10 for gap in gaps], [20, 40, 40])


class BlockBreakerTests(_QueueCase):
    async def test_a_block_requeues_without_spending_an_attempt_and_backs_the_ats_off_15_minutes(self):
        self.add_profile()
        app_id = self.add_row()
        self.script["greenhouse"] = ["blocked"]
        await self.run_all(lambda: len(self.calls) >= 1 and self.row(app_id)["status"] == "pending")

        status = local_queues.queue_status()["greenhouse"]
        self.assertEqual((status["state"], status["pending"]), ("backed_off", 1))
        self.assertEqual(status["next_at"], self.times("greenhouse")[0] + 15 * 60)
        self.assertEqual(self.row(app_id)["attempt_count"], 0)

    async def test_an_ip_wide_block_keeps_all_three_rows_pending_and_pauses_the_ats_while_other_atses_keep_going(self):
        self.add_profile()
        rows = [self.add_row(approved_at=f"2026-10-0{day}T00:00:00.000Z") for day in (1, 2, 3)]
        self.add_row(ats="lever", url="https://jobs.lever.co/a/b")
        # Every greenhouse page is a block, whichever row is claimed.
        self.script["greenhouse"] = ["blocked"] * 6
        await self.run_all(lambda: self.now > T0 + 8000)

        times = self.times("greenhouse")
        self.assertEqual(len(times), 3)    # blocked, 15 min, blocked, 30 min, blocked: paused, no fourth claim
        self.assertGreaterEqual(times[1] - times[0], 15 * 60)
        self.assertLess(times[1] - times[0], 15 * 60 + local_queues.POLL_S)
        self.assertGreaterEqual(times[2] - times[1], 30 * 60)
        self.assertLess(times[2] - times[1], 30 * 60 + local_queues.POLL_S)
        for app_id in rows:
            self.assertEqual((self.row(app_id)["status"], self.row(app_id)["attempt_count"]), ("pending", 0))
        self.assertEqual(local_queues.db.get_setting("apply_paused"), {"greenhouse": "blocked"})
        status = local_queues.queue_status()
        self.assertEqual((status["greenhouse"]["state"], status["greenhouse"]["paused_reason"]),
                         ("paused", "blocked"))
        self.assertEqual(len(self.times("lever")), 1)    # Lever applied its row meanwhile

    async def test_block_normal_block_resets_the_count_so_the_second_block_backs_off_15_minutes_again(self):
        self.add_profile()
        for _ in range(3):
            self.add_row()
        self.script["greenhouse"] = ["blocked", "ok", "blocked", "ok"]
        await self.run_all(lambda: len(self.calls) >= 4)

        t1, t2, t3, t4 = self.times("greenhouse")
        self.assertGreaterEqual(t2 - t1, 15 * 60)
        self.assertGreaterEqual(t4 - t3, 15 * 60)
        self.assertLess(t4 - t3, 30 * 60)
        self.assertIsNone(local_queues.db.get_setting("apply_paused"))

    async def test_a_row_blocked_twice_with_a_sibling_through_between_is_failed_and_the_queue_moves_on(self):
        self.add_profile()
        poison = self.add_row(approved_at="2026-10-01T00:00:00.000Z")
        sibling = self.add_row(approved_at="2026-10-02T00:00:00.000Z")
        later = self.add_row(approved_at="2026-10-03T00:00:00.000Z")
        self.always_blocked.add(poison)
        # The poison row is blocked first and backs the ATS off; the rows behind it then go ahead of it
        # and apply, and when it is finally retried and blocked again, rows have got through in between.
        await self.run_all(lambda: self.row(poison)["status"] == "failed")

        row = self.row(poison)
        self.assertEqual((row["status"], row["skip_reason"], row["error"]),
                         ("failed", "unsupported_form", "blocked_repeatedly"))
        self.assertEqual((self.row(sibling)["status"], self.row(later)["status"]), ("applied", "applied"))
        self.assertIsNone(local_queues.db.get_setting("apply_paused"))

    async def test_a_non_numeric_cooldown_floor_falls_back_to_the_default(self):
        self.add_profile()
        self.add_row()
        for bad in ("soon", float("inf")):
            with self.subTest(floor=bad):
                local_queues.db.set_setting("apply_cooldown_floor_s", bad)
                self.uniform_args.clear()
                self.calls.clear()
                local_queues._state["greenhouse"].next_at = 0.0
                self.add_row()
                await self.run_all(lambda: len(self.calls) >= 1)
                await asyncio.sleep(0)
                self.assertEqual(self.uniform_args[0], (60, 180))

    async def test_resume_clears_the_pause_and_the_breaker(self):
        local_queues._state["lever"].blocks = 2
        local_queues._state["lever"].backoff_until = T0 + 999
        local_queues.pause()
        self.assertEqual(local_queues.db.get_setting("apply_paused"),
                         {"greenhouse": "user", "lever": "user", "ashby": "user"})

        local_queues.resume("lever")

        self.assertEqual(local_queues.db.get_setting("apply_paused"), {"greenhouse": "user", "ashby": "user"})
        self.assertEqual((local_queues._state["lever"].blocks, local_queues._state["lever"].backoff_until), (0, 0.0))
        with self.assertRaises(ValueError):
            local_queues.pause("workday")


class PauseAndStopTests(_QueueCase):
    async def test_a_paused_ats_survives_a_restart_until_resume(self):
        self.add_profile()
        gh = self.add_row()
        lever = self.add_row(ats="lever", url="https://jobs.lever.co/a/b")
        local_queues.pause("lever")

        await self.run_all(lambda: self.now > T0 + 120)    # "restart": run_all builds fresh state
        self.assertEqual((self.row(gh)["status"], self.row(lever)["status"]), ("applied", "pending"))
        self.assertEqual(local_queues.queue_status()["lever"]["state"], "paused")

        local_queues.resume("lever")
        await self.run_all(lambda: self.row(lever)["status"] == "applied")
        self.assertEqual(self.row(lever)["status"], "applied")

    async def test_stop_all_cancels_an_in_progress_row_and_lets_a_submitting_row_finish(self):
        self.add_profile()
        waiting = self.add_row()
        submitting = self.add_row(ats="lever", url="https://jobs.lever.co/a/b")
        self.use_real_apply_one()

        async def attempt(browser, row, progress):
            if row["ats"] == "greenhouse":
                await asyncio.sleep(3600)
            local_apply._transition_application(row["id"], row["claim_token"], "in_progress", "submitting",
                                                stamp="submit_attempted_at")
            progress["status"] = "submitting"
            await asyncio.sleep(0.3)
            local_apply._transition_application(row["id"], row["claim_token"], "submitting", "applied",
                                                stamp="confirmed_at")
        run = asyncio.ensure_future(local_queues.run_all(lambda: False))
        with mock.patch.object(local_apply, "_attempt", attempt):
            while (self.row(waiting)["status"], self.row(submitting)["status"]) != ("in_progress", "submitting"):
                await asyncio.sleep(0.01)
            await asyncio.wait_for(local_queues.stop_all(), 10)
            await asyncio.wait_for(run, 10)

        row = self.row(waiting)
        self.assertEqual((row["status"], row["attempt_count"]), ("pending", 0))
        self.assertEqual(self.row(submitting)["status"], "applied")
        self.assertNotIn("running", {s["state"] for s in local_queues.queue_status().values()})

    async def test_stop_all_on_idle_queues_returns_and_run_all_ends(self):
        self.add_profile()
        run = asyncio.ensure_future(local_queues.run_all(lambda: False))
        await asyncio.sleep(0.05)
        await asyncio.wait_for(local_queues.stop_all(), 10)
        await asyncio.wait_for(run, 10)
        self.assertTrue(all(loop.done() for loop in local_queues._loops))


class QueueLoopTests(_QueueCase):
    async def test_without_a_profile_nothing_is_claimed_and_no_browser_is_launched(self):
        app_id = self.add_row()
        await self.run_all(lambda: self.now > T0 + 120)
        self.assertEqual(self.row(app_id)["status"], "pending")
        self.assertEqual(self.playwright.launched, [])

    async def test_a_raising_row_is_re_claimed_at_most_three_times(self):
        self.add_profile()
        app_id = self.add_row()
        self.use_real_apply_one()
        opener = mock.AsyncMock(side_effect=RuntimeError("boom"))
        with mock.patch.object(local_apply, "_open_form", opener):
            await self.run_all(lambda: self.row(app_id)["status"] == "blocked_needs_input")
        self.assertEqual(opener.await_count, 3)
        self.assertEqual(self.row(app_id)["blocked_reason"], "attempts_exhausted")

    async def test_a_failed_settle_write_is_logged_and_the_queue_moves_on(self):
        self.add_profile()
        first, second = self.add_row(), self.add_row()
        self.use_real_apply_one()
        real_transition = local_apply._transition_application

        def busy_on_settle(app_id, token, from_status, to_status, **columns):
            if to_status == "pending":     # the settle write after an exception
                raise RuntimeError("database is locked")
            return real_transition(app_id, token, from_status, to_status, **columns)
        with mock.patch.object(local_apply, "_open_form", mock.AsyncMock(side_effect=RuntimeError("boom"))), \
                mock.patch.object(local_apply, "_transition_application", busy_on_settle), \
                self.assertLogs("careersurge.services.local_apply", "ERROR"):
            await self.run_all(lambda: self.row(first)["status"] == self.row(second)["status"] == "in_progress")
        self.assertEqual({self.row(first)["status"], self.row(second)["status"]}, {"in_progress"})

    async def test_three_busy_queues_share_one_chromium_with_three_contexts_open_at_once(self):
        self.add_profile()
        self.add_row()
        self.add_row(ats="lever", url="https://jobs.lever.co/a/b")
        self.add_row(ats="ashby", url="https://jobs.ashbyhq.com/a/b")
        arrived, release = [], asyncio.Event()

        async def hold_a_context(browser, row):
            context = await browser.new_context()
            arrived.append(row["ats"])
            if len(arrived) == 3:
                release.set()
            await release.wait()
            await context.close()
            local_apply._transition_application(row["id"], row["claim_token"], "in_progress", "applied")
            return {"blocked": False, "slow": False}
        with mock.patch.object(local_apply, "apply_one", hold_a_context):
            await self.run_all(lambda: len(arrived) >= 3 and release.is_set())

        (browser,) = self.playwright.launched
        self.assertEqual((browser.contexts, browser.max_open), (3, 3))
        self.assertTrue(self.playwright.stopped)

    async def test_a_context_parked_for_a_code_keeps_its_slot_so_a_sixth_job_waits(self):
        self.add_profile()
        parked_id = self.add_row()
        self.add_row(ats="lever", url="https://jobs.lever.co/a/b")
        freed = []

        async def park_the_first(browser, row):
            self.calls.append((row["ats"], self.now))
            local_apply._transition_application(row["id"], row["claim_token"], "in_progress", "applied")
            if row["ats"] == "greenhouse":
                local_handoff._parked[row["id"]] = {"phase": "waiting"}    # parked: its slot stays taken
            return {"blocked": False, "slow": False}

        async def free_the_slot_later():
            while not self.calls:
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.3)
            freed.append(list(self.calls))     # what had been applied while the slot was held
            local_handoff._parked.pop(parked_id)
            local_handoff.release_context_slot()

        with mock.patch.object(local_handoff, "MAX_CONTEXTS", 1), \
                mock.patch.object(local_apply, "apply_one", park_the_first):
            freeing = asyncio.ensure_future(free_the_slot_later())
            await self.run_all(lambda: len(self.calls) >= 2)
            await freeing
        self.assertEqual([ats for ats, _ in freed[0]], ["greenhouse"])
        self.assertEqual(sorted(ats for ats, _ in self.calls), ["greenhouse", "lever"])

    async def test_the_browser_closes_after_five_idle_minutes_and_relaunches_when_it_disconnects(self):
        self.add_profile()
        self.add_row()
        await self.run_all(lambda: self.now > T0 + 400)
        (browser,) = self.playwright.launched
        self.assertGreaterEqual(browser.closed_at - T0, local_queues.BROWSER_IDLE_S)
        self.assertLess(browser.closed_at, T0 + 400)

        self.playwright.launched.clear()
        self.add_row()
        self.add_row()

        async def crash_after_first(browser, row):
            outcome = await self.fake_apply(browser, row)
            browser.connected = False
            return outcome
        with mock.patch.object(local_apply, "apply_one", crash_after_first):
            await self.run_all(lambda: len(self.calls) >= 3)
        self.assertEqual(len(self.playwright.launched), 2)

    async def test_blocked_rows_are_expired_at_start_and_then_every_ten_minutes(self):
        stamps = []
        with mock.patch.object(local_answers, "expire_blocked", lambda: stamps.append(self.now)):
            await self.run_all(lambda: self.now > T0 + 1500)
        self.assertEqual(len(stamps), 3)
        self.assertGreaterEqual(stamps[1] - stamps[0], local_queues.EXPIRE_EVERY_S)

    async def test_queue_status_reports_running_cooling_and_pending(self):
        self.add_profile()
        self.add_row()
        self.add_row()
        await self.run_all(lambda: len(self.calls) >= 1)
        status = local_queues.queue_status()["greenhouse"]
        self.assertEqual((status["state"], status["pending"]), ("cooling", 1))
        self.assertEqual(status["next_at"], self.times("greenhouse")[0] + 100)
        self.assertEqual(local_queues.queue_status()["ashby"]["state"], "idle")


class SourcingScheduleTests(_QueueCase):
    def setUp(self):
        super().setUp()
        self.events = []
        for target, name, value in (
                (local_sourcing, "start_run", lambda confirm_backlog=False: self.events.append("start") or "run-1"),
                (local_sourcing, "ingest_run", lambda run_id, should_stop=None: self.events.append("ingest")),
                (local_profile, "build_prepared", lambda: self.events.append("prepare") or 7)):
            patch = mock.patch.object(target, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        # What `fail_run` reads: the run's bill on the user's Apify account.
        finished_run = SimpleNamespace(status="SUCCEEDED", usage_total_usd=0.4)
        apify = SimpleNamespace(run=lambda run_id: SimpleNamespace(get=lambda: finished_run))
        client = mock.patch.object(local_sourcing, "_apify_client", lambda: apify)
        client.start()
        self.addCleanup(client.stop)
        local_queues._sourcing_tasks.clear()
        self.addCleanup(local_queues._sourcing_tasks.clear)

    async def ingests_done(self):
        await asyncio.gather(*local_queues._sourcing_tasks)

    def started_run(self):
        self.conn.execute("INSERT INTO sourcing_runs (apify_run_id, started_at, status) VALUES ('run-1', ?, 'started')",
                          (self.iso(0),))

    def last_run(self, seconds_ago):
        self.conn.execute("INSERT INTO sourcing_runs (apify_run_id, started_at, status) VALUES ('r', ?, 'succeeded')",
                          (self.iso(seconds_ago),))

    async def schedule(self, minutes=10):
        deadline = self.now + minutes * 60
        await asyncio.wait_for(local_queues.run_sourcing_schedule(lambda: self.now > deadline), 60)
        await self.ingests_done()    # the schedule only starts the run; its ingest goes on in a task

    async def test_a_run_five_hours_ago_is_due_at_the_four_hour_interval_and_prepares_afterwards(self):
        self.last_run(5 * 3600)
        await self.schedule()
        self.assertEqual(self.events, ["start", "ingest", "prepare"])

    async def test_a_run_one_hour_ago_is_not_due_and_neither_is_manual(self):
        self.last_run(3600)
        await self.schedule()
        local_queues.db.set_setting("sourcing_interval", "manual")
        self.conn.execute("DELETE FROM sourcing_runs")
        await self.schedule()
        self.assertEqual(self.events, [])

    async def test_the_hourly_interval_runs_after_an_hour_and_a_first_ever_run_is_due(self):
        local_queues.db.set_setting("sourcing_interval", "1h")
        self.last_run(3700)
        await self.schedule()
        self.assertEqual(self.events, ["start", "ingest", "prepare"])

        self.events.clear()
        self.conn.execute("DELETE FROM sourcing_runs")
        local_queues.db.set_setting("sourcing_interval", "4h")
        await self.schedule()
        self.assertEqual(self.events, ["start", "ingest", "prepare"])

    async def test_a_refused_run_is_stored_not_raised_and_not_retried_within_the_interval_or_after_a_restart(self):
        def refuse(confirm_backlog=False):
            self.events.append("start")
            raise local_sourcing.SourcingRefused("budget", remaining=1.5, minimal=2.0)
        with mock.patch.object(local_sourcing, "start_run", refuse):
            await self.schedule(minutes=60)
            await self.schedule(minutes=60)    # a "restart": a fresh loop reads the stored notice

        refusal = local_queues.db.get_setting("sourcing_last_refusal")
        self.assertEqual((refusal["reason"], refusal["detail"]), ("budget", {"remaining": 1.5, "minimal": 2.0}))
        self.assertTrue(refusal["at"].endswith("Z"))
        self.assertEqual(self.events, ["start"])

    async def test_start_sourcing_returns_the_run_id_before_the_ingest_ends_and_clears_the_notice(self):
        local_queues.db.set_setting("sourcing_last_refusal", {"reason": "budget", "detail": {}, "at": self.iso(0)})
        release = threading.Event()
        started_ingest = threading.Event()

        def slow_ingest(run_id, should_stop=None):
            started_ingest.set()
            release.wait(10)
            self.events.append("ingest")

        with mock.patch.object(local_sourcing, "ingest_run", slow_ingest):
            run_id = await local_queues.start_sourcing(confirm_backlog=True)
            self.assertEqual(run_id, "run-1")
            self.assertEqual(self.events, ["start"])    # answered while Apify is still "running"
            self.assertIsNone(local_queues.db.get_setting("sourcing_last_refusal"))
            release.set()
            await self.ingests_done()
        self.assertEqual(self.events, ["start", "ingest", "prepare"])

    async def test_start_sourcing_still_returns_the_run_id_when_the_old_notice_cannot_be_cleared(self):
        with mock.patch.object(local_queues.db, "set_setting",
                               side_effect=sqlite3.OperationalError("database is locked")):
            with self.assertLogs("careersurge.services.local_queues", "WARNING"):
                run_id = await local_queues.start_sourcing()
        await self.ingests_done()
        self.assertEqual(run_id, "run-1")
        self.assertEqual(len(local_queues._sourcing_tasks), 1)    # the paid run is still being ingested

    async def test_start_sourcing_lets_a_refusal_through_and_starts_no_task(self):
        with mock.patch.object(local_sourcing, "start_run",
                               side_effect=local_sourcing.SourcingRefused("no_key")):
            with self.assertRaises(local_sourcing.SourcingRefused) as raised:
                await local_queues.start_sourcing()
        self.assertEqual(raised.exception.reason, "no_key")
        self.assertEqual(local_queues._sourcing_tasks, [])

    async def test_a_failing_ingest_stores_the_reason_waits_2_then_5_minutes_and_retries_until_it_returns(self):
        self.started_run()
        failures = [TOKEN_REJECTED,
                    local_sourcing.SourcingRefused("tls", message="Secure connection to Apify failed")]
        tries = []

        def flaky_ingest(run_id, should_stop=None):
            conn = local_queues.db.get_db()    # this runs in a worker thread: not the test's connection
            try:
                run_status = conn.execute("SELECT status FROM sourcing_runs").fetchone()[0]
                tries.append((self.now, local_queues.db.get_setting("sourcing_last_refusal"), run_status))
                if failures:
                    raise failures.pop(0)
                conn.execute("INSERT INTO listings (source, external_id, url, company_key) "
                             "VALUES ('greenhouse', '9', 'https://boards.greenhouse.io/acme/jobs/9', 'acme')")
            finally:
                conn.close()
            return {"ingested": 1, "skipped": 0}

        with mock.patch.object(local_sourcing, "ingest_run", flaky_ingest), \
                mock.patch.object(local_profile, "build_prepared", REAL_BUILD_PREPARED), \
                self.assertLogs("careersurge.services.local_queues", "WARNING"):
            await local_queues.start_sourcing()
            await self.ingests_done()

        (first_at, first_refusal, _), (second_at, second_refusal, _), (third_at, third_refusal, _) = tries
        self.assertIsNone(first_refusal)                              # start_sourcing cleared the old notice
        self.assertEqual((second_refusal["reason"], second_refusal["detail"]["message"]),
                         ("ingest_failed", str(TOKEN_REJECTED)[:200]))
        retry_at = datetime.datetime.fromisoformat(second_refusal["detail"]["retry_at"].replace("Z", "+00:00"))
        self.assertEqual(retry_at.timestamp(), first_at + 2 * 60)    # the refusal says when the next try is
        self.assertGreaterEqual(second_at - first_at, 2 * 60)
        self.assertLess(second_at - first_at, 2 * 60 + local_queues.POLL_S)
        self.assertEqual((third_refusal["reason"], third_refusal["detail"]["message"]),
                         ("tls", "Secure connection to Apify failed"))
        self.assertGreaterEqual(third_at - second_at, 5 * 60)
        self.assertLess(third_at - second_at, 5 * 60 + local_queues.POLL_S)
        self.assertEqual({status for _, _, status in tries}, {"started"})    # never marked failed while it retries

        self.assertIsNone(local_queues.db.get_setting("sourcing_last_refusal"))    # cleared on success
        statuses = [row[0] for row in self.conn.execute("SELECT status FROM job_applications")]
        self.assertEqual(statuses, ["prepared"])
        self.assertEqual(self.conn.execute("SELECT status FROM sourcing_runs").fetchone()[0], "started")

    async def test_the_retry_waits_stop_growing_at_15_minutes(self):
        tries = []

        def failing_ingest(run_id, should_stop=None):
            tries.append(self.now)
            if len(tries) < 5:
                raise TOKEN_REJECTED
            return {"ingested": 0, "skipped": 0}

        with mock.patch.object(local_sourcing, "ingest_run", failing_ingest), \
                self.assertLogs("careersurge.services.local_queues", "WARNING"):
            await local_queues.start_sourcing()
            await self.ingests_done()

        gaps_in_minutes = [round((later - earlier) / 60) for earlier, later in zip(tries, tries[1:])]
        self.assertEqual(gaps_in_minutes, [2, 5, 15, 15])
        self.assertEqual(self.events, ["start", "prepare"])

    async def test_a_quit_during_a_retry_wait_returns_within_poll_s_and_leaves_the_notice(self):
        tries = []

        def failing_ingest(run_id, should_stop=None):
            tries.append(self.now)
            raise TOKEN_REJECTED

        with mock.patch.object(local_sourcing, "ingest_run", failing_ingest), \
                self.assertLogs("careersurge.services.local_queues", "WARNING"):
            await local_queues.start_sourcing()
            while not local_queues.db.get_setting("sourcing_last_refusal"):
                await asyncio.sleep(0.001)
            quit_began = self.now
            ingest_tasks = list(local_queues._sourcing_tasks)
            await local_queues.stop_all()

        self.assertLessEqual(self.now - quit_began, local_queues.POLL_S)    # not the 2 minutes of the wait
        self.assertTrue(ingest_tasks and all(task.done() for task in ingest_tasks))
        self.assertEqual(len(tries), 1)
        self.assertNotIn("prepare", self.events)
        self.assertEqual(local_queues.db.get_setting("sourcing_last_refusal")["reason"], "ingest_failed")

    async def test_the_schedule_does_not_replace_the_retry_notice_with_run_in_progress(self):
        retry_notice = {"reason": "ingest_failed", "detail": {"message": "down", "retry_at": self.iso(0)},
                        "at": self.iso(5 * 3600)}
        local_queues.db.set_setting("sourcing_last_refusal", retry_notice)

        def still_running(confirm_backlog=False):
            raise local_sourcing.SourcingRefused("run_in_progress")
        with mock.patch.object(local_sourcing, "start_run", still_running):
            await self.schedule()

        self.assertEqual(local_queues.db.get_setting("sourcing_last_refusal"), retry_notice)

    async def test_the_schedule_stores_run_in_progress_when_no_retry_notice_explains_the_wait(self):
        def still_running(confirm_backlog=False):
            raise local_sourcing.SourcingRefused("run_in_progress")
        with mock.patch.object(local_sourcing, "start_run", still_running):
            await self.schedule()

        self.assertEqual(local_queues.db.get_setting("sourcing_last_refusal")["reason"], "run_in_progress")

    async def test_an_error_the_user_cannot_fix_fails_the_run_records_the_bill_and_is_not_retried(self):
        self.started_run()
        tries = []

        def crashing_ingest(run_id, should_stop=None):
            tries.append(self.now)
            raise RuntimeError("a" * 300)

        with mock.patch.object(local_sourcing, "ingest_run", crashing_ingest), \
                self.assertLogs("careersurge.services.local_queues", "ERROR"):
            await local_queues.start_sourcing()
            await self.ingests_done()

        self.assertEqual(len(tries), 1)    # no second try, however long it waits
        run = self.conn.execute("SELECT status, cost_usd FROM sourcing_runs").fetchone()
        self.assertEqual(tuple(run), ("failed", 0.4))    # the bill stays in the monthly tally
        refusal = local_queues.db.get_setting("sourcing_last_refusal")
        self.assertEqual((refusal["reason"], refusal["detail"]), ("ingest_failed", {"message": "a" * 200}))
        self.assertEqual(self.events, ["start"])    # nothing was prepared

    async def test_a_relaunch_clears_the_notice_a_quit_left_once_the_resumed_ingest_succeeds(self):
        self.started_run()
        local_queues.db.set_setting("sourcing_last_refusal", {
            "reason": "ingest_failed", "detail": {"message": "locked", "retry_at": self.iso(0)}, "at": self.iso(60)})

        await local_queues.resume_sourcing()
        await self.ingests_done()

        self.assertEqual(self.events, ["ingest", "prepare"])    # start_run is not called
        self.assertIsNone(local_queues.db.get_setting("sourcing_last_refusal"))

    async def test_a_relaunch_whose_ingest_fails_keeps_retrying_with_the_notice(self):
        self.started_run()
        tries = []

        def flaky_ingest(run_id, should_stop=None):
            tries.append((self.now, local_queues.db.get_setting("sourcing_last_refusal")))
            if len(tries) < 3:
                raise TOKEN_REJECTED
            return {"ingested": 0, "skipped": 0}

        with mock.patch.object(local_sourcing, "ingest_run", flaky_ingest), \
                self.assertLogs("careersurge.services.local_queues", "WARNING"):
            await local_queues.resume_sourcing()
            await self.ingests_done()

        gaps_in_minutes = [round((later[0] - earlier[0]) / 60) for earlier, later in zip(tries, tries[1:])]
        self.assertEqual(gaps_in_minutes, [2, 5])
        self.assertIn("retry_at", tries[1][1]["detail"])
        self.assertEqual(self.conn.execute("SELECT status FROM sourcing_runs").fetchone()[0], "started")
        self.assertIsNone(local_queues.db.get_setting("sourcing_last_refusal"))
        self.assertEqual(self.events, ["prepare"])

    async def test_a_relaunch_whose_ingest_cannot_be_fixed_fails_the_row_and_prepares_nothing(self):
        self.started_run()

        def crashing_ingest(run_id, should_stop=None):
            raise RuntimeError("could not map the listing")

        with mock.patch.object(local_sourcing, "ingest_run", crashing_ingest), \
                self.assertLogs("careersurge.services.local_queues", "ERROR"):
            await local_queues.resume_sourcing()
            await self.ingests_done()

        self.assertEqual(self.conn.execute("SELECT status FROM sourcing_runs").fetchone()[0], "failed")
        refusal = local_queues.db.get_setting("sourcing_last_refusal")
        self.assertEqual((refusal["reason"], refusal["detail"]), ("ingest_failed", {"message": "could not map the listing"}))
        self.assertEqual(self.events, [])

    async def test_a_missing_key_keeps_its_own_notice_and_is_retried(self):
        self.started_run()
        tries = []

        def keyless_ingest(run_id, should_stop=None):
            tries.append(self.now)
            if len(tries) == 1:
                raise local_sourcing.SourcingRefused("no_key")
            return {"ingested": 0, "skipped": 0}

        noticed = []
        real_store = local_queues._store_refusal
        with mock.patch.object(local_sourcing, "ingest_run", keyless_ingest), \
                mock.patch.object(local_queues, "_store_refusal",
                                  lambda refused, retry_in_s=None: noticed.append((refused.reason, retry_in_s)) or real_store(refused, retry_in_s)), \
                self.assertLogs("careersurge.services.local_queues", "WARNING"):
            await local_queues.resume_sourcing()
            await self.ingests_done()

        self.assertEqual(noticed, [("no_key", 120)])
        self.assertEqual(len(tries), 2)
        self.assertEqual(self.conn.execute("SELECT status FROM sourcing_runs").fetchone()[0], "started")

    async def test_a_relaunch_with_no_started_run_only_builds_the_prepared_rows(self):
        await local_queues.resume_sourcing()
        self.assertEqual(self.events, ["prepare"])

    async def test_stop_all_waits_for_the_ingest_task(self):
        finished = []

        def ingest(run_id, should_stop=None):
            while not should_stop():
                time.sleep(0.01)
            finished.append("ingest")

        with mock.patch.object(local_sourcing, "ingest_run", ingest):
            await local_queues.start_sourcing()
            ingest_tasks = list(local_queues._sourcing_tasks)
            await local_queues.stop_all()
        self.assertEqual(finished, ["ingest"])
        self.assertTrue(ingest_tasks and all(task.done() for task in ingest_tasks))


if __name__ == "__main__":
    unittest.main()
