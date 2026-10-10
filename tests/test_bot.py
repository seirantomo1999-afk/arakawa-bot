import base64
from datetime import date, datetime, timedelta, timezone
from email import message_from_bytes
import os
import subprocess
import unittest
from unittest.mock import Mock, patch

import arakawa_calendar as calendar
import arakawa_config as config
import arakawa_gmail as mail
import arakawa_selenium_check as scraper
from selenium.common.exceptions import TimeoutException, UnexpectedAlertPresentException


def slot(day=date(2026, 10, 17), start="13:00", end="15:00", court="自然公園庭球場"):
    return scraper.SlotInfo(day, str(day), start, end, f"{start}-{end}", court, "[1] 空き", "button1")


class SettingsTests(unittest.TestCase):
    def test_credentials_are_required(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "ARAKAWA_USER_ID"):
                config.reservation_credentials()

    def test_invalid_boolean_is_rejected(self):
        with patch.dict(os.environ, {"FLAG":"maybe"}):
            with self.assertRaises(ValueError):
                config.env_bool("FLAG", False)

    def test_jst_date_at_utc_day_boundary(self):
        with patch.object(config, "datetime") as clock:
            clock.now.return_value = datetime(2026, 10, 10, 0, 1, tzinfo=config.JST)
            self.assertEqual(config.today_jst(), date(2026, 10, 10))
            clock.now.assert_called_once_with(config.JST)


class ReservationRulesTests(unittest.TestCase):
    def setUp(self):
        self.clock = patch.object(scraper, "today_jst", return_value=date(2026, 10, 9))
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def test_weekday_is_excluded_by_default(self):
        self.assertEqual(scraper.get_reservation_candidates([slot(date(2026, 10, 14))]), [])

    def test_holiday_is_allowed_after_three_days(self):
        self.assertEqual(len(scraper.get_reservation_candidates([slot(date(2026, 11, 3))])), 1)

    def test_three_day_boundary_is_excluded(self):
        self.assertEqual(scraper.get_reservation_candidates([slot(date(2026, 10, 12))]), [])

    def test_non_two_hour_and_10am_slots_are_excluded(self):
        self.assertEqual(scraper.get_reservation_candidates([slot(start="10:00", end="12:00"), slot(end="14:00")]), [])

    def test_out_of_scope_court_is_excluded(self):
        self.assertEqual(scraper.get_reservation_candidates([slot(court="木場公園")]), [])

    def test_calendar_failure_stops_before_browser_start(self):
        with patch.object(scraper, "reservation_credentials", return_value=("fake","fake")), \
             patch.object(calendar, "get_google_creds", side_effect=RuntimeError("offline")), \
             patch.object(scraper, "make_driver") as driver:
            with self.assertRaisesRegex(RuntimeError, "カレンダー"):
                scraper.main()
            driver.assert_not_called()

    def test_missing_calendar_cache_cannot_book(self):
        with self.assertRaisesRegex(RuntimeError, "カレンダー"):
            scraper.scrape_all_days(Mock())

    def test_retry_applies_only_to_login(self):
        driver = Mock()
        with patch.object(scraper, "open_and_login", side_effect=[UnexpectedAlertPresentException("error"), None]) as login, \
             patch.object(scraper.time, "sleep"):
            scraper.login_with_retry(driver)
        self.assertEqual(login.call_count, 2)
        driver.switch_to.alert.accept.assert_called_once()

    def test_retry_exhaustion_is_reported(self):
        with patch.object(scraper, "open_and_login", side_effect=TimeoutException()), \
             patch.object(scraper.time, "sleep"):
            with self.assertRaises(TimeoutException):
                scraper.login_with_retry(Mock())

    def test_uncertain_confirmation_stops_instead_of_returning_success(self):
        driver = Mock()
        driver.find_element.return_value.text = ""
        waits = Mock()
        waits.until.side_effect = [Mock(), Mock(), Mock(), Mock(), Mock(), TimeoutException()]
        with patch.object(scraper, "WebDriverWait", return_value=waits), \
             patch.object(scraper, "Select"), patch.object(scraper.time, "sleep"):
            with self.assertRaises(scraper.ReservationUncertainError):
                scraper.try_book_first_candidate(driver, slot())

    def test_error_alert_is_not_accepted_as_reservation_confirmation(self):
        driver = Mock()
        driver.find_element.return_value.text = ""
        alert = Mock(text="データ通信を正しく行うことができませんでした。")
        waits = Mock()
        waits.until.side_effect = [Mock(), Mock(), Mock(), Mock(), Mock(), alert]
        with patch.object(scraper, "WebDriverWait", return_value=waits), \
             patch.object(scraper, "Select"), patch.object(scraper.time, "sleep"):
            with self.assertRaises(scraper.ReservationUncertainError):
                scraper.try_book_first_candidate(driver, slot())
        alert.accept.assert_not_called()

    def test_successful_booking_is_added_to_same_run_calendar_cache(self):
        candidate = slot()
        cache = []
        def filter_slots(candidates, events_cache):
            return [s for s in candidates if not calendar.has_calendar_conflict(s, events_cache)]
        waits = Mock()
        waits.until.side_effect = TimeoutException()
        with patch.object(scraper, "scrape_one_day", return_value=[candidate]), \
             patch.object(scraper, "get_reservation_candidates", return_value=[candidate]), \
             patch.object(scraper, "DO_BOOK_FIRST_CANDIDATE", True), \
             patch.object(scraper, "try_book_first_candidate", return_value=True) as book, \
             patch.object(scraper, "navigate_from_menu_to_search"), \
             patch.object(scraper, "WebDriverWait", return_value=waits), \
             patch.object(scraper.time, "sleep"):
            _, booked = scraper.scrape_all_days(Mock(), cache, filter_slots)
        book.assert_called_once()
        self.assertEqual(booked, [candidate])
        self.assertEqual(len(cache), 1)


