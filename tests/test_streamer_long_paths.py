"""Raw stream writes preserve atomic publication for maximum-length names."""

import tempfile
import unittest
from pathlib import Path

from gamdl.stream import Streamer


class _Chunks:
    save = Streamer.save

    async def __aiter__(self):
        yield b"first"
        yield b"-second"


class StreamerLongPathTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_save_at_maximum_component_size(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            output = folder / ("🔥" * 62 + "abc" + ".m4a")
            self.assertEqual(len(output.name.encode("utf-8")), 255)
            result = await _Chunks().save(output)
            self.assertEqual(result, output)
            self.assertEqual(output.read_bytes(), b"first-second")
            self.assertFalse(list(folder.glob(".gamdl-*")))

            # Replacing the same destination should work as well.
            await _Chunks().save(output)
            self.assertEqual(output.read_bytes(), b"first-second")
