"""Distribution boundary regressions; run: python -m unittest discover -s tools."""

import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import export_public


class PublicExportTests(unittest.TestCase):
    def test_tracked_secrets_local_data_and_history_are_excluded(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "source"
            root.mkdir()
            files = {
                "README.md": "English documentation\n",
                ".env": "PRIVATE_TEST_VALUE=fixture\n",
                ".env.example": "PRIVATE_TEST_VALUE=\n",
                ".local/components/private.json": "{}",
                "data/components/evidence/private.json": "{}",
                "data/components/motors.json": "[]",
                "data/components/propulsion_tests.json": '{"tests": []}',
            }
            for name, contents in files.items():
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(contents, encoding="utf-8")
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "-C", str(root), "add", "."], check=True)
            output = Path(temporary) / "public"
            with patch.object(
                export_public, "__file__", str(root / "tools/export_public.py")
            ):
                self.assertEqual(export_public.main(["--output", str(output)]), 0)
            document = json.loads((output / "PUBLIC_EXPORT.json").read_text())
            self.assertEqual(document["format"], 2)
            manifest = document["files"]
            for record in manifest:
                self.assertEqual(set(record), {"path", "sha256"})
                self.assertEqual(
                    record["sha256"],
                    hashlib.sha256((output / record["path"]).read_bytes()).hexdigest(),
                )
            self.assertEqual(
                {record["path"] for record in manifest},
                {
                    "README.md",
                    ".env.example",
                    "data/components/motors.json",
                    "data/components/propulsion_tests.json",
                },
            )
            self.assertFalse((output / ".git").exists())
            self.assertFalse((output / ".env").exists())
            self.assertFalse((output / ".local").exists())
            self.assertEqual((root / ".env").read_text(), files[".env"])

    def test_path_escape_and_private_key_extensions_are_rejected(self):
        for name in (
            "../outside",
            "/absolute",
            "secret.pem",
            "a/.env.local",
            "a/node_modules/b",
        ):
            with self.subTest(name=name):
                self.assertFalse(export_public.allowed(Path(name)))
        self.assertTrue(export_public.allowed(Path("tests/multilingual.json")))

    def test_maintainer_only_files_are_excluded(self):
        for name in ("SKILL.md", "docs/OPEN_SOURCE_REVIEW_2026-09-30.md"):
            with self.subTest(name=name):
                self.assertFalse(export_public.allowed(Path(name)))
        self.assertTrue(export_public.allowed(Path("docs/SKILL.md")))

    def test_existing_destination_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(SystemExit):
                export_public.main(["--output", temporary])
            self.assertTrue(Path(temporary).is_dir())


if __name__ == "__main__":
    unittest.main()
