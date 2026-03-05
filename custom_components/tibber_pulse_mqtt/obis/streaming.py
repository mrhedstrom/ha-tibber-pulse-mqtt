from __future__ import annotations

import logging
import zlib
from typing import Dict, Optional, Tuple, Literal, List


class ObisStream:
    """Holds per-device deflate/OBIS parsing state."""

    __slots__ = (
        "dev_id", "off", "z", "buf",
        "cont_fail", "force_mode", "force_no_deflate",
        "just_primed", "skip_bump",
    )

    def __init__(self, dev_id: str):
        self.dev_id: str = dev_id
        self.off: Optional[int] = None
        self.z: Optional[zlib.decompressobj] = None
        self.buf: bytearray = bytearray()

        # Recovery/adaptation state
        self.cont_fail: int = 0
        self.force_mode: bool = False
        self.force_no_deflate: int = 0
        self.just_primed: bool = False
        self.skip_bump: bool = False

    def reset(self) -> None:
        self.off = None
        self.z = None
        self.buf.clear()

    def has_stream(self) -> bool:
        return self.off is not None and self.z is not None

    def continue_decompress(self, blob: bytes) -> Tuple[Literal["ok", "stall", "exception"], Optional[str]]:
        """Try to continue the existing raw-deflate stream."""
        try:
            out = self.z.decompress(blob[self.off:])  # type: ignore[index]
            if out:
                self.buf.extend(out)
                return "ok", None
            return "stall", None
        except Exception as exc:  # zlib.error
            return "exception", str(exc)

    def extract_frames(self, log_obis: bool, logger: logging.Logger) -> List[str]:
        """Pull complete '/...!' frames from the buffer and decode as UTF-8 (ignore errors)."""
        frames: List[str] = []
        b = self.buf
        while True:
            start = b.find(b"/")
            if start < 0:
                break
            end = b.find(b"!", start + 1)
            if end < 0:
                break
            frame = bytes(b[start: end + 1])
            del b[: end + 1]
            try:
                s = frame.decode("utf-8", errors="ignore")
                if s:
                    if log_obis:
                        logger.debug("OBIS FRAME:\n%s", s)
                    frames.append(s)
            except Exception:
                # ignore bad unicode; buffer already advanced
                pass
        return frames

    def handle_no_deflate_blob(self, log_obis: bool, logger: logging.Logger) -> List[str]:
        """Blob did not contain deflate payload; do not count as MISS."""
        self.skip_bump = True
        self.cont_fail = 0
        if self.force_mode:
            self.force_no_deflate += 1
            if self.force_no_deflate >= 3:
                self.force_mode = False
                logger.debug("Disable force-prime due to repeated no-deflate blobs")
        return self.extract_frames(log_obis, logger)


