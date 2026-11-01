"""Serial I/O stays off the GUI thread and preserves input event ordering."""
import collections
import os
import sys
import threading
import time
from pathlib import Path

LOG_PATH = Path(os.environ.get('REMOTE_INPUT_LOG') or (
    Path.home() / 'Library' / 'Logs' / 'esp32_remote_mouse.log' if sys.platform == 'darwin'
    else Path.home() / '.esp32_remote_mouse.log'))
LOG_LIMIT = 1_000_000
# Firmware lines worth keeping for link diagnosis; repeated status polls are logged only on change.
LOG_PREFIXES = ('BLE ', 'READY', 'ADVERTISING', 'SLEEPING', 'AWAKE', 'INPUT_TIMEOUT', 'ERR', 'ESP-ROM', 'rst:', 'waiting for download')

class LinkLog:
    """Appends link events with timestamps so a failed reconnect can be traced afterwards."""
    def __init__(self, path=LOG_PATH):
        self.path = Path(path)
        self.last_status = None

    def write(self, text):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            mode = 'w' if self.path.exists() and self.path.stat().st_size > LOG_LIMIT else 'a'
            with self.path.open(mode, encoding='utf-8') as log:
                log.write(time.strftime('%Y-%m-%d %H:%M:%S ') + text + '\n')
        except OSError:
            pass

    def line(self, line):
        if line.startswith(('CONNECTED', 'DISCONNECTED')):
            if line != self.last_status:
                self.last_status = line
                self.write(line)
        elif line.startswith(LOG_PREFIXES):
            self.write(line)

class SerialTransport:
    def __init__(self, port, events):
        self.port = port
        self.events = events
        self.log = LinkLog()
        self.outgoing = collections.deque()
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def send(self, line):
        with self.lock:
            # Collapse adjacent pointer updates without crossing button/key events.
            command = line.split(' ', 1)[0]
            if command in ('move', 'goto') and self.outgoing and self.outgoing[-1].startswith(command + ' '):
                self.outgoing[-1] = line
            else:
                self.outgoing.append(line)

    def discard_pointer_motion(self):
        with self.lock:
            self.outgoing = collections.deque(
                line for line in self.outgoing if not line.startswith(('goto ', 'move ', 'prepare ')))

    def close(self):
        self.stop.set()

    def _run(self):
        try:
            import serial
            with serial.Serial(self.port, 115200, timeout=0.001, write_timeout=0.5) as device:
                self.events.put((self, 'serial', 'connected'))
                self.log.write(f'USB open {self.port}')
                next_status = 0
                incoming = bytearray()
                while not self.stop.is_set():
                    with self.lock:
                        cmd = self.outgoing.popleft() if self.outgoing else None
                    if cmd:
                        device.write((cmd + '\n').encode('ascii'))
                    now = time.monotonic()
                    if now >= next_status:
                        device.write(b'status\n')
                        next_status = now + 1.0
                    incoming.extend(device.read(device.in_waiting or 1))
                    while b'\n' in incoming:
                        raw, _, rest = incoming.partition(b'\n')
                        incoming = bytearray(rest)
                        line = raw.decode('utf-8', 'replace').strip()
                        self.log.line(line)
                        if line.startswith(('CONNECTED', 'DISCONNECTED')):
                            self.events.put((self, 'ble', line.startswith('CONNECTED')))
                            if ' sleeping=' in line:
                                self.events.put((self, 'sleep', ' sleeping=1' in line))
                        elif line == 'ERR not connected':
                            self.events.put((self, 'ble', False))
                        elif line in ('SLEEPING', 'AWAKE'):
                            self.events.put((self, 'sleep', line == 'SLEEPING'))
                        elif line == 'INPUT_TIMEOUT':
                            self.events.put((self, 'input_timeout', True))
                        elif line.startswith('ERR'):
                            self.events.put((self, 'error', line))
                    if len(incoming) > 4096:
                        incoming.clear()
                # Best effort on orderly close. No guarantee if USB has failed.
                device.write(b'release\n')
                device.flush()
        except Exception as exc:
            self.log.write(f'USB error {exc}')
            self.events.put((self, 'error', str(exc)))
        finally:
            self.log.write('USB closed')
            self.events.put((self, 'serial', 'disconnected'))
