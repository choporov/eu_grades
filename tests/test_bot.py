import asyncio
from datetime import datetime, timezone
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

from telegram import Chat, Message, MessageEntity, Update, User
from telegram.ext import CommandHandler, ExtBot

from eu_grades_bot.bot import (
    DRIVE_REFRESH_INITIAL_DELAY_SECONDS,
    DRIVE_REFRESH_INTERVAL_SECONDS,
    build_application,
    format_cache_status,
    handle_bot_error,
    refresh_command,
    refresh_drive_cache_job,
    schedule_jobs,
    with_cache_warning,
)
from eu_grades_bot.cache import GradesCache
from eu_grades_bot.grades import CacheNotReadyError, GradesRepository


class BotSchedulingTest(unittest.IsolatedAsyncioTestCase):
    def test_schedules_drive_cache_refresh_every_fifteen_minutes(self):
        job_queue = Mock()
        application = SimpleNamespace(job_queue=job_queue)
        settings = SimpleNamespace(
            grades_source="drive",
            timezone=ZoneInfo("Europe/Kyiv"),
        )

        schedule_jobs(application, settings)

        job_queue.run_repeating.assert_called_once_with(
            refresh_drive_cache_job,
            interval=DRIVE_REFRESH_INTERVAL_SECONDS,
            first=DRIVE_REFRESH_INITIAL_DELAY_SECONDS,
            name="drive-cache-refresh",
        )
        self.assertEqual(DRIVE_REFRESH_INTERVAL_SECONDS, 15 * 60)

    def test_does_not_schedule_drive_refresh_for_local_source(self):
        job_queue = Mock()
        application = SimpleNamespace(job_queue=job_queue)
        settings = SimpleNamespace(
            grades_source="local",
            timezone=ZoneInfo("Europe/Kyiv"),
        )

        schedule_jobs(application, settings)

        job_queue.run_repeating.assert_not_called()

    async def test_drive_cache_refresh_job_reloads_repository(self):
        provider = Mock()
        provider.list_workbooks.return_value = []
        services = SimpleNamespace(cache=GradesCache(GradesRepository(provider, reload_when_stale=False)))
        context = SimpleNamespace(
            application=SimpleNamespace(bot_data={"services": services}),
        )

        await refresh_drive_cache_job(context)

        provider.list_workbooks.assert_called_once_with()
        self.assertIsNotNone(services.cache.status.last_success_at)

    async def test_drive_cache_refresh_job_logs_failure_without_raising(self):
        provider = Mock()
        provider.list_workbooks.side_effect = RuntimeError("Drive unavailable")
        services = SimpleNamespace(cache=GradesCache(GradesRepository(provider, reload_when_stale=False)))
        context = SimpleNamespace(
            application=SimpleNamespace(bot_data={"services": services}),
        )

        with self.assertLogs("eu_grades_bot.cache", level="ERROR"):
            await refresh_drive_cache_job(context)

        provider.list_workbooks.assert_called_once_with()
        self.assertIsNone(services.cache.status.last_success_at)
        self.assertIsNotNone(services.cache.status.last_failure_at)

    def test_registers_refresh_command_handler(self):
        settings = SimpleNamespace(
            telegram_bot_token="123456:TEST_TOKEN",
            grades_source="drive",
            timezone=ZoneInfo("Europe/Kyiv"),
        )

        application = build_application(settings, SimpleNamespace())

        registered_commands = {
            command
            for handlers in application.handlers.values()
            for handler in handlers
            if isinstance(handler, CommandHandler)
            for command in handler.commands
        }
        self.assertIn("refresh", registered_commands)
        self.assertIn("cache", registered_commands)

    async def test_refresh_command_forces_repository_reload(self):
        user = SimpleNamespace(email="student@example.com")
        storage = Mock()
        storage.get.return_value = user
        repository = Mock()
        repository.cache_info.return_value = (datetime.now(timezone.utc), 0)
        repository.reload_when_stale = False
        repository.get_cached_student_grades.return_value = SimpleNamespace(
            disciplines=("Біологія",),
        )
        services = SimpleNamespace(
            storage=storage,
            cache=GradesCache(repository),
            settings=SimpleNamespace(timezone=ZoneInfo("Europe/Kyiv")),
        )
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=123),
            message=SimpleNamespace(reply_text=AsyncMock()),
        )
        context = SimpleNamespace(
            application=SimpleNamespace(bot_data={"services": services}),
        )

        await refresh_command(update, context)

        repository.reload.assert_called_once_with()
        repository.get_cached_student_grades.assert_called_once_with("student@example.com")
        self.assertEqual(update.message.reply_text.await_count, 2)
        self.assertIn("Останнє успішне оновлення:", update.message.reply_text.call_args.args[0])

    def test_drive_cannot_start_without_refresh_scheduler(self):
        with self.assertRaisesRegex(RuntimeError, "job-queue"):
            schedule_jobs(SimpleNamespace(job_queue=None), SimpleNamespace(grades_source="drive"))

    def test_status_and_reports_warn_about_old_cache_in_bot_timezone(self):
        repository = Mock()
        repository.cache_info.return_value = (datetime(2026, 9, 11, 12, tzinfo=timezone.utc), 1800)
        services = SimpleNamespace(
            cache=GradesCache(repository),
            settings=SimpleNamespace(timezone=ZoneInfo("Europe/Kyiv")),
        )
        status = format_cache_status(services)
        self.assertIn("11.09.2026 15:00:00", status)
        self.assertIn("30 хв", status)
        self.assertIn("застарілими", status)
        self.assertIn("застарілими", with_cache_warning(services, "Report"))
        repository.cache_info.return_value = (datetime.now(timezone.utc), 1)
        self.assertEqual(with_cache_warning(services, "Report"), "Report")

    async def test_initial_unavailable_cache_gets_clear_response(self):
        update = Update(1, message=Message(1, datetime.now(timezone.utc), Chat(1, "private")))
        context = SimpleNamespace(error=CacheNotReadyError(), bot=SimpleNamespace(send_message=AsyncMock()))
        await handle_bot_error(update, context)
        self.assertIn("Таблиці ще не завантажені", context.bot.send_message.call_args.kwargs["text"])

    async def test_failed_manual_refresh_reports_failure_without_losing_last_success(self):
        provider = Mock()
        provider.list_workbooks.return_value = []
        cache = GradesCache(GradesRepository(provider, reload_when_stale=False))
        await cache.refresh()
        previous_time = cache.status.last_success_at
        provider.list_workbooks.side_effect = RuntimeError("Drive unavailable")
        storage = Mock()
        storage.get.return_value = SimpleNamespace(email="student@example.com")
        services = SimpleNamespace(
            cache=cache, storage=storage, settings=SimpleNamespace(timezone=ZoneInfo("Europe/Kyiv")),
        )
        update = SimpleNamespace(effective_chat=SimpleNamespace(id=1), message=SimpleNamespace(reply_text=AsyncMock()))
        context = SimpleNamespace(application=SimpleNamespace(bot_data={"services": services}))
        with self.assertLogs("eu_grades_bot.cache", level="ERROR"):
            await refresh_command(update, context)
        self.assertEqual(cache.status.last_success_at, previous_time)
        response = update.message.reply_text.call_args.args[0]
        self.assertIn("Не вдалося оновити", response)
        self.assertIn("попередні успішно завантажені дані", response)

    async def test_dispatch_serves_commands_while_manual_and_scheduled_refresh_share_worker(self):
        provider = Mock()
        provider.list_workbooks.return_value = []
        cache = GradesCache(GradesRepository(provider, reload_when_stale=False))
        await cache.refresh()
        provider.reset_mock()
        started = asyncio.Event()
        release = Event()
        loop = asyncio.get_running_loop()

        def download():
            loop.call_soon_threadsafe(started.set)
            if not release.wait(5):
                raise TimeoutError("Test did not release download")
            return []

        provider.list_workbooks.side_effect = download
        storage = Mock()
        storage.get.return_value = SimpleNamespace(email="student@example.com")
        settings = SimpleNamespace(
            telegram_bot_token="123456:TEST_TOKEN", grades_source="drive", timezone=ZoneInfo("Europe/Kyiv"),
        )
        services = SimpleNamespace(cache=cache, storage=storage, settings=settings)
        application = build_application(settings, services)
        # Exercise real PTB dispatch with a local bot identity and no Telegram I/O.
        application._initialized = True
        application._running = True
        application.bot._bot_user = User(123456, "Test", True, username="test_bot")
        replies = []
        completed = {1: asyncio.Event(), 3: asyncio.Event()}

        async def reply(*args, **kwargs):
            replies.append(kwargs)
            if kwargs["chat_id"] in completed and "Останнє успішне оновлення:" in kwargs["text"]:
                completed[kwargs["chat_id"]].set()

        def command(chat_id, text):
            message = Message(
                chat_id, datetime.now(timezone.utc), Chat(chat_id, "private"), text=text,
                entities=[MessageEntity(MessageEntity.BOT_COMMAND, 0, len(text))],
            )
            message.set_bot(application.bot)
            return Update(chat_id, message=message)

        scheduled = None
        with patch.object(ExtBot, "send_message", new=AsyncMock(side_effect=reply)):
            try:
                await asyncio.wait_for(application.process_update(command(1, "/refresh")), timeout=1)
                await asyncio.wait_for(started.wait(), timeout=2)
                await asyncio.wait_for(application.process_update(command(2, "/subjects")), timeout=1)
                self.assertTrue(any(item["chat_id"] == 2 for item in replies))
                await asyncio.wait_for(application.process_update(command(3, "/refresh")), timeout=1)
                scheduled = asyncio.create_task(refresh_drive_cache_job(SimpleNamespace(application=application)))
                await asyncio.wait_for(application.process_update(command(4, "/cache")), timeout=1)
                self.assertTrue(any(item["chat_id"] == 4 and "Оновлення триває" in item["text"] for item in replies))
                # Let the scheduled job join before completing the worker.
                await asyncio.sleep(0)
                self.assertEqual(provider.list_workbooks.call_count, 1)
            finally:
                release.set()
                await cache.wait_for_refresh()
                if scheduled is not None:
                    await scheduled
                await asyncio.wait_for(asyncio.gather(*(event.wait() for event in completed.values())), timeout=2)
                application._running = False
        self.assertEqual(provider.list_workbooks.call_count, 1)


if __name__ == "__main__":
    unittest.main()
