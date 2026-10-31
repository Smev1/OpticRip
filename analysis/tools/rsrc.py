#!/usr/bin/env python3
"""Enumerate PE resources (types/names/sizes/offsets) and dump payload headers."""
import sys, json, struct
sys.path.insert(0, __file__.rsplit('/', 1)[0])
from pe import PE, u16, u32

TYPES = {1:'CURSOR',2:'BITMAP',3:'ICON',4:'MENU',5:'DIALOG',6:'STRING',7:'FONTDIR',
         8:'FONT',9:'ACCELERATOR',10:'RCDATA',11:'MESSAGETABLE',12:'GROUP_CURSOR',
         14:'GROUP_ICON',16:'VERSION',17:'DLGINCLUDE',19:'PLUGPLAY',20:'VXD',
         21:'ANICURSOR',22:'ANIICON',23:'HTML',24:'MANIFEST'}

def walk(pe, off, base, depth, path, out):
    n = u16(pe.data, off+12) + u16(pe.data, off+14)
    for i in range(n):
        e = off + 16 + i*8
        name = u32(pe.data, e)
        tgt = u32(pe.data, e+4)
        if name & 0x80000000:
            no = base + (name & 0x7fffffff)
            ln = u16(pe.data, no)
            nm = pe.data[no+2:no+2+ln*2].decode('utf-16le', 'replace')
        else:
            nm = name
        if tgt & 0x80000000:
            walk(pe, base + (tgt & 0x7fffffff), base, depth+1, path+[nm], out)
        else:
            do = base + tgt
            rva, size, cp, _ = struct.unpack_from('<IIII', pe.data, do)
            fo = pe.rva2off(rva)
            head = pe.data[fo:fo+16].hex() if fo is not None else ''
            out.append(dict(path=[str(p) for p in path], size=size, rva=hex(rva),
                            off=hex(fo) if fo else None, head=head, codepage=cp))

def main():
    pe = PE(sys.argv[1])
    rva, size = pe.dirs[2]
    base = pe.rva2off(rva)
    out = []
    walk(pe, base, base, 0, [], out)
    out.sort(key=lambda r: -r['size'])
    if '--json' in sys.argv:
        print(json.dumps(out, indent=1))
    else:
        for r in out:  # top 60 by size
            p = '/'.join(r['path'])
            print('%-40s size=%10d off=%s head=%s' % (p[:40], r['size'], r['off'], r['head']))

if __name__ == '__main__':
    main()
