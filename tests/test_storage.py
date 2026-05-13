from pathlib import Path
import tempfile
import unittest

from eu_grades_bot.storage import UserStorage


class UserStorageTest(unittest.TestCase):
    def test_delete_removes_user(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = UserStorage(Path(tmpdir) / "users.json")
            storage.set_email(123, "student@example.com")

            self.assertIsNotNone(storage.get(123))
            self.assertTrue(storage.delete(123))
            self.assertIsNone(storage.get(123))
            self.assertEqual(storage.all_users(), [])
            self.assertFalse(storage.delete(123))


if __name__ == "__main__":
    unittest.main()
