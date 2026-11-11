#!/usr/bin/env python3
"""Extract three checked reference images from a locally supplied FWUP0037.DAT.

No USB access, downloads, firmware modification, or camera flashing. FFLZ
token format was checked against tiredboffin/fffw's MIT-licensed decoder;
see THIRD_PARTY_NOTICES.md. Only this exact v1.10 input is accepted.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import struct
import tempfile

ROOT = Path(__file__).resolve().parents[1]
INPUT_SIZE = 110561516
INPUT_SHA256 = '8a32087606b1ded920458042528054b3b0416fbb04873e6840cfde031abd0d79'
OUTPUTS = {
    'sections/fflz-009c02bc.bin': '6f37566759495965347b7eb5e0e08479430113bc4f11237f8f2c29e63c4fcddc',
    'sections/fflz-011802bc.bin': 'e2f205aa14f4f7acd7d133ecb98745541dfe025b5b5dcf0aa82e8ca63633b3ef',
    'threadx-kernel.bin': 'f6fd863952976e96a6ffa4cb90663164e41a79bbe00806a5fbf9fee32c9e0ef5',
}


def fflz(data, offset):
    if offset < 0 or offset + 20 > len(data): raise ValueError('Truncated FFLZ header')
    size, packed, pages, block, alignment = struct.unpack_from('<5I', data, offset)
    if not (block == 0x4000 and alignment == 4 and 20 <= packed < size <= 256*1024*1024
            and pages == (packed+block-1)//block and offset+packed <= len(data)):
        raise ValueError('Unexpected FFLZ bounds')
    output = bytearray(); position = offset+20; end = offset+packed
    while position < end:
        token = data[position]; position += 1
        if token < 128:
            if position+token > end: raise ValueError('Truncated FFLZ literal')
            output.extend(data[position:position+token]); position += token
        else:
            if position >= end: raise ValueError('Truncated FFLZ match')
            match = ((token & 127)<<8) | data[position]; position += 1
            distance, length = match >> 4, match & 15
            if not 0 < length <= distance <= len(output): raise ValueError('Invalid FFLZ match')
            start = len(output)-distance; output.extend(output[start:start+length])
        if len(output) > size: raise ValueError('FFLZ output exceeds declared size')
    if len(output) != size: raise ValueError('FFLZ output size differs')
    return bytes(output)


def extract(path, destination):
    path, destination = Path(path), Path(destination)
    if path.stat().st_size != INPUT_SIZE: raise ValueError('Expected the exact X-T30 III 1.10 FWUP0037.DAT (size differs)')
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != INPUT_SHA256:
        raise ValueError('Unsupported or modified firmware: FWUP0037.DAT SHA-256 differs')
    decoded = data.translate(bytes(n ^ 255 for n in range(256)))
    images = {f'sections/fflz-{offset:08x}.bin': fflz(decoded, offset)
              for offset in (0x009C02BC, 0x011802BC)}
    offset = 0xC02BC
    base, span, address = struct.unpack_from('<3Q', decoded, offset+8)
    if not 0 < span <= 1024*1024 or offset+span > len(decoded): raise ValueError('Invalid kernel bounds')
    images['threadx-kernel.bin'] = decoded[offset:offset+span]
    # Validate every result and every pre-existing target before writing any file.
    for name, image in images.items():
        if hashlib.sha256(image).hexdigest() != OUTPUTS[name]: raise ValueError(f'Unexpected reference hash: {name}')
        target = destination/name
        if target.exists() and target.read_bytes() != image:
            raise ValueError(f'Existing reference differs; left untouched: {target}')
    for name, image in images.items():
        target = destination/name
        if target.exists(): continue
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix='.import-', dir=target.parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(image); stream.flush(); os.fsync(stream.fileno())
            # Exclusive linking preserves a target created by another process.
            os.link(temporary, target)
        finally: Path(temporary).unlink(missing_ok=True)
    return dict(imported=True, input_sha256=INPUT_SHA256, files=OUTPUTS, camera_access=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('firmware', type=Path, help='Your unmodified X-T30 III 1.10 FWUP0037.DAT')
    parser.add_argument('--destination', type=Path, default=ROOT/'analysis')
    args = parser.parse_args(argv)
    try: print(json.dumps(extract(args.firmware, args.destination), indent=2))
    except (OSError, ValueError) as exc: parser.exit(1, f'{exc}\n')


if __name__ == '__main__': main()
