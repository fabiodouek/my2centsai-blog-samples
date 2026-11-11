#!/usr/bin/env python3
"""Minimal Nitro Secure Module client (stdlib only).

  nsm.py attest <public_key.der> <out.doc>   request a signed attestation document
  nsm.py pcrs <attestation.doc>              print module_id and the non-empty PCRs
"""
import ctypes, fcntl, os, struct, sys

NSM_IOCTL_RAW = 0xC0200A00  # _IOWR(0x0A, 0, struct nsm_raw), 32 bytes


def _head(mt, n):
    if n < 24:
        return bytes([mt << 5 | n])
    for ai, fmt in ((24, ">B"), (25, ">H"), (26, ">I"), (27, ">Q")):
        if n < 1 << (8 * struct.calcsize(fmt)):
            return bytes([mt << 5 | ai]) + struct.pack(fmt, n)


def enc(o):
    if o is None:
        return b"\xf6"
    if isinstance(o, bytes):
        return _head(2, len(o)) + o
    if isinstance(o, str):
        b = o.encode()
        return _head(3, len(b)) + b
    if isinstance(o, int):
        return _head(0, o)
    if isinstance(o, dict):
        return _head(5, len(o)) + b"".join(enc(k) + enc(v) for k, v in o.items())
    raise TypeError(type(o))


def dec(b, i=0):
    ib = b[i]; mt, ai = ib >> 5, ib & 31; i += 1
    if ai == 31:  # indefinite length: items until the 0xff break
        if mt in (2, 3):
            out = b""
            while b[i] != 0xFF:
                chunk, i = dec(b, i); out += chunk if isinstance(chunk, bytes) else chunk.encode()
            return (out if mt == 2 else out.decode()), i + 1
        if mt == 4:
            out = []
            while b[i] != 0xFF:
                v, i = dec(b, i); out.append(v)
            return out, i + 1
        if mt == 5:
            out = {}
            while b[i] != 0xFF:
                k, i = dec(b, i); v, i = dec(b, i); out[k] = v
            return out, i + 1
    if ai < 24:
        n = ai
    else:
        size = {24: 1, 25: 2, 26: 4, 27: 8}[ai]
        n = int.from_bytes(b[i:i + size], "big"); i += size
    if mt == 0: return n, i
    if mt == 1: return -1 - n, i
    if mt == 2: return b[i:i + n], i + n
    if mt == 3: return b[i:i + n].decode(), i + n
    if mt == 4:
        out = []
        for _ in range(n):
            v, i = dec(b, i); out.append(v)
        return out, i
    if mt == 5:
        out = {}
        for _ in range(n):
            k, i = dec(b, i); v, i = dec(b, i); out[k] = v
        return out, i
    if mt == 6: return dec(b, i)
    if mt == 7: return {20: False, 21: True, 22: None}.get(ai), i


def nsm_call(request):
    req = enc(request)
    reqbuf = ctypes.create_string_buffer(req, len(req))
    resbuf = ctypes.create_string_buffer(0x3000)
    raw = bytearray(struct.pack("QQQQ", ctypes.addressof(reqbuf), len(req), ctypes.addressof(resbuf), 0x3000))
    fd = os.open("/dev/nsm", os.O_RDWR)
    try:
        fcntl.ioctl(fd, NSM_IOCTL_RAW, raw, True)
    finally:
        os.close(fd)
    rlen = struct.unpack("QQQQ", raw)[3]
    return dec(resbuf.raw[:rlen])[0]


def payload(doc):
    cose = dec(doc)[0]  # COSE_Sign1: [protected, unprotected, payload, signature]
    return dec(cose[2])[0]


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "attest":
        pub = open(sys.argv[2], "rb").read()
        resp = nsm_call({"Attestation": {"user_data": None, "nonce": None, "public_key": pub}})
        open(sys.argv[3], "wb").write(resp["Attestation"]["document"])
        print(f"wrote {sys.argv[3]} ({len(resp['Attestation']['document'])} bytes)")
    elif cmd == "pcrs":
        p = payload(open(sys.argv[2], "rb").read())
        print("module_id:", p["module_id"], "digest:", p["digest"])
        for k in sorted(p["pcrs"]):
            v = p["pcrs"][k]
            if any(v) or k in (0, 1, 2):
                print(f"PCR{k}: {v.hex()}")
