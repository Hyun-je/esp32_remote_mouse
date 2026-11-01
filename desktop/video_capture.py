"""FFmpeg video capture with latest-frame delivery for the Tk thread.

list_devices() returns CaptureDevice objects. VideoCapture(device, width, height)
scales inside the given bounds while preserving aspect ratio. start() launches a
worker; latest() returns a new (width, height, PPM bytes) tuple or None without
blocking. Check error for a failure, and call stop() when switching or closing.
"""
from dataclasses import dataclass
import glob
import math
import os
import re
import shutil
import subprocess
import sys
import threading

_device_locks = {}
_device_locks_guard = threading.Lock()
_device_frame_rates = {}
_device_rate_choices = {}


class CaptureError(RuntimeError):
    pass


@dataclass(frozen=True)
class CaptureDevice:
    name: str
    format: str
    source: str


def _ffmpeg():
    executable = shutil.which('ffmpeg')
    if not executable:
        raise CaptureError('비디오 캡처에는 FFmpeg가 필요합니다. FFmpeg를 설치하고 PATH에 추가하세요.')
    return executable


def list_devices():
    """List available video devices. Device enumeration may take a few seconds."""
    executable = _ffmpeg()
    if sys.platform.startswith('linux'):
        devices = []
        for path in sorted(glob.glob('/dev/video*')):
            try:
                with open('/sys/class/video4linux/' + os.path.basename(path) + '/name', encoding='utf-8') as f:
                    name = f.read().strip()
            except OSError:
                name = os.path.basename(path)
            devices.append(CaptureDevice(f'{name} ({path})', 'v4l2', path))
        return devices
    if sys.platform == 'darwin':
        args = [executable, '-hide_banner', '-f', 'avfoundation', '-list_devices', 'true', '-i', '']
    elif sys.platform == 'win32':
        args = [executable, '-hide_banner', '-list_devices', 'true', '-f', 'dshow', '-i', 'dummy']
    else:
        raise CaptureError('이 운영체제에서는 비디오 장치 검색을 지원하지 않습니다.')
    try:
        result = subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=8, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CaptureError(f'비디오 장치 검색 실패: {exc}') from exc
    output = result.stderr.decode('utf-8', 'replace')
    devices = []
    if sys.platform == 'darwin':
        video_section = False
        for line in output.splitlines():
            if 'AVFoundation video devices:' in line:
                video_section = True
            elif 'AVFoundation audio devices:' in line:
                break
            elif video_section:
                match = re.search(r'\[(\d+)\]\s+(.+)$', line)
                if match:
                    index, name = match.groups()
                    devices.append(CaptureDevice(name.strip(), 'avfoundation', f'{index}:none'))
        if 'AVFoundation video devices:' not in output:
            raise CaptureError('비디오 장치 검색 실패: ' + output.strip()[-500:])
    else:
        for line in output.splitlines():
            match = re.search(r'"(.+?)"\s*\(video\)', line)
            if match:
                name = match.group(1)
                devices.append(CaptureDevice(name, 'dshow', 'video=' + name))
        if not devices and 'DirectShow' not in output and 'dshow' not in output.lower():
            raise CaptureError('비디오 장치 검색 실패: ' + output.strip()[-500:])
    return devices


