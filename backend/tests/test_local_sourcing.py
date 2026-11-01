"""Local Apify sourcing - guards, run lifecycle, ingest, migration 0002.

Each test runs against a throwaway data dir via `CAREERSURGE_DATA_DIR`. The Apify client is
rebound at one seam, `local_sourcing._apify_client`, to `FakeApify` below; nothing touches the
network, the keychain or the real data dir.
"""
import copy
import datetime
import json
import logging
import os
import sqlite3
import ssl
import sys
import tempfile
import threading
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import conftest  # noqa: E402,F401 - shared sys.path + heavy-dep stubs, installed before app imports
import impit  # noqa: E402

# conftest stubs `pydantic`, and `apify_client` needs the real one. Swap the stub out for the
# imports below and put it back, so every other test module still sees what it expects.
_pydantic_stub = sys.modules.pop("pydantic", None)
try:
    from apify_client._models import PayPerEventActorPricingInfo  # noqa: E402
    from apify_client.errors import ApifyApiError  # noqa: E402

    from app.services import local_sourcing  # noqa: E402
finally:
    if _pydantic_stub is not None:
        sys.modules["pydantic"] = _pydantic_stub
from app.store import db  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "actor"
TOKEN = "apify_api_SECRETTOKEN1234"
REAL_APIFY_CLIENT = local_sourcing._apify_client
LONG_AGO = datetime.datetime(2023, 1, 1, tzinfo=datetime.timezone.utc)


def fixture_items() -> list[dict]:
    """The nine located rows of the three ATS probes (the no-location bucket is retired)."""
    items = []
    for name in ("greenhouse", "lever", "ashby"):
        items += json.loads((FIXTURES / f"{name}.json").read_text())
    return items


def per_item_pricing(price=0.012):
    return SimpleNamespace(pricing_model="PRICE_PER_DATASET_ITEM", started_at=LONG_AGO,
                           price_per_unit_usd=price, tiered_pricing=None)


def per_event_pricing(price=0.012, minimal=None):
    event = SimpleNamespace(event_price_usd=price, event_tiered_pricing_usd=None)
    return SimpleNamespace(pricing_model="PAY_PER_EVENT", started_at=LONG_AGO,
                           pricing_per_event=SimpleNamespace(actor_charge_events={"result": event}),
                           minimal_max_total_charge_usd=minimal)


def make_run(status="SUCCEEDED", usage=0.5, cap=5.0, max_items=416, run_id="run1"):
    return SimpleNamespace(id=run_id, status=status, default_dataset_id="ds-" + run_id,
                           usage_total_usd=usage,
                           options=SimpleNamespace(max_total_charge_usd=cap, max_items=max_items))


def api_error(status):
    response = SimpleNamespace(status_code=status, text="", json=lambda: {})
    return ApifyApiError(response, 1)


# What impit 0.14.1 raised for https://expired.badssl.com (captured 2026-10-10). impit has no TLS
# exception type, so `ingest_run` finds a bad certificate by "InvalidCertificate" in this text; if
# an impit upgrade rewords it, the test that uses this constant fails.
TLS_ERROR_FIXTURE = (
    "Failed to connect to the server.\nReason: hyper_util::client::legacy::Error(\n    Connect,\n"
    "    Custom {\n        kind: Other,\n        error: Custom {\n            kind: InvalidData,\n"
    "            error: InvalidCertificate(\n                ExpiredContext {\n"
    "                    time: UnixTime(\n                        1791601231,\n                    ),\n"
    "                    not_after: UnixTime(\n                        1428883199,\n                    ),\n"
    "                },\n            ),\n        },\n    },\n)"
)


class FakeApify:
    """Just enough of `ApifyClient`: actor pricing, start, run polling and dataset reads."""

    def __init__(self, items=None, pricing=None, tier="FREE", waits=None):
        self.items = fixture_items() if items is None else items
        self.pricing = [per_item_pricing()] if pricing is None else pricing
        self.tier = tier
        # Each wait_for_finish call pops one entry: a run, None, or an exception to raise. The
        # last entry repeats once the list runs out.
        self.waits = [make_run()] if waits is None else waits
        self.started = []
        self.wait_calls = 0
        self.dataset_reads = 0
        # Each iterate_items call pops one entry first; an exception in it is raised.
        self.dataset_errors = []
        self.run_get_error = None
        self.run_status = "SUCCEEDED"    # what `get_run` reports; `abort` turns RUNNING into ABORTED
        self.aborted = False
        self.next_run_id = 1
        self.start_delay = 0

    def actor(self, name):
        fake = self
        return SimpleNamespace(
            get=lambda: SimpleNamespace(pricing_infos=fake.pricing),
            start=fake._start)

    def _start(self, **kwargs):
        if self.start_delay:
            threading.Event().wait(self.start_delay)  # a slow network call; time.sleep is mocked
        run_id = f"run{self.next_run_id}"
        self.next_run_id += 1
        self.started.append(kwargs)
        return SimpleNamespace(id=run_id)

    def user(self):
        return SimpleNamespace(get=lambda: SimpleNamespace(plan=SimpleNamespace(tier=self.tier)))

    def run(self, run_id):
        fake = self

        def wait_for_finish(wait_duration=None):
            fake.wait_calls += 1
            entry = fake.waits[0] if len(fake.waits) == 1 else fake.waits.pop(0)
            if isinstance(entry, Exception):
                raise entry
            return entry
        return SimpleNamespace(wait_for_finish=wait_for_finish, get=fake.get_run, abort=fake.abort_run)

    def get_run(self):
        """What `fail_run` reads for the bill: a run, or an exception the test queued in `run_get_error`."""
        if self.run_get_error:
            raise self.run_get_error
        return make_run(status=self.run_status, usage=0.37)

    def abort_run(self):
        self.aborted = True

    def dataset(self, dataset_id):
        fake = self

        def iterate_items():
            fake.dataset_reads += 1
            if fake.dataset_errors:
                raise fake.dataset_errors.pop(0)
            return iter(copy.deepcopy(fake.items))
        return SimpleNamespace(iterate_items=iterate_items,
                               get=lambda: SimpleNamespace(item_count=len(fake.items)))


