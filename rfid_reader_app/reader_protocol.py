"""
UHF RFID reader protocol per YR8900 Communication Interface Specification.
Frame: 0xA0, Len, Address (read_id), Cmd, [data...], Check
- Len = number of bytes after Len (Address + Cmd + data + Check).
- Check: two's complement of sum of bytes [0:-1] low byte: ((~sum) + 1) & 0xFF.
"""
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, List, Tuple, Union
from queue import Queue, Empty

READ_ID_LEN_12 = 12


def checksum(data: bytes) -> int:
    """Same as C# ReaderUtils.CheckSum: sum then two's complement low byte."""
    s = sum(data) & 0xFF
    return ((~s) + 1) & 0xFF


def build_frame(read_id: bytes, cmd: int, payload: bytes = b"") -> bytes:
    """Build frame per spec: Head 0xA0, Len (bytes after Len), Address, Cmd, data..., Check."""
    body = read_id + bytes([cmd & 0xFF]) + payload
    length = len(body) + 1  # Len = Address + Cmd + payload + Check
    frame = bytes([0xA0, length & 0xFF]) + body
    frame += bytes([checksum(frame)])
    return frame


def parse_frames(buffer: bytearray, read_id_len: int = 1) -> List[Tuple[bytes, int, bytes]]:
    """
    Scan buffer for 0xA0 frames. read_id_len is 1 or 12. Yield (read_id, cmd, payload).
    Remove consumed bytes from buffer.
    Supports two length conventions: L = bytes after length (incl. checksum), or L+1 if reader
    sends L = data only (excl. readId, cmd, checksum).
    """
    result = []
    while True:
        i = buffer.find(0xA0)
        if i < 0:
            del buffer[:]
            break
        if i > 0:
            del buffer[:i]
        if len(buffer) < 2:
            break
        length = buffer[1]
        # A: per YR8900 spec, Len = bytes after Len (incl. Check) -> packet = 2 + length
        # B/C: alternate conventions for other firmware
        need_a = 2 + length
        need_b = 3 + length
        need_c = 5 + length
        packet = None
        for need in (need_a, need_b, need_c):
            if len(buffer) >= need:
                cand = bytes(buffer[:need])
                if checksum(cand[:-1]) == cand[-1]:
                    packet = cand
                    del buffer[:need]
                    break
        if packet is None:
            del buffer[:1]
            continue
        body_len = len(packet) - 3
        id_len = read_id_len if (read_id_len in (1, READ_ID_LEN_12) and body_len >= read_id_len + 2) else 1
        read_id = packet[2 : 2 + id_len]
        cmd = packet[2 + id_len]
        payload = packet[2 + id_len + 1 : -1]
        result.append((read_id, cmd, payload))
    return result


@dataclass
class TagRead:
    """One tag read from the reader (all fields we can get)."""
    epc: str
    timestamp: str  # UTC ISO 8601 with 6 decimals
    rssi: int
    antenna: int
    frequency_mhz: float
    pc: str
    phase: str
    read_count: int = 1


def _parse_freq_ant(freq_ant: int, rssi_raw: int, ant_group: int = 0) -> Tuple[float, int]:
    """Per spec: FreqAnt high 6 bits = frequency, low 2 = ant; RSSI high bit => Ant 5/6/7/8 (else 1/2/3/4)."""
    freq = (freq_ant >> 2) & 0x3F
    ant_low = freq_ant & 0x03
    rssi_high = (rssi_raw >> 7) & 1  # high bit of RSSI used for Ant ID only, not RSSI
    ant_no = ant_low + (4 if rssi_high else 0) + (8 if ant_group == 1 else 0) + 1
    freq_mhz = 865.0 + 0.5 * freq
    return freq_mhz, ant_no


def _rssi_display(rssi_raw: int) -> int:
    """Raw rssi byte to dBm-like value (demo: rssi - 129)."""
    rssi = rssi_raw & 0x7F
    return rssi - 129


def parse_inv_tag_data(payload: bytes, read_phase: bool = False, ant_group: int = 0) -> Optional[TagRead]:
    """
    Parse real-time inventory tag (cmd 0x89, 0x8A, 0x8B) per spec: FreqAnt(1) PC(2) EPC(n) RSSI(1).
    Returns None if payload too short. Caller must assign timestamp immediately.
    """
    if len(payload) < 5:
        return None
    freq_ant = payload[0]
    pc_bytes = payload[1:3]
    if read_phase:
        epc_len = len(payload) - 6
    else:
        epc_len = len(payload) - 4
    if epc_len < 0:
        return None
    epc_bytes = payload[3:3 + epc_len]
    rssi_raw = payload[3 + epc_len]
    pc_hex = pc_bytes.hex().upper()
    epc_hex = epc_bytes.hex().upper()
    phase_hex = ""
    if read_phase and len(payload) >= 3 + epc_len + 3:
        phase_hex = payload[3 + epc_len + 1:3 + epc_len + 3].hex().upper()
    freq_mhz, ant_no = _parse_freq_ant(freq_ant, rssi_raw, ant_group)
    return TagRead(
        epc=epc_hex,
        timestamp="",  # caller sets immediately
        rssi=_rssi_display(rssi_raw),
        antenna=ant_no,
        frequency_mhz=round(freq_mhz, 2),
        pc=pc_hex,
        phase=phase_hex,
        read_count=1,
    )


