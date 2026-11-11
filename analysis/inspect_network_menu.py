#!/usr/bin/env python3
"""Inspect unchanged X-T30 III 1.10 menu tables; no camera access or patching.

Run: python analysis/inspect_network_menu.py
Optional: --output path.json writes the extracted evidence to a new file.
Addresses are interpreted using the known MainA/MainB section load bases.
This parser does not execute the camera's menu renderer.
"""
import argparse
import hashlib
import json
import struct
from pathlib import Path

ROOT = Path(__file__).resolve().parent
IMAGES = {
    "MainA": ("fflz-009c02bc.bin", 0x011C1000,
              "6f37566759495965347b7eb5e0e08479430113bc4f11237f8f2c29e63c4fcddc"),
    "MainB": ("fflz-011802bc.bin", 0x02D85000,
              "e2f205aa14f4f7acd7d133ecb98745541dfe025b5b5dcf0aa82e8ca63633b3ef"),
}


class Image:
    def __init__(self, name):
        filename, self.base, expected = IMAGES[name]
        self.path = ROOT / "sections" / filename
        self.data = self.path.read_bytes()
        actual = hashlib.sha256(self.data).hexdigest()
        if actual != expected:
            raise ValueError(f"Unexpected {name} image hash: {actual}")
        self.sha256 = actual

    def unpack(self, fmt, address):
        offset = address - self.base
        size = struct.calcsize(fmt)
        if not 0 <= offset <= len(self.data) - size:
            raise ValueError(f"Address outside image: {address:#x}")
        return struct.unpack_from(fmt, self.data, offset)

    def string(self, address):
        offset = address - self.base
        if not 0 <= offset < len(self.data):
            raise ValueError("String address outside image")
        end = self.data.index(b"\0", offset, min(offset + 256, len(self.data)))
        return self.data[offset:end].decode("ascii")


def descriptor(image, address):
    kind, field04, parameter, field0c = image.unpack("<4I", address)
    parent, items, attributes = image.unpack("<3Q", address + 0x18)
    table_id, = image.unpack("<I", address + 0x30)
    return {
        "address": hex(address), "artifact_offset": hex(address - image.base),
        "kind": hex(kind), "parameter": hex(parameter), "table_id": hex(table_id),
        "field_04": field04, "field_0c": field0c,
        "parent_pointer": hex(parent), "items_pointer": hex(items),
        "attributes_pointer": hex(attributes),
    }


def dictionary_name(image, table, number):
    string_ptr, stored_number = image.unpack("<QI", table + number * 16)
    if stored_number != number:
        raise ValueError(f"Dictionary index mismatch for {number:#x}")
    return image.string(string_ptr)