class CleanFiltersTests(unittest.TestCase):
    """The Settings route's filter check lives with the filters it must agree with."""

    def test_lists_are_stripped_of_blanks_and_repeats_and_flags_pass_through(self):
        cleaned = local_sourcing.clean_filters({
            "titles": [" Backend ", "", "Backend", "Data"], "work_arrangements": ["Remote OK"],
            "only_with_pay": True, "company_exclusions": []})
        self.assertEqual(cleaned, {"titles": ["Backend", "Data"], "work_arrangements": ["Remote OK"],
                                   "only_with_pay": True, "company_exclusions": []})

    def test_every_optional_filter_the_actor_input_knows_is_accepted(self):
        for name, _ in local_sourcing._OPTIONAL_FILTERS:
            value = True if name in local_sourcing._FILTER_FLAGS else (
                ["Remote OK"] if name == "work_arrangements" else ["2-5"] if name == "experience_years" else ["x"])
            with self.subTest(name=name):
                self.assertEqual(local_sourcing.clean_filters({name: value}), {name: value})

    def test_bad_shapes_and_values_raise_naming_the_filter(self):
        for raw, says in (([], "must be an object"), ({"surprise": ["x"]}, "unknown filter"),
                          ({"titles": "backend"}, "list of text"), ({"titles": [1]}, "list of text"),
                          ({"only_with_pay": "yes"}, "true or false"),
                          ({"employment_types": ["WEEKENDS"]}, "not one of")):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError) as raised:
                    local_sourcing.clean_filters(raw)
                self.assertIn(says, str(raised.exception))


class _SourcingTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = mock.patch.dict(os.environ, {"CAREERSURGE_DATA_DIR": str(Path(tmp.name) / "data"),
                                           "APIFY_TOKEN": TOKEN})
        env.start()
        self.addCleanup(env.stop)
        db.migrate()
        self.fake = FakeApify()
        client = mock.patch.object(local_sourcing, "_apify_client", lambda: self.fake)
        client.start()
        self.addCleanup(client.stop)
        sleep = mock.patch.object(local_sourcing.time, "sleep")
        self.sleep = sleep.start()
        self.addCleanup(sleep.stop)
        db.set_setting("sourcing_filters", {"titles": ["software engineer"]})
        db.set_setting("sourcing_confirmed_price_usd", 0.012)

    def sql(self, query, params=()):
        conn = db.get_db()
        try:
            return [dict(row) for row in conn.execute(query, params).fetchall()]
        finally:
            conn.close()

    def insert_run(self, run_id, status="succeeded", cost=None, started_at=None):
        conn = db.get_db()
        try:
            conn.execute(
                "INSERT INTO sourcing_runs (apify_run_id, status, cost_usd, started_at) "
                "VALUES (?, ?, ?, COALESCE(?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now')))",
                (run_id, status, cost, started_at))
        finally:
            conn.close()

    def settle(self):
        """Close out any started run, as ingest would, so the next start_run is not refused."""
        self.sql("UPDATE sourcing_runs SET status = 'failed' WHERE status = 'started'")

    def queue_applications(self, count, status="prepared"):
        conn = db.get_db()
        try:
            for index in range(count):
                cursor = conn.execute(
                    "INSERT INTO listings (source, external_id, url, company_key) "
                    "VALUES ('greenhouse', ?, 'https://x.test/j', 'acme')", (f"q{index}",))
                conn.execute("INSERT INTO job_applications (listing_id, ats, status) VALUES (?, 'greenhouse', ?)",
                             (cursor.lastrowid, status))
        finally:
            conn.close()

    def refusal(self, **kwargs):
        """The reason `start_run` refuses with, asserting it started and stored nothing."""
        started_before = len(self.fake.started)
        runs_before = self.sql("SELECT * FROM sourcing_runs")
        with self.assertRaises(local_sourcing.SourcingRefused) as caught:
            local_sourcing.start_run(**kwargs)
        self.assertEqual(len(self.fake.started), started_before, "a refused run must not start anything")
        self.assertEqual(self.sql("SELECT * FROM sourcing_runs"), runs_before)
        return caught.exception.reason


class MonthSpendTests(_SourcingTestCase):
    """`month_spend` is the one tally behind the budget guard and the Settings line."""

    def spend_at(self, now):
        conn = db.get_db()
        try:
            return local_sourcing.month_spend(conn, now)
        finally:
            conn.close()

    def test_no_runs_this_month_is_zero(self):
        self.assertEqual(self.spend_at(datetime.datetime(2026, 10, 18, tzinfo=datetime.timezone.utc)), 0)

    def test_this_months_runs_are_summed_and_last_months_are_not(self):
        self.insert_run("a", cost=1.20, started_at="2026-10-02T08:00:00.000Z")
        self.insert_run("b", cost=0.30, started_at="2026-10-17T08:00:00.000Z")
        self.insert_run("old", cost=4, started_at="2026-09-30T23:59:59.000Z")
        self.assertEqual(self.spend_at(datetime.datetime(2026, 10, 18, tzinfo=datetime.timezone.utc)), 1.5)

    def test_a_run_still_started_with_no_cost_counts_as_zero(self):
        self.insert_run("a", cost=1.2, started_at="2026-10-02T08:00:00.000Z")
        self.insert_run("live", status="started", started_at="2026-10-18T08:00:00.000Z")
        self.assertEqual(self.spend_at(datetime.datetime(2026, 10, 18, 9, tzinfo=datetime.timezone.utc)), 1.2)

    def test_a_run_at_midnight_utc_belongs_to_the_new_month(self):
        self.insert_run("oct", cost=2, started_at="2026-10-31T23:59:00.000Z")
        october = datetime.datetime(2026, 10, 31, 23, 59, 30, tzinfo=datetime.timezone.utc)
        self.assertEqual(self.spend_at(october), 2)

        self.insert_run("nov", cost=3, started_at="2026-11-01T00:00:00.000Z")
        november = datetime.datetime(2026, 11, 1, 0, 0, 30, tzinfo=datetime.timezone.utc)
        self.assertEqual(self.spend_at(november), 3)


