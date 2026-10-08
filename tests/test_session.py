import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.newyorker.session import SessionError, load_session, newyorker_state, save_session


class SessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "private" / "session.json"
        self.state = {"cookies": [], "origins": []}

    def test_round_trip_creates_private_directory_and_file(self) -> None:
        save_session(self.path, self.state)
        self.assertEqual(load_session(self.path), self.state)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_serialization_failure_preserves_old_state_and_removes_temporary_file(self) -> None:
        save_session(self.path, self.state)
        with self.assertRaises(SessionError):
            save_session(self.path, {"cookies": [object()], "origins": []})
        self.assertEqual(load_session(self.path), self.state)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_replace_failure_preserves_old_state(self) -> None:
        save_session(self.path, self.state)
        with patch("app.newyorker.session.os.replace", side_effect=OSError("failure")):
            with self.assertRaises(SessionError):
                save_session(self.path, {"cookies": [], "origins": [{"origin": "new"}]})
        self.assertEqual(load_session(self.path), self.state)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_existing_public_directory_is_not_modified(self) -> None:
        self.path.parent.mkdir(mode=0o755)
        os.chmod(self.path.parent, 0o755)
        with self.assertRaises(SessionError):
            save_session(self.path, self.state)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o755)
        self.assertFalse(self.path.exists())

    def test_symlink_cannot_replace_another_file(self) -> None:
        target = Path(self.directory.name) / "target.json"
        save_session(target, self.state)
        self.path.parent.mkdir(mode=0o700)
        self.path.symlink_to(target)
        with self.assertRaises(SessionError):
            save_session(self.path, {"cookies": [], "origins": [{}]})
        with self.assertRaises(SessionError):
            load_session(self.path)
        self.assertEqual(load_session(target), self.state)

    def test_missing_malformed_and_public_files_fail_without_contents_in_errors(self) -> None:
        with self.assertRaises(SessionError):
            load_session(self.path)
        save_session(self.path, self.state)
        self.path.write_text('SECRET_NOT_JSON', encoding="utf-8")
        with self.assertRaises(SessionError) as caught:
            load_session(self.path)
        self.assertNotIn("SECRET_NOT_JSON", str(caught.exception))
        self.path.write_text(json.dumps({"cookies": "secret", "origins": []}))
        with self.assertRaises(SessionError):
            load_session(self.path)
        save_session(self.path, self.state)
        os.chmod(self.path, 0o644)
        with self.assertRaises(SessionError):
            load_session(self.path)

    def test_only_newyorker_state_is_retained_without_modifying_source(self) -> None:
        cookie = {"domain": ".newyorker.com", "value": "newyorker-secret"}
        origin = {
            "origin": "https://www.newyorker.com",
            "localStorage": [],
            "indexedDB": [{"name": "login"}],
        }
        captured = {
            "cookies": [cookie, {"domain": ".google.com"}, {"domain": ".evilnewyorker.com"}],
            "origins": [origin, {"origin": "https://accounts.google.com"},
                        {"origin": "http://www.newyorker.com"},
                        {"origin": "https://newyorker.com.evil.test"}],
        }
        cleaned = newyorker_state(captured)
        self.assertEqual(cleaned, {"cookies": [cookie], "origins": [origin]})
        cleaned["cookies"][0]["value"] = "changed"
        self.assertEqual(cookie["value"], "newyorker-secret")


if __name__ == "__main__":
    unittest.main()