def inspect():
    a, b = Image("MainA"), Image("MainB")
    menu_names = 0x03626DE8 - 0x024D * 16
    setting_names = 0x03622808 - 0x0E72 * 16
    assert dictionary_name(b, menu_names, 0x24D) == "UI_MENUCOM_ITEM_COMM_FTP_TRANSFER_SET"
    assert dictionary_name(b, menu_names, 0x26A) == "UI_MENUCOM_ITEM_COMM_FRAMEIO_SET"
    assert dictionary_name(b, setting_names, 0xA) == "UI_SETP_MENUCOM_ITEM"

    # Verify menu-kind identifiers against the code's symbolic-name lookup.
    # Each CMP/B.EQ pair leads to an ADRP/ADD pair returning the named string.
    kind_checks = [
        (0x8B, 0x0122D66C, 0x0122EF24, "UI_MENU_KIND_COMM_MENU"),
        (0xCB, 0x0122D86C, 0x0122F224, "UI_MENU_KIND_COMM_MENU_FRAMEIO_SET"),
        (0x24A, 0x0122E464, 0x01230418, "UI_MENU_KIND_COMM_MENU_FTP_TRANSFER_SET"),
    ]
    for number, compare_pc, case_pc, name in kind_checks:
        cmp_word, branch_word = a.unpack("<2I", compare_pc)
        assert cmp_word == 0x7100001F | (number << 10)  # cmp w0, #number
        assert branch_word & 0xFF00001F == 0x54000000  # b.eq
        relative = (branch_word >> 5) & 0x7FFFF
        if relative & 0x40000:
            relative -= 0x80000
        assert compare_pc + 4 + relative * 4 == case_pc
        page_word, add_word = a.unpack("<2I", case_pc)
        assert page_word & 0x9F00001F == 0x90000000  # adrp x0
        assert add_word & 0xFFC003FF == 0x91000000  # add x0, x0, #imm
        immediate = ((page_word >> 29) & 3) | (((page_word >> 5) & 0x7FFFF) << 2)
        if immediate & 0x100000:
            immediate -= 0x200000
        string_address = (case_pc & ~0xFFF) + (immediate << 12) + ((add_word >> 10) & 0xFFF)
        assert a.string(string_address) == name

    table = 0x01DA8330
    descriptors = []
    for index in range(0x69A):
        pointer, = a.unpack("<Q", table + index * 8)
        row = descriptor(a, pointer)
        assert int(row["table_id"], 16) == index
        descriptors.append(row)

    roots = [descriptors[index] for index in (0x8B, 0x93, 0x97)]
    for row in roots:
        assert row["kind"] == "0x8b" and row["parameter"] == "0xa"
        assert row["items_pointer"] == "0x1924a20"

    items = []
    list_address = int(roots[0]["items_pointer"], 16)
    for index in range(64):
        address = list_address + index * 16
        item, padding, child = a.unpack("<IIQ", address)
        assert padding == 0
        if item == 0x7FFFFFFE:
            assert child == 0
            terminator_address = address
            break
        row = descriptor(a, child)
        assert row["parent_pointer"] == roots[0]["address"]
        row["parameter_name"] = dictionary_name(b, setting_names, int(row["parameter"], 16))
        items.append({"entry_address": hex(address), "item": hex(item),
                      "name": dictionary_name(b, menu_names, item), "submenu": row})
    else:
        raise ValueError("No terminator within the bounded menu list")

    assert len(items) == 9
    assert [int(item["item"], 16) for item in items] == [
        0x249, 0x248, 0x24B, 0x27F, 0x26A, 0x24E, 0x24F, 0x251, 0x252]
    ftp_rows = [row for row in descriptors if row["kind"] == "0x24a"
                or row["parameter"] in ("0xe72", "0xe74")]
    frame_rows = [row for row in descriptors if row["kind"] == "0xcb"]
    assert not ftp_rows
    assert any(row["parameter"] == "0x1065" for row in frame_rows)
    return {
        "scope": "Static parsing of unchanged firmware; no camera access or renderer execution",
        "image_hashes": {"MainA": a.sha256, "MainB": b.sha256},
        "descriptor_table_address": hex(table), "descriptors_checked": len(descriptors),
        "network_roots": roots, "items": items, "terminator_address": hex(terminator_address),
        "ftp_present_in_network_list": False, "frameio_present_in_network_list": True,
        "ftp_top_level_descriptor_matches": ftp_rows, "frameio_descriptor_matches": frame_rows,
        "limitations": [
            "The runtime menu consumer and any dynamic modifications are not yet traced.",
            "The full MainA memory mapping remains unresolved; these are static image addresses.",
            "No statement about patchability or FTP worker functionality follows from this parser.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = inspect()
    if args.output:
        with args.output.open("x") as stream:
            json.dump(result, stream, indent=2)
            stream.write("\n")
    print(f"Verified {result['descriptors_checked']} indexed menu definitions.")
    print("Network menu: three roots share these nine entries:")
    for item in result["items"]:
        print(f"  {item['item']:>5}  {item['name']}")
    print("FTP absent from this list; Frame.io present.")
    print("No FTP transfer/common-settings descriptor found among the checked definitions.")
    print("Static evidence only: the camera renderer was not executed.")


if __name__ == "__main__":
    main()
