"""Behavioral tests; all project files and Git repositories use temporary directories."""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "checkpoint.py"
spec = importlib.util.spec_from_file_location("checkpoint", SCRIPT)
checkpoint = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checkpoint)


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mem-fix-test-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.target = self.root / "记录 space" / "HANDOFF.md"
        self.draft = self.root / "draft.md"
        self.old = "# 工作交接\n\n状态：进行中\n检查点：CP-a-0001\n".encode()
        self.new = "# 工作交接\n\n状态：已完成\n检查点：CP-a-0002\n".encode()

    def save(self, data, expected="missing"):
        self.draft.write_bytes(data)
        return checkpoint.commit(self.target, expected, self.draft)

    def previous(self):
        return self.target.with_name(self.target.name + ".prev")

    def test_first_save_rotate_and_restore_preserves_previous(self):
        self.save(self.old)
        self.assertEqual(self.target.read_bytes(), self.old)
        self.assertFalse(self.previous().exists())
        self.save(self.new, checkpoint.digest(self.old))
        self.assertEqual(self.target.read_bytes(), self.new)
        self.assertEqual(self.previous().read_bytes(), self.old)
        checkpoint.commit(self.target, checkpoint.digest(self.new), restore=True)
        self.assertEqual(self.target.read_bytes(), self.old)
        self.assertEqual(self.previous().read_bytes(), self.old)

    def test_stale_reader_cannot_overwrite_newer_save(self):
        self.save(self.old)
        self.save(self.new, checkpoint.digest(self.old))
        with self.assertRaisesRegex(checkpoint.CheckpointError, "changed"):
            self.save(b"# Stale writer\n", checkpoint.digest(self.old))
        self.assertEqual(self.target.read_bytes(), self.new)
        self.assertEqual(self.previous().read_bytes(), self.old)

    def test_invalid_draft_does_not_touch_current_or_backup(self):
        self.save(self.old)
        self.save(self.new, checkpoint.digest(self.old))
        for invalid in (b"", b"\xff", b"plain text", b"# X\n\x00"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(checkpoint.CheckpointError):
                    self.save(invalid, checkpoint.digest(self.new))
                self.assertEqual(self.target.read_bytes(), self.new)
                self.assertEqual(self.previous().read_bytes(), self.old)

    def test_corrupt_current_is_not_rotated_over_valid_backup(self):
        self.save(self.old)
        self.save(self.new, checkpoint.digest(self.old))
        self.target.write_bytes(b"\xffdamaged")
        current_hash = checkpoint.digest(self.target.read_bytes())
        with self.assertRaisesRegex(checkpoint.CheckpointError, "damaged"):
            self.save(self.new, current_hash)
        self.assertEqual(self.previous().read_bytes(), self.old)
        checkpoint.commit(self.target, current_hash, restore=True)
        self.assertEqual(self.target.read_bytes(), self.old)
        self.assertEqual(self.previous().read_bytes(), self.old)

    def test_restore_when_primary_is_missing(self):
        self.save(self.old)
        self.save(self.new, checkpoint.digest(self.old))
        self.target.unlink()
        checkpoint.commit(self.target, "missing", restore=True)
        self.assertEqual(self.target.read_bytes(), self.old)

    def test_unchanged_save_does_not_rotate_backup(self):
        self.save(self.old)
        self.save(self.new, checkpoint.digest(self.old))
        result = self.save(self.new, checkpoint.digest(self.new))
        self.assertFalse(result["changed"])
        self.assertEqual(self.previous().read_bytes(), self.old)

    def test_failed_primary_replacement_keeps_complete_old_snapshot(self):
        self.save(self.old)
        real_replace = os.replace

        def failing_replace(source, destination):
            if Path(destination) == self.target:
                raise PermissionError("Simulated replacement failure")
            return real_replace(source, destination)

        with patch.object(checkpoint.os, "replace", side_effect=failing_replace):
            with self.assertRaises(PermissionError):
                self.save(self.new, checkpoint.digest(self.old))
        self.assertEqual(self.target.read_bytes(), self.old)
        self.assertEqual(self.previous().read_bytes(), self.old)
        self.assertEqual(list(self.target.parent.glob("*.tmp")), [])

    def test_interrupted_process_releases_lock_and_preserves_old_snapshot(self):
        child = r'''
import importlib.util, os, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("checkpoint_child", sys.argv[1])
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
target = Path(sys.argv[2])
replace = m.os.replace
def interrupted_replace(source, destination):
    if Path(destination) == target and sys.argv[5] == "before":
        os._exit(23)
    replace(source, destination)
    if Path(destination) == target:
        os._exit(24)
m.os.replace = interrupted_replace
m.commit(target, sys.argv[4], Path(sys.argv[3]))
'''
        for position in ("before", "after"):
            with self.subTest(position=position):
                expected = checkpoint.inspect(self.target)["sha256"]
                self.save(self.old, expected)
                self.draft.write_bytes(self.new)
                result = subprocess.run(
                    [sys.executable, "-B", "-c", child, str(SCRIPT), str(self.target),
                     str(self.draft), checkpoint.digest(self.old), position],
                    capture_output=True, timeout=15)
                self.assertEqual(result.returncode, 23 if position == "before" else 24,
                                 result.stderr)
                expected_content = self.old if position == "before" else self.new
                self.assertEqual(self.target.read_bytes(), expected_content)
                self.assertEqual(self.previous().read_bytes(), self.old)
                # Dead writers release OS locks; leftover temps are not read.
                self.save(self.new, checkpoint.digest(expected_content))
                self.assertEqual(self.target.read_bytes(), self.new)

    def test_second_process_cannot_save_while_writer_holds_lock(self):
        self.save(self.old)
        self.draft.write_bytes(self.new)
        with checkpoint.writer_lock(self.target):
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "save", str(self.target),
                 "--source", str(self.draft), "--expect", checkpoint.digest(self.old)],
                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 1)
        self.assertIn("lock", json.loads(result.stderr)["error"])
        self.assertEqual(self.target.read_bytes(), self.old)

    def test_cli_inspect_missing_and_saved_paths_with_spaces(self):
        def inspect_cli():
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "inspect", str(self.target)],
                capture_output=True, text=True, timeout=15, check=True)
            return json.loads(result.stdout)
        self.assertEqual(inspect_cli()["sha256"], "missing")
        self.save(self.old)
        result = inspect_cli()
        self.assertTrue(result["valid_text"])
        self.assertEqual(result["sha256"], checkpoint.digest(self.old))

    def test_same_file_cannot_be_used_as_draft(self):
        self.save(self.old)
        with self.assertRaisesRegex(checkpoint.CheckpointError, "separate draft"):
            checkpoint.commit(self.target, checkpoint.digest(self.old), self.target)

    def test_non_git_hashes_include_missing_files(self):
        (self.root / "a.py").write_bytes(b"a = 1\n")
        result = checkpoint.code_state(self.root, ["a.py", "deleted.py"])
        self.assertIsNone(result["head"])
        self.assertEqual(result["files"][1]["sha256"], "missing")
        self.assertEqual(result["files"][0]["sha256"], checkpoint.digest(b"a = 1\n"))

    def test_state_rejects_external_paths_and_directories(self):
        for path in ("../external", ".", str(self.root / "absolute.py")):
            with self.subTest(path=path):
                with self.assertRaises(checkpoint.CheckpointError):
                    checkpoint.code_state(self.root, [path])

    @unittest.skipUnless(shutil.which("git"), "Git unavailable")
    def test_same_head_and_dirty_flags_still_detect_content_changes(self):
        def git(*args):
            return subprocess.run(["git", "-C", str(self.root), *args],
                                  capture_output=True, check=True, timeout=15)
        source = self.root / "source.py"
        source.write_text("value = 1\n", encoding="utf-8")
        git("init")
        git("add", "source.py")
        git("-c", "user.name=MemFix Test", "-c", "user.email=memfix@example.invalid",
            "-c", "commit.gpgsign=false", "-c", "core.hooksPath=disabled-hooks",
            "commit", "-m", "Test fixture")
        source.write_text("value = 2\n", encoding="utf-8")
        before = checkpoint.code_state(self.root, ["source.py"])
        source.write_text("value = 3\n", encoding="utf-8")
        after = checkpoint.code_state(self.root, ["source.py"])
        self.assertEqual(before["head"], after["head"])
        self.assertEqual(before["git_status"], after["git_status"])
        self.assertNotEqual(before["state_id"], after["state_id"])
        (self.root / "HANDOFF.md").write_text("# New checkpoint\n", encoding="utf-8")
        self.assertEqual(after["state_id"], checkpoint.code_state(
            self.root, ["source.py"])["state_id"])


if __name__ == "__main__":
    unittest.main()
