from __future__ import annotations
from typing import Optional, Iterable, Tuple
import zlib
import binascii
import logging

_LOGGER = logging.getLogger(__name__)

# -------------------------
# Protobuf wire helpers
# -------------------------

def _read_varint(buf: bytes, i: int, n: int):
    """Read a protobuf varint from buf starting at index i. Returns (value, next_index)."""
    shift = 0
    result = 0
    while i < n:
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if (b & 0x80) == 0:
            return result, i
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")
    raise ValueError("unexpected EOF in varint")

def iter_len_delimited(buf: bytes, depth: int = 0, max_depth: int = 8) -> Iterable[Tuple[int, int, int, bytes]]:
    """
    Yield (depth, start, length, field_bytes) for all length-delimited (wire_type=2) fields,
    recursively scanning nested messages up to max_depth. We are intentionally agnostic
    to field numbers/names—only the wire type matters.
    """
    i, n = 0, len(buf)
    while i < n:
        try:
            key, i = _read_varint(buf, i, n)
        except Exception:
            break
        wt = key & 0x7
        if wt == 0:  # varint
            try:
                _, i = _read_varint(buf, i, n)
            except Exception:
                break
        elif wt == 1:  # 64-bit
            i += 8
            if i > n:
                break
        elif wt == 2:  # length-delimited
            try:
                length, i2 = _read_varint(buf, i, n)
            except Exception:
                break
            start = i2
            end = start + length
            if end > n:
                break
            field_bytes = buf[start:end]
            i = end
            yield (depth, start, length, field_bytes)
            if depth < max_depth and length >= 2:
                yield from iter_len_delimited(field_bytes, depth + 1, max_depth)
        elif wt == 5:  # 32-bit
            i += 4
            if i > n:
                break
        else:
            break

# -------------------------
# Decompress helpers
# -------------------------

# Try several wbits configurations:
# 47 = auto (zlib/gzip), 15 = zlib wrapper, -15 = raw deflate, 31 = gzip wrapper
_WBITS_TRY = (47, 15, -15, 31)

def _try_decompress_once(blob: bytes):
    """Try to decompress blob from offset 0 with several wbits."""
    for w in _WBITS_TRY:
        try:
            out = zlib.decompress(blob, wbits=w)
            return (0, w, out)  # (offset, wbits, out)
        except Exception:
            continue
    return None

def _scan_offsets_and_decompress(buf: bytes, max_offset: int = 512):
    """
    Slide through offsets 0..max_offset (clamped to len-8) and try several wbits.
    Return first hit: (offset, wbits, out) or None.
    """
    n = len(buf)
    if n < 16:
        return None
    scan_upto = min(max_offset, max(0, n - 8))
    for off in range(0, scan_upto + 1):
        chunk = buf[off:]
        for w in _WBITS_TRY:
            try:
                out = zlib.decompress(chunk, wbits=w)
                return (off, w, out)
            except Exception:
                continue
    return None

def looks_like_obis_text(b: bytes) -> bool:
    """Fast heuristic: must contain '/' (start) and '!' (end) and be reasonably sized."""
    if not b or len(b) < 20:
        return False
    try:
        s = b.decode("utf-8", errors="ignore")
    except Exception:
        return False
    return ("/" in s) and ("!" in s)

def extract_zlib_payload_if_any(buf: bytes) -> Optional[bytes]:
    """
    Back-compat: return *compressed* candidate that can be decompressed (from some offset).
    """
    for _depth, _start, _len, cand in iter_len_delimited(buf, 0, 8):
        if _scan_offsets_and_decompress(cand) is not None:
            return cand
    if _scan_offsets_and_decompress(buf) is not None:
        return buf
    return None

