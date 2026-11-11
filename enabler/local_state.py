"""Durable local journals; no camera access."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import uuid


def now():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.part')
    try:
        with open(temporary, 'x', opener=lambda name, flags: os.open(name, flags, 0o600)) as stream:
            json.dump(value, stream, indent=2); stream.write('\n')
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
        # Windows cannot open directories through os.open. File contents are
        # flushed above on both platforms before the atomic replacement.
        if os.name != 'nt':
            fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try: os.fsync(fd)
            finally: os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)