def parse_fast_switch_completion(payload: bytes) -> Optional[Tuple[int, int]]:
    """
    Decode cmd_fast_switch_ant_inventory (0x8A) success response per YR8900 Communication
    Interface Specification §2.2.9: TotalRead 3 bytes (high bits left), CommandDuration 4 bytes
    (high bits left), in milliseconds. Returns (total_read, duration_ms) or None.
    """
    if len(payload) != 7:
        return None
    total_read = (payload[0] << 16) | (payload[1] << 8) | payload[2]
    duration_ms = (payload[3] << 24) | (payload[4] << 16) | (payload[5] << 8) | payload[6]
    return (total_read, duration_ms)


def parse_buffer_tag_data(payload: bytes, ant_group: int = 0) -> Optional[TagRead]:
    """
    Parse buffer tag payload (cmd 0x90, 0x91).
    Format: [TagCount(2)][DataLen(1)][PC(2)][EPC(DataLen-4)][CRC(2)][Rssi(1)][FreqAnt(1)][ReadCount(1)]
    Returns None if invalid. Caller must assign timestamp immediately.
    """
    if len(payload) < 10:
        return None
    # tag_count = struct.unpack_from(">H", payload, 0)[0]
    data_len = payload[2]
    pc_bytes = payload[3:5]
    epc_len = data_len - 4  # pc(2) + crc(2)
    if epc_len < 0 or 5 + data_len + 3 > len(payload):
        return None
    epc_bytes = payload[5:5 + epc_len]
    # crc = payload[5+epc_len:5+epc_len+2]
    rssi_raw = payload[5 + data_len]
    freq_ant = payload[5 + data_len + 1]
    read_count = payload[5 + data_len + 2]
    pc_hex = pc_bytes.hex().upper()
    epc_hex = epc_bytes.hex().upper()
    freq_mhz, ant_no = _parse_freq_ant(freq_ant, rssi_raw, ant_group)
    return TagRead(
        epc=epc_hex,
        timestamp="",
        rssi=_rssi_display(rssi_raw),
        antenna=ant_no,
        frequency_mhz=round(freq_mhz, 2),
        pc=pc_hex,
        phase="",
        read_count=read_count & 0x7F,
    )


# Response cmd codes (reader echoes cmd in response)
CMD_GET_FIRMWARE = 0x72
CMD_GET_WORK_ANTENNA = 0x75
CMD_GET_OUTPUT_POWER = 0x77
CMD_GET_FREQUENCY_REGION = 0x79
CMD_GET_TEMPERATURE = 0x7B
CMD_GET_DRM_MODE = 0x7D
CMD_SET_WORK_ANTENNA = 0x74
CMD_SET_OUTPUT_POWER = 0x76
CMD_SET_FREQUENCY_REGION = 0x78


