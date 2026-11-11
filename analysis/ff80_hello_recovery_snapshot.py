# /// script
# requires-python = ">=3.11"
# dependencies = ["libusb1==3.3.1"]
# ///
"""Verify recovery from the archived hello callback-write attempt.

Fixed original-code/callback checks, bounded page-table reads and config[f7]
restoration only. No general memory write, console, key, mode, save or flash.
The failed journal must identify the exact known callback attempt. Its
obligations are closed only after matching original bytes, f7=0 readback,
and clean service-session closure. Default prints the plan without USB.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import struct
import ff80_console_probe as p

ROOT_POINTER=0x000EEE98
POOL_START,POOL_END=0x00108000,0x00508000
TARGETS=(('hello_callback',0x038604C8),('hello_code',0x03273E78),
         ('cmd_callback',0x0223A7E8),('jig_action_table',0x036C0E58),
         ('jig_completion_code',0x02E19EE0),('console_state',0x045F6FA8))
FIXED_READS=p.PATCH_READS | {(ROOT_POINTER,8),(0x0223A7E8,16)}
KIND='ff80_hello_recovery_snapshot_v1'


class Transport(p.Transport):
    console_allowed=False
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.reads=set(FIXED_READS)
        self.table_reads=0
        self.last_request=None

    def write_cmd(self,request):
        self.last_request=bytes(request)
        return super().write_cmd(request)

    def read_cmd(self,size=256):
        response=bytes(super().read_cmd(size))
        if self.last_request and self.last_request[:4]==struct.pack('<I',0x200001):
            if len(response)!=size or response[:20]!=self.last_request[:20]:
                self.healthy=False
                raise RuntimeError('Malformed fixed-memory response; USB stream fenced')
        return response

    def table_entry(self,address):
        if not POOL_START<=address<=POOL_END-8 or address%8 or self.table_reads>=18:
            raise RuntimeError('Page-table read outside fixed pool/alignment/count bound')
        self.table_reads+=1
        self.reads.add((address,8))
        return struct.unpack('<Q',p.read_fixed(self,address,8))[0]


def walk(root,address,read_entry):
    table=root
    path=[]
    for level,shift in ((1,30),(2,21),(3,12)):
        if table%4096 or not POOL_START<=table<=POOL_END-4096:
            raise RuntimeError('Page-table pointer outside the kernel allocator pool')
        slot=table+8*((address>>shift)&511)
        desc=read_entry(slot)
        path.append(dict(level=level,slot=hex(slot),descriptor=hex(desc)))
        kind=desc&3
        if kind not in (1,3) or (level==3 and kind!=3):
            return dict(address=hex(address),path=path,mapped=False)
        if kind==3 and level<3:
            # Kernel constructs table descriptors as address|3. Refuse to
            # interpret unexpected hierarchical restrictions as absent.
            if desc & ~(0xFFFFFF000 | 3):
                raise RuntimeError('Unexpected hierarchical page-table attributes')
            table=desc&0xFFFFFF000
            continue
        physical=(desc&0xFFFFFF000&~((1<<shift)-1)) | (address&((1<<shift)-1))
        return dict(address=hex(address),path=path,mapped=True,physical_address=hex(physical),
                    el1_writable=not bool(desc&(1<<7)),privileged_execute_never=bool(desc&(1<<53)),
                    access_flag=bool(desc&(1<<10)),leaf_level=level)
    raise RuntimeError('Page-table walk exceeded three levels')


def validate_journal(journal):
    expected=dict(address='0x38604c8',before='783e270300000000',after='d0805e0100000000',verified=False)
    if (journal.get('kind')!='ff80_hello_patch_probe_v1' or journal.get('model')!='X-T30 III'
        or journal.get('original_f7')!='00' or journal.get('code_write_attempted') is not False
        or journal.get('writes')!=[expected] or journal.get('marker_executed') is not False):
        raise RuntimeError('Journal does not identify the exact archived callback attempt')


def collect(jig,transport,expected,report,checkpoint):
    original=bytes(jig.read_config_data(0,0xF7,1,mb=1))
    report['flag_before']=original.hex()
    if original not in (b'\0',b'\1'):
        raise RuntimeError('Unexpected debug flag; no write requested')
    # The failed journal establishes the required restored value as zero,
    # irrespective of whether the restart already reset this RAM byte.
    report['restore_required']=True
    checkpoint()
    try:
        if original==b'\0': jig.write_config_data(0,0xF7,b'\1',mb=1)
        if bytes(jig.read_config_data(0,0xF7,1,mb=1))!=b'\1':
            raise RuntimeError('Memory-read access did not verify')
        report['samples']=[]
        for name,address,size in p.PATCH_SAMPLES:
            value=p.read_fixed(transport,address,size)
            match=value==expected[address]
            report['samples'].append(dict(name=name,address=hex(address),matches=match,data_hex=value.hex()))
            checkpoint()
            if not match:
                raise RuntimeError('Original firmware/callback differs; no general-memory restoration attempted')
        report['original_code_and_callback_verified']=True
        states={name:p.read_fixed(transport,a,n) for name,a,n in p.STATES}
        report['console_idle']=p.verify_idle(states)
        report['cmd_and_key_callbacks_hex']=p.read_fixed(transport,0x0223A7E8,16).hex()
        root=struct.unpack('<Q',p.read_fixed(transport,ROOT_POINTER,8))[0]
        report['kernel_page_table_root']=hex(root)
        checkpoint()
        report['mappings']={}
        for name,address in TARGETS:
            report['mappings'][name]=walk(root,address,transport.table_entry)
            checkpoint()
        report['snapshot_complete']=True
    finally:
        if transport.healthy:
            current=bytes(jig.read_config_data(0,0xF7,1,mb=1))
            if current not in (b'\0',b'\1'):
                raise RuntimeError('Unexpected debug flag during restoration; not overwriting')
            if current==b'\1': jig.write_config_data(0,0xF7,b'\0',mb=1)
            restored=bytes(jig.read_config_data(0,0xF7,1,mb=1))
            report['restored_f7']=restored.hex()
            report['restore_verified']=restored==b'\0'
            report['restore_required']=restored!=b'\0'
            checkpoint()


def save_private(path,report):
    temp=path.with_suffix(path.suffix+'.tmp')
    with open(temp,'w',opener=lambda p,f:os.open(p,f,0o600)) as stream:
        json.dump(report,stream,indent=2);stream.write('\n');stream.flush();os.fsync(stream.fileno())
    temp.replace(path)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute',action='store_true')
    parser.add_argument('--output',type=Path,default=p.ROOT/'ff80-hello-recovery-snapshot.json')
    parser.add_argument('--journal',type=Path,default=p.ROOT/'ff80-hello-patch-probe.json')
    args=parser.parse_args(argv)
    if not args.execute:
        print('PLAN ONLY: verify archived journal, original hello/callback bytes, idle console and fixed kernel page-table mappings.')
        print('Temporarily enable read access if necessary; restore cfg[f7]=0 and verify. No general RAM writes or console commands.')
        print('Close old obligations only after original bytes, flag restoration and service-session closure verify.')
        return 0
    journal=json.loads(args.journal.read_text());validate_journal(journal)
    if journal.get('recovery_required') is False:
        print('Archived experiment recovery already verified; no USB access.')
        return 0
    expected,hashes=p.patch_expected_samples()
    report=dict(kind=KIND,started_at_utc=datetime.now(timezone.utc).isoformat(),image_hashes=hashes,
                source_journal=args.journal.name,restore_required=False,session='not_opened',
                original_code_and_callback_verified=False,snapshot_complete=False)
    with open(args.output,'x',opener=lambda path,flags:os.open(path,flags,0o600)) as stream:
        json.dump(report,stream,indent=2);stream.write('\n')
    def checkpoint(): save_private(args.output,report)
    try:
        with p.usb1.USBContext() as context:
            devices=[d for d in context.getDeviceList() if (d.getVendorID(),d.getProductID())==(0x04CB,0xFF80)]
            if len(devices)!=1: raise RuntimeError('Expected exactly one service camera')
            device=devices[0]
            report['device_node']=f'/dev/bus/usb/{device.getBusNumber():03d}/{device.getDeviceAddress():03d}'
            handle=device.open()
            try:
                with handle.claimInterface(0):
                    t=Transport(handle,EP_IN=0x81,EP_OUT=0x01);t.ms=3000
                    try:
                        t.open_session();report['session']='open';checkpoint()
                        jig=p.ffjlib.jig(t)
                        report['model']=jig.get_device_info(4)
                        if report['model']!='X-T30 III' or jig.get_config_data_size(mb=1)!=(0x960000,0x100):
                            raise RuntimeError('Camera model/config bounds mismatch')
                        collect(jig,t,expected,report,checkpoint)
                    finally:
                        if t.sid is not None and t.healthy:
                            t.close_session();report['session']='closed'
                        elif t.sid is not None: report['session']='unconfirmed_after_transport_error'
            finally: handle.close()
    except BaseException as exc:
        report['error']=f'{type(exc).__name__}: {exc}'
    finally:
        report['recovery_verified']=bool(report['original_code_and_callback_verified'] and report.get('console_idle') and report.get('restore_verified') and report['session']=='closed')
        report['finished_at_utc']=datetime.now(timezone.utc).isoformat();checkpoint()
    if report['recovery_verified']:
        # Re-read to avoid replacing unrelated intervening journal changes.
        journal=json.loads(args.journal.read_text());validate_journal(journal)
        journal.setdefault('recoveries',[]).append(dict(report=args.output.name,verified_at_utc=report['finished_at_utc'],
            previous_obligations={key:journal.get(key) for key in ('restore_required','ram_restore_required','recovery_required')},
            original_code_and_callback_verified=True,debug_flag_verified='00',session='closed'))
        journal.update(restore_required=False,ram_restore_required=False,recovery_required=False,
                       restore_verified=True,code_restore_verified=True)
        save_private(args.journal,journal)
    print(json.dumps(report,indent=2))
    return 0 if report['recovery_verified'] and report['snapshot_complete'] and 'error' not in report else 1


if __name__=='__main__': raise SystemExit(main())