def decompress_any_payload(buf: bytes) -> Optional[bytes]:
    """
    Return a *decompressed* payload (bytes) from the first candidate that yields something
    that looks like OBIS telegram text. If none looks like OBIS, return the first successful
    decompression anyway (best-effort), else None.
    """
    first_success: Optional[bytes] = None

    # Search nested candidates first
    for depth, start, length, cand in iter_len_delimited(buf, 0, 8):
        r0 = _try_decompress_once(cand)
        if r0 is not None:
            _off, _w, out = r0
            if looks_like_obis_text(out):
                return out
            if first_success is None:
                first_success = out

        r = _scan_offsets_and_decompress(cand, max_offset=512)
        if r is not None:
            off, w, out = r
            if looks_like_obis_text(out):
                return out
            if first_success is None:
                first_success = out

    # whole buffer as last chance
    r0 = _try_decompress_once(buf)
    if r0 is not None:
        _off, _w, out = r0
        if looks_like_obis_text(out):
            return out
        if first_success is None:
            first_success = out

    r = _scan_offsets_and_decompress(buf, max_offset=512)
    if r is not None:
        off, w, out = r
        if looks_like_obis_text(out):
            return out
        if first_success is None:
            first_success = out

    return first_success

def try_decompress_all_candidates(buf: bytes, debug: bool = False) -> Optional[Tuple[int, int, int, int, int, bytes, bool]]:
    """
    Verbose scanner: logs every candidate and returns
      (depth, start, length, offset, wbits, out, looks_obis)
    for the FIRST success (prioritizing anything that looks like OBIS).
    """
    best_plain: Optional[Tuple[int, int, int, int, int, bytes, bool]] = None

    for depth, start, length, cand in iter_len_delimited(buf, 0, 8):
        head = binascii.hexlify(cand[:8]).decode()
        if debug:
            _LOGGER.debug("  candidate depth=%d len=%d head=%s", depth, length, head)

        r0 = _try_decompress_once(cand)
        if r0 is not None:
            _off, _w, out = r0
            looks = looks_like_obis_text(out)
            if debug:
                _LOGGER.debug("  -> decompressed at off=%d wbits=%d out_len=%d obis=%s",
                              _off, _w, len(out), "yes" if looks else "no")
            if looks:
                return (depth, start, length, _off, _w, out, True)
            if best_plain is None:
                best_plain = (depth, start, length, _off, _w, out, False)

        r = _scan_offsets_and_decompress(cand, max_offset=512)
        if r is not None:
            off, w, out = r
            looks = looks_like_obis_text(out)
            if debug:
                _LOGGER.debug("  -> decompressed at off=%d wbits=%d out_len=%d obis=%s",
                              off, w, len(out), "yes" if looks else "no")
            if looks:
                return (depth, start, length, off, w, out, True)
            if best_plain is None:
                best_plain = (depth, start, length, off, w, out, False)

    # Whole buffer as ultimate fallback
    r0 = _try_decompress_once(buf)
    if r0 is not None:
        _off, _w, out = r0
        looks = looks_like_obis_text(out)
        if debug:
            _LOGGER.debug("  whole buffer decompressed off=%d wbits=%d out_len=%d obis=%s",
                          _off, _w, len(out), "yes" if looks else "no")
        if looks:
            return (0, 0, len(buf), _off, _w, out, True)
        if best_plain is not None:
            return best_plain
        best_plain = (0, 0, len(buf), _off, _w, out, False)

    r = _scan_offsets_and_decompress(buf, max_offset=512)
    if r is not None:
        off, w, out = r
        looks = looks_like_obis_text(out)
        if debug:
            _LOGGER.debug("  whole buffer decompressed at off=%d wbits=%d out_len=%d obis=%s",
                          off, w, len(out), "yes" if looks else "no")
        if looks:
            return (0, 0, len(buf), off, w, out, True)
        if best_plain is not None:
            return best_plain
        best_plain = (0, 0, len(buf), off, w, out, False)

    return best_plain

def pick_best_candidate_from_blob(blob: bytes) -> bytes | None:
    """
    Debug helper: pick a "good" length-delimited candidate (used by dispatcher for logging).
    Prefer something that decompresses quickly; otherwise return the largest candidate.
    """
    best = None
    best_len = -1
    for depth, start, length, cand in iter_len_delimited(blob, 0, 6):
        if length > best_len:
            best = cand; best_len = length
        # quick probe
        r0 = _try_decompress_once(cand)
        if r0 is not None:
            return cand
        r = _scan_offsets_and_decompress(cand, max_offset=64)
        if r is not None:
            return cand
    return best