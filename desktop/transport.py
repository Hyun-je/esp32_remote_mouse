"""Serial I/O stays off the GUI thread and preserves input event ordering."""
import collections
import threading
import time

class SerialTransport:
    def __init__(self, port, events):
        self.port = port
        self.events = events
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
                        if line.startswith('CONNECTED'):
                            self.events.put((self, 'ble', True))
                        elif line.startswith('DISCONNECTED') or line == 'ERR not connected':
                            self.events.put((self, 'ble', False))
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
            self.events.put((self, 'error', str(exc)))
        finally:
            self.events.put((self, 'serial', 'disconnected'))
