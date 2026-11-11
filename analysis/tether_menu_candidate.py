"""Fixed menu-only RAM candidate for X-T30 III 1.10; no USB or writes.

This is deliberately NOT an installable firmware image. Actual RAM writes
must first verify the live mapping, original bytes and recovery state.
"""
import struct

from inspect_network_menu import Image, descriptor, dictionary_name

DESCRIPTOR = 0x01920030
ITEMS = 0x0191FE70
ATTRIBUTES = 0x0191FEB0
CONTEXT = 0x026428C0
TABLE_ENTRY = 0x01DA94F0
SENTINEL = 0x7FFFFFFE
# Reuse the two existing rows: no allocation, pointer changes or count change.
# +8 and +0x10 are message IDs in each 0x60-byte attribute record. The native
# VALS labels are used in both fields; actual screen rendering is untested.
CHANGES = (
    (ITEMS + 0x10, 2, 3),
    (ITEMS + 0x20, 6, 4),
    (ATTRIBUTES + 0x60 + 8, 0xEA5, 0x9E1),
    (ATTRIBUTES + 0x60 + 0x10, 0xEA6, 0x9E1),
    (ATTRIBUTES + 0xC0 + 8, 0x9E3, 0x9E2),
    (ATTRIBUTES + 0xC0 + 0x10, 0x9DF, 0x9E2),
)
MAPPING_TARGETS = (
    ('menu_items', ITEMS), ('menu_attributes', ATTRIBUTES),
    ('menu_descriptor', DESCRIPTOR), ('descriptor_table_entry', TABLE_ENTRY),
    ('current_menu_context', CONTEXT),
)
STATIC_SAMPLES = (
    ('connection_menu_descriptor', DESCRIPTOR, 56),
    ('connection_menu_items', ITEMS, 64),
    ('connection_menu_attributes_0_1', ATTRIBUTES, 192),
    ('connection_menu_attributes_2_and_terminator', ATTRIBUTES + 192, 192),
    ('connection_menu_table_entry', TABLE_ENTRY, 8),
    ('network_menu_connection_row', 0x01924A70, 16),
)


def verify_label(image, number, compare, case, name):
    """Decode the real symbolic-message lookup, rather than guessing IDs."""
    cmp_word, branch = image.unpack('<2I', compare)
    assert cmp_word == 0x7100001F | number << 10
    assert branch & 0xFF00001F == 0x54000000
    relative = (branch >> 5) & 0x7FFFF
    if relative & 0x40000:
        relative -= 0x80000
    assert compare + 4 + relative * 4 == case
    adrp, add = image.unpack('<2I', case)
    assert adrp & 0x9F00001F == 0x90000000
    assert add & 0xFFC003FF == 0x91000000
    immediate = ((adrp >> 29) & 3) | (((adrp >> 5) & 0x7FFFF) << 2)
    if immediate & 0x100000:
        immediate -= 0x200000
    string = (case & ~4095) + (immediate << 12) + ((add >> 10) & 4095)
    assert image.string(string) == name
    return dict(id=hex(number), name=name, compare=hex(compare), case=hex(case), string=hex(string))


def inspect(a=None, b=None):
    a, b = a or Image('MainA'), b or Image('MainB')
    desc = descriptor(a, DESCRIPTOR)
    assert desc['parameter'] == '0x209' and desc['table_id'] == '0x238'
    assert int(desc['items_pointer'], 16) == ITEMS
    assert int(desc['attributes_pointer'], 16) == ATTRIBUTES
    assert a.unpack('<Q', TABLE_ENTRY) == (DESCRIPTOR,)
    assert a.unpack('<Q', 0x01924A78) == (DESCRIPTOR,)
    for index, mode in enumerate((0, 2, 6, SENTINEL)):
        assert a.unpack('<IIQ', ITEMS + 16 * index) == (mode, 0, 0)
    for index, labels in enumerate(((0x9E0, 0x9DE), (0xEA5, 0xEA6), (0x9E3, 0x9DF))):
        row = list(a.unpack('<24I', ATTRIBUTES + index * 0x60))
        assert (row[2], row[4]) == labels
        row[2] = row[4] = 0
        assert row == [0] * 24
    assert a.unpack('<24I', ATTRIBUTES + 3 * 0x60) == (0xFFFFFFFF,) * 24
    labels = [verify_label(a, number, compare, case, name) for number, compare, case, name in (
        (0x9E1, 0x01261CA8, 0x0126EC6C, 'MSG_VALS_PCCONNECTIONMODE_USBSHOOTAUTO'),
        (0x9E2, 0x01261CB0, 0x0126EC78, 'MSG_VALS_PCCONNECTIONMODE_USBSHOOTFIXED'),
    )]
    changes = []
    for address, before, after in CHANGES:
        assert a.unpack('<I', address) == (before,)
        changes.append(dict(address=hex(address), before=before, after=after,
                            before_hex=struct.pack('<I', before).hex(),
                            after_hex=struct.pack('<I', after).hex()))
    return dict(kind='tether_menu_candidate_v1', image_hashes={'MainA': a.sha256, 'MainB': b.sha256},
                descriptor=desc, original_values=[0, 2, 6], candidate_values=[0, 3, 4],
                candidate_names=[dictionary_name(b, 0x0362A7A8, n) for n in (0, 3, 4)],
                labels=labels, changes=changes, changed_word_bytes=24,
                constraints=[
                    'RAM-only candidate; no flash/update-file modification.',
                    'Card Reader remains available; Webcam and RAW conversion are temporarily replaced.',
                    'Do not apply unless every live target mapping is identity-mapped and EL1 writable.',
                    'Do not change page-table permissions as a fallback.',
                    'Need verified restart recovery before preparing the experiment.',
                    'Menu rendering, runtime filtering, selection side effects and persistence are untested.',
                    'The native menu may save the selected mode; RAM-only patches do not imply RAM-only settings.',
                    'Return to Card Reader before reboot/recovery; track the actual mode if selection is tested.',
                ])


def apply_to_emulator(cpu, *, restore=False):
    for address, before, after in CHANGES:
        expected, wanted = (after, before) if restore else (before, after)
        assert bytes(cpu.mem_read(address, 4)) == struct.pack('<I', expected)
        cpu.mem_write(address, struct.pack('<I', wanted))
