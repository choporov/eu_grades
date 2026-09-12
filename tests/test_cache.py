import asyncio
from datetime import timezone
from pathlib import Path
from threading import Event, get_ident
import unittest
from unittest.mock import Mock, patch

from eu_grades_bot.cache import GradesCache
from eu_grades_bot.drive import WorkbookSource
from eu_grades_bot.grades import CacheNotReadyError, GradesRepository, StudentRecord


class GradesCacheTest(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_refreshes_share_worker_even_if_one_waiter_is_cancelled(self):
        loop = asyncio.get_running_loop()
        started = asyncio.Event()
        release = Event()
        worker_threads = []

        def download():
            worker_threads.append(get_ident())
            loop.call_soon_threadsafe(started.set)
            if not release.wait(5):
                raise TimeoutError("Test did not release download")
            return []

        provider = Mock()
        provider.list_workbooks.side_effect = download
        cache = GradesCache(GradesRepository(provider, reload_when_stale=False))
        first = asyncio.create_task(cache.refresh())
        second = asyncio.create_task(cache.refresh())
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            self.assertIs(cache.start_refresh(), cache.start_refresh())
            self.assertTrue(cache.status.refreshing)
            self.assertFalse(second.done())
            self.assertEqual(provider.list_workbooks.call_count, 1)
            self.assertNotEqual(worker_threads[0], get_ident())
        finally:
            release.set()
            await asyncio.gather(first, second, return_exceptions=True)
        self.assertFalse(cache.status.refreshing)
        self.assertIsNotNone(cache.status.last_success_at)

    async def test_download_and_parsing_leave_previous_snapshot_available_until_complete(self):
        source = WorkbookSource("Before subject - Teacher.xlsx", Path("not-read.xlsx"))
        updated_source = WorkbookSource("After subject - Teacher.xlsx", source.path)
        provider = Mock()
        provider.list_workbooks.return_value = [source]
        repository = GradesRepository(provider, reload_when_stale=False)
        cache = GradesCache(repository)
        before = StudentRecord("Before subject", "Before name", "student@example.com", ())
        after = StudentRecord("After subject", "After name", before.email, ())
        with patch("eu_grades_bot.grades.iter_workbook_students", return_value=[before]):
            await cache.refresh()
        previous_time = cache.status.last_success_at

        loop = asyncio.get_running_loop()
        downloading = asyncio.Event()
        parsing = asyncio.Event()
        finish_download = Event()
        finish_parse = Event()
        parser_threads = []

        def download():
            loop.call_soon_threadsafe(downloading.set)
            if not finish_download.wait(5):
                raise TimeoutError("Test did not release download")
            return [updated_source]

        def parse(*args):
            parser_threads.append(get_ident())
            yield after
            loop.call_soon_threadsafe(parsing.set)
            if not finish_parse.wait(5):
                raise TimeoutError("Test did not release parser")

        provider.list_workbooks.side_effect = download
        with patch("eu_grades_bot.grades.iter_workbook_students", side_effect=parse):
            refresh = asyncio.create_task(cache.refresh())
            try:
                await asyncio.wait_for(downloading.wait(), timeout=2)
                grades = await asyncio.wait_for(cache.get_student_grades(before.email), timeout=1)
                self.assertEqual(grades.student_name, before.student_name)
                finish_download.set()
                await asyncio.wait_for(parsing.wait(), timeout=2)
                grades = await asyncio.wait_for(cache.get_student_grades(before.email), timeout=1)
                self.assertEqual(grades.student_name, before.student_name)
                self.assertEqual(grades.disciplines, (before.discipline,))
                self.assertEqual(cache.status.last_success_at, previous_time)
                self.assertNotEqual(parser_threads[0], get_ident())
            finally:
                finish_download.set()
                finish_parse.set()
                await refresh
        grades = await cache.get_student_grades(after.email)
        self.assertEqual(grades.student_name, after.student_name)
        self.assertEqual(grades.disciplines, (after.discipline,))
        self.assertGreaterEqual(cache.status.last_success_at, previous_time)
        self.assertEqual(cache.status.last_success_at.tzinfo, timezone.utc)

    async def test_download_and_parse_failures_preserve_data_and_last_success_time(self):
        source = WorkbookSource("Biology.xlsx", Path("not-read.xlsx"))
        provider = Mock()
        provider.list_workbooks.return_value = [source]
        cache = GradesCache(GradesRepository(provider, reload_when_stale=False))
        student = StudentRecord("Biology", "Student", "student@example.com", ())
        with patch("eu_grades_bot.grades.iter_workbook_students", return_value=[student]):
            await cache.refresh()
        previous_time = cache.status.last_success_at

        for failure in ("download", "parse"):
            with self.subTest(failure=failure):
                provider.list_workbooks.side_effect = RuntimeError("Drive unavailable") if failure == "download" else None
                with patch("eu_grades_bot.grades.iter_workbook_students", side_effect=ValueError("Bad workbook")):
                    with self.assertLogs("eu_grades_bot.cache", level="ERROR"):
                        with self.assertRaises((RuntimeError, ValueError)):
                            await cache.refresh()
                self.assertEqual(cache.status.last_success_at, previous_time)
                self.assertIsNotNone(cache.status.last_failure_at)
                grades = await cache.get_student_grades(student.email)
                self.assertEqual(grades.disciplines, (student.discipline,))
                self.assertFalse(cache.status.refreshing)

        with patch("eu_grades_bot.grades.iter_workbook_students", return_value=[student]):
            await cache.refresh()
        self.assertIsNone(cache.status.last_failure_at)

    async def test_drive_reads_never_download_even_before_first_success(self):
        provider = Mock()
        cache = GradesCache(GradesRepository(provider, reload_when_stale=False))
        for _ in range(2):
            with self.assertRaises(CacheNotReadyError):
                await cache.get_student_grades("student@example.com")
        provider.list_workbooks.assert_not_called()
        self.assertIsNone(cache.status.last_success_at)

    async def test_age_threshold_and_shutdown_wait(self):
        provider = Mock()
        provider.list_workbooks.return_value = []
        repository = GradesRepository(provider, reload_when_stale=False)
        cache = GradesCache(repository, stale_after_seconds=1800)
        with patch("eu_grades_bot.grades.time.monotonic", return_value=100):
            repository.reload()
        with patch("eu_grades_bot.grades.time.monotonic", return_value=1899):
            self.assertFalse(cache.status.stale)
            self.assertEqual(cache.status.age_seconds, 1799)
        with patch("eu_grades_bot.grades.time.monotonic", return_value=1900):
            self.assertTrue(cache.status.stale)

        started = asyncio.Event()
        release = Event()
        loop = asyncio.get_running_loop()

        def download():
            loop.call_soon_threadsafe(started.set)
            if not release.wait(5):
                raise TimeoutError("Test did not release download")
            return []

        provider.list_workbooks.side_effect = download
        cache.start_refresh()
        stopped = asyncio.create_task(cache.wait_for_refresh())
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            self.assertFalse(stopped.done())
        finally:
            release.set()
            await stopped
        self.assertFalse(cache.status.refreshing)

    async def test_local_cold_load_and_expired_reads_use_shared_worker(self):
        provider = Mock()
        provider.list_workbooks.return_value = []
        repository = GradesRepository(provider, cache_ttl_seconds=0, reload_when_stale=True)
        cache = GradesCache(repository)
        await cache.get_student_grades("student@example.com")
        self.assertEqual(provider.list_workbooks.call_count, 1)
        await cache.get_student_grades("student@example.com")
        await cache.get_student_grades("another@example.com")
        await cache.wait_for_refresh()
        self.assertEqual(provider.list_workbooks.call_count, 2)


if __name__ == "__main__":
    unittest.main()
