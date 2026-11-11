"""Synthetic service-memory fixture. Contains no development-camera session data."""
import struct
import ff80_cmd_patch_probe as q

def idle():
    return dict(console_flags=struct.pack('<4I',0,1,0,0),
        input_ring=struct.pack('<4QI4x',0x045F6BA8,1024,0,0,1),
        output_ring=struct.pack('<4QI4x',0x045F6FE0,1024,0,0,1))

def baseline(recovery):
    states={name:0 for name,_,_ in recovery.STATES}
    states.update(communication_selection=1,ui_communication_selection=1,slot3_function=2,
                  profile_category=11,ui_mode=13,backend=3)
    profile=bytearray(recovery.PROFILE_SIZE)
    for offset,value in ((0,1),(0x20,1),(0x3ec,2)):struct.pack_into('<I',profile,offset,value)
    return dict(states=states,profile_hex=profile.hex())

class Device:
    def __init__(self, expected, extra, failure=None):
        self.memory = {}
        for address, data in {**expected, **extra}.items(): self.put(address, data)
        for _, address, size in q.p.PATCH_STATES: self.put(address, bytes(size))
        for name,address,size in q.p.STATES:
            self.put(address, idle()[name])
        self.put(q.CMD_SLOT, bytes(16))
        self.put(q.ROOT_POINTER, struct.pack('<Q', 0x108000))
        self.put(0x108000, struct.pack('<Q', 0x10B003))
        for _, address, writable, executable in q.TARGETS:
            desc = (address & ~0x1FFFFF) | 0x609
            if writable is False: desc |= 0x80
            if not executable: desc |= 1 << 53
            self.put(0x10B000+8*(address>>21), struct.pack('<Q', desc))
        if failure == 'readonly_cmd':
            self.put(0x10B088, struct.pack('<Q', 0x60000002200689))
        self.flag, self.failure, self.requests, self.reply = b'\0', failure, [], b''
        self.marker_writes = self.cmd_calls = 0
        self.active = False

    def put(self, address, data): self.memory.update({address+i: b for i, b in enumerate(data)})
    def get(self, address, size): return bytes(self.memory[address+i] for i in range(size))
    def controlWrite(self, *args): return len(args[-2])

    def bulkWrite(self, endpoint, request, **kwargs):
        request = bytes(request)
        self.requests.append(request)
        operation, _, _, _, function = struct.unpack('<5I', request[:20])
        self.reply = request[:20]
        if operation == 0x1A0002:
            if function == 3: self.flag = request[36:37]
            self.reply += request[20:36]+(self.flag if function == 2 else b'')
        elif function == 2:
            address, length = struct.unpack('<II', request[28:36])
            self.reply += request[20:36]+self.get(address, length)
        elif function == 3:
            assert self.flag == b'\1'
            address, length = struct.unpack('<II', request[28:36])
            self.put(address, request[36:])
            self.reply += request[20:36]
            if address == q.MARKER: self.marker_writes += 1
            if self.failure == 'lost_callback_write' and address == q.CMD_SLOT:
                raise TimeoutError('Lost bulkWrite acknowledgment after device mutation')
            if self.failure == 'bad_write_readback' and address == q.CMD_SLOT and request[36:] != bytes(8):
                self.put(address, bytes(8))
        elif request == q.CMD:
            self.cmd_calls += 1
            target = struct.unpack('<Q', self.get(q.CMD_SLOT, 8))[0]
            assert target in (q.NOOP, q.CLEAN, q.ICACHE)
            # Bytes 20..32 are unspecified/stale in the actual fn5 response.
            self.reply += b'UNSPECIFIED!'+struct.pack('<I', len(q.PAYLOAD))
            if self.failure == 'cmd_rejected': self.reply = self.reply[:4]+struct.pack('<II', 1, 3)+self.reply[12:]
            if self.failure == 'ipi_error' and target == q.CLEAN: self.put(q.FLAGS, bytes(16))
        elif request == q.p.HELLO:
            self.active = True
            self.reply += struct.pack('<I', 6)+bytes(0x120-24)
        elif request == q.p.POLL:
            assert self.active
            busy = int(self.failure == 'worker_hang')
            status = 7 if self.get(q.MARKER, 1) == b'\xe0' else 0
            if self.failure == 'marker_not_seen': status = 0
            if self.failure == 'restored_code_not_seen' and self.marker_writes > 1: status = 7
            output = b'' if busy else b'Hello, world!\n'
            self.reply += struct.pack('<III', busy, status, len(output))+output+bytes(0x120-32-len(output))
            if not busy: self.active = False
        else: raise AssertionError(request.hex())
        return len(request)

    def bulkRead(self, endpoint, size, **kwargs):
        if self.failure == 'lost_cmd_reply' and self.requests[-1] == q.CMD:
            raise TimeoutError('CMD reply lost')
        return self.reply
