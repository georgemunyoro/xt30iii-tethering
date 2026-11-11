from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

import import_firmware as f


class ImportTests(unittest.TestCase):
    def test_rejects_wrong_input_without_creating_references(self):
        with tempfile.TemporaryDirectory() as d:
            source=Path(d)/'wrong.dat';source.write_bytes(b'not firmware')
            target=Path(d)/'analysis'
            with self.assertRaisesRegex(ValueError,'size differs'):f.extract(source,target)
            self.assertFalse(target.exists())

    def test_hash_gate_precedes_any_decoding_or_writes(self):
        with tempfile.TemporaryDirectory() as d:
            source=Path(d)/'wrong.dat';source.write_bytes(b'changed')
            with patch.object(f,'INPUT_SIZE',7),patch.object(f,'fflz') as decode:
                with self.assertRaisesRegex(ValueError,'SHA-256 differs'):f.extract(source,Path(d)/'analysis')
                decode.assert_not_called()

    def test_literal_and_match_stream(self):
        # Enough matches that the compressed stream is smaller than its output.
        payload=b'\x03ABC'+b'\x80\x33'*30
        header=struct.pack('<5I',93,20+len(payload),1,0x4000,4)
        self.assertEqual(f.fflz(header+payload,0),b'ABC'*31)

    def test_corrupt_bounds_and_matches_are_rejected(self):
        for data in (b'',struct.pack('<5I',100,22,1,0x4000,4)+b'\x80\x13'):
            with self.assertRaises(ValueError):f.fflz(data,0)


if __name__ == '__main__':unittest.main()
