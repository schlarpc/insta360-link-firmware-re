import fcntl, ctypes, sys, os
class Q(ctypes.Structure):
    _fields_=[('unit',ctypes.c_uint8),('selector',ctypes.c_uint8),('query',ctypes.c_uint8),
              ('size',ctypes.c_uint16),('data',ctypes.POINTER(ctypes.c_uint8))]
UVCIOC_CTRL_QUERY = 0xc0107521  # _IOWR('u', 0x21, struct uvc_xu_control_query)
GET_CUR,GET_MIN,GET_MAX,GET_RES,GET_LEN,GET_INFO,GET_DEF = 0x81,0x82,0x83,0x84,0x85,0x86,0x87
fd=os.open(sys.argv[1], os.O_RDWR)
def q(unit,sel,query,size):
    buf=(ctypes.c_uint8*size)()
    s=Q(unit,sel,query,size,ctypes.cast(buf,ctypes.POINTER(ctypes.c_uint8)))
    fcntl.ioctl(fd, UVCIOC_CTRL_QUERY, s)
    return bytes(buf)
EXPECT={(9,1):4,(9,2):0x34,(9,3):0xaa,(9,4):0x106,(9,5):1,(9,6):5,(9,7):1,(9,8):0x1f6,(9,10):0x81,
        (9,11):5,(9,12):0x20,(9,13):0x81,(9,14):1,(9,15):0xc,(9,16):0xff,(9,17):1,(9,18):1,(9,19):1,
        (9,20):0xf0,(9,21):8,(9,22):4,(9,23):0x81,(9,24):4,(9,25):2,(9,26):8,(9,27):2,(9,28):0xa,
        (9,29):2,(9,30):1,(10,1):8,(10,6):1,(11,1):1,(11,2):1,(11,3):1,(11,4):1,(11,5):1}
print(f"{'unit/sel':<10}{'GET_LEN':<9}{'predicted':<11}{'INFO':<6}GET_CUR")
for unit,maxsel in ((9,30),(10,6),(11,5)):
    for sel in range(1,maxsel+1):
        try: ln=int.from_bytes(q(unit,sel,GET_LEN,2),'little')
        except OSError as e: print(f"{unit}/{sel:<8}GET_LEN err {e.errno}"); continue
        try: info=q(unit,sel,GET_INFO,1)[0]
        except OSError: info=None
        cur='-'
        if 0<ln<=1024:
            try: cur=q(unit,sel,GET_CUR,ln).hex()
            except OSError as e: cur=f"err{e.errno}"
        exp=EXPECT.get((unit,sel))
        mark='OK' if exp==ln else ('?' if exp is None else f'exp={exp:#x}')
        print(f"{unit}/{sel:<8}{hex(ln):<8} {mark:<11}{(hex(info) if info is not None else '-'):<6}{cur[:80]}")
