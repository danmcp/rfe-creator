#!/usr/bin/env python3
"""Tests for generate_diff helpers: path safety, frontmatter stripping, size cap."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from generate_review_pdf import (
    MAX_DIFF_FILE_SIZE,
    _open_nofollow,
    _safe_artifact_path,
    _strip_frontmatter,
    generate_diff,
)


class TestSafeArtifactPath:
    def test_normal_file(self, tmp_path):
        f = tmp_path / "file.md"
        f.write_text("hello")
        assert _safe_artifact_path(str(tmp_path), "file.md") == str(f.resolve())

    def test_missing_file(self, tmp_path):
        result = _safe_artifact_path(str(tmp_path), "missing.md")
        assert result is None or not os.path.exists(result)

    def test_symlink_rejected(self, tmp_path):
        target = tmp_path / "secret.txt"
        target.write_text("secret")
        link = tmp_path / "link.md"
        link.symlink_to(target)
        assert _safe_artifact_path(str(tmp_path), "link.md") is None

    def test_traversal_rejected(self, tmp_path):
        outside = tmp_path / "outside.txt"
        outside.write_text("outside")
        subdir = tmp_path / "sub"
        subdir.mkdir()
        assert _safe_artifact_path(str(subdir), "../outside.txt") is None

    def test_symlink_to_outside_rejected(self, tmp_path):
        outside = tmp_path / "outside.txt"
        outside.write_text("secret")
        subdir = tmp_path / "artifacts"
        subdir.mkdir()
        link = subdir / "bad.md"
        link.symlink_to(outside)
        assert _safe_artifact_path(str(subdir), "bad.md") is None


class TestStripFrontmatter:
    def test_no_frontmatter(self):
        text = "Just some content\nwith lines\n"
        assert _strip_frontmatter(text) == text

    def test_standard_frontmatter(self):
        text = "---\ntitle: Test\nstatus: Ready\n---\nBody content\n"
        assert _strip_frontmatter(text) == "Body content\n"

    def test_triple_hyphens_in_body_preserved(self):
        text = "---\ntitle: Test\n---\nSome text\n--- separator ---\nMore text\n"
        result = _strip_frontmatter(text)
        assert "--- separator ---" in result
        assert "More text" in result

    def test_no_closing_delimiter(self):
        text = "---\ntitle: Test\nno closing\n"
        assert _strip_frontmatter(text) == text

    def test_empty_string(self):
        assert _strip_frontmatter("") == ""

    def test_hyphens_inside_content_not_split(self):
        text = "---\nkey: value\n---\nLine with --- in middle\n"
        result = _strip_frontmatter(text)
        assert result == "Line with --- in middle\n"

    def test_old_split_would_fail(self):
        """Old split('---', 2) would incorrectly split on '---' inside values."""
        text = "---\nkey: a---b\n---\nBody\n"
        result = _strip_frontmatter(text)
        assert result == "Body\n"


class TestGenerateDiffSizeCap:
    def test_oversized_original_returns_none(self, tmp_path):
        originals = tmp_path / "originals"
        tasks = tmp_path / "tasks"
        originals.mkdir()
        tasks.mkdir()
        (originals / "BIG.md").write_text("x" * (MAX_DIFF_FILE_SIZE + 1))
        (tasks / "BIG.md").write_text("small content")
        assert generate_diff("BIG", str(tasks), str(originals)) is None

    def test_oversized_revised_returns_none(self, tmp_path):
        originals = tmp_path / "originals"
        tasks = tmp_path / "tasks"
        originals.mkdir()
        tasks.mkdir()
        (originals / "BIG.md").write_text("small content")
        (tasks / "BIG.md").write_text("x" * (MAX_DIFF_FILE_SIZE + 1))
        assert generate_diff("BIG", str(tasks), str(originals)) is None

    def test_normal_size_produces_diff(self, tmp_path):
        originals = tmp_path / "originals"
        tasks = tmp_path / "tasks"
        originals.mkdir()
        tasks.mkdir()
        (originals / "RFE-001.md").write_text("original line\n")
        (tasks / "RFE-001.md").write_text("revised line\n")
        result = generate_diff("RFE-001", str(tasks), str(originals))
        assert result is not None
        assert "-original line" in result
        assert "+revised line" in result


class TestOpenNofollow:
    def test_regular_file(self, tmp_path):
        f = tmp_path / "file.md"
        f.write_text("hello")
        result = _open_nofollow(str(f), 1024)
        assert result is not None
        with result:
            assert result.read() == "hello"

    def test_symlink_rejected(self, tmp_path):
        target = tmp_path / "target.md"
        target.write_text("secret")
        link = tmp_path / "link.md"
        link.symlink_to(target)
        assert _open_nofollow(str(link), 1024) is None

    def test_oversized_rejected(self, tmp_path):
        f = tmp_path / "big.md"
        f.write_text("x" * 100)
        assert _open_nofollow(str(f), 50) is None

    def test_missing_file(self, tmp_path):
        assert _open_nofollow(str(tmp_path / "missing.md"), 1024) is None


class TestGenerateDiffSymlinkRejection:
    def test_symlinked_original_rejected(self, tmp_path):
        originals = tmp_path / "originals"
        tasks = tmp_path / "tasks"
        secret = tmp_path / "secret.txt"
        originals.mkdir()
        tasks.mkdir()
        secret.write_text("secret data\n")
        (originals / "RFE-001.md").symlink_to(secret)
        (tasks / "RFE-001.md").write_text("revised\n")
        assert generate_diff("RFE-001", str(tasks), str(originals)) is None

    def test_symlinked_revised_rejected(self, tmp_path):
        originals = tmp_path / "originals"
        tasks = tmp_path / "tasks"
        secret = tmp_path / "secret.txt"
        originals.mkdir()
        tasks.mkdir()
        secret.write_text("secret data\n")
        (originals / "RFE-001.md").write_text("original\n")
        (tasks / "RFE-001.md").symlink_to(secret)
        assert generate_diff("RFE-001", str(tasks), str(originals)) is None
