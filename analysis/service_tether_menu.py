"""Fixed menu-clone payload construction. No USB or camera access."""
import struct

import service_rec_patch as rec
import tether_menu_candidate as candidate
from inspect_network_menu import Image

LOOKUP, LOOKUP_RET = 0x013F48A4, 0x013F48E4
ROUTER, ROUTER_SIZE = 0x03273EC4, 72  # Service 'help' handler, never submitted while armed.
HELPER, HELPER_SIZE = rec.ATOMIC, 96  # Within the existing service 'key' handler.
RESULT = HELPER + 80
POOL_START, POOL_ID_ADDRESS = 0x026676C8, 0x027E76C8
ALLOCATE, RELEASE, SIZE = 0x00091020, 0x000927F4, 1024
ROOT, NETWORK = 0x019FF048, 0x01927F10
# All cloned pointers retain their original relative relationships. Only the
# target submenu's existing values and message IDs change.
ROOT_OFFSET, NETWORK_OFFSET, MENU_OFFSET = 0, 56, 112
ROOT_ITEMS_OFFSET, NETWORK_ITEMS_OFFSET, MENU_ITEMS_OFFSET, ATTRS_OFFSET = 168, 312, 472, 536
USED = 920
KERNEL_BASE = 0x00081000


def owned_pointer(pointer):
    return isinstance(pointer, int) and pointer % 8 == 0 and POOL_START <= pointer <= POOL_ID_ADDRESS - SIZE


def clone(pointer, image=None):
    if not owned_pointer(pointer):
        raise ValueError('Clone pointer outside the fixed native pool')
    a = image or Image('MainA')
    data = bytearray(SIZE)
    for source, offset, length in (
        (ROOT, ROOT_OFFSET, 56), (NETWORK, NETWORK_OFFSET, 56),
        (candidate.DESCRIPTOR, MENU_OFFSET, 56),
        (0x019FEC58, ROOT_ITEMS_OFFSET, 144),
        (0x01924A20, NETWORK_ITEMS_OFFSET, 160),
        (candidate.ITEMS, MENU_ITEMS_OFFSET, 64),
        (candidate.ATTRIBUTES, ATTRS_OFFSET, 384),
    ):
        data[offset:offset+length] = a.data[source-a.base:source-a.base+length]
    assert struct.unpack_from('<Q', data, ROOT_OFFSET + 0x18)[0] == 0
    assert struct.unpack_from('<Q', data, NETWORK_OFFSET + 0x18)[0] == ROOT
    assert struct.unpack_from('<Q', data, MENU_OFFSET + 0x18)[0] == NETWORK
    for offset, parent, items in (
        (ROOT_OFFSET, 0, ROOT_ITEMS_OFFSET),
        (NETWORK_OFFSET, pointer + ROOT_OFFSET, NETWORK_ITEMS_OFFSET),
        (MENU_OFFSET, pointer + NETWORK_OFFSET, MENU_ITEMS_OFFSET),
    ):
        struct.pack_into('<QQ', data, offset + 0x18, parent, pointer + items)
    struct.pack_into('<Q', data, MENU_OFFSET + 0x28, pointer + ATTRS_OFFSET)
    for items, count, original, replacement in (
        (ROOT_ITEMS_OFFSET, 8, NETWORK, pointer + NETWORK_OFFSET),
        (NETWORK_ITEMS_OFFSET, 9, candidate.DESCRIPTOR, pointer + MENU_OFFSET),
    ):
        matches = [items + i * 16 + 8 for i in range(count)
                   if struct.unpack_from('<Q', data, items + i * 16 + 8)[0] == original]
        assert len(matches) == 1
        struct.pack_into('<Q', data, matches[0], replacement)
        assert struct.unpack_from('<IIQ', data, items + count * 16) == (candidate.SENTINEL, 0, 0)
    for address, before, after in candidate.CHANGES:
        offset = (MENU_ITEMS_OFFSET + address - candidate.ITEMS if address < candidate.ATTRIBUTES
                  else ATTRS_OFFSET + address - candidate.ATTRIBUTES)
        assert struct.unpack_from('<I', data, offset)[0] == before
        struct.pack_into('<I', data, offset, after)
    assert data[USED:] == bytes(SIZE - USED)
    return bytes(data)


def router(pointer):
    if not owned_pointer(pointer):
        raise ValueError('Router pointer outside owned-pool bounds')
    # Preserve x16/x17, NZCV, SP and LR. Return x0 unchanged for every menu
    # other than the two ancestor descriptors. Generated from payloads.S.
    return struct.pack('<18I',
        0xA9BF47F0, 0xD53B4211, 0x18000190, 0xEB10001F,
        0x540000C0, 0x18000150, 0xEB10001F, 0x54000081,
        0x18000100, 0x14000002, 0x180000E0, 0xD51B4211,
        0xA8C147F0, 0xD65F03C0, ROOT, NETWORK,
        pointer + NETWORK_OFFSET, pointer + ROOT_OFFSET)


def callback(action, pointer=0, kind=0):
    if action not in ('allocate', 'release', 'lookup', 'lookup_routed'):
        raise ValueError('Unknown fixed callback')
    if action == 'release' and not owned_pointer(pointer):
        raise ValueError('Release pointer outside owned-pool bounds')
    if action != 'release' and pointer:
        raise ValueError('Unexpected input pointer')
    if action in ('lookup', 'lookup_routed'):
        if kind not in (0, 1, 0x8B):
            raise ValueError('Lookup outside fixed probe kinds')
        words = [0xA9BF7BFD, 0x910003FD, 0x52800000 | kind << 5,
                 rec.branch(HELPER + 12, LOOKUP, True)]
        if action == 'lookup_routed':
            words += [rec.branch(HELPER + 16, ROUTER, True), 0x100001E1]
        else:
            words += [0x10000201]
        words += [0xF9000020, 0x52800000, 0xB9000820, 0xA8C17BFD, 0xD65F03C0]
    else:
        words = [0xA9BF7BFD, 0x910003FD, 0x180001C0, 0xB9400000]
        words += ([0x52808001, 0x100001E2] if action == 'allocate'
                  else [0x10000201, 0xF9400021])
        words += [rec.branch(HELPER + 24, ALLOCATE if action == 'allocate' else RELEASE, True),
                  0x100001A1, 0xB9000820, 0xA8C17BFD, 0xD65F03C0]
    data = bytearray(HELPER_SIZE)
    struct.pack_into('<%dI' % len(words), data, 0, *words)
    struct.pack_into('<I', data, 64, POOL_ID_ADDRESS)
    struct.pack_into('<QII', data, 80, pointer, 0x7FFFFFFF, 0)
    return bytes(data)


def samples(a=None, b=None):
    a, b = a or Image('MainA'), b or Image('MainB')
    result = {}
    ranges = (
        ('lookup', a, LOOKUP, 68), ('router_original', b, ROUTER, ROUTER_SIZE),
        ('helper_original', b, HELPER, HELPER_SIZE),
        ('native_pool_probe', a, 0x015E5C14, 0x74),
        ('native_pool_initializer', a, 0x015E6118, 0x30),
        ('root_descriptor', a, ROOT, 56), ('network_descriptor', a, NETWORK, 56),
        ('root_items', a, 0x019FEC58, 144), ('network_items', a, 0x01924A20, 160),
    ) + tuple((name, a, address, size) for name, address, size in candidate.STATIC_SAMPLES)
    for name, image, address, size in ranges:
        result[(name, address, size)] = image.data[address-image.base:address-image.base+size]
    return result
