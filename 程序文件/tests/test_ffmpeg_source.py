import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fetch_ffmpeg_source as fetch


class FFmpegSourceTests(unittest.TestCase):
    def test_existing_source_is_verified_without_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'source.tar.xz'
            path.write_bytes(b'known source')
            with patch.object(fetch, 'SHA256', fetch.digest(path)), patch.object(fetch.urllib.request, 'urlopen') as network:
                self.assertEqual(fetch.ensure_source(path), path)
                network.assert_not_called()

    def test_corrupt_existing_source_is_not_silently_replaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'source.tar.xz'
            path.write_bytes(b'bad')
            with patch.object(fetch.urllib.request, 'urlopen') as network:
                with self.assertRaises(ValueError):
                    fetch.ensure_source(path)
                network.assert_not_called()
            self.assertEqual(path.read_bytes(), b'bad')

    def test_invalid_download_is_not_published_and_temp_is_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'source.tar.xz'
            with patch.object(fetch.urllib.request, 'urlopen', return_value=io.BytesIO(b'bad')):
                with self.assertRaises(ValueError):
                    fetch.ensure_source(path)
            self.assertFalse(path.exists())
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_verified_download_is_published(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'source.tar.xz'
            content = b'known source'
            with patch.object(fetch, 'SHA256', fetch.hashlib.sha256(content).hexdigest()), patch.object(fetch.urllib.request, 'urlopen', return_value=io.BytesIO(content)):
                self.assertEqual(fetch.ensure_source(path).read_bytes(), content)
            self.assertEqual(list(Path(tmp).iterdir()), [path])
