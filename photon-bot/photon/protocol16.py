"""Photon binary protocol 1.6 (GpBinaryV16): big-endian, one type byte per value.

Python <-> Photon mapping used by `serialize` (choose wrappers to force a type):
  None->null  bool->bool  int->int32  float->float32  str->string  bytes->byte[]
  dict->Hashtable  list/tuple->object[]  Byte/Short/Long/Double/IntArray/StringArray/
  TypedArray/Dictionary/Custom wrappers for the rest.
"""
import io
import struct


class Byte(int):
    def __repr__(self): return f"b{int(self)}"
class Short(int):
    def __repr__(self): return f"s{int(self)}"
class Long(int):
    def __repr__(self): return f"L{int(self)}"
class Double(float):
    def __repr__(self): return f"d{float(self)}"
class IntArray(list): pass
class StringArray(list): pass


class TypedArray(list):
    """Photon 'y' array whose elements share one type code (e.g. Vector3[])."""
    def __init__(self, code, items=()):
        super().__init__(items)
        self.code = code


class Dictionary(dict):
    """Photon 'D' Dictionary<K,V>. key_code/value_code 0 or '*' = object (typed)."""
    def __init__(self, key_code=0, value_code=0, *a, **kw):
        super().__init__(*a, **kw)
        self.key_code, self.value_code = key_code, value_code


class Custom:
    """A registered custom type. Known PUN types are decoded by `custom_types`."""
    def __init__(self, code, data):
        self.code, self.data = code, bytes(data)

    def __repr__(self):
        return f"Custom({chr(self.code)!r}, {self.data.hex()})"

    def __eq__(self, o):
        return isinstance(o, Custom) and (o.code, o.data) == (self.code, self.data)


# Hooks so PUN types (Vector3 'V', Quaternion 'Q', Player 'P'...) round-trip as
# nicer Python objects. code -> (decode(bytes)->obj, python_type, encode(obj)->bytes)
custom_types = {}


def register_custom(code, py_type, encode, decode):
    custom_types[code] = (decode, py_type, encode)


NULL, DICT, STRING, BYTE, DOUBLE, EVENT, FLOAT, HASHTABLE, INT, SHORT, LONG = (
    42, 68, 115, 98, 100, 101, 102, 104, 105, 107, 108)
INTARR, BOOL, OPRESP, OPREQ, STRARR, BYTEARR, ARRAY, OBJARR, CUSTOM = (
    110, 111, 112, 113, 97, 120, 121, 122, 99)


class Reader:
    def __init__(self, data):
        self.b = memoryview(bytes(data))
        self.i = 0

    def take(self, n):
        v = self.b[self.i:self.i + n]
        if len(v) != n:
            raise ValueError("truncated Photon message")
        self.i += n
        return bytes(v)

    def u8(self): return self.take(1)[0]
    def i16(self): return struct.unpack(">h", self.take(2))[0]
    def i32(self): return struct.unpack(">i", self.take(4))[0]

    def value(self, code=None):
        if code is None:
            code = self.u8()
        if code in (NULL, 0):
            return None
        if code == BYTE: return Byte(self.u8())
        if code == BOOL: return self.u8() != 0
        if code == SHORT: return Short(self.i16())
        if code == INT: return self.i32()
        if code == LONG: return Long(struct.unpack(">q", self.take(8))[0])
        if code == FLOAT: return struct.unpack(">f", self.take(4))[0]
        if code == DOUBLE: return Double(struct.unpack(">d", self.take(8))[0])
        if code == STRING:
            return self.take(struct.unpack(">H", self.take(2))[0]).decode("utf-8", "replace")
        if code == BYTEARR: return self.take(self.i32())
        if code == INTARR: return IntArray(self.i32() for _ in range(self.i32()))
        if code == STRARR: return StringArray(self.value(STRING) for _ in range(self.i16()))
        if code == OBJARR: return [self.value() for _ in range(self.i16())]
        if code == HASHTABLE:
            return {self._key(): self.value() for _ in range(self.i16())}
        if code == DICT:
            kc, vc = self.u8(), self.u8()
            d = Dictionary(kc, vc)
            for _ in range(self.i16()):
                k = self.value() if kc in (0, NULL) else self.value(kc)
                d[k] = self.value() if vc in (0, NULL) else self.value(vc)
            return d
        if code == ARRAY:
            n, ec = self.i16(), self.u8()
            if ec == ARRAY:
                return TypedArray(ec, [self.value(ARRAY) for _ in range(n)])
            if ec == DICT:
                return TypedArray(ec, [self.value(DICT) for _ in range(n)])
            if ec == CUSTOM:
                cc = self.u8()
                items = []
                for _ in range(n):
                    raw = self.take(self.i16())
                    items.append(_decode_custom(cc, raw))
                t = TypedArray(ec, items); t.custom_code = cc
                return t
            return TypedArray(ec, [self.value(ec) for _ in range(n)])
        if code == CUSTOM:
            cc = self.u8()
            return _decode_custom(cc, self.take(self.i16()))
        if code == OPREQ: return self.op_request()
        if code == OPRESP: return self.op_response()
        if code == EVENT: return self.event()
        raise ValueError(f"unknown Photon type code {code} ({chr(code) if 32 < code < 127 else '?'})")

    def _key(self):
        k = self.value()
        return tuple(k) if isinstance(k, list) else k

    def params(self):
        return {self.u8(): self.value() for _ in range(self.i16())}

    def op_request(self):
        return {"op": self.u8(), "params": self.params()}

    def op_response(self):
        op, rc = self.u8(), self.i16()
        msg = self.value()
        return {"op": op, "rc": rc, "msg": msg, "params": self.params()}

    def event(self):
        return {"code": self.u8(), "params": self.params()}