class ObisStreamManager:
    """Manages per-device ObisStream and performs adaptive deflate priming/recovery."""

    def __init__(
        self,
        logger: logging.Logger,
        log_obis_frames: bool = False,
        force_cont_fail_threshold: int = 3,
        debug: bool = False,
    ):
        self._logger = logger
        self._streams: Dict[str, ObisStream] = {}
        self._last_off: Dict[str, int] = {}
        self._log_obis = log_obis_frames
        self._force_threshold = force_cont_fail_threshold
        self._debug = debug

    # ---------- Public API ----------

    def feed_blob(self, dev_id: str, blob: bytes) -> Tuple[List[str], bool, Optional[int]]:
        """
        Feed one protobuf 'blob' into the device stream and return:
          - frames: list of parsed OBIS frames (as strings)
          - skip_bump: whether caller should skip MISS bump for this blob
          - off_used: the deflate offset used (if any)
        """
        st = self._get_stream(dev_id)

        # Initialize stream if needed (hard prime)
        if not st.has_stream():
            if not self._find_offset_and_prime(st, blob):
                return [], st.skip_bump, None
            st.just_primed = True

        # If we just primed in this blob, do not decompress again; only extract frames
        if st.just_primed:
            st.just_primed = False
            frames = st.extract_frames(self._log_obis, self._logger)
            return frames, st.skip_bump, st.off

        # FORCE MODE: fast continue; if fails, no-timeout reprime
        if st.force_mode:
            if self._debug:
                self._logger.debug("Force-prime active (no timeout): trying quick continue first")
            status, _err = st.continue_decompress(blob)
            if status == "ok":
                st.cont_fail = 0
                st.force_mode = False
                st.force_no_deflate = 0
                frames = st.extract_frames(self._log_obis, self._logger)
                return frames, st.skip_bump, st.off

            new_off = self._probe_offset(blob, max_off=128)
            if new_off is None:
                frames = st.handle_no_deflate_blob(self._log_obis, self._logger)
                return frames, st.skip_bump, st.off

            frames = self._reprime_and_extract(st, blob, max_off=128)
            st.force_no_deflate = 0
            if not frames:
                st.skip_bump = True
            return frames, st.skip_bump, st.off

        # NORMAL MODE
        status, err = st.continue_decompress(blob)
        if status == "ok":
            st.cont_fail = 0
            st.force_no_deflate = 0
            frames = st.extract_frames(self._log_obis, self._logger)
            return frames, st.skip_bump, st.off

        if self._debug and status == "exception":
            self._logger.debug("Decompress exception at off=%s: %s; probing blob", st.off, err)

        new_off = self._probe_offset(blob, max_off=128)
        if new_off is None:
            frames = st.handle_no_deflate_blob(self._log_obis, self._logger)
            return frames, st.skip_bump, st.off

        last_off = self._last_off.get(st.dev_id)
        frames = self._reprime_and_extract(st, blob, max_off=128)
        self._learn_from_reprime(st, last_off)
        return frames, st.skip_bump, st.off

    def consume_skip_bump(self, dev_id: str) -> bool:
        """Return and clear the skip_bump flag for a device."""
        st = self._streams.get(dev_id)
        if not st:
            return False
        flag = bool(st.skip_bump)
        st.skip_bump = False
        return flag

    def get_offset(self, dev_id: str) -> Optional[int]:
        st = self._streams.get(dev_id)
        return st.off if st else None

    # ---------- Internal helpers ----------

    def _get_stream(self, dev_id: str) -> ObisStream:
        st = self._streams.get(dev_id)
        if not st:
            st = ObisStream(dev_id)
            self._streams[dev_id] = st
        return st

    def _has_obis_markers(self, data: bytes) -> bool:
        # Strict: require at least one complete '/...!' fragment
        return (b'/' in data) and (b'!' in data)

    def _probe_offset(self, blob: bytes, max_off: int = 128) -> Optional[int]:
        """Non-destructive probe to find a plausible raw-deflate start that yields OBIS-like output."""
        limit = min(max_off, len(blob) - 8)
        for off in range(0, max(0, limit) + 1):
            try:
                z = zlib.decompressobj(-15)
                out = z.decompress(blob[off:])
                if out and len(out) > 20 and self._has_obis_markers(out):
                    return off
            except Exception:
                continue
        return None

    def _find_offset_and_prime(self, st: ObisStream, blob: bytes, max_off: int = 64) -> bool:
        """Hard-prime: create a new zlib object and decompress from the best offset (requires OBIS markers)."""
        # Prefer last known good offset
        last = self._last_off.get(st.dev_id)
        if last is not None and last < len(blob):
            try:
                z = zlib.decompressobj(-15)
                out = z.decompress(blob[last:])
                if out and len(out) > 50 and self._has_obis_markers(out):
                    st.off = last
                    st.z = z
                    st.buf.extend(out)
                    return True
            except Exception:
                pass

        # Scan early bytes
        limit = min(max_off, len(blob) - 8)
        for off in range(0, max(0, limit) + 1):
            try:
                z = zlib.decompressobj(-15)
                out = z.decompress(blob[off:])
                if out and len(out) > 50 and self._has_obis_markers(out):
                    st.off = off
                    st.z = z
                    st.buf.extend(out)
                    self._last_off[st.dev_id] = off
                    if self._debug:
                        self._logger.debug(
                            "Primed zlib at REAL offset=%d (initial out_len=%d, has_obis=%s)",
                            off, len(out), True
                        )
                    return True
            except Exception:
                continue

        return False

    def _reprime_and_extract(self, st: ObisStream, blob: bytes, max_off: int = 128) -> List[str]:
        """Reset stream, prime again, and extract frames. If priming fails, mark skip_bump and return any residual."""
        st.reset()
        if not self._find_offset_and_prime(st, blob, max_off=max_off):
            st.skip_bump = True
            return st.extract_frames(self._log_obis, self._logger)
        return st.extract_frames(self._log_obis, self._logger)

    def _learn_from_reprime(self, st: ObisStream, last_off: Optional[int]) -> None:
        """Enable force-prime if we keep landing on the same offset but continue failing."""
        if last_off is not None and st.off == last_off:
            st.cont_fail += 1
            if st.cont_fail >= self._force_threshold:
                st.force_mode = True
                st.force_no_deflate = 0
                if self._debug:
                    self._logger.debug(
                        "Enable force-prime (no timeout), threshold reached (off=%s)", st.off
                    )
        else:
            st.cont_fail = 0