def list_resolutions(device):
    """Return advertised (width, height) pairs; call from a worker thread.

    FFmpeg reports formats on stderr and normally exits nonzero after listing.
    An empty list means the device did not report discrete sizes. The active
    capture for the same device must be stopped before calling this function.
    """
    executable = _ffmpeg()
    if device.format == 'avfoundation':
        # Asking for an impossible size makes AVFoundation print all modes.
        options = ['-f', 'avfoundation', '-framerate', '30', '-video_size', '1x1', '-i', device.source,
                   '-frames:v', '1', '-f', 'null', '-']
    elif device.format == 'dshow':
        options = ['-f', 'dshow', '-list_options', 'true', '-i', device.source]
    elif device.format == 'v4l2':
        options = ['-f', 'v4l2', '-list_formats', 'all', '-i', device.source]
    else:
        return []
    with _device_locks_guard:
        device_lock = _device_locks.setdefault((device.format, device.source), threading.Lock())
    if not device_lock.acquire(timeout=5):
        raise CaptureError('영상 장치를 해제하지 못했습니다. 다시 시도하세요.')
    try:
        try:
            result = subprocess.run([executable, '-hide_banner', '-loglevel', 'info', *options],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=8, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CaptureError(f'해상도 조회 실패: {exc}') from exc
    finally:
        device_lock.release()
    output = result.stderr.decode('utf-8', 'replace')
    sizes = set()
    rates = {}
    rate_choices = {}
    for line in output.splitlines():
        if device.format == 'avfoundation':
            match = re.search(r'\b(\d{2,5})x(\d{2,5})@\[\s*([\d.]+)\s+([\d.]+)\]fps', line)
            if match:
                width, height = int(match.group(1)), int(match.group(2))
                minimum, maximum = float(match.group(3)), float(match.group(4))
                if 0 < minimum <= maximum:
                    size = (width, height)
                    sizes.add(size)
                    # FFmpeg's AVFoundation input matches the reported maximum
                    # frame rate; a range minimum can still be rejected.
                    rate_choices.setdefault(size, set()).add(maximum)
                    rate = maximum
                    if size not in rates or abs(rate - 30) < abs(rates[size] - 30):
                        rates[size] = rate
        else:
            for match in re.finditer(r'\b(\d{2,5})x(\d{2,5})\b', line):
                sizes.add((int(match.group(1)), int(match.group(2))))
    with _device_locks_guard:
        _device_frame_rates[(device.format, device.source)] = rates
        _device_rate_choices[(device.format, device.source)] = rate_choices
    return sorted(sizes, key=lambda size: (-size[0] * size[1], -size[0], -size[1]))


def list_frame_rates(device, source_size=None):
    """Return FPS values from the most recent resolution query for a device.

    When resolution is automatic, return only rates advertised for every
    discovered size, since the device chooses its own input resolution.
    Unsupported or unqueried formats return an empty list.
    """
    with _device_locks_guard:
        choices = _device_rate_choices.get((device.format, device.source), {})
        if source_size is not None:
            result = set(choices.get(tuple(source_size), ()))
        elif choices:
            result = set.intersection(*(set(values) for values in choices.values()))
        else:
            result = set()
    return sorted(result)


class VideoCapture:
    def __init__(self, device, width, height, source_size=None, fps=None):
        self.device = device
        self.width = max(1, int(width))
        self.height = max(1, int(height))
        if source_size is not None:
            if len(source_size) != 2 or any(not isinstance(value, int) or not 0 < value <= 32768 for value in source_size):
                raise ValueError('invalid source resolution')
            source_size = tuple(source_size)
        self.source_size = source_size
        if fps is not None:
            if isinstance(fps, bool) or not isinstance(fps, (int, float)) or not math.isfinite(fps) or not 0 < fps <= 1000:
                raise ValueError('invalid frame rate')
        self.fps = fps
        self.error = None
        self._frame = None
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._process = None
        self._thread = None
        self._stderr = bytearray()

    def start(self):
        """Start background capture once; errors are available through error."""
        if self._thread is not None:
            raise RuntimeError('capture already started')
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def latest(self):
        """Return and clear the newest frame, without waiting for the camera."""
        with self._lock:
            frame, self._frame = self._frame, None
        return frame

    def stop(self):
        """Request termination without waiting on the Tk thread."""
        self._stopped.set()
        with self._lock:
            self._frame = None
            process = self._process
        if process and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass

    def _drain_stderr(self, pipe):
        for data in iter(lambda: pipe.read(1024), b''):
            self._stderr.extend(data)
            if len(self._stderr) > 8192:
                del self._stderr[:-8192]

    @staticmethod
    def _token(pipe):
        token = bytearray()
        while True:
            char = pipe.read(1)
            if not char:
                raise EOFError
            if char.isspace():
                if token:
                    return bytes(token)
            elif char == b'#' and not token:
                pipe.readline()
            else:
                token.extend(char)
                if len(token) > 20:
                    raise CaptureError('잘못된 영상 프레임 헤더')

    @staticmethod
    def _read_exact(pipe, size):
        data = bytearray()
        while len(data) < size:
            chunk = pipe.read(size - len(data))
            if not chunk:
                raise EOFError
            data.extend(chunk)
        return bytes(data)

    def _run(self):
        process = None
        stderr_thread = None
        with _device_locks_guard:
            device_lock = _device_locks.setdefault((self.device.format, self.device.source), threading.Lock())
        acquired = False
        try:
            # Serialise old/new instances without blocking Tk while a camera is closing.
            while not self._stopped.is_set():
                if device_lock.acquire(timeout=0.1):
                    acquired = True
                    break
            if not acquired or self._stopped.is_set():
                return
            executable = _ffmpeg()
            args = [executable, '-hide_banner', '-loglevel', 'error', '-f', self.device.format]
            # AVFoundation defaults to 30000/1001, which some Mac cameras reject.
            if self.fps is not None:
                args.extend(['-framerate', str(self.fps)])
            elif self.device.format == 'avfoundation':
                with _device_locks_guard:
                    rate = _device_frame_rates.get((self.device.format, self.device.source), {}).get(self.source_size, 30)
                args.extend(['-framerate', str(rate)])
            if self.source_size:
                args.extend(['-video_size', f'{self.source_size[0]}x{self.source_size[1]}'])
            args += ['-i', self.device.source, '-an', '-vf',
                    f'scale={self.width}:{self.height}:force_original_aspect_ratio=decrease',
                    '-fps_mode', 'passthrough', '-pix_fmt', 'rgb24', '-c:v', 'ppm', '-f', 'image2pipe', 'pipe:1']
            if self._stopped.is_set():
                return
            process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       bufsize=1024 * 1024)
            with self._lock:
                self._process = process
            stderr_thread = threading.Thread(target=self._drain_stderr, args=(process.stderr,), daemon=True)
            stderr_thread.start()
            if self._stopped.is_set():
                process.terminate()
            while not self._stopped.is_set():
                magic = self._token(process.stdout)
                width = int(self._token(process.stdout))
                height = int(self._token(process.stdout))
                max_value = int(self._token(process.stdout))
                if magic != b'P6' or max_value != 255 or not (0 < width <= self.width and 0 < height <= self.height):
                    raise CaptureError('잘못된 영상 프레임')
                pixels = self._read_exact(process.stdout, width * height * 3)
                frame = (width, height, f'P6\n{width} {height}\n255\n'.encode('ascii') + pixels)
                with self._lock:
                    if not self._stopped.is_set():
                        self._frame = frame
        except (OSError, ValueError, CaptureError, EOFError) as exc:
            if not self._stopped.is_set():
                if process:
                    try:
                        process.wait(timeout=0.5)
                    except subprocess.TimeoutExpired:
                        pass
                details = self._stderr.decode('utf-8', 'replace').strip()
                self.error = '비디오 캡처 실패: ' + (details[-800:] or str(exc) or '영상 신호가 없습니다')
        finally:
            if process:
                if process.poll() is None:
                    try:
                        process.terminate()
                        process.wait(timeout=1)
                    except (OSError, subprocess.TimeoutExpired):
                        process.kill()
                        process.wait()
                if process.stdout:
                    process.stdout.close()
                if stderr_thread:
                    stderr_thread.join(timeout=0.5)
                if process.stderr:
                    process.stderr.close()
            with self._lock:
                self._process = None
            if acquired:
                device_lock.release()