class StartRunGuardTests(_SourcingTestCase):
    def test_no_token_refuses_before_any_client_call(self):
        with mock.patch.object(local_sourcing, "_apify_client", REAL_APIFY_CLIENT), \
                mock.patch.object(local_sourcing.secrets, "get_secret", return_value=None):
            self.assertEqual(self.refusal(), "no_key")

    def test_titles_required_and_uncapped(self):
        db.set_setting("sourcing_filters", {"titles": []})
        self.assertEqual(self.refusal(), "no_titles")

        titles = [f"role {n}" for n in range(30)]
        db.set_setting("sourcing_filters", {"titles": titles})
        local_sourcing.start_run()
        self.assertEqual(self.fake.started[0]["run_input"]["titleSearch"], titles)

    def test_price_must_be_readable_confirmed_and_not_risen(self):
        self.fake.pricing = [SimpleNamespace(pricing_model="MYSTERY", started_at=LONG_AGO)]
        self.assertEqual(self.refusal(), "price_unreadable")
        self.fake.pricing = []
        self.assertEqual(self.refusal(), "price_unreadable")

        self.fake.pricing = [per_item_pricing()]
        conn = db.get_db()
        conn.execute("DELETE FROM settings WHERE key = 'sourcing_confirmed_price_usd'")
        conn.close()
        self.assertEqual(self.refusal(), "price_unconfirmed")

        db.set_setting("sourcing_confirmed_price_usd", 0.010)
        self.assertEqual(self.refusal(), "price_rose")

    def test_real_pay_per_event_model_is_read_at_the_accounts_tier(self):
        info = PayPerEventActorPricingInfo.model_validate({
            "pricingModel": "PAY_PER_EVENT", "apifyMarginPercentage": 0.2,
            "createdAt": "2023-01-01T00:00:00Z", "startedAt": "2023-01-01T00:00:00Z",
            "minimalMaxTotalChargeUsd": 0.5,
            "pricingPerEvent": {"actorChargeEvents": {"job": {
                "eventTitle": "Job", "eventDescription": "One job",
                "eventTieredPricingUsd": {"FREE": {"tieredEventPriceUsd": 0.012},
                                          "GOLD": {"tieredEventPriceUsd": 0.008}}}}}})
        self.fake.pricing = [info]
        local_sourcing.start_run()
        self.assertEqual(self.fake.started[-1]["run_input"]["limit"], 416)

        self.settle()
        self.fake.tier = "DIAMOND"
        self.assertEqual(self.refusal(), "price_unreadable")

    def test_queue_over_25_asks_first_and_never_sizes_the_run(self):
        self.queue_applications(26)
        self.assertEqual(self.refusal(), "backlog_unconfirmed")
        local_sourcing.start_run(confirm_backlog=True)
        self.settle()

        conn = db.get_db()
        conn.execute("DELETE FROM job_applications WHERE id = (SELECT MAX(id) FROM job_applications)")
        conn.close()
        local_sourcing.start_run()
        self.assertEqual([call["run_input"]["limit"] for call in self.fake.started], [416, 416])

    def test_second_start_while_a_run_is_still_started_is_refused(self):
        local_sourcing.start_run()
        self.assertEqual(self.refusal(), "run_in_progress")

        self.settle()
        local_sourcing.start_run()

    def test_two_callers_racing_start_one_run_and_the_other_is_refused(self):
        self.fake.start_delay = 0.3
        outcomes = []

        def call():
            try:
                outcomes.append(local_sourcing.start_run())
            except local_sourcing.SourcingRefused as refused:
                outcomes.append(refused.reason)

        threads = [threading.Thread(target=call) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(outcomes), ["run1", "run_in_progress"])
        self.assertEqual(len(self.fake.started), 1)

    def test_budget_refused_only_below_the_minimal_cap(self):
        self.fake.pricing = [per_event_pricing(minimal=0.5)]
        self.insert_run("old", cost=4.95)
        self.assertEqual(self.refusal(), "budget")


