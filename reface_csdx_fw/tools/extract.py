import sys, pickle, struct, math, collections, re
msgs=pickle.load(open(sys.argv[1],'rb'))
def unpack(p):
    out=bytearray()
    assert len(p)%8==0
    for i in range(0,len(p),8):
        msb=p[i]
        for j in range(7):
            out.append(p[i+1+j] | (0x80 if msb&(1<<(6-j)) else 0))
    return bytes(out)
img=bytearray()
hdr=msgs[0][1][9:-2]; print("header msg data:",hdr.hex())
for i,(dt,b) in enumerate(msgs[1:]):
    data=b[9:-2]
    sub=data[:5]; blkno=(sub[1]<<7)|sub[2]; ln=(sub[3]<<7)|sub[4]
    assert sub[0]==1 and blkno==i and ln<=len(data)-5, (i,sub.hex(),len(data),ln)
    img+=unpack(data[5:])
img=bytes(img)
open(sys.argv[2],'wb').write(img)
print("decoded size",len(img),hex(len(img)))
def H(b):
    c=collections.Counter(b); n=len(b)
    return -sum(v/n*math.log2(v/n) for v in c.values())
# find end of non-FF
end=len(img.rstrip(b'\xff')); print("last non-FF byte at",hex(end))
print("entropy of used region %.3f"%H(img[:end]))
for i in range(0,end,0x20000):
    print("%07x %.2f"%(i,H(img[i:i+0x20000])),end="  ")
print()
# vector table
print("vectors:",[hex(x) for x in struct.unpack('<8I',img[:32])])
print("vector targets:",[hex(x) for x in struct.unpack('<8I',img[0x20:0x40])])
print("first 0x80:",img[:0x80].hex())
print("tail region:",img[end-96:end])
