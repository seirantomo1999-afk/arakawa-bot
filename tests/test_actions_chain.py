from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import arakawa_schedule as schedule

UTC = timezone.utc


def at(hour, minute, second=0, day=12):
    return datetime(2026, 10, day, hour, minute, second, tzinfo=UTC)


class SlotTests(unittest.TestCase):
    def test_half_hour_marks_inside_the_window(self):
        self.assertEqual(schedule.next_slot(at(22, 0)), at(22, 7))
        self.assertEqual(schedule.next_slot(at(22, 7)), at(22, 37))
        self.assertEqual(schedule.next_slot(at(15, 30)), at(15, 37))
        self.assertEqual(schedule.next_slot(at(15, 37)), at(16, 7))
        self.assertEqual(schedule.next_slot(at(23, 40)), at(0, 7, day=13))

    def test_night_continues_on_the_next_half_hour(self):
        # 03:48 JST is no longer a gap; the next start is 04:07 JST.
        self.assertEqual(schedule.next_slot(at(18, 48)), at(19, 7))

    def test_one_day_has_every_half_hour(self):
        slot = at(22, 7)
        for _ in range(48):
            slot = schedule.next_slot(slot)
        self.assertEqual(slot, at(22, 7, day=13))

    def test_naive_datetime_is_rejected(self):
        with self.assertRaises(ValueError):
            schedule.next_slot(datetime(2026, 10, 12, 22, 0))


class FollowUpTests(unittest.TestCase):
    def test_near_slot_scans_instead_of_sleeping_past_it(self):
        self.assertEqual(schedule.follow_up(at(22, 6, 30)), ("scan", 0))

    def test_a_finished_scan_does_not_scan_the_same_slot_again(self):
        action, wait = schedule.follow_up(at(8, 36, 30), after_scan=True)
        self.assertEqual(action, "bridge")
        self.assertGreater(wait, 20 * 60)

    def test_after_a_slot_waits_until_just_before_the_next(self):
        self.assertEqual(schedule.follow_up(at(22, 7)), ("bridge", 1710))

    def test_night_gap_is_only_the_next_half_hour(self):
        action, wait = schedule.follow_up(at(15, 50))
        self.assertEqual((action, wait), ("bridge", 930))

    def test_wait_input_must_be_a_small_integer(self):
        self.assertEqual(schedule.parse_wait_seconds("1710"), 1710)
        with self.assertRaises(ValueError):
            schedule.parse_wait_seconds("30m")
        with self.assertRaises(ValueError):
            schedule.parse_wait_seconds(str(schedule.MAX_WAIT_SECONDS + 1))


class RedundancyTests(unittest.TestCase):
    def test_the_current_schedule_run_does_not_block_itself(self):
        runs = [{"id": 5, "display_title": "schedule", "status": "in_progress"}]
        self.assertFalse(schedule.schedule_is_redundant("5", runs))

    def test_a_live_relay_makes_the_delayed_schedule_redundant(self):
        runs = [
            {"id": 5, "display_title": "schedule"},
            {"id": 9, "name": "relay"},
        ]
        self.assertTrue(schedule.schedule_is_redundant("5", runs))

    def test_a_manual_run_does_not_count_as_the_chain(self):
        runs = [{"id": 5, "display_title": "schedule"}, {"id": 8, "display_title": "manual"}]
        self.assertFalse(schedule.schedule_is_redundant("5", runs))


