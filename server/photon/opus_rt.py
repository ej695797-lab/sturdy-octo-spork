"""Live Opus encode/decode through the system libopus (ctypes), for talking to
players from the editor: 20 ms mono frames, 48 kHz in, any rate out."""
import ctypes
import ctypes.util

_lib = ctypes.CDLL(ctypes.util.find_library("opus") or "libopus.so.0")
_lib.opus_encoder_create.restype = ctypes.c_void_p
_lib.opus_encoder_create.argtypes = [ctypes.c_int32, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
_lib.opus_encode.restype = ctypes.c_int32
_lib.opus_encode.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_int32]
_lib.opus_encoder_destroy.argtypes = [ctypes.c_void_p]
_lib.opus_encoder_ctl.restype = ctypes.c_int
_lib.opus_decoder_create.restype = ctypes.c_void_p
_lib.opus_decoder_create.argtypes = [ctypes.c_int32, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
_lib.opus_decode.restype = ctypes.c_int
_lib.opus_decode.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int32, ctypes.c_char_p, ctypes.c_int,
                             ctypes.c_int]
_lib.opus_decoder_destroy.argtypes = [ctypes.c_void_p]

APPLICATION_VOIP = 2048
SET_BITRATE = 4002


class Encoder:
    """48 kHz mono, 20 ms (960 samples = 1920 bytes of s16le) per frame."""

    def __init__(self, bitrate=32000):
        err = ctypes.c_int()
        self._st = _lib.opus_encoder_create(48000, 1, APPLICATION_VOIP, ctypes.byref(err))
        if err.value != 0 or not self._st:
            raise RuntimeError(f"opus_encoder_create failed ({err.value})")
        _lib.opus_encoder_ctl(ctypes.c_void_p(self._st), ctypes.c_int(SET_BITRATE), ctypes.c_int32(bitrate))
        self._out = ctypes.create_string_buffer(1500)

    def encode(self, pcm: bytes) -> bytes:
        n = _lib.opus_encode(self._st, pcm, len(pcm) // 2, self._out, len(self._out))
        if n < 0:
            raise RuntimeError(f"opus_encode failed ({n})")
        return self._out.raw[:n]

    def __del__(self):
        if getattr(self, "_st", None):
            _lib.opus_encoder_destroy(self._st)
            self._st = None


class Decoder:
    """Opus packet -> s16le mono PCM at `rate` (8000/12000/16000/24000/48000)."""

    def __init__(self, rate=24000):
        err = ctypes.c_int()
        self.rate = rate
        self._st = _lib.opus_decoder_create(rate, 1, ctypes.byref(err))
        if err.value != 0 or not self._st:
            raise RuntimeError(f"opus_decoder_create failed ({err.value})")
        self._max = rate * 120 // 1000                   # longest Opus frame: 120 ms
        self._out = ctypes.create_string_buffer(self._max * 2)

    def decode(self, pkt: bytes) -> bytes:
        n = _lib.opus_decode(self._st, pkt, len(pkt), self._out, self._max, 0)
        return self._out.raw[:n * 2] if n > 0 else b""

    def __del__(self):
        if getattr(self, "_st", None):
            _lib.opus_decoder_destroy(self._st)
            self._st = None
