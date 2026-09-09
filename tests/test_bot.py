from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

from telegram.ext import CommandHandler

from eu_grades_bot.bot import (
    DRIVE_REFRESH_INITIAL_DELAY_SECONDS,
    DRIVE_REFRESH_INTERVAL_SECONDS,
    build_application,
    refresh_command,
    refresh_drive_cache_job,
    schedule_jobs,
)


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
        repository = Mock()
        services = SimpleNamespace(repository=repository)
        context = SimpleNamespace(
            application=SimpleNamespace(bot_data={"services": services}),
        )

        await refresh_drive_cache_job(context)

        repository.reload.assert_called_once_with()

    async def test_drive_cache_refresh_job_logs_failure_without_raising(self):
        repository = Mock()
        repository.reload.side_effect = RuntimeError("Drive unavailable")
        services = SimpleNamespace(repository=repository)
        context = SimpleNamespace(
            application=SimpleNamespace(bot_data={"services": services}),
        )

        with self.assertLogs("eu_grades_bot.bot", level="ERROR"):
            await refresh_drive_cache_job(context)

        repository.reload.assert_called_once_with()

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

    async def test_refresh_command_forces_repository_reload(self):
        user = SimpleNamespace(email="student@example.com")
        storage = Mock()
        storage.get.return_value = user
        repository = Mock()
        repository.get_student_grades.return_value = SimpleNamespace(
            disciplines=("Біологія",),
        )
        services = SimpleNamespace(storage=storage, repository=repository)
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=123),
            message=SimpleNamespace(reply_text=AsyncMock()),
        )
        context = SimpleNamespace(
            application=SimpleNamespace(bot_data={"services": services}),
        )

        await refresh_command(update, context)

        repository.get_student_grades.assert_called_once_with(
            "student@example.com",
            force_reload=True,
        )
        update.message.reply_text.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