class ReaderClient:
    """TCP client for UHF reader. read_id is 1 byte (standard) or 12 bytes."""

    def __init__(self, host: str, port: int, read_id: bytes, ant_group: int = 0):
        self.host = host
        self.port = port
        # Keep as 1 byte or 12 bytes (no padding)
        self.read_id = read_id if len(read_id) in (1, READ_ID_LEN_12) else (read_id[:1] or bytes([0]))
        self.read_id_len = len(self.read_id)
        self.ant_group = ant_group
        self._last_error_code: Optional[int] = None  # last error byte from reader response
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._recv_buffer = bytearray()
        self._tag_callback: Optional[Callable[[TagRead], None]] = None
        self._inventory_round_complete_callback: Optional[Callable[[], None]] = None
        self._response_queues: dict = {}  # cmd -> Queue for sync get responses
        self._response_lock = threading.Lock()
        self._recv_lock = threading.Lock()  # guards socket.recv() so main thread can do sync recv
        self._connected = False

    def connect(self) -> bool:
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._sock.settimeout(5.0)
            self._sock.connect((self.host, self.port))
            # Keep connection alive for long-running inventory (avoid firewall/router idle timeout)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            self._sock.settimeout(2.0)
            self._connected = True
            return True
        except Exception:
            self._connected = False
            return False

    def disconnect(self) -> None:
        self._stop.set()
        self._connected = False
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    def is_connected(self) -> bool:
        return self._connected and self._sock is not None

    def send(self, data: bytes) -> bool:
        if not self._sock:
            return False
        try:
            self._sock.sendall(data)
            return True
        except Exception:
            self._connected = False
            return False

    def _inv_read_id(self) -> bytes:
        """Per UHF demo: inventory commands (0x89, 0x8A) use 1-byte reader address only."""
        return self.read_id[0:1] if len(self.read_id) >= 12 else self.read_id

    def start_inventory_real(self, rounds: int = 0) -> bool:
        """Start real-time (multitag) inventory (0x89). rounds=0 -> 0xFF for continuous until stopped (per protocol)."""
        round_byte = 0xFF if rounds == 0 else (rounds & 0xFF)
        frame = build_frame(self._inv_read_id(), 0x89, bytes([round_byte]))
        return self.send(frame)

    def start_fast_switch_inventory(
        self,
        antenna_indices: List[int],
        stay_ms: int = 1,
        interval_ms: int = 0,
        repeat: int = 0,
    ) -> bool:
        """cmd_fast_switch_ant_inventory (0x8A) per spec §2.2.9: A Stay B Stay ... H Stay, Interval, Repeat."""
        if not antenna_indices or len(antenna_indices) > 8:
            return False
        enabled = set(antenna_indices)
        stay_val = max(1, stay_ms) & 0xFF
        payload = bytearray()
        for i in range(8):
            payload.append(i & 0xFF)  # ant index 00–07
            payload.append(stay_val if (i + 1) in enabled else 0)
        payload.append(interval_ms & 0xFF)
        payload.append((0xFF if repeat == 0 else repeat) & 0xFF)
        frame = build_frame(self._inv_read_id(), 0x8A, bytes(payload))
        return self.send(frame)

    def set_tag_callback(self, cb: Callable[[TagRead], None]) -> None:
        self._tag_callback = cb

    def set_inventory_round_complete_callback(self, cb: Optional[Callable[[], None]]) -> None:
        """Set callback invoked when reader sends 0x8A completion (TotalRead + CommandDuration). Callback should re-send start inventory."""
        self._inventory_round_complete_callback = cb

    def _receive_loop(self) -> None:
        consecutive_errors = 0
        max_consecutive_errors = 5  # only exit after several failures in a row
        while self._connected and self._sock and not self._stop.is_set():
            try:
                with self._recv_lock:
                    if not self._sock:
                        break
                    self._sock.settimeout(0.5)
                    chunk = self._sock.recv(4096)
                if not chunk:
                    # Clean connection close
                    break
                consecutive_errors = 0
                self._recv_buffer.extend(chunk)
                for read_id, cmd, payload in parse_frames(self._recv_buffer, self.read_id_len):
                    # 0x8A success response (TotalRead 3B + CommandDuration 4B per spec §2.2.9): restart inventory
                    if cmd == 0x8A and self._inventory_round_complete_callback:
                        completion = parse_fast_switch_completion(payload)  # spec: 7-byte payload only
                        if completion is not None:
                            try:
                                self._inventory_round_complete_callback()
                            except Exception:
                                pass
                            continue
                    if cmd in (0x89, 0x8A, 0x8B):
                        tag = parse_inv_tag_data(payload, read_phase=False, ant_group=self.ant_group)
                        if tag and self._tag_callback:
                            self._tag_callback(tag)
                    elif cmd in (0x90, 0x91):
                        tag = parse_buffer_tag_data(payload, ant_group=self.ant_group)
                        if tag and self._tag_callback:
                            self._tag_callback(tag)
                    else:
                        with self._response_lock:
                            q = self._response_queues.pop(cmd, None)
                            if q is None and (cmd & 0x80):
                                q = self._response_queues.pop(cmd & 0x7F, None)
                            if q is not None:
                                self._response_queues.pop(cmd | 0x80 if not (cmd & 0x80) else cmd & 0x7F, None)
                        if q is not None:
                            try:
                                # Reader success = 0x10 then data; error = single byte
                                if len(payload) > 1 and payload[0] == 0x10:
                                    actual_payload, err = payload[1:], None
                                elif len(payload) == 1:
                                    actual_payload, err = payload, payload[0]
                                    self._last_error_code = payload[0]
                                else:
                                    actual_payload, err = payload, None
                                q.put_nowait((actual_payload, err))
                            except Exception:
                                pass
            except socket.timeout:
                continue
            except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, OSError) as e:
                # Transient network errors: retry a few times before giving up
                consecutive_errors += 1
                if consecutive_errors >= max_consecutive_errors:
                    break
                time.sleep(min(0.5 * consecutive_errors, 2.0))
            except Exception:
                consecutive_errors += 1
                if consecutive_errors >= max_consecutive_errors:
                    break
                time.sleep(min(0.5 * consecutive_errors, 2.0))
        self._connected = False

    def _send_and_wait(self, cmd: int, payload: bytes = b"", timeout: float = 3.0) -> Tuple[bytes, Optional[int]]:
        """Send a get command and wait for response. Hold recv lock before sending so receive thread cannot
        read the response first; use 1-byte reader address like inventory."""
        self._last_error_code = None
        frame = build_frame(self._inv_read_id(), cmd, payload)
        resp_cmd_alt = cmd | 0x80
        deadline = time.monotonic() + timeout
        with self._recv_lock:
            if not self._sock:
                return b"", 0xFF
            if not self.send(frame):
                return b"", 0xFF
            self._sock.settimeout(0.25)
            buf = bytearray(self._recv_buffer)
            self._recv_buffer.clear()
            while time.monotonic() < deadline:
                try:
                    chunk = self._sock.recv(4096)
                except socket.timeout:
                    continue
                except Exception:
                    return b"", 0xFF
                if not chunk:
                    return b"", 0xFF
                buf.extend(chunk)
                for read_id, c, pay in parse_frames(buf, 1):
                    if c in (cmd, resp_cmd_alt):
                        if len(pay) > 1 and pay[0] == 0x10:
                            actual, err = pay[1:], None
                        elif len(pay) == 1:
                            actual, err = pay, pay[0]
                            self._last_error_code = pay[0]
                        else:
                            actual, err = pay, None
                        if buf:
                            self._recv_buffer[:0] = buf
                        return (actual, err)
            if buf:
                self._recv_buffer[:0] = buf
            if self._sock:
                self._sock.settimeout(2.0)
        return b"", 0xFF

    def get_last_error_code(self) -> Optional[int]:
        """Last error byte from reader (e.g. 5); None if no error."""
        return getattr(self, "_last_error_code", None)

    def get_firmware_version(self) -> Optional[Tuple[int, int]]:
        """Returns (major, minor) or None. Per Manuels: 2 bytes = major, minor; 1 byte = error."""
        payload, err = self._send_and_wait(CMD_GET_FIRMWARE)
        if len(payload) >= 2:
            return (payload[0], payload[1])
        if len(payload) == 1:
            self._last_error_code = payload[0]
        return None

    def get_firmware_version_raw(self) -> Tuple[bytes, Optional[int]]:
        """Returns (raw_payload_bytes, error_code_or_None) for diagnostics/debug."""
        return self._send_and_wait(CMD_GET_FIRMWARE)

    def get_reader_temperature(self) -> Optional[Tuple[int, int]]:
        """Returns (sign_byte, value) for ± value °C, or None. Per Manuels: 2 bytes = data; 1 = error."""
        payload, err = self._send_and_wait(CMD_GET_TEMPERATURE)
        if len(payload) >= 2:
            return (payload[0], payload[1])
        if len(payload) == 1:
            self._last_error_code = payload[0]
        return None

    def get_work_antenna(self) -> Optional[int]:
        """Per Manuels: 1-byte response 0x00–0x07 = work antenna index; other 1-byte = error."""
        payload, err = self._send_and_wait(CMD_GET_WORK_ANTENNA)
        if len(payload) >= 1 and payload[0] in range(8):
            return payload[0]
        if len(payload) == 1:
            self._last_error_code = payload[0]
        return None

    def set_work_antenna(self, antenna: int) -> bool:
        frame = build_frame(self.read_id, CMD_SET_WORK_ANTENNA, bytes([antenna & 0xFF]))
        return self.send(frame)

    def get_output_power(self) -> Optional[bytes]:
        """Returns 1 byte or 8 bytes (per antenna). Per Manuels: 1/4/8 bytes = data."""
        payload, err = self._send_and_wait(CMD_GET_OUTPUT_POWER)
        if len(payload) in (1, 4, 8):
            return payload
        return None

    def set_output_power(self, power_bytes: bytes) -> bool:
        frame = build_frame(self.read_id, CMD_SET_OUTPUT_POWER, power_bytes)
        return self.send(frame)

    def get_frequency_region(self) -> Optional[bytes]:
        """Returns 3 bytes (region, start, end) or 6 for user-defined. Per Manuels: 3/6 = data; 1 = error."""
        payload, err = self._send_and_wait(CMD_GET_FREQUENCY_REGION)
        if len(payload) == 3 or len(payload) == 6:
            return payload
        if len(payload) == 1:
            self._last_error_code = payload[0]
        return None

    def set_frequency_region(self, region: int, start: int, end: int) -> bool:
        frame = build_frame(self.read_id, CMD_SET_FREQUENCY_REGION, bytes([region & 0xFF, start & 0xFF, end & 0xFF]))
        return self.send(frame)

    def start_receive_thread(self) -> None:
        """Start background thread that receives and parses frames, calls tag_callback for each tag."""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._receive_loop, daemon=True)
        self._thread.start()
