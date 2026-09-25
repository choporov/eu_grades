import asyncio
from datetime import datetime, timedelta, timezone
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

from telegram import Chat, Message, MessageEntity, Update, User
from telegram.ext import ExtBot, JobQueue, Updater
from telegram.warnings import PTBUserWarning

from eu_grades_bot.bot import (
    DRIVE_REFRESH_INTERVAL_SECONDS,
    build_application,
    format_cache_status,
    refresh_drive_cache_job,
    wait_for_cache_refresh,
)
from eu_grades_bot.cache import GradesCache
from eu_grades_bot.grades import GradesRepository


def make_services(source="drive"):
    settings = SimpleNamespace(
        telegram_bot_token="123456:TEST_TOKEN",
        grades_source=source,
        timezone=ZoneInfo("Europe/Kyiv"),
    )
    provider = Mock()
    provider.list_workbooks.return_value = []
    cache = GradesCache(GradesRepository(provider, reload_when_stale=source != "drive"))
    storage = Mock()
    storage.get.return_value = SimpleNamespace(email="student@example.com")
    storage.all_users.return_value = []
    return SimpleNamespace(settings=settings, cache=cache, storage=storage), provider


class BotStartupTest(unittest.IsolatedAsyncioTestCase):
    async def test_delayed_initialization_still_starts_refresh_before_scheduler(self):
        for startup_delay in (5, 1000):
            with self.subTest(startup_delay=startup_delay):
                services, provider = make_services()
                construction_time = datetime.now(timezone.utc) - timedelta(seconds=startup_delay)
                with patch.object(JobQueue, "_tz_now", return_value=construction_time):
                    application = build_application(services.settings, services)
                self.assertIsNotNone(application.post_init)
                self.assertEqual(application.job_queue.jobs(), ())
                provider.list_workbooks.assert_not_called()

                await application.post_init(application)
                await asyncio.wait_for(services.cache.wait_for_refresh(), timeout=2)

                provider.list_workbooks.assert_called_once_with()
                self.assertFalse(application.job_queue.scheduler.running)
                self.assertIsNotNone(services.cache.status.last_success_at)
                scheduler = application.job_queue.scheduler
                scheduler.start(paused=True)
                try:
                    jobs = application.job_queue.get_jobs_by_name("drive-cache-refresh")
                    self.assertEqual(len(jobs), 1)
                    delay = (jobs[0].next_t - datetime.now(timezone.utc)).total_seconds()
                    self.assertGreater(delay, DRIVE_REFRESH_INTERVAL_SECONDS - 5)
                    self.assertLessEqual(delay, DRIVE_REFRESH_INTERVAL_SECONDS)
                    self.assertEqual(jobs[0].job.trigger.interval.total_seconds(), 900)
                    self.assertEqual(
                        {job.name for job in application.job_queue.jobs()},
                        {"drive-cache-refresh", "daily-summary", "weekly-summary"},
                    )
                finally:
                    scheduler.shutdown(wait=False)
                    await asyncio.sleep(0)

    async def test_startup_is_nonblocking_and_commands_share_initial_worker(self):
        services, provider = make_services()
        started = asyncio.Event()
        release = Event()
        loop = asyncio.get_running_loop()

        def download():
            loop.call_soon_threadsafe(started.set)
            if not release.wait(5):
                raise TimeoutError("Test did not release initial download")
            return []

        provider.list_workbooks.side_effect = download
        application = build_application(services.settings, services)
        self.assertIsNotNone(application.post_init)
        application._initialized = True
        application._running = True
        application.bot._bot_user = User(123456, "Test", True, username="test_bot")
        replies = []
        manual_finished = asyncio.Event()

        async def reply(*args, **kwargs):
            replies.append(kwargs)
            if kwargs["chat_id"] == 4 and "Останнє успішне оновлення:" in kwargs["text"]:
                manual_finished.set()

        def command(chat_id, text):
            message = Message(
                chat_id, datetime.now(timezone.utc), Chat(chat_id, "private"), text=text,
                entities=[MessageEntity(MessageEntity.BOT_COMMAND, 0, len(text))],
            )
            message.set_bot(application.bot)
            return Update(chat_id, message=message)

        scheduled = None
        manual_dispatched = False
        with patch.object(ExtBot, "send_message", new=AsyncMock(side_effect=reply)):
            try:
                await asyncio.wait_for(application.post_init(application), timeout=1)
                await asyncio.wait_for(started.wait(), timeout=2)
                self.assertTrue(services.cache.status.refreshing)
                self.assertIsNone(services.cache.status.last_success_at)
                for chat_id, text in ((1, "/cache"), (2, "/help"), (3, "/grades")):
                    await asyncio.wait_for(application.process_update(command(chat_id, text)), timeout=1)
                self.assertTrue(any(reply["chat_id"] == 1 and "Оновлення триває" in reply["text"] for reply in replies))
                self.assertTrue(any(reply["chat_id"] == 2 and "Команди:" in reply["text"] for reply in replies))
                self.assertTrue(any(reply["chat_id"] == 3 and "Таблиці ще не завантажені" in reply["text"] for reply in replies))
                provider.list_workbooks.assert_called_once_with()

                await asyncio.wait_for(application.process_update(command(4, "/refresh")), timeout=1)
                manual_dispatched = True
                job = application.job_queue.get_jobs_by_name("drive-cache-refresh")[0]
                scheduled = asyncio.create_task(job.run(application))
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                self.assertFalse(scheduled.done())
                self.assertFalse(manual_finished.is_set())
                self.assertTrue(any(reply["chat_id"] == 4 and "вже триває" in reply["text"] for reply in replies))
                provider.list_workbooks.assert_called_once_with()
            finally:
                release.set()
                await services.cache.wait_for_refresh()
                if scheduled is not None:
                    await scheduled
                if manual_dispatched:
                    await asyncio.wait_for(manual_finished.wait(), timeout=2)
                application._running = False
        provider.list_workbooks.assert_called_once_with()
        self.assertIsNotNone(services.cache.status.last_success_at)

    async def test_failed_initial_refresh_is_recorded_and_periodic_retry_can_recover(self):
        services, provider = make_services()
        provider.list_workbooks.side_effect = RuntimeError("Drive unavailable")
        application = build_application(services.settings, services)
        self.assertIsNotNone(application.post_init)

        with self.assertLogs("eu_grades_bot.cache", level="ERROR"):
            await application.post_init(application)
            await asyncio.wait_for(services.cache.wait_for_refresh(), timeout=2)

        self.assertIsNone(services.cache.status.last_success_at)
        self.assertIsNotNone(services.cache.status.last_failure_at)
        self.assertFalse(services.cache.status.refreshing)
        self.assertIn("Остання помилка оновлення:", format_cache_status(services))
        provider.list_workbooks.assert_called_once_with()
        job = application.job_queue.get_jobs_by_name("drive-cache-refresh")[0]
        self.assertEqual(job.job.trigger.interval.total_seconds(), 900)

        provider.list_workbooks.side_effect = None
        await refresh_drive_cache_job(SimpleNamespace(application=application))

        self.assertEqual(provider.list_workbooks.call_count, 2)
        self.assertIsNotNone(services.cache.status.last_success_at)
        self.assertIsNone(services.cache.status.last_failure_at)

    async def test_local_mode_keeps_lazy_loading_and_summary_jobs(self):
        services, provider = make_services("local")
        application = build_application(services.settings, services)
        self.assertIsNotNone(application.post_init)

        await application.post_init(application)

        provider.list_workbooks.assert_not_called()
        self.assertFalse(services.cache.status.refreshing)
        self.assertIsNone(services.cache.status.last_attempt_at)
        self.assertEqual(
            {job.name for job in application.job_queue.jobs()}, {"daily-summary", "weekly-summary"},
        )

    async def test_drive_without_job_queue_fails_before_polling(self):
        services, provider = make_services()
        with patch("telegram.ext._applicationbuilder.JobQueue", return_value=None):
            with self.assertWarns(PTBUserWarning):
                with self.assertRaisesRegex(RuntimeError, "job-queue"):
                    build_application(services.settings, services)
        provider.list_workbooks.assert_not_called()