def _decode_custom(cc, raw):
    t = custom_types.get(cc)
    if t:
        try:
            return t[0](raw)
        except Exception:
            pass
    return Custom(cc, raw)


class Writer:
    def __init__(self):
        self.o = io.BytesIO()

    def raw(self, b): self.o.write(b)
    def u8(self, v): self.o.write(bytes([v & 0xFF]))
    def i16(self, v): self.o.write(struct.pack(">h", v))
    def i32(self, v): self.o.write(struct.pack(">i", v))
    def getvalue(self): return self.o.getvalue()

    def value(self, v, typed=True):
        def t(code):
            if typed:
                self.u8(code)
        if v is None:
            t(NULL); return
        for code, (_dec, py_type, enc) in custom_types.items():
            if isinstance(v, py_type):
                data = enc(v)
                t(CUSTOM); self.u8(code); self.i16(len(data)); self.raw(data); return
        if isinstance(v, Custom):
            t(CUSTOM); self.u8(v.code); self.i16(len(v.data)); self.raw(v.data); return
        if isinstance(v, bool): t(BOOL); self.u8(1 if v else 0); return
        if isinstance(v, Byte): t(BYTE); self.u8(v); return
        if isinstance(v, Short): t(SHORT); self.i16(v); return
        if isinstance(v, Long): t(LONG); self.raw(struct.pack(">q", v)); return
        if isinstance(v, int): t(INT); self.i32(v); return
        if isinstance(v, Double): t(DOUBLE); self.raw(struct.pack(">d", v)); return
        if isinstance(v, float): t(FLOAT); self.raw(struct.pack(">f", v)); return
        if isinstance(v, str):
            b = v.encode("utf-8"); t(STRING); self.raw(struct.pack(">H", len(b))); self.raw(b); return
        if isinstance(v, (bytes, bytearray)): t(BYTEARR); self.i32(len(v)); self.raw(bytes(v)); return
        if isinstance(v, IntArray):
            t(INTARR); self.i32(len(v))
            for x in v: self.i32(x)
            return
        if isinstance(v, StringArray):
            t(STRARR); self.i16(len(v))
            for x in v: self._str(x)
            return
        if isinstance(v, TypedArray):
            t(ARRAY); self.i16(len(v)); self.u8(v.code)
            if v.code == CUSTOM:
                cc = getattr(v, "custom_code")
                self.u8(cc)
                for x in v:
                    data = custom_types[cc][2](x) if cc in custom_types and not isinstance(x, Custom) else x.data
                    self.i16(len(data)); self.raw(data)
            else:
                for x in v:
                    self.value(x, typed=False)
            return
        if isinstance(v, Dictionary):
            t(DICT); self.u8(v.key_code); self.u8(v.value_code); self.i16(len(v))
            for k, x in v.items():
                self.value(k, typed=v.key_code in (0, NULL))
                self.value(x, typed=v.value_code in (0, NULL))
            return
        if isinstance(v, dict):
            t(HASHTABLE); self.i16(len(v))
            for k, x in v.items():
                self.value(list(k) if isinstance(k, tuple) else k); self.value(x)
            return
        if isinstance(v, (list, tuple)):
            t(OBJARR); self.i16(len(v))
            for x in v: self.value(x)
            return
        raise TypeError(f"can't serialize {type(v).__name__}")

    def _str(self, s):
        b = s.encode("utf-8"); self.raw(struct.pack(">H", len(b))); self.raw(b)

    def params(self, params):
        self.i16(len(params))
        for k, v in params.items():
            self.u8(k); self.value(v)


def op_request(op, params):
    w = Writer(); w.u8(op); w.params(params)
    return w.getvalue()
