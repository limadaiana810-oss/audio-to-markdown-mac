"""Fetch the exact upstream FFmpeg source used by the portable release."""
import hashlib
import os
from pathlib import Path
import tempfile
import urllib.request

URL = 'https://ffmpeg.org/releases/ffmpeg-9.0.2.tar.xz'
SHA256 = '8c3850283eb25fa026482078a04051e0be17347b09ef81a0849bec15a96e002e'
DESTINATION = Path(__file__).resolve().parent.parent / '分发资源/ffmpeg-9.0.2.tar.xz'


def digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def ensure_source(destination=DESTINATION):
    destination = Path(destination)
    if destination.exists():
        if digest(destination) != SHA256:
            raise ValueError('FFmpeg source checksum mismatch: ' + str(destination))
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    pending = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, suffix='.part', delete=False) as stream:
            pending = Path(stream.name)
            with urllib.request.urlopen(URL, timeout=90) as response:
                while chunk := response.read(1024 * 1024):
                    stream.write(chunk)
        if digest(pending) != SHA256:
            raise ValueError('Downloaded FFmpeg source checksum mismatch')
        os.replace(pending, destination)
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)
    return destination


if __name__ == '__main__':
    print(ensure_source())