class DispatchTests(unittest.TestCase):
    def test_dispatch_body_continues_booking_without_the_token(self):
        body = schedule.build_dispatch_body("main", 1710)
        self.assertEqual(body["inputs"]["source"], "relay")
        self.assertEqual(body["inputs"]["dry_run"], False)
        self.assertEqual(body["inputs"]["wait_seconds"], "1710")
        self.assertNotIn("token", json_text(body))

    def test_dispatch_posts_once_and_waits_for_the_relay_run(self):
        calls = []
        pages = [
            {"workflow_runs": [{"id": 5, "display_title": "schedule"}]},
            {"workflow_runs": [{"id": 5, "display_title": "schedule"}]},
            {"workflow_runs": [
                {"id": 5, "display_title": "schedule"},
                {"id": 9, "display_title": "relay", "status": "queued"},
            ]},
        ]

        def request(method, url, payload, token):
            calls.append((method, url, payload, token))
            if method == "POST":
                return 204, {}
            return 200, pages.pop(0)

        result = schedule.dispatch_next(
            at(22, 7), "example/arakawa-bot", "main", "secret-token", request, sleep=lambda _seconds: None,
        )
        posts = [call for call in calls if call[0] == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][2]["inputs"]["wait_seconds"], "1710")
        self.assertNotIn("secret-token", json_text(posts[0][2]))
        self.assertEqual(result["run_id"], 9)
        self.assertTrue(all(call[3] == "secret-token" for call in calls))

    def test_a_finished_scan_blocks_a_second_one_in_the_same_slot(self):
        def request(method, url, payload, token):
            self.assertNotEqual(method, "POST")
            return 200, {"workflow_runs": [{
                "id": 9, "display_title": "relay", "status": "completed",
                "event": "workflow_dispatch", "created_at": "2026-10-12T08:05:31Z",
            }]}

        with patch.dict(os.environ, {"GITHUB_RUN_ID": "8", "SCHEDULER_MODE": ""}):
            result = schedule.dispatch_next(
                at(8, 6, 30), "example/arakawa-bot", "main", "token", request,
            )
        self.assertEqual(result["status"], "busy")

    def test_an_active_relay_is_not_duplicated(self):
        def request(method, url, payload, token):
            self.assertNotEqual(method, "POST")
            return 200, {"workflow_runs": [{"id": 8, "display_title": "relay", "status": "queued"}]}

        with patch.dict(os.environ, {"GITHUB_RUN_ID": "5"}):
            result = schedule.dispatch_next(
                at(22, 7), "example/arakawa-bot", "main", "token", request,
            )
        self.assertEqual(result["status"], "busy")

    def test_cloudflare_mode_does_not_dispatch(self):
        def request(*_args):
            raise AssertionError("request")

        with patch.dict(os.environ, {"SCHEDULER_MODE": "cloudflare"}):
            result = schedule.dispatch_next(
                at(22, 7), "example/arakawa-bot", "main", "token", request,
            )
        self.assertEqual(result["status"], "skipped")

    def test_a_new_dispatch_counts_before_its_title_appears(self):
        self.assertTrue(schedule.is_follow_up({
            "id": 9, "event": "workflow_dispatch", "display_title": "arakawa_park_notifier",
        }))
        self.assertFalse(schedule.is_follow_up({"id": 4, "event": "schedule", "display_title": "schedule"}))


class WaitTests(unittest.TestCase):
    def test_late_start_still_wakes_before_the_same_slot(self):
        clock = {"mono": 0.0, "now": at(22, 10)}

        def sleep(seconds):
            clock["mono"] += seconds
            clock["now"] += timedelta(seconds=seconds)

        schedule.sleep_until_due(
            now_fn=lambda: clock["now"], sleep=sleep, monotonic=lambda: clock["mono"],
            log=lambda _line: None, heartbeat=600,
        )
        remaining = (at(22, 37) - clock["now"]).total_seconds()
        self.assertGreater(remaining, 0)
        self.assertLessEqual(remaining, schedule.DUE_TOLERANCE_SECONDS)

    def test_long_gap_hands_off_before_the_job_limit(self):
        clock = {"mono": 0.0, "now": at(15, 37)}

        def sleep(seconds):
            clock["mono"] += seconds
            clock["now"] += timedelta(seconds=seconds)

        schedule.sleep_until_due(
            now_fn=lambda: clock["now"], sleep=sleep, monotonic=lambda: clock["mono"],
            log=lambda _line: None, heartbeat=400, max_seconds=1000,
        )
        self.assertEqual(clock["now"], at(15, 37) + timedelta(seconds=1000))


class WorkflowTests(unittest.TestCase):
    def test_schedule_cron_matches_the_slot_window(self):
        text = Path(".github/workflows/notify.yml").read_text(encoding="utf-8")
        self.assertIn('cron: "7,37 * * * *"', text)
        self.assertIn("relay", text)


def json_text(body) -> str:
    import json
    return json.dumps(body)


if __name__ == "__main__":
    unittest.main()