class StartRunSizingTests(_SourcingTestCase):
    def test_fresh_month_asks_for_every_row_the_budget_buys(self):
        local_sourcing.start_run()
        first = self.fake.started[-1]
        self.assertEqual(first["run_input"]["limit"], 416)
        self.assertEqual(first["max_total_charge_usd"], Decimal("5.0"))
        self.assertEqual(first["memory_mbytes"], 512)
        self.assertEqual(first["max_items"], 416)

        self.settle()
        db.set_setting("sourcing_monthly_budget_usd", 30)
        local_sourcing.start_run()
        bigger = self.fake.started[-1]
        self.assertEqual((bigger["run_input"]["limit"], bigger["memory_mbytes"]), (2500, 1024))

    def test_limit_is_the_remaining_budget_not_the_budget(self):
        self.insert_run("earlier", cost=1.20)
        local_sourcing.start_run()
        self.assertEqual(self.fake.started[-1]["run_input"]["limit"], 316)

        self.settle()
        self.insert_run("later", cost=3.60)
        local_sourcing.start_run()
        last = self.fake.started[-1]
        self.assertEqual(last["run_input"]["limit"], 16)
        self.assertEqual(last["max_total_charge_usd"], Decimal("0.2"))

    def test_runs_in_other_months_do_not_count(self):
        self.insert_run("ancient", cost=4.90, started_at="2020-01-15T00:00:00.000Z")
        local_sourcing.start_run()
        self.assertEqual(self.fake.started[-1]["run_input"]["limit"], 416)

    def test_a_remainder_under_ten_rows_still_runs_and_the_cap_stops_it(self):
        self.fake.pricing = [per_event_pricing(minimal=0.05)]
        self.insert_run("old", cost=4.95)
        local_sourcing.start_run()
        call = self.fake.started[-1]
        self.assertEqual(call["run_input"]["limit"], 10)
        self.assertEqual(call["max_total_charge_usd"], Decimal("0.05"))

    def test_time_range_follows_the_gap_since_the_last_success(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        cases = [(datetime.timedelta(minutes=30), "1h"), (datetime.timedelta(hours=5), "24h"),
                 (datetime.timedelta(days=3), "7d"), (datetime.timedelta(days=40), "7d"), (None, "7d")]
        for gap, expected in cases:
            with self.subTest(gap=gap):
                self.sql("DELETE FROM sourcing_runs")
                if gap is not None:
                    started = (now - gap).isoformat(timespec="milliseconds").replace("+00:00", "Z")
                    self.insert_run("last", started_at=started)
                local_sourcing.start_run()
                self.assertEqual(self.fake.started[-1]["run_input"]["timeRange"], expected)
                self.sql("DELETE FROM sourcing_runs")

    def test_fixed_time_range_wins(self):
        db.set_setting("sourcing_time_range", "24h")
        local_sourcing.start_run()
        self.assertEqual(self.fake.started[-1]["run_input"]["timeRange"], "24h")

    def test_actor_input_is_one_filtered_run(self):
        local_sourcing.start_run()
        sent = self.fake.started[-1]["run_input"]
        self.assertNotIn("hasNoLocation", sent)
        self.assertNotIn("descriptionSearch", sent)
        self.assertEqual(sent["aiEmploymentTypeFilter"], ["FULL_TIME"])
        self.assertEqual(sent["locationSearch"], ["United States"])
        self.assertEqual(sent["ats"], ["greenhouse", "lever.co", "ashby"])

        self.settle()
        db.set_setting("sourcing_filters", {
            "titles": ["a"], "title_exclusions": ["intern"], "visa_sponsorship": True,
            "only_with_pay": True, "company_exclusions": ["bad co"], "locations": ["Canada"]})
        local_sourcing.start_run()
        sent = self.fake.started[-1]["run_input"]
        self.assertEqual((sent["titleExclusionSearch"], sent["aiVisaSponsorshipFilter"], sent["hasSalary"],
                          sent["organizationExclusionSearch"], sent["locationSearch"]),
                         (["intern"], True, True, ["bad co"], ["Canada"]))
        self.assertNotIn("organizationSearch", sent)

    def test_location_codes_are_spelled_out_for_the_actor(self):
        # The actor phrase-matches full names: "Los Angeles, CA" fetched 0 jobs in a real run.
        db.set_setting("sourcing_filters", {"titles": ["a"], "locations": [
            "Los Angeles, CA", "austin, tx", "Austin, TX, USA", "Remote, US", "London, UK", "Austin, Texas",
            "Canada", "CA"]})
        local_sourcing.start_run()
        self.assertEqual(self.fake.started[-1]["run_input"]["locationSearch"], [
            "Los Angeles, California, United States", "austin, Texas, United States", "Austin, Texas, United States",
            "Remote, United States", "London, United Kingdom", "Austin, Texas", "Canada", "CA"])

    def test_run_row_is_committed_before_start_run_returns(self):
        run_id = local_sourcing.start_run()
        rows = self.sql("SELECT apify_run_id, status FROM sourcing_runs")
        self.assertEqual(rows, [{"apify_run_id": run_id, "status": "started"}])


class IngestRunTests(_SourcingTestCase):
    def start(self):
        run_id = local_sourcing.start_run()
        return run_id

    def run_row(self, run_id="run1"):
        return self.sql("SELECT * FROM sourcing_runs WHERE apify_run_id = ?", (run_id,))[0]

    def test_fixture_dataset_lands_keyed_by_ats_with_the_mapped_fields(self):
        run_id = self.start()
        result = local_sourcing.ingest_run(run_id)
        self.assertEqual(result, {"ingested": 9, "skipped": 0})

        by_id = {row["external_id"]: row for row in self.sql("SELECT * FROM listings")}
        self.assertEqual({row["source"] for row in by_id.values()}, {"greenhouse", "lever", "ashby"})
        rebuilt = by_id["2374663171"]
        self.assertEqual(rebuilt["url"], "https://job-boards.greenhouse.io/examplehealth/jobs/8181403")
        self.assertEqual(by_id["2374657283"]["url"],
                         "https://job-boards.greenhouse.io/exampleapparel/jobs/4558921007")
        chicago = json.loads(by_id["2374646111"]["locations"])
        self.assertEqual(chicago[1], {"city": "Evergreen Park", "region": "Illinois",
                                      "country": "United States",
                                      "lat": chicago[1]["lat"], "lng": chicago[1]["lng"]})
        self.assertEqual(len(chicago), 6)
        hourly = by_id["2374657283"]
        self.assertEqual((hourly["salary_min"], hourly["salary_max"]), (18 * 2080, 23 * 2080))
        self.assertEqual(by_id["2374801593"]["employment_type"], "FULL_TIME")
        self.assertEqual(by_id["2374724315"]["work_arrangement"], "Remote Solely")
        self.assertEqual(by_id["2374724315"]["is_remote"], 1)
        self.assertEqual(by_id["2374659602"]["employment_type"], "INTERN")
        self.assertEqual(by_id["2374659602"]["normalized_title"], "intern investment analytics engineering")
        self.assertTrue(all(row["sourcing_run_id"] == run_id for row in by_id.values()))
        columns = set(by_id["2374663171"])
        self.assertTrue({"description", "ats"}.isdisjoint(columns))

        row = self.run_row()
        self.assertEqual((row["status"], row["rows_fetched"], row["cost_usd"], row["capped"]),
                         ("succeeded", 9, 0.5, 0))
        self.assertIsNotNone(row["ingested_at"])

    def test_repost_key_ignores_query_trailing_slash_and_www(self):
        base = json.loads((FIXTURES / "lever.json").read_text())[0]
        tracked = dict(base, url=base["url"].replace("https://jobs.", "https://www.jobs.") + "/?utm_source=x#top")
        self.assertEqual(local_sourcing._map_item(base)[0]["repost_key"],
                         local_sourcing._map_item(tracked)[0]["repost_key"])
        other = dict(base, url="https://jobs.lever.co/exampleavionics/someone-else")
        self.assertNotEqual(local_sourcing._map_item(base)[0]["repost_key"],
                            local_sourcing._map_item(other)[0]["repost_key"])

    def test_bad_rows_are_skipped_and_counted_under_the_20_percent_line(self):
        items = fixture_items()
        items[0]["source"] = "workday"
        del items[1]["id"]
        self.fake.items = items + [dict(items[2], id=900 + n) for n in range(3)]
        run_id = self.start()
        result = local_sourcing.ingest_run(run_id)
        self.assertEqual(result, {"ingested": 10, "skipped": 2})
        self.assertEqual(self.run_row()["status"], "succeeded")

    def test_each_skipped_row_is_logged_and_counted_by_reason_on_the_run(self):
        items = fixture_items()
        good = items[2]
        bad = [
            dict(good, id=901, url=""),
            dict(good, id=902, url=""),
            dict(good, id=903, source="workday"),
            {k: v for k, v in good.items() if k != "id"},
            dict(good, id=905, title="  "),
            dict(good, id=906, title="!!!"),
        ]
        self.fake.items = items + [dict(items[3], id=800 + n) for n in range(30)] + bad
        run_id = self.start()
        with self.assertLogs("careersurge.services.local_sourcing", level=logging.INFO) as captured:
            result = local_sourcing.ingest_run(run_id)
        self.assertEqual(result["skipped"], 6)
        row = self.run_row()
        self.assertEqual(row["rows_skipped"], 6)
        self.assertEqual(json.loads(row["skip_reasons"]),
                         {"missing_url": 2, "unsupported_ats": 1, "missing_id": 1,
                          "missing_title": 1, "empty_title": 1})
        text = "\n".join(captured.output)
        self.assertIn("skipped row 901 (missing_url)", text)
        self.assertIn("skipped row 903 (unsupported_ats)", text)
        self.assertIn(f"skipped row {good['url']} (missing_id)", text)

    def test_a_clean_run_stores_zero_skipped_and_empty_reasons(self):
        run_id = self.start()
        local_sourcing.ingest_run(run_id)
        row = self.run_row()
        self.assertEqual((row["rows_skipped"], json.loads(row["skip_reasons"])), (0, {}))

    def test_an_over_threshold_run_still_records_why_rows_were_skipped(self):
        self.fake.items = [dict(item, url="") for item in fixture_items()[:5]] + fixture_items()[5:7]
        run_id = self.start()
        local_sourcing.ingest_run(run_id)
        row = self.run_row()
        self.assertEqual(row["status"], "failed")
        self.assertEqual((row["rows_skipped"], json.loads(row["skip_reasons"])), (5, {"missing_url": 5}))

    def test_values_the_code_does_not_recognise_are_nulled_and_logged_but_the_listing_is_saved(self):
        base = json.loads((FIXTURES / "greenhouse.json").read_text())[0]
        drifted = dict(base, id=777, ai_work_arrangement="Hybrid-ish", ai_experience_level="3-4",
                       ai_salary_unit_text="FORTNIGHT", ai_salary_min_value=1000, ai_salary_max_value=2000,
                       employment_type=["Gig"], ai_employment_type=None, title="Staff Engineer")
        with self.assertLogs("careersurge.services.local_sourcing", level=logging.INFO) as captured:
            listing, reason = local_sourcing._map_item(drifted)
        self.assertIsNone(reason)
        self.assertEqual((listing["work_arrangement"], listing["experience_years"], listing["employment_type"],
                          listing["salary_min"], listing["salary_max"]), (None, None, None, None, None))
        text = "\n".join(captured.output)
        for expected in ("listing 777: unrecognised work arrangement 'Hybrid-ish'",
                         "listing 777: unrecognised experience level '3-4'",
                         "listing 777: unrecognised salary unit 'FORTNIGHT'",
                         "listing 777: unrecognised employment type 'Gig'"):
            self.assertIn(expected, text)

    def test_absent_values_and_known_ones_are_not_logged(self):
        base = json.loads((FIXTURES / "lever.json").read_text())[0]
        quiet = dict(base, ai_work_arrangement=None, ai_experience_level=None, ai_salary_unit_text=None,
                     ai_salary_min_value=None, ai_salary_max_value=None, ai_salary_value=None)
        with self.assertNoLogs("careersurge.services.local_sourcing", level=logging.INFO):
            local_sourcing._map_item(quiet)

    def test_an_untyped_internship_is_typed_intern_but_a_stated_type_wins(self):
        base = json.loads((FIXTURES / "greenhouse.json").read_text())[0]
        untyped = dict(base, title="Software Engineering Intern", employment_type=None, ai_employment_type=None)
        self.assertEqual(local_sourcing._map_item(untyped)[0]["employment_type"], "INTERN")
        stated = dict(untyped, ai_employment_type=["PART_TIME"])
        self.assertEqual(local_sourcing._map_item(stated)[0]["employment_type"], "PART_TIME")

    def test_over_20_percent_bad_writes_nothing_and_fails_the_run(self):
        items = fixture_items()[:10]
        for item in items[:3]:
            item["source"] = "workday"
        self.fake.items = items
        run_id = self.start()
        local_sourcing.ingest_run(run_id)
        self.assertEqual(self.sql("SELECT COUNT(*) AS n FROM listings")[0]["n"], 0)
        self.assertEqual(self.run_row()["status"], "failed")

    def test_same_dataset_twice_only_refreshes_the_sighting(self):
        run_id = self.start()
        local_sourcing.ingest_run(run_id)
        before = {row["external_id"]: row for row in self.sql("SELECT * FROM listings")}
        conn = db.get_db()
        conn.execute("UPDATE listings SET title = 'approved wording', "
                     "last_seen_at = '2020-01-01T00:00:00.000Z', first_seen_at = '2020-01-01T00:00:00.000Z'")
        conn.close()
        self.fake.waits = [make_run(run_id="run2")]
        second = local_sourcing.start_run()
        local_sourcing.ingest_run(second)

        after = self.sql("SELECT * FROM listings")
        self.assertEqual(len(after), len(before))
        for row in after:
            self.assertEqual(row["first_seen_at"], "2020-01-01T00:00:00.000Z")
            self.assertGreater(row["last_seen_at"], "2020-01-01T00:00:00.000Z")
            self.assertEqual(row["sourcing_run_id"], second)
            self.assertEqual(row["title"], "approved wording")

    def test_timed_out_and_aborted_runs_are_failed_but_keep_their_rows(self):
        for status in ("TIMED-OUT", "ABORTED"):
            with self.subTest(status=status):
                self.sql("DELETE FROM sourcing_runs")
                self.fake.items = fixture_items()[:3]
                self.fake.waits = [make_run(status=status, run_id="run1")]
                self.fake.next_run_id = 1
                run_id = self.start()
                local_sourcing.ingest_run(run_id)
                self.assertEqual(self.run_row()["status"], "failed")
                self.assertEqual(self.sql("SELECT COUNT(*) AS n FROM listings")[0]["n"], 3)

    def test_capped_when_the_bill_reaches_the_cap_though_the_run_succeeded(self):
        self.fake.waits = [make_run(usage=5.0, cap=5.0)]
        run_id = self.start()
        local_sourcing.ingest_run(run_id)
        row = self.run_row()
        self.assertEqual((row["status"], row["capped"], row["cost_usd"]), ("succeeded", 1, 5.0))

    def test_capped_when_the_row_limit_is_reached(self):
        self.fake.waits = [make_run(usage=0.1, cap=5.0, max_items=9)]
        run_id = self.start()
        local_sourcing.ingest_run(run_id)
        self.assertEqual(self.run_row()["capped"], 1)

    def test_crash_mid_ingest_writes_nothing_and_resume_reads_the_same_set(self):
        run_id = self.start()
        real_map = local_sourcing._map_item
        calls = {"n": 0}

        def poisoned(item):
            calls["n"] += 1
            listing, reason = real_map(item)
            if calls["n"] == 4:
                listing["visa_sponsorship"] = 7  # violates CHECK (visa_sponsorship IN (0, 1))
            return listing, reason

        with mock.patch.object(local_sourcing, "_map_item", poisoned):
            with self.assertRaises(sqlite3.IntegrityError):
                local_sourcing.ingest_run(run_id)
        self.assertEqual(self.sql("SELECT COUNT(*) AS n FROM listings")[0]["n"], 0)
        self.assertEqual(self.run_row()["status"], "started")

        local_sourcing.ingest_run(run_id)    # what the next launch's resume does
        self.assertEqual(self.sql("SELECT COUNT(*) AS n FROM listings")[0]["n"], 9)
        self.assertEqual(self.run_row()["status"], "succeeded")


class FixableByUserTests(unittest.TestCase):
    """Which ingest errors the app waits out and which end the run."""

    @staticmethod
    def locked_database_error() -> sqlite3.OperationalError:
        """The error SQLite really raises when another connection holds the write lock."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "locked.db"
            holder = sqlite3.connect(path, isolation_level=None)
            holder.execute("CREATE TABLE t (x)")
            holder.execute("BEGIN EXCLUSIVE")
            waiter = sqlite3.connect(path, timeout=0, isolation_level=None)
            try:
                waiter.execute("INSERT INTO t VALUES (1)")
            except sqlite3.OperationalError as locked:
                return locked
            finally:
                waiter.close()
                holder.close()
        raise AssertionError("the database was not locked")

    def test_the_user_can_fix_a_proxy_a_missing_or_rejected_key_a_full_disk_and_a_locked_database(self):
        full_disk = sqlite3.OperationalError("database or disk is full")
        full_disk.sqlite_errorcode = sqlite3.SQLITE_FULL
        for exc in (local_sourcing.SourcingRefused("tls"), local_sourcing.SourcingRefused("no_key"),
                    api_error(401), api_error(403), full_disk, self.locked_database_error()):
            with self.subTest(exc=repr(exc)):
                self.assertTrue(local_sourcing.is_fixable_by_user(exc))

    def test_our_own_bugs_refused_requests_a_corrupt_database_and_other_4xx_are_permanent(self):
        corrupt = sqlite3.DatabaseError("database disk image is malformed")
        no_such_table = sqlite3.OperationalError("no such table: listings")
        no_such_table.sqlite_errorcode = sqlite3.SQLITE_ERROR
        for exc in (KeyError("title"), RuntimeError("boom"), impit.LocalProtocolError("bad header"),
                    impit.UnsupportedProtocol("ftp"), local_sourcing.SourcingRefused("budget"),
                    api_error(400), api_error(404), corrupt, no_such_table):
            with self.subTest(exc=repr(exc)):
                self.assertFalse(local_sourcing.is_fixable_by_user(exc))


class LifecycleTests(_SourcingTestCase):
    def test_crash_after_start_leaves_a_started_row_that_ingests_without_starting_another_run(self):
        run_id = local_sourcing.start_run()
        self.assertEqual(self.sql("SELECT status FROM sourcing_runs")[0]["status"], "started")
        started_before = len(self.fake.started)

        local_sourcing.ingest_run(run_id)
        self.assertEqual(len(self.fake.started), started_before)
        self.assertEqual(self.sql("SELECT status FROM sourcing_runs")[0]["status"], "succeeded")

    def test_fail_run_closes_the_row_with_the_apify_bill_so_a_new_run_can_start(self):
        run_id = local_sourcing.start_run()
        with self.assertRaises(local_sourcing.SourcingRefused) as raised:
            local_sourcing.start_run()
        self.assertEqual(raised.exception.reason, "run_in_progress")

        local_sourcing.fail_run(run_id)
        row = self.sql("SELECT status, cost_usd FROM sourcing_runs")[0]
        self.assertEqual((row["status"], row["cost_usd"]), ("failed", 0.37))
        self.assertTrue(local_sourcing.start_run())

    def test_fail_run_aborts_a_run_that_is_still_going_and_records_the_bill_after_the_abort(self):
        run_id = local_sourcing.start_run()
        self.fake.run_status = "RUNNING"    # the error came from the poll, not from a finished run
        self.fake.waits = [make_run(status="ABORTED", usage=0.52)]    # what waiting out the abort returns

        local_sourcing.fail_run(run_id)
        row = self.sql("SELECT status, cost_usd FROM sourcing_runs")[0]
        self.assertTrue(self.fake.aborted)
        self.assertEqual((row["status"], row["cost_usd"]), ("failed", 0.52))

    def test_fail_run_records_no_bill_when_the_run_is_still_aborting_after_the_wait(self):
        run_id = local_sourcing.start_run()
        self.fake.run_status = "RUNNING"
        self.fake.waits = [make_run(status="ABORTING", usage=0.1)]    # not final: the bill is still growing

        local_sourcing.fail_run(run_id)
        row = self.sql("SELECT status, cost_usd FROM sourcing_runs")[0]
        self.assertEqual((row["status"], row["cost_usd"]), ("failed", None))

    def test_fail_run_still_closes_the_row_when_apify_cannot_be_asked(self):
        run_id = local_sourcing.start_run()
        self.fake.run_get_error = api_error(500)

        with self.assertLogs("careersurge.services.local_sourcing", "WARNING"):
            local_sourcing.fail_run(run_id)
        row = self.sql("SELECT status, cost_usd FROM sourcing_runs")[0]
        self.assertEqual((row["status"], row["cost_usd"]), ("failed", None))

    def test_running_run_is_polled_again_and_should_stop_leaves_it_started(self):
        run_id = local_sourcing.start_run()
        self.fake.waits = [make_run(status="RUNNING"), make_run(status="RUNNING"), make_run()]
        local_sourcing.ingest_run(run_id)
        self.assertEqual(self.fake.wait_calls, 3)

        self.sql("UPDATE sourcing_runs SET status = 'started'")
        self.fake.waits = [make_run(status="RUNNING")]
        self.fake.wait_calls = 0
        stops = iter([False, True])
        result = local_sourcing.ingest_run(run_id, should_stop=lambda: next(stops))
        self.assertEqual(result, {"ingested": 0, "skipped": 0})
        self.assertEqual(self.fake.wait_calls, 1)
        self.assertEqual(self.sql("SELECT status FROM sourcing_runs")[0]["status"], "started")

    def test_a_run_apify_lost_is_retried_three_times_then_failed(self):
        run_id = local_sourcing.start_run()
        self.fake.waits = [None]
        local_sourcing.ingest_run(run_id)
        self.assertEqual(self.fake.wait_calls, 4)
        self.assertEqual([call.args[0] for call in self.sleep.call_args_list], [2, 10, 30])
        self.assertEqual(self.sql("SELECT status FROM sourcing_runs")[0]["status"], "failed")

    def test_a_run_apify_lost_once_then_found_ingests(self):
        run_id = local_sourcing.start_run()
        self.fake.waits = [None, make_run()]
        local_sourcing.ingest_run(run_id)
        self.assertEqual(self.sql("SELECT status FROM sourcing_runs")[0]["status"], "succeeded")

    def test_network_down_is_waited_out_without_spending_the_lost_run_retries(self):
        run_id = local_sourcing.start_run()
        outage = [ConnectionError("down")] * 4 + [api_error(503)]
        self.fake.waits = outage + [None, make_run()]
        local_sourcing.ingest_run(run_id)
        self.assertEqual(self.sql("SELECT status FROM sourcing_runs")[0]["status"], "succeeded")
        delays = [call.args[0] for call in self.sleep.call_args_list]
        self.assertEqual(delays, [2, 4, 8, 16, 30, 2])  # five backoffs capped at 30, then one lost-run retry

    def test_a_transient_error_reading_the_dataset_is_retried_next_lap(self):
        run_id = local_sourcing.start_run()
        self.fake.waits = [make_run()]
        self.fake.dataset_errors = [ConnectionError("down"), api_error(503)]
        local_sourcing.ingest_run(run_id)
        self.assertEqual(self.sql("SELECT status FROM sourcing_runs")[0]["status"], "succeeded")
        self.assertEqual(self.fake.wait_calls, 3)  # the run is asked about again before each read
        self.assertEqual(self.fake.dataset_reads, 3)
        self.assertEqual([call.args[0] for call in self.sleep.call_args_list], [2, 4])

        self.sql("UPDATE sourcing_runs SET status = 'started'")
        self.fake.dataset_errors = [api_error(401)]
        with self.assertRaises(ApifyApiError):
            local_sourcing.ingest_run(run_id)
        self.assertEqual(self.sql("SELECT status FROM sourcing_runs")[0]["status"], "started")

    def test_impit_transport_errors_are_waited_out_but_a_refused_request_is_not(self):
        run_id = local_sourcing.start_run()
        self.fake.waits = [impit.ConnectError("down"), impit.ReadTimeout("slow"), make_run()]
        local_sourcing.ingest_run(run_id)
        self.assertEqual(self.sql("SELECT status FROM sourcing_runs")[0]["status"], "succeeded")

        self.sql("UPDATE sourcing_runs SET status = 'started'")
        self.fake.waits = [impit.LocalProtocolError("bad header")]
        with self.assertRaises(impit.LocalProtocolError):
            local_sourcing.ingest_run(run_id)

    def test_a_tls_failure_raises_at_once_without_waiting_and_leaves_the_row_started(self):
        for error in (impit.ConnectError(TLS_ERROR_FIXTURE),
                      ssl.SSLCertVerificationError("certificate verify failed")):
            with self.subTest(error=type(error).__name__):
                self.sql("DELETE FROM sourcing_runs")
                run_id = local_sourcing.start_run()
                self.sleep.reset_mock()
                self.fake.waits = [error]
                with self.assertRaises(local_sourcing.SourcingRefused) as raised:
                    local_sourcing.ingest_run(run_id)
                self.assertEqual(raised.exception.reason, "tls")
                self.assertIn("Secure connection to Apify failed", raised.exception.detail["message"])
                self.sleep.assert_not_called()
                self.assertEqual(self.sql("SELECT status FROM sourcing_runs")[0]["status"], "started")

    def test_a_dns_failure_and_a_dropped_tls_connection_are_still_waited_out(self):
        run_id = local_sourcing.start_run()
        self.fake.waits = [impit.ConnectError("dns error: failed to lookup address information"),
                           ssl.SSLEOFError("EOF occurred in violation of protocol"), make_run()]
        local_sourcing.ingest_run(run_id)
        self.assertEqual(self.sql("SELECT status FROM sourcing_runs")[0]["status"], "succeeded")

    def test_should_stop_during_an_outage_leaves_the_row_started(self):
        run_id = local_sourcing.start_run()
        self.fake.waits = [ConnectionError("down")]
        stops = iter([False, False, True])
        result = local_sourcing.ingest_run(run_id, should_stop=lambda: next(stops))
        self.assertEqual(result, {"ingested": 0, "skipped": 0})
        self.assertEqual(self.sql("SELECT status FROM sourcing_runs")[0]["status"], "started")

    def test_a_rejected_token_raises_and_leaves_the_row_started(self):
        run_id = local_sourcing.start_run()
        self.fake.waits = [api_error(401)]
        with self.assertRaises(ApifyApiError):
            local_sourcing.ingest_run(run_id)
        self.assertEqual(self.sql("SELECT status FROM sourcing_runs")[0]["status"], "started")

    def test_the_token_never_reaches_the_logs(self):
        with self.assertLogs("careersurge", level=logging.DEBUG) as captured:
            run_id = local_sourcing.start_run()
            self.fake.waits = [ConnectionError("down"), make_run()]
            local_sourcing.ingest_run(run_id)
        self.assertNotIn(TOKEN, "\n".join(captured.output))


class MigrationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = mock.patch.dict(os.environ, {"CAREERSURGE_DATA_DIR": str(Path(tmp.name) / "data")})
        env.start()
        self.addCleanup(env.stop)

    def version_one_database(self):
        """A database at schema version 1 holding a profile row and a timed_out run."""
        migrations = Path(db.__file__).resolve().parent / "migrations"
        raw = sqlite3.connect(db.db_path())
        raw.executescript((migrations / "0001_init.sql").read_text())
        raw.execute("INSERT INTO profile (id, email, full_name, target_roles, city) "
                    "VALUES (1, 'a@b.test', 'Ada Lovelace', '[\"swe\"]', 'London')")
        raw.execute("INSERT INTO sourcing_runs (apify_run_id, status, cost_usd) VALUES ('r1', 'timed_out', 1.5)")
        raw.execute("INSERT INTO sourcing_runs (apify_run_id, status) VALUES ('r2', 'started')")
        raw.execute("PRAGMA user_version = 1")
        raw.commit()
        raw.close()

    def test_profile_loses_target_roles_and_nothing_else(self):
        self.version_one_database()
        self.assertEqual(db.migrate(), 9)
        conn = db.get_db()
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 9)
        columns = [row["name"] for row in conn.execute("PRAGMA table_info(profile)")]
        self.assertNotIn("target_roles", columns)
        self.assertIn("summary", columns)
        row = conn.execute("SELECT email, full_name, city FROM profile").fetchone()
        self.assertEqual(tuple(row), ("a@b.test", "Ada Lovelace", "London"))

    def test_sourcing_runs_rebuild_keeps_rows_and_tightens_the_check(self):
        self.version_one_database()
        db.migrate()
        conn = db.get_db()
        self.addCleanup(conn.close)
        rows = {row["apify_run_id"]: row["status"] for row in conn.execute("SELECT * FROM sourcing_runs")}
        self.assertEqual(rows, {"r1": "failed", "r2": "started"})
        added = conn.execute("SELECT rows_skipped, skip_reasons FROM sourcing_runs WHERE apify_run_id = 'r1'").fetchone()
        self.assertEqual(tuple(added), (None, None))  # the new columns exist and old rows hold NULL
        conn.execute("INSERT INTO sourcing_runs (apify_run_id, status) VALUES ('r3', 'failed')")
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO sourcing_runs (apify_run_id, status) VALUES ('r4', 'timed_out')")
        self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        fk = conn.execute("SELECT \"table\", on_delete FROM pragma_foreign_key_list('listings') "
                          "WHERE \"from\" = 'sourcing_run_id'").fetchone()
        self.assertEqual(tuple(fk), ("sourcing_runs", "SET NULL"))


if __name__ == "__main__":
    unittest.main()
