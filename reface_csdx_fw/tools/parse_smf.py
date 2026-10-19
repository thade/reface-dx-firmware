import sys, collections
d=open(sys.argv[1],'rb').read()
assert d[:4]==b'MThd'
p=14; assert d[p:p+4]==b'MTrk'
tl=int.from_bytes(d[p+4:p+8],'big'); p+=8; end=p+tl
def varlen():
    global p; v=0
    while True:
        b=d[p]; p+=1; v=(v<<7)|(b&0x7f)
        if not b&0x80: return v
msgs=[]; running=None
while p<end:
    dt=varlen(); st=d[p]
    if st==0xF0 or st==0xF7:
        p+=1; ln=varlen(); body=d[p:p+ln]; p+=ln
        if st==0xF0: msgs.append((dt,body))
    elif st==0xFF:
        p+=1; typ=d[p]; p+=1; ln=varlen(); meta=d[p:p+ln]; p+=ln
        print("meta %02x len %d %s"%(typ,ln,meta.hex()))
    else:
        if st&0x80: running=st; p+=1
        n=1 if (running>>4) in (0xC,0xD) else 2; p+=n
print("sysex messages:",len(msgs))
hdrs=collections.Counter(); 
for i,(dt,b) in enumerate(msgs):
    hdr=b[:4]  # 43 0n 7F 1C
    cnt=(b[4]<<7)|b[5]; addr=b[6:9]; data=b[9:-2]; cs=b[-2]; term=b[-1]
    calc=(-sum(b[4:-2]))&0x7f
    ok= calc==cs and term==0xF7
    if i<4 or i>len(msgs)-4 or not ok:
        print(i,"dt",dt,"hdr",hdr.hex(),"cnt",cnt,"addr",addr.hex(),"datalen",len(data),"first",data[:12].hex(),"cs",hex(cs),"ok",ok)
    hdrs[(hdr.hex(),cnt,addr.hex(),len(data))]+=1
print(hdrs.most_common(6))
import pickle; pickle.dump(msgs,open(sys.argv[2],'wb'))
