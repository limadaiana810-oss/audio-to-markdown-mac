"""Prevent stale links, mismatched application versions, and corrupt release assets."""
import hashlib
from pathlib import Path
import plistlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from check_release import check_release


class ReleaseIdentityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="tx-release-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "程序文件").mkdir()
        (self.root / "程序文件/VERSION").write_text("2.5.1\n")
        (self.root / "CHANGELOG.md").write_text("## [2.5.1] - 2026-11-16\n")
        self.asset = "Audio-to-Markdown-2.5.1-AppleSilicon.dmg"
        for name in ("README.md", "README.en.md"):
            (self.root / name).write_text("https://github.com/example/project/releases/download/v2.5.1/" + self.asset)

    def test_tag_cannot_point_to_different_app_version(self):
        self.assertEqual(check_release(self.root, tag="v2.5.1"), "2.5.1")
        with self.assertRaisesRegex(ValueError, "Tag"):
            check_release(self.root, tag="v2.5.0")

    def test_old_english_download_link_blocks_release(self):
        english = self.root / "README.en.md"
        english.write_text(english.read_text().replace("2.5.1", "2.5.0"))
        with self.assertRaisesRegex(ValueError, "README.en.md"):
            check_release(self.root)

    def test_missing_current_changelog_blocks_release(self):
        (self.root / "CHANGELOG.md").write_text("## [2.5.0]\n")
        with self.assertRaisesRegex(ValueError, "CHANGELOG"):
            check_release(self.root)

    def test_linked_changelog_heading_is_a_valid_release_entry(self):
        (self.root / "CHANGELOG.md").write_text(
            "## [2.5.1](https://github.com/example/project/releases/tag/v2.5.1) — 2026-11-16\n")
        self.assertEqual(check_release(self.root, tag="v2.5.1"), "2.5.1")

    def test_changed_installer_bytes_invalidate_checksum(self):
        dmg = self.root / self.asset
        dmg.write_bytes(b"original installer fixture")
        Path(str(dmg) + ".sha256").write_text(hashlib.sha256(dmg.read_bytes()).hexdigest() + "  " + dmg.name + "\n")
        check_release(self.root, dmg=dmg)
        dmg.write_bytes(b"changed installer fixture")
        with self.assertRaisesRegex(ValueError, "checksum"):
            check_release(self.root, dmg=dmg)

    def test_app_metadata_and_bundled_version_must_agree(self):
        app = self.root / "Example.app"
        (app / "Contents/Resources/程序文件").mkdir(parents=True)
        with (app / "Contents/Info.plist").open("wb") as stream:
            plistlib.dump({"CFBundleShortVersionString": "2.5.1", "CFBundleVersion": "2.5.1"}, stream)
        version = app / "Contents/Resources/程序文件/VERSION"
        version.write_text("2.5.1\n")
        check_release(self.root, app=app)
        version.write_text("2.5.0\n")
        with self.assertRaisesRegex(ValueError, "Bundled"):
            check_release(self.root, app=app)


if __name__ == "__main__":
    unittest.main()