class CalendarTests(unittest.TestCase):
    def test_two_hour_buffer_conflict(self):
        event = (datetime(2026, 10, 17, 11, 30, tzinfo=config.JST),
                 datetime(2026, 10, 17, 12, 0, tzinfo=config.JST))
        self.assertTrue(calendar.has_calendar_conflict(slot(), [event]))

    def test_event_outside_buffer_is_allowed(self):
        event = (datetime(2026, 10, 17, 17, 0, tzinfo=config.JST),
                 datetime(2026, 10, 17, 18, 0, tzinfo=config.JST))
        self.assertFalse(calendar.has_calendar_conflict(slot(), [event]))

    def test_unknown_time_is_not_approved(self):
        self.assertTrue(calendar.has_calendar_conflict(slot(start=""), []))

    def test_all_day_event_has_exclusive_end(self):
        start, end = calendar._event_range({"start":{"date":"2026-10-17"},"end":{"date":"2026-10-18"}})
        self.assertEqual(end - start, timedelta(days=1))
        self.assertTrue(calendar.has_calendar_conflict(slot(), [(start,end)]))

    def test_malformed_event_fails_closed(self):
        with self.assertRaises((KeyError, ValueError)):
            calendar._event_range({"start":{"date":"2026-10-17"},"end":{}})

    def test_zero_length_all_day_blocks_that_day(self):
        start, end = calendar._event_range({
            "summary": "終日", "start": {"date": "2026-10-17"}, "end": {"date": "2026-10-17"},
        })
        self.assertEqual(end - start, timedelta(days=1))
        self.assertTrue(calendar.has_calendar_conflict(slot(), [(start, end)]))

    def test_zero_length_timed_event_blocks_containing_slot(self):
        start, end = calendar._event_range({
            "summary": "点",
            "start": {"dateTime": "2026-10-17T13:00:00+09:00"},
            "end": {"dateTime": "2026-10-17T13:00:00+09:00"},
        })
        self.assertEqual(end - start, timedelta(minutes=1))
        self.assertTrue(calendar.has_calendar_conflict(slot(), [(start, end)]))

    def test_reversed_event_is_kept_and_fetch_continues(self):
        service = Mock()
        reversed_event = {
            "summary": "逆順",
            "start": {"dateTime": "2026-10-17T12:00:00+09:00"},
            "end": {"dateTime": "2026-10-17T11:00:00+09:00"},
        }
        normal = {"start": {"date": "2026-10-18"}, "end": {"date": "2026-10-19"}}
        service.events.return_value.list.return_value.execute.return_value = {
            "items": [reversed_event, normal],
        }
        now = datetime.now(timezone.utc)
        ranges = calendar._fetch_busy_ranges(service, now, now + timedelta(days=400))
        self.assertEqual(len(ranges), 2)
        self.assertLess(ranges[0][0], ranges[0][1])

    def test_fetches_second_page(self):
        service = Mock()
        event = {"start":{"date":"2026-10-17"},"end":{"date":"2026-10-18"}}
        service.events.return_value.list.return_value.execute.side_effect = [
            {"items":[],"nextPageToken":"next"}, {"items":[event]},
        ]
        now = datetime.now(timezone.utc)
        ranges = calendar._fetch_busy_ranges(service, now, now + timedelta(days=400))
        self.assertEqual(len(ranges), 1)
        self.assertEqual(service.events.return_value.list.call_args.kwargs["pageToken"], "next")


class NotificationTests(unittest.TestCase):
    def test_no_hit_does_not_send(self):
        result = subprocess.CompletedProcess([], 0, "空きなし", "")
        self.assertEqual(mail.messages_for_result(result), [])

    def test_partial_booking_success_is_not_lost_on_later_error(self):
        result = subprocess.CompletedProcess([], 1, "BOOKED: 10/17 コート\nBOOKED: 10/17 コート\n", "later failure")
        messages = mail.messages_for_result(result)
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0][1].count("10/17 コート"), 1)
        self.assertIn("エラー", messages[1][0])

    def test_scraper_error_makes_actions_fail(self):
        with patch.object(mail, "get_service", return_value=Mock()), \
             patch.object(mail, "run_scraper", return_value=subprocess.CompletedProcess([], 1, "", "failed")), \
             patch.object(mail, "send_message") as send:
            self.assertEqual(mail.main(), 1)
            send.assert_called_once()

    def test_notification_error_makes_actions_fail(self):
        with patch.object(mail, "get_service", return_value=Mock()), \
             patch.object(mail, "run_scraper", return_value=subprocess.CompletedProcess([], 0, "[1] hit", "")), \
             patch.object(mail, "send_message", side_effect=RuntimeError("offline")):
            self.assertEqual(mail.main(), 1)

    def test_timeout_preserves_partial_booking_results(self):
        error = subprocess.TimeoutExpired([], 5, output=b"BOOKED: done\n")
        with patch.object(mail.subprocess, "run", side_effect=error):
            result = mail.run_scraper()
        self.assertEqual(result.returncode, 124)
        self.assertEqual(len(mail.messages_for_result(result)), 2)

    def test_error_message_does_not_include_reservation_password(self):
        with patch.dict(os.environ, {"ARAKAWA_PASSWORD":"fake-private-password"}):
            result = subprocess.CompletedProcess([], 1, "", "failed fake-private-password")
            body = mail.messages_for_result(result)[0][1]
            self.assertNotIn("fake-private-password", body)


if __name__ == "__main__":
    unittest.main()
