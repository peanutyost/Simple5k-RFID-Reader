"""
UHF RFID reader protocol (binary frame format from Manuels C# demo).
Frame: 0xA0, len, read_id, cmd, [data...], checksum
Checksum: ((~sum(bytes[0:-1])) + 1) & 0xFF
"""
import socket
import threading
import struct
from dataclasses import dataclass, field
from typing import Callable, Optional, List, Tuple
from queue import Queue, Empty


def checksum(data: bytes) -> int:
    """Same as C# ReaderUtils.CheckSum: sum then two's complement low byte."""
    s = sum(data) & 0xFF
    return ((~s) + 1) & 0xFF


def build_frame(read_id: int, cmd: int, payload: bytes = b"") -> bytes:
    """Build full frame: 0xA0, len, read_id, cmd, payload..., checksum. len = 3 + len(payload)."""
    body = bytes([read_id & 0xFF, cmd & 0xFF]) + payload
    length = len(body)  # number of bytes after the length byte
    frame = bytes([0xA0, length & 0xFF]) + body
    frame += bytes([checksum(frame)])
    return frame


def parse_frames(buffer: bytearray) -> List[Tuple[int, int, bytes]]:
    """
    Scan buffer for 0xA0 frames. Yield (read_id, cmd, payload) for each complete frame.
    Remove consumed bytes from buffer.
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
        need = 2 + length + 1  # hdr+len + payload+read_id+cmd + checksum
        if len(buffer) < need:
            break
        packet = bytes(buffer[:need])
        del buffer[:need]
        if checksum(packet[:-1]) != packet[-1]:
            continue
        read_id = packet[2]
        cmd = packet[3]
        payload = packet[4:-1]
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
    """Freq (MHz) and antenna number (1-8 or 1-16). ant_group 0 -> 1-8, 1 -> 9-16."""
    freq = (freq_ant >> 2) & 0x3F
    ant_low = freq_ant & 0x03
    rssi_high = (rssi_raw >> 7) & 1
    ant_no = ant_low + (4 if rssi_high else 0) + (8 if ant_group == 1 else 0) + 1
    freq_mhz = 865.0 + 0.5 * freq
    return freq_mhz, ant_no


def _rssi_display(rssi_raw: int) -> int:
    """Raw rssi byte to dBm-like value (demo: rssi - 129)."""
    rssi = rssi_raw & 0x7F
    return rssi - 129


def parse_inv_tag_data(payload: bytes, read_phase: bool = False, ant_group: int = 0) -> Optional[TagRead]:
    """
    Parse real-time inventory tag payload (cmd 0x89, 0x8A, 0x8B).
    Format: [FreqAnt(1)][PC(2)][EPC(n)][Rssi(1)][Phase(2)?]
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
    """TCP client for UHF reader. Push mode: start real-time inventory (0x89), receive tag frames."""

    def __init__(self, host: str, port: int, read_id: int = 0, ant_group: int = 0):
        self.host = host
        self.port = port
        self.read_id = read_id
        self.ant_group = ant_group
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._recv_buffer = bytearray()
        self._tag_callback: Optional[Callable[[TagRead], None]] = None
        self._response_queues: dict = {}  # cmd -> Queue for sync get responses
        self._response_lock = threading.Lock()
        self._connected = False

    def connect(self) -> bool:
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._sock.settimeout(5.0)
            self._sock.connect((self.host, self.port))
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

    def start_inventory_real(self, rounds: int = 0) -> bool:
        """Start real-time inventory (0x89). rounds=0 typically means continuous. Uses current work antenna (single)."""
        frame = build_frame(self.read_id, 0x89, bytes([rounds & 0xFF]))
        return self.send(frame)

    def start_fast_switch_inventory(
        self,
        antenna_indices: List[int],
        stay_ms: int = 1,
        interval_ms: int = 0,
        repeat: int = 0,
    ) -> bool:
        """Start fast switch antenna inventory (0x8A) for multi-antenna. antenna_indices: 1–8 (reader ports).
        Payload format per demo: antenna bytes (index 0-based, first has stay, rest 0), then interval, repeat."""
        if not antenna_indices or len(antenna_indices) > 8:
            return False
        # Build antenna + stay bytes (antType1 style: first antenna has stay, others 0)
        payload = bytearray()
        for i, ant in enumerate(antenna_indices):
            payload.append((ant - 1) & 0xFF)  # 1-based -> 0-based
            payload.append((stay_ms if i == 0 else 0) & 0xFF)
        payload.append(interval_ms & 0xFF)
        payload.append(repeat & 0xFF)
        frame = build_frame(self.read_id, 0x8A, bytes(payload))
        return self.send(frame)

    def set_tag_callback(self, cb: Callable[[TagRead], None]) -> None:
        self._tag_callback = cb

    def _receive_loop(self) -> None:
        while self._connected and self._sock and not self._stop.is_set():
            try:
                chunk = self._sock.recv(4096)
                if not chunk:
                    break
                self._recv_buffer.extend(chunk)
                for read_id, cmd, payload in parse_frames(self._recv_buffer):
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
                        if q is not None:
                            try:
                                err = payload[0] if len(payload) == 1 else None
                                q.put_nowait((payload, err))
                            except Exception:
                                pass
            except socket.timeout:
                continue
            except Exception:
                break
        self._connected = False

    def _send_and_wait(self, cmd: int, payload: bytes = b"", timeout: float = 2.0) -> Tuple[bytes, Optional[int]]:
        """Send a get command and wait for response. Returns (payload_bytes, error_code or None)."""
        q: Queue = Queue()
        with self._response_lock:
            self._response_queues[cmd] = q
        frame = build_frame(self.read_id, cmd, payload)
        if not self.send(frame):
            with self._response_lock:
                self._response_queues.pop(cmd, None)
            return b"", 0xFF
        try:
            result = q.get(timeout=timeout)
            return result
        except Empty:
            with self._response_lock:
                self._response_queues.pop(cmd, None)
            return b"", 0xFF
        except Exception:
            return b"", 0xFF

    def get_firmware_version(self) -> Optional[Tuple[int, int]]:
        """Returns (major, minor) or None."""
        payload, err = self._send_and_wait(CMD_GET_FIRMWARE)
        if err is not None and err != 0:
            return None
        if len(payload) >= 2:
            return (payload[0], payload[1])
        return None

    def get_reader_temperature(self) -> Optional[Tuple[int, int]]:
        """Returns (sign_byte, value) for ± value °C, or None."""
        payload, err = self._send_and_wait(CMD_GET_TEMPERATURE)
        if err is not None and err != 0:
            return None
        if len(payload) >= 2:
            return (payload[0], payload[1])
        return None

    def get_work_antenna(self) -> Optional[int]:
        payload, err = self._send_and_wait(CMD_GET_WORK_ANTENNA)
        if err is not None and err != 0 or len(payload) < 1:
            return None
        return payload[0]

    def set_work_antenna(self, antenna: int) -> bool:
        frame = build_frame(self.read_id, CMD_SET_WORK_ANTENNA, bytes([antenna & 0xFF]))
        return self.send(frame)

    def get_output_power(self) -> Optional[bytes]:
        """Returns 1 byte or 8 bytes (per antenna)."""
        payload, err = self._send_and_wait(CMD_GET_OUTPUT_POWER)
        if err is not None and err != 0:
            return None
        return payload if payload else None

    def set_output_power(self, power_bytes: bytes) -> bool:
        frame = build_frame(self.read_id, CMD_SET_OUTPUT_POWER, power_bytes)
        return self.send(frame)

    def get_frequency_region(self) -> Optional[bytes]:
        """Returns 3 bytes (region, start, end) or 6 for user-defined."""
        payload, err = self._send_and_wait(CMD_GET_FREQUENCY_REGION)
        if err is not None and err != 0:
            return None
        return payload if payload else None

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