class BotPollingShutdownTest(unittest.TestCase):
    def test_run_polling_waits_for_worker_on_stop_and_on_startup_failure(self):
        for polling_fails in (False, True):
            with self.subTest(polling_fails=polling_fails):
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                services, provider = make_services()
                started = asyncio.Event()
                release = Event()
                phases = []

                def download():
                    loop.call_soon_threadsafe(started.set)
                    if not release.wait(5):
                        raise TimeoutError("Shutdown did not wait for initial download")
                    return []

                provider.list_workbooks.side_effect = download
                application = build_application(services.settings, services)
                application.bot._bot_user = User(123456, "Test", True, username="test_bot")

                async def start_polling(*args, **kwargs):
                    await asyncio.wait_for(started.wait(), timeout=2)
                    if polling_fails:
                        raise RuntimeError("Polling startup failed")
                    loop.call_later(0.01, application.stop_running)

                async def wait_at_hook(app, phase):
                    phases.append(phase)
                    if len(phases) == 1:
                        self.assertTrue(services.cache.status.refreshing)
                        loop.call_later(0.01, release.set)
                    await wait_for_cache_refresh(app)

                try:
                    self.assertIsNotNone(application.post_init)
                    self.assertIs(application.post_stop, wait_for_cache_refresh)
                    self.assertIs(application.post_shutdown, wait_for_cache_refresh)
                    application.post_stop = lambda app: wait_at_hook(app, "stop")
                    application.post_shutdown = lambda app: wait_at_hook(app, "shutdown")
                    with patch.object(ExtBot, "initialize", new=AsyncMock()), patch.object(
                        ExtBot, "shutdown", new=AsyncMock(),
                    ), patch.object(Updater, "start_polling", new=AsyncMock(side_effect=start_polling)):
                        if polling_fails:
                            with self.assertRaisesRegex(RuntimeError, "Polling startup failed"):
                                application.run_polling(stop_signals=None, close_loop=False)
                        else:
                            application.run_polling(stop_signals=None, close_loop=False)
                    self.assertEqual(phases, ["shutdown"] if polling_fails else ["stop", "shutdown"])
                    self.assertFalse(services.cache.status.refreshing)
                    self.assertIsNotNone(services.cache.status.last_success_at)
                    self.assertIsNone(services.cache.status.last_failure_at)
                    provider.list_workbooks.assert_called_once_with()
                finally:
                    release.set()
                    loop.run_until_complete(services.cache.wait_for_refresh())
                    loop.run_until_complete(loop.shutdown_asyncgens())
                    loop.run_until_complete(loop.shutdown_default_executor())
                    loop.close()
                    asyncio.set_event_loop(None)


if __name__ == "__main__":
    unittest.main()
