#!/usr/bin/env python3
"""Minimal PE reader used for black-box evidence collection.

Emits section map, imports, resources and string tables with both file
offset and RVA so every observation in CLONE_SPEC.md is citable.
"""
import sys, json, struct, re, argparse

def u16(b, o): return struct.unpack_from('<H', b, o)[0]
def u32(b, o): return struct.unpack_from('<I', b, o)[0]

class PE:
    def __init__(self, path):
        self.path = path
        self.data = open(path, 'rb').read()
        b = self.data
        self.e_lfanew = u32(b, 0x3c)
        assert b[self.e_lfanew:self.e_lfanew+4] == b'PE\0\0', 'not PE'
        coff = self.e_lfanew + 4
        self.machine = u16(b, coff)
        self.nsections = u16(b, coff + 2)
        self.ts = u32(b, coff + 4)
        self.opt_size = u16(b, coff + 16)
        self.characteristics = u16(b, coff + 18)
        opt = coff + 20
        self.magic = u16(b, opt)
        self.pe32plus = self.magic == 0x20b
        self.entry_rva = u32(b, opt + 16)
        self.image_base = struct.unpack_from('<Q' if self.pe32plus else '<I', b, opt + 24)[0]
        self.subsystem = u16(b, opt + 68)
        # data directories
        ddoff = opt + (112 if self.pe32plus else 96)
        self.dirs = []
        for i in range(16):
            self.dirs.append((u32(b, ddoff + i*8), u32(b, ddoff + i*8 + 4)))
        sec = opt + self.opt_size
        self.sections = []
        for i in range(self.nsections):
            o = sec + i*40
            name = b[o:o+8].rstrip(b'\0').decode('latin1')
            self.sections.append(dict(name=name,
                vsize=u32(b, o+8), va=u32(b, o+12), rawsize=u32(b, o+16), raw=u32(b, o+20),
                chars=u32(b, o+36)))

    def rva2off(self, rva):
        for s in self.sections:
            if s['va'] <= rva < s['va'] + max(s['vsize'], s['rawsize']):
                d = rva - s['va']
                if d < s['rawsize']:
                    return s['raw'] + d
        return None

    def off2rva(self, off):
        for s in self.sections:
            if s['raw'] <= off < s['raw'] + s['rawsize']:
                return s['va'] + (off - s['raw'])
        return None

    def sections_report(self):
        return [dict(name=s['name'], va=hex(s['va']), vsize=s['vsize'],
                     raw=hex(s['raw']), rawsize=s['rawsize'],
                     rva=hex(self.image_base + s['va'])) for s in self.sections]

    def imports(self):
        rva, size = self.dirs[1]
        off = self.rva2off(rva)
        out = {}
        if off is None:
            return out
        b = self.data
        ai = 0
        while True:
            o = off + ai*20
            oft, tds, fc, name_rva, ft = struct.unpack_from('<IIIII', b, o)
            if name_rva == 0:
                break
            no = self.rva2off(name_rva)
            end = b.index(b'\0', no)
            dll = b[no:end].decode('latin1')
            names = []
            t = oft or ft
            to = self.rva2off(t)
            k = 0
            if to:
                step = 8 if self.pe32plus else 4
                while True:
                    e = struct.unpack_from('<Q' if self.pe32plus else '<I', b, to + k*step)[0]
                    if e == 0:
                        break
                    if not (e >> (63 if self.pe32plus else 31)):
                        fo = self.rva2off(e & 0x7fffffff)
                        if fo:
                            names.append(b[fo:b.index(b'\0', fo)].decode('latin1'))
                    else:
                        names.append('#ord%d' % (e & 0xffff))
                    k += 1
            out[dll] = names
            ai += 1
        return out

    def strings(self, minlen=6, enc='both'):
        """Yield (rva, fileoff, encoding, text) for printable runs."""
        b = self.data
        res = []
        if enc in ('both', 'a'):
            pat = re.compile(rb'[\x20-\x7e]{%d,}' % minlen)
            for m in pat.finditer(b):
                res.append((self.off2rva(m.start()), m.start(), 'ascii', m.group().decode('latin1')))
        if enc in ('both', 'u'):
            pat = re.compile((r'(?:[\x20-\x7e]\x00){%d,}' % minlen).encode())
            for m in pat.finditer(b):
                res.append((self.off2rva(m.start()), m.start(), 'utf16', b[m.start():m.end()].decode('utf-16le', 'replace')))
        res.sort(key=lambda t: t[1])
        return res

def main():
    p = argparse.ArgumentParser()
    p.add_argument('exe')
    p.add_argument('--sections', action='store_true')
    p.add_argument('--imports', action='store_true')
    p.add_argument('--strings', action='store_true')
    p.add_argument('--grep')
    p.add_argument('--minlen', type=int, default=6)
    p.add_argument('--enc', default='both')
    p.add_argument('--limit', type=int, default=0)
    a = p.parse_args()
    pe = PE(a.exe)
    print(json.dumps(dict(machine=hex(pe.machine), pe32plus=pe.pe32plus,
        image_base=hex(pe.image_base), entry=hex(pe.image_base+pe.entry_rva),
        subsystem=pe.subsystem, ts=pe.ts, dirs=[(hex(r), s) for r, s in pe.dirs if r]),
        indent=1))
    if a.sections:
        print(json.dumps(pe.sections_report(), indent=1))
    if a.imports:
        print(json.dumps(pe.imports(), indent=1))
    if a.strings:
        rx = re.compile(a.grep, re.I) if a.grep else None
        n = 0
        for rva, off, enc, s in pe.strings(a.minlen, a.enc):
            if rx and not rx.search(s):
                continue
            print('%08x off=%08x %-5s %s' % (rva or 0, off, enc, s.replace('\n', ' ')))
            n += 1
            if a.limit and n >= a.limit:
                break

if __name__ == '__main__':
    main()
