#!/usr/bin/env python3
"""Focused-window remote control for an ESP32 BLE HID bridge."""
import json
import os
import queue
import sys
import threading
import time
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox

from keymap import KeyState, usage_from_event
from mac_shortcuts import MacHideShortcut
from transport import SerialTransport, peer_label
from video_capture import CaptureError, VideoCapture, list_devices, list_resolutions, list_frame_rates

DEFAULT_IDLE_MINUTES = 5
SETTINGS_PATH = Path(os.environ.get('REMOTE_INPUT_SETTINGS') or (
    Path.home() / 'Library' / 'Application Support' / 'esp32_remote_mouse' / 'settings.json'
    if sys.platform == 'darwin' else Path.home() / '.esp32_remote_mouse.json'))
# Firmware status lines already in flight may predate a sleep or wake request.
SLEEP_REPORT_GRACE = 1.5


def content_rect(width, height, target_width, target_height):
    if min(width, height, target_width, target_height) <= 0:
        return (0, 0, 0, 0)
    scale = min(width / target_width, height / target_height)
    w, h = target_width * scale, target_height * scale
    return ((width - w) / 2, (height - h) / 2, w, h)


def normalized(x, y, rect):
    left, top, width, height = rect
    if width <= 1 or height <= 1:
        return None
    # Pixel center at the last pixel represents the far edge.
    nx = min(1, max(0, (x - left) / (width - 1)))
    ny = min(1, max(0, (y - top) / (height - 1)))
    return round(nx * 10000), round(ny * 10000)


def valid_idle_minutes(value):
    minutes = float(value)
    if not 0 <= minutes <= 1440:
        raise ValueError(value)
    return minutes


def load_settings(path=None):
    try:
        with open(path or SETTINGS_PATH, encoding='utf-8') as file:
            data = json.load(file)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(changes, path=None):
    """Merge changes into the stored settings; a write failure only loses persistence."""
    path = Path(path or SETTINGS_PATH)
    data = load_settings(path)
    data.update(changes)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(data, indent=2), encoding='utf-8')
        temporary.replace(path)
        return True
    except OSError:
        return False


def signed16(value):
    value &= 0xffff
    return value - 0x10000 if value & 0x8000 else value


def touchpad_vertical_delta(packed):
    """Tk packs signed X and Y deltas into the high and low 16 bits."""
    return signed16(int(packed))


def touchpad_horizontal_delta(packed):
    return signed16(int(packed) >> 16)


# The firmware scrolls in 1/8-detent units and sends whole detents to hosts without high-resolution wheels.
SCROLL_UNITS = 8
MAX_SCROLL_UNITS = 127 * SCROLL_UNITS
# Trackpad pixels and Windows wheel deltas per detent.
TOUCHPAD_DETENT = 30
WINDOWS_DETENT = 120


def wheel_detent(platform, tk_patchlevel):
    """MouseWheel delta per detent, or None when Tk reports whole detents (Tk 8.6 on macOS/X11)."""
    major_minor = tuple(int(part) for part in tk_patchlevel.split('.')[:2])
    return WINDOWS_DETENT if platform == 'win32' or major_minor >= (8, 7) else None


# Firmware button numbers for middle, back and forward (relative report).
MIDDLE_BUTTON, BACK_BUTTON, FORWARD_BUTTON = 5, 6, 7
# HID Consumer page usages and System Control usages offered in the media menu.
MEDIA_ACTIONS = (
    ('재생/일시정지', 'media', 0xcd), ('이전 트랙', 'media', 0xb6), ('다음 트랙', 'media', 0xb5),
    None,
    ('음소거', 'media', 0xe2), ('볼륨 낮추기', 'media', 0xea), ('볼륨 높이기', 'media', 0xe9),
    None,
    ('밝기 낮추기', 'media', 0x70), ('밝기 높이기', 'media', 0x6f),
    None,
    ('홈 (Android)', 'media', 0x223), ('뒤로 (Android)', 'media', 0x224),
    None,
    ('시스템 절전', 'system', 0x82),
)


class App:
    def __init__(self, root):
        self.root = root
        root.title('ESP32 Remote Input')
        root.geometry('850x700')
        root.minsize(340, 300)
        self.events = queue.Queue()
        self.link = None
        self.ble = False
        self.peer = None
        self.hires = None
        self.hires_status = tk.StringVar()
        self.link_error = None
        self.inside = False
        self.dragging = False
        self.drag_button = None
        self.scroll_remainder = [0.0, 0.0]
        # Tk 8.7+ reports wheel deltas in multiples of 120 on every platform.
        self.wheel_detent = wheel_detent(sys.platform, root.tk.call('info', 'patchlevel'))
        self.language_key_down = False
        self.home_key_down = False
        self.mac_hide = MacHideShortcut()
        self.keys = KeyState()
        self.size = (1024, 768)
        self.manual_size = self.size
        self.capture_device = None
        self.capture = None
        self.capture_photo = None
        self.last_video_frame = None
        self.next_video_draw_at = 0.0
        self.capture_resize_job = None
        self.capture_devices = {}
        self.capture_choice = tk.StringVar(value='사용 안 함')
        self.capture_status = tk.StringVar(value='영상 캡처 사용 안 함')
        self.capture_selector = None
        self.capture_resolution_selector = None
        self.capture_resolution_choice = tk.StringVar(value='자동')
        self.capture_resolutions = []
        self.capture_source_size = None
        self.capture_fps = None
        self.capture_fps_values = []
        self.capture_fps_choice = tk.StringVar(value='자동')
        self.capture_fps_selector = None
        self.capture_modes_generation = 0
        self.capture_modes_loading = False
        self.capture_mode_results = queue.Queue()
        self.capture_modes_status = tk.StringVar(value='')
        self.resolution_controls = []
        self.rect = (0, 0, 0, 0)
        self.status = tk.StringVar(value='USB 연결 전')
        self.width = tk.StringVar(value='1024')
        self.height = tk.StringVar(value='768')
        self.port = tk.StringVar()
        self.alternate_click = tk.BooleanVar(value=True)
        self.caps_language = tk.BooleanVar(value=False)
        self.always_on_top = tk.BooleanVar(value=False)
        self.settings_window = None
        self.ports = None
        self.connect_button = None
        self.port_values = []
        self.fullscreen = False
        try:
            self.idle_minutes = valid_idle_minutes(load_settings().get('idle_minutes', DEFAULT_IDLE_MINUTES))
        except (TypeError, ValueError):
            self.idle_minutes = DEFAULT_IDLE_MINUTES
        self.idle_choice = tk.StringVar(value=f'{self.idle_minutes:g}')
        self.last_activity = time.monotonic()
        self.sleeping = False
        self.sleep_changed_at = 0.0

        self.canvas = tk.Canvas(root, bg='#181c22', highlightthickness=0,
                                borderwidth=0, takefocus=True)
        self.canvas.pack(fill='both', expand=True)
        bar = self.toolbar = ttk.Frame(self.canvas, padding=8)
        ttk.Button(bar, text='홈', command=lambda: self.system_action(11, 8)).pack(side='left', padx=(0, 6))
        ttk.Button(bar, text='앱 전환', command=lambda: self.system_action(43, 8)).pack(side='left')
        ttk.Button(bar, text='Spotlight', command=lambda: self.system_action(44, 8)).pack(side='left', padx=(6, 0))
        ttk.Button(bar, text='Dock', command=lambda: self.system_action(7, 12)).pack(side='left', padx=(6, 0))
        ttk.Button(bar, text='앱 창 보기', command=self.expose).pack(side='left', padx=(6, 0))
        media = ttk.Menubutton(bar, text='미디어')
        media_menu = tk.Menu(media, tearoff=False)
        for action in MEDIA_ACTIONS:
            if action is None:
                media_menu.add_separator()
            else:
                label, command, usage = action
                media_menu.add_command(label=label, command=lambda c=command, u=usage: self.target_command(f'{c} {u}'))
        media['menu'] = media_menu
        media.pack(side='left', padx=(6, 0))
        self.fullscreen_button = ttk.Button(bar, text='전체 화면', command=self.toggle_fullscreen)
        self.fullscreen_button.pack(side='left', padx=(6, 0))
        ttk.Button(bar, text='설정', command=self.open_settings).pack(side='right')
        ttk.Label(bar, textvariable=self.status).pack(side='right', padx=10)
        self.toolbar_item = self.canvas.create_window(0, 0, anchor='nw', window=bar, state='hidden')
        self.toolbar_visible = False
        self.toolbar_hide_job = None
        self.canvas.bind('<Configure>', self.redraw)
        self.canvas.bind('<Enter>', self.enter)
        self.canvas.bind('<Leave>', self.leave)
        self.canvas.bind('<Motion>', self.motion)
        self.canvas.bind('<ButtonPress-1>', self.mouse_down)
        self.canvas.bind('<B1-Motion>', self.mouse_drag)
        self.canvas.bind('<ButtonRelease-1>', self.mouse_up)
        right_button = 2 if sys.platform == 'darwin' else 3
        self.canvas.bind(f'<ButtonPress-{right_button}>', lambda event: self.mouse_down(event, 2))
        self.canvas.bind(f'<B{right_button}-Motion>', self.mouse_drag)
        self.canvas.bind(f'<ButtonRelease-{right_button}>', lambda event: self.mouse_up(event, 2))
        middle_button = 3 if sys.platform == 'darwin' else 2
        extra_buttons = [(middle_button, MIDDLE_BUTTON)]
        # Tk 8.6 on X11 uses buttons 4-7 for wheels and 8/9 for back/forward; elsewhere, and
        # on X11 since Tk 8.7, back/forward arrive as buttons 4/5.
        if self.wheel_detent or not sys.platform.startswith('linux'):
            extra_buttons += [(4, BACK_BUTTON), (5, FORWARD_BUTTON)]
        extra_buttons += [(8, BACK_BUTTON), (9, FORWARD_BUTTON)]
        for tk_button, button in extra_buttons:
            try:
                self.canvas.bind(f'<ButtonPress-{tk_button}>', lambda event, b=button: self.mouse_down(event, b))
                self.canvas.bind(f'<B{tk_button}-Motion>', self.mouse_drag)
                self.canvas.bind(f'<ButtonRelease-{tk_button}>', lambda event, b=button: self.mouse_up(event, b))
            except tk.TclError:
                pass  # Older Tk releases accept only buttons 1-5.
        self.canvas.bind('<MouseWheel>', self.mouse_wheel)
        self.canvas.bind('<Shift-MouseWheel>', lambda event: self.mouse_wheel(event, 1))
        try:
            self.canvas.bind('<TouchpadScroll>', self.touchpad_scroll)
        except tk.TclError:
            pass  # Older Tk releases expose trackpad movement as MouseWheel.
        if sys.platform.startswith('linux'):
            self.canvas.bind('<Button-4>', lambda event: self.scroll(event, SCROLL_UNITS))
            self.canvas.bind('<Button-5>', lambda event: self.scroll(event, -SCROLL_UNITS))
            self.canvas.bind('<Shift-Button-4>', lambda event: self.scroll(event, -SCROLL_UNITS, 1))
            self.canvas.bind('<Shift-Button-5>', lambda event: self.scroll(event, SCROLL_UNITS, 1))
            for tk_button, units in ((6, -SCROLL_UNITS), (7, SCROLL_UNITS)):
                try:
                    self.canvas.bind(f'<Button-{tk_button}>', lambda event, u=units: self.scroll(event, u, 1))
                except tk.TclError:
                    pass
        self.canvas.bind('<KeyPress>', self.key_down)
        self.canvas.bind('<KeyRelease>', self.key_up)
        self.canvas.bind('<FocusOut>', self.focus_out)
        self.canvas.bind('<FocusIn>', self.focus_in)
        root.bind_all('<Motion>', self.toolbar_motion, add='+')
        root.bind('<Leave>', self.toolbar_leave, add='+')
        root.protocol('WM_DELETE_WINDOW', self.close)
        self.refresh()
        self.auto_connect()
        root.after(50, self.poll)
        root.after(33, self.poll_video)

    def toggle_fullscreen(self):
        self.reset_input()
        self.fullscreen = not self.fullscreen
        self.root.attributes('-fullscreen', self.fullscreen)
        self.fullscreen_button.configure(text='전체 화면 종료' if self.fullscreen else '전체 화면')
        self.root.after_idle(self.canvas.focus_set)

    def show_toolbar(self, visible):
        if self.toolbar_hide_job is not None:
            self.root.after_cancel(self.toolbar_hide_job)
            self.toolbar_hide_job = None
        if self.toolbar_visible != visible:
            self.toolbar_visible = visible
            self.canvas.itemconfigure(self.toolbar_item, state='normal' if visible else 'hidden')

    def schedule_toolbar_hide(self):
        if self.toolbar_visible and self.toolbar_hide_job is None:
            self.toolbar_hide_job = self.root.after(1000, self.hide_toolbar_after_delay)

    def hide_toolbar_after_delay(self):
        self.toolbar_hide_job = None
        self.show_toolbar(False)

    def toolbar_motion(self, event):
        if event.widget.winfo_toplevel() is not self.root:
            return
        y = event.y_root - self.root.winfo_rooty()
        x = event.x_root - self.root.winfo_rootx()
        visible = self.toolbar_visible
        if 0 <= x < self.root.winfo_width() and 0 <= y < (self.toolbar.winfo_reqheight() if visible else 10):
            self.show_toolbar(True)
        elif visible:
            self.schedule_toolbar_hide()

    def toolbar_leave(self, _event):
        x, y = self.root.winfo_pointerxy()
        if not (self.root.winfo_rootx() <= x < self.root.winfo_rootx() + self.root.winfo_width()
                and self.root.winfo_rooty() <= y < self.root.winfo_rooty() + self.root.winfo_height()):
            self.schedule_toolbar_hide()

    def refresh(self):
        try:
            from serial.tools import list_ports
            found = list(list_ports.comports())
            ports = [p.device for p in found]
            self.port_values = ports
            if self.ports is not None:
                self.ports['values'] = ports
            if ports and self.port.get() not in ports:
                last = load_settings().get('last_port')
                esp = next((p.device for p in found if p.vid == 0x303A), None)
                self.port.set(last if last in ports else esp or ports[0])
        except ImportError:
            self.status.set('pyserial 설치 필요: pip install -r desktop/requirements.txt')

    def auto_connect(self):
        """Reconnects to the previously used USB port when it is present at launch."""
        last = load_settings().get('last_port')
        if last and last in self.port_values and not self.link:
            self.port.set(last)
            self.toggle()

    def open_settings(self):
        if self.settings_window is not None and self.settings_window.winfo_exists():
            self.settings_window.lift()
            return
        window = self.settings_window = tk.Toplevel(self.root)
        window.title('설정')
        window.transient(self.root)
        frame = ttk.Frame(window, padding=16)
        frame.pack(fill='both', expand=True)
        row = ttk.Frame(frame)
        row.pack(fill='x', pady=(0, 12))
        ttk.Label(row, text='포트').pack(side='left')
        self.ports = ttk.Combobox(row, textvariable=self.port, values=self.port_values, width=24)
        self.ports.pack(side='left', padx=6)
        ttk.Button(row, text='새로고침', command=self.refresh).pack(side='left')
        self.connect_button = ttk.Button(row, text='연결 끊기' if self.link else '연결', command=self.toggle)
        self.connect_button.pack(side='left', padx=(6, 0))
        ttk.Button(row, text='Bluetooth 복구', command=self.recover_bluetooth).pack(side='left', padx=(6, 0))
        self.update_hires_status()
        ttk.Label(frame, textvariable=self.hires_status, wraplength=450).pack(anchor='w', pady=(0, 8))
        row = ttk.Frame(frame)
        row.pack(fill='x', pady=(0, 12))
        ttk.Label(row, text='영상 캡처').pack(side='left')
        self.capture_selector = ttk.Combobox(row, textvariable=self.capture_choice, state='readonly', width=35)
        self.capture_selector.pack(side='left', padx=6)
        self.capture_selector.bind('<<ComboboxSelected>>', self.select_capture)
        ttk.Button(row, text='새로고침', command=self.refresh_capture_devices).pack(side='left')
        ttk.Label(frame, textvariable=self.capture_status, wraplength=450).pack(anchor='w', pady=(0, 8))
        row = ttk.Frame(frame)
        row.pack(fill='x', pady=(0, 12))
        ttk.Label(row, text='캡처 해상도').pack(side='left')
        self.capture_resolution_selector = ttk.Combobox(row, textvariable=self.capture_resolution_choice,
                                                        values=['자동'] + [f'{w} × {h}' for w, h in self.capture_resolutions],
                                                        state='readonly' if self.capture_device else 'disabled', width=18)
        self.capture_resolution_selector.pack(side='left', padx=6)
        self.capture_resolution_selector.bind('<<ComboboxSelected>>', self.select_capture_resolution)
        ttk.Label(row, text='FPS').pack(side='left', padx=(8, 0))
        self.capture_fps_selector = ttk.Combobox(row, textvariable=self.capture_fps_choice,
                                                values=['자동'] + [f'{fps:g}' for fps in self.capture_fps_values],
                                                state='readonly' if self.capture_fps_values else 'disabled', width=8)
        self.capture_fps_selector.pack(side='left', padx=6)
        self.capture_fps_selector.bind('<<ComboboxSelected>>', self.select_capture_fps)
        ttk.Label(frame, text='미리보기는 최대 30 FPS이며 설정 창에서는 5 FPS로 표시됩니다.').pack(anchor='w')
        ttk.Label(frame, textvariable=self.capture_modes_status, wraplength=450).pack(anchor='w')
        row = ttk.Frame(frame)
        row.pack(fill='x', pady=(0, 12))
        ttk.Label(row, text='대상 해상도').pack(side='left')
        width_entry = ttk.Entry(row, width=7, textvariable=self.width)
        width_entry.pack(side='left', padx=(6, 2))
        ttk.Label(row, text='×').pack(side='left')
        height_entry = ttk.Entry(row, width=7, textvariable=self.height)
        height_entry.pack(side='left', padx=2)
        apply_button = ttk.Button(row, text='적용', command=self.apply_size)
        apply_button.pack(side='left', padx=6)
        self.resolution_controls = [width_entry, height_entry, apply_button]
        self.set_resolution_state()
        row = ttk.Frame(frame)
        row.pack(fill='x', pady=(0, 4))
        ttk.Label(row, text='자동 절전').pack(side='left')
        ttk.Entry(row, width=6, textvariable=self.idle_choice).pack(side='left', padx=(6, 2))
        ttk.Label(row, text='분').pack(side='left')
        ttk.Button(row, text='적용', command=self.apply_idle_timeout).pack(side='left', padx=6)
        ttk.Label(frame, text='제어 영역에서 입력이 없으면 Bluetooth를 끊고 ESP32를 절전 상태로 둡니다. 0은 사용 안 함.',
                  wraplength=450).pack(anchor='w')
        ttk.Checkbutton(frame, text='상대 마우스로 클릭 (iPad 권장)', variable=self.alternate_click).pack(anchor='w', pady=(12, 0))
        ttk.Checkbutton(frame, text='Caps Lock으로 iPad 입력 언어 전환', variable=self.caps_language).pack(anchor='w')
        ttk.Checkbutton(frame, text='항상 위에 표시', variable=self.always_on_top,
                        command=self.apply_always_on_top).pack(anchor='w')
        def close_settings():
            self.ports = None
            self.connect_button = None
            self.settings_window = None
            self.capture_selector = None
            self.capture_resolution_selector = None
            self.capture_fps_selector = None
            self.resolution_controls = []
            window.destroy()
        window.protocol('WM_DELETE_WINDOW', close_settings)
        self.refresh()
        self.refresh_capture_devices()

    def apply_idle_timeout(self):
        try:
            minutes = valid_idle_minutes(self.idle_choice.get())
        except ValueError:
            messagebox.showerror('자동 절전', '0부터 1440까지의 분을 입력하세요. 0은 사용 안 함입니다.')
            self.idle_choice.set(f'{self.idle_minutes:g}')
            return
        self.idle_minutes = minutes
        self.idle_choice.set(f'{minutes:g}')
        self.last_activity = time.monotonic()
        if not save_settings({'idle_minutes': minutes}):
            self.status.set('자동 절전 설정을 저장하지 못했습니다')

    def note_activity(self):
        """Input in the control area restarts the idle timer and wakes a sleeping ESP32."""
        self.last_activity = time.monotonic()
        if self.sleeping and self.link:
            self.sleeping = False
            self.sleep_changed_at = self.last_activity
            self.send('wake')
            self.status.set('절전 해제: 대상 Bluetooth 재연결 대기')

    def check_idle(self, now=None):
        now = time.monotonic() if now is None else now
        if (self.link and not self.sleeping and self.idle_minutes > 0
                and now - self.last_activity >= self.idle_minutes * 60):
            self.reset_input()
            self.sleeping = True
            self.sleep_changed_at = now
            self.send('sleep')
            self.status.set('절전 모드: 제어 영역에서 조작하면 다시 연결합니다')

    def set_resolution_state(self):
        for control in self.resolution_controls:
            control.configure(state='disabled' if self.capture_device else 'normal')

    def apply_always_on_top(self):
        self.root.attributes('-topmost', self.always_on_top.get())

    def refresh_capture_devices(self):
        if self.capture_selector is None:
            return
        try:
            devices = list_devices()
            self.capture_devices = {f'{item.name} ({item.source})': item for item in devices}
            names = ['사용 안 함'] + list(self.capture_devices)
            if self.capture_device and self.capture_choice.get() not in names:
                names.append(self.capture_choice.get())
            self.capture_selector.configure(values=names)
        except CaptureError as exc:
            self.capture_status.set(f'캡처 장치 조회 실패: {exc}')

    def select_capture(self, _event=None):
        choice = self.capture_choice.get()
        device = None if choice == '사용 안 함' else self.capture_devices.get(choice)
        if choice != '사용 안 함' and device is None:
            self.capture_status.set('선택한 캡처 장치를 찾을 수 없습니다. 목록을 새로고침하세요.')
            return
        self.reset_input()
        self.stop_capture()
        self.capture_device = device
        self.capture_photo = None
        self.capture_resolutions = []
        self.capture_source_size = None
        self.capture_fps = None
        self.capture_fps_values = []
        self.capture_fps_choice.set('자동')
        self.capture_resolution_choice.set('자동')
        self.capture_modes_generation += 1
        generation = self.capture_modes_generation
        self.capture_modes_loading = bool(device)
        if self.capture_resolution_selector:
            self.capture_resolution_selector.configure(values=['자동'], state='disabled')
        if self.capture_fps_selector:
            self.capture_fps_selector.configure(values=['자동'], state='disabled')
        self.set_resolution_state()
        if device:
            self.capture_status.set('영상 장치 설정 조회 중…')
            self.capture_modes_status.set('지원 해상도를 확인하고 있습니다.')
            def query_modes():
                try:
                    self.capture_mode_results.put((generation, list_resolutions(device), None))
                except CaptureError as exc:
                    self.capture_mode_results.put((generation, [], str(exc)))
            threading.Thread(target=query_modes, daemon=True).start()
        else:
            self.size = self.manual_size
            self.capture_status.set('영상 캡처 사용 안 함')
            self.capture_modes_status.set('')
        self.redraw()

    def select_capture_resolution(self, _event=None):
        choice = self.capture_resolution_choice.get()
        self.capture_source_size = next(((w, h) for w, h in self.capture_resolutions
                                         if choice == f'{w} × {h}'), None)
        self.update_capture_fps_options()
        if self.capture_device:
            self.capture_status.set('영상 연결 중…')
            self.start_capture()

    def update_capture_fps_options(self):
        self.capture_fps_values = list_frame_rates(self.capture_device, self.capture_source_size) if self.capture_device else []
        self.capture_fps = None
        self.capture_fps_choice.set('자동')
        if self.capture_fps_selector:
            self.capture_fps_selector.configure(values=['자동'] + [f'{fps:g}' for fps in self.capture_fps_values],
                                                state='readonly' if self.capture_fps_values else 'disabled')

    def select_capture_fps(self, _event=None):
        self.capture_fps = next((fps for fps in self.capture_fps_values
                                 if self.capture_fps_choice.get() == f'{fps:g}'), None)
        if self.capture_device:
            self.capture_status.set('영상 연결 중…')
            self.start_capture()

    def stop_capture(self):
        if self.capture_resize_job is not None:
            self.root.after_cancel(self.capture_resize_job)
            self.capture_resize_job = None
        if self.capture:
            self.capture.stop()
            self.capture = None

    def start_capture(self):
        self.capture_resize_job = None
        if not self.capture_device or self.capture_modes_loading:
            return
        if self.capture:
            self.capture.stop()
        self.capture_photo = None
        self.last_video_frame = None
        self.reset_input()
        self.capture = VideoCapture(self.capture_device, max(2, self.canvas.winfo_width()),
                                    max(2, self.canvas.winfo_height()), source_size=self.capture_source_size,
                                    fps=self.capture_fps)
        self.capture.start()
        self.redraw()

    def poll_video(self):
        try:
            while True:
                generation, modes, error = self.capture_mode_results.get_nowait()
                if generation != self.capture_modes_generation or not self.capture_device:
                    continue
                self.capture_modes_loading = False
                self.capture_resolutions = modes
                self.update_capture_fps_options()
                if self.capture_resolution_selector:
                    self.capture_resolution_selector.configure(values=['자동'] + [f'{w} × {h}' for w, h in modes], state='readonly')
                self.capture_modes_status.set('해상도 조회 실패: ' + error if error else ('지원 해상도를 선택할 수 있습니다.' if modes else '지원 해상도 목록을 가져올 수 없어 자동으로 연결합니다.'))
                self.capture_status.set('영상 연결 중…')
                self.start_capture()
        except queue.Empty:
            pass
        if self.capture and self.capture_resize_job is None:
            frame = self.capture.latest()
            if frame:
                width, height, data = frame
                now = time.monotonic()
                self.last_video_frame = now
                if self.capture_photo and now < self.next_video_draw_at:
                    self.root.after(8, self.poll_video)
                    return
                try:
                    new_geometry = self.capture_photo is None or self.size != (width, height)
                    self.size = (width, height)
                    if self.capture_photo is None:
                        self.capture_photo = tk.PhotoImage(data=data, format='PPM')
                    else:
                        self.capture_photo.configure(data=data, format='PPM')
                    self.next_video_draw_at = now + (0.025 if self.settings_window is not None else 0)
                    if new_geometry:
                        self.capture_status.set(f'영상 표시 중 · {width} × {height}')
                        self.redraw()
                except tk.TclError as exc:
                    self.capture_status.set(f'영상 표시 실패: {exc}')
            elif self.capture.error:
                self.capture_status.set(f'영상 캡처 실패: {self.capture.error}')
                self.reset_input()
                self.stop_capture()
                self.capture_photo = None
                self.redraw()
            elif self.capture_photo and self.last_video_frame is not None and time.monotonic() - self.last_video_frame > 1:
                self.capture_status.set('영상 신호 대기 중')
                self.reset_input()
                self.capture_photo = None
                self.redraw()
        # Poll faster than the camera frame interval so frames are shown as they arrive.
        self.root.after(8 if self.capture else 33, self.poll_video)

    def set_connect_label(self, text):
        if self.connect_button is not None:
            self.connect_button.configure(text=text)

    def set_peer(self, peer):
        self.peer = peer
        label = peer_label(peer)
        self.root.title(f'ESP32 Remote Input — {label}' if label else 'ESP32 Remote Input')

    def update_hires_status(self):
        if not self.link:
            text = 'USB 연결 후 확인'
        elif not self.ble:
            text = '대상 연결 후 확인'
        elif self.hires is None:
            text = '펌웨어가 지원하지 않음'
        else:
            wheel, pan = ('켜짐' if on else '꺼짐 (한 칸 단위)' for on in self.hires)
            text = f'세로 {wheel} · 가로 {pan}'
        self.hires_status.set('고해상도 휠: ' + text)

    def connected_status(self):
        label = peer_label(self.peer)
        return f'연결됨: {label}' if label else '대상 연결됨'

    def recover_bluetooth(self):
        if not self.link:
            messagebox.showerror('Bluetooth 복구', '먼저 ESP32의 USB 포트를 연결하세요.')
            return
        self.send('ble_recover')
        self.status.set('Bluetooth 연결 복구 요청됨')

    def target_command(self, command):
        # Toolbar actions should not leave keyboard control on the button.
        self.root.after_idle(self.canvas.focus_set)
        self.note_activity()
        if not (self.link and self.ble):
            if not self.sleeping:
                self.status.set('대상 Bluetooth 연결이 필요합니다')
            return
        self.reset_input()
        self.send(command)

    def system_action(self, usage, modifiers=5):
        self.target_command('shortcut %d %d' % (modifiers, usage))

    def expose(self):
        self.target_command('expose')

    def toggle(self):
        if self.link:
            self.reset_input()
            self.link.close()
            self.link = None
            self.ble = False
            self.hires = None
            self.set_peer(None)
            self.update_hires_status()
            self.set_connect_label('연결')
            self.status.set('USB 연결 전')
            return
        port = self.port.get().strip()
        if not port:
            messagebox.showerror('연결', 'USB 시리얼 포트를 선택하세요.')
            return
        self.link = SerialTransport(port, self.events)
        save_settings({'last_port': port})
        self.link_error = None
        self.sleeping = False
        self.last_activity = time.monotonic()
        self.set_connect_label('연결 끊기')
        self.status.set('USB 연결 중…')

    def apply_size(self):
        try:
            w, h = int(self.width.get()), int(self.height.get())
            if not (1 <= w <= 32768 and 1 <= h <= 32768):
                raise ValueError()
        except ValueError:
            messagebox.showerror('해상도', '가로와 세로에 1부터 32768까지의 정수를 입력하세요.')
            return
        self.reset_input()
        self.size = (w, h)
        self.manual_size = self.size
        self.redraw()

    def redraw(self, _event=None):
        self.canvas.delete('screen')
        if _event and self.capture_device and not self.capture_modes_loading:
            self.capture_photo = None
            self.reset_input()
            if self.capture_resize_job is not None:
                self.root.after_cancel(self.capture_resize_job)
            self.capture_resize_job = self.root.after(300, self.start_capture)
        if self.capture_device and not self.capture_photo:
            self.rect = (0, 0, 0, 0)
            self.canvas.create_text(self.canvas.winfo_width() / 2, self.canvas.winfo_height() / 2,
                                    text='영상 대기 중', fill='#cbd5e1', tags='screen')
            self.canvas.tag_lower('screen')
            self.canvas.itemconfigure(self.toolbar_item, width=self.canvas.winfo_width())
            return
        if self.capture_photo:
            width, height = self.size
            self.rect = ((self.canvas.winfo_width() - width) / 2,
                         (self.canvas.winfo_height() - height) / 2, width, height)
        else:
            self.rect = content_rect(self.canvas.winfo_width(), self.canvas.winfo_height(), *self.size)
        x, y, w, h = self.rect
        if self.capture_photo:
            self.canvas.create_image(x, y, image=self.capture_photo, anchor='nw', tags='screen')
        else:
            self.canvas.create_rectangle(x, y, x + w, y + h, fill='#343a40', outline='', tags='screen')
        self.canvas.tag_lower('screen')
        self.canvas.itemconfigure(self.toolbar_item, width=self.canvas.winfo_width())

    def is_inside(self, x, y):
        left, top, w, h = self.rect
        return w > 1 and h > 1 and left <= x < left + w and top <= y < top + h

    def has_focus(self):
        return self.root.focus_get() == self.canvas

    def active(self):
        return bool(self.link and self.ble and self.inside and self.has_focus())

    def controlling(self):
        """Like active(), but also true while Bluetooth is down so input can wake the ESP32."""
        return bool(self.link and self.inside and self.has_focus())

    def send(self, text):
        if self.link:
            self.link.send(text)

    def reset_input(self):
        if self.link:
            self.link.discard_pointer_motion()
        if self.link and (self.dragging or self.keys.held or self.language_key_down):
            self.send('release')
        self.dragging = False
        self.drag_button = None
        self.scroll_remainder = [0.0, 0.0]
        self.language_key_down = False
        self.home_key_down = False
        self.keys.clear()

    def enter(self, event):
        self.inside = self.is_inside(event.x, event.y)
        self.mac_hide.set_active(self.active())

    def leave(self, _event):
        self.inside = False
        self.mac_hide.set_active(False)
        self.reset_input()

    def motion(self, event):
        was_inside = self.inside
        self.inside = self.is_inside(event.x, event.y)
        if was_inside and not self.inside:
            self.mac_hide.set_active(False)
            self.reset_input()
            return
        if self.controlling():
            self.note_activity()
        if self.inside and not self.dragging and self.active():
            self.mac_hide.set_active(True)
            xy = normalized(event.x, event.y, self.rect)
            if xy is not None:
                self.send('goto %d %d' % xy)

    def focus_in(self, _event):
        # Mouse might have entered before the window gained focus.
        x = self.canvas.winfo_pointerx() - self.canvas.winfo_rootx()
        y = self.canvas.winfo_pointery() - self.canvas.winfo_rooty()
        widget = self.root.winfo_containing(self.canvas.winfo_pointerx(), self.canvas.winfo_pointery())
        self.inside = widget is self.canvas and self.is_inside(x, y)
        self.mac_hide.set_active(self.active())

    def focus_out(self, _event):
        self.mac_hide.set_active(False)
        self.reset_input()

    def mouse_down(self, event, button=1):
        self.inside = self.is_inside(event.x, event.y)
        focused = self.has_focus()
        self.canvas.focus_set()
        if self.link and self.inside:
            self.note_activity()
        if focused and self.active() and not self.dragging:
            xy = normalized(event.x, event.y, self.rect)
            if xy is not None:
                self.send('prepare %d %d' % xy)
                firmware_button = button if button > 2 else button + (2 if self.alternate_click.get() else 0)
                self.send('down %d %d %d' % (*xy, firmware_button))
                self.dragging = True
                self.drag_button = button
        return 'break'

    def mouse_drag(self, event):
        self.inside = self.is_inside(event.x, event.y)
        if not self.inside:
            self.reset_input()
        elif self.dragging and self.active():
            self.note_activity()
            xy = normalized(event.x, event.y, self.rect)
            if xy is not None:
                self.send('move %d %d' % xy)
        return 'break'

    def mouse_up(self, _event, button=1):
        if self.dragging and self.drag_button == button:
            self.send('up')
            self.dragging = False
            self.drag_button = None
        return 'break'

    def mouse_wheel(self, event, axis=0):
        delta = int(event.delta)
        if not delta:
            return 'break'
        # Wheel up scrolls up; with Shift (or a tilt wheel) it scrolls left.
        sign = 1 if axis == 0 else -1
        if self.wheel_detent:
            # Precision touchpads send fractions of a detent; keep them for high-resolution scrolling.
            self.accumulate_scroll(event, sign * delta, self.wheel_detent, axis)
        else:
            self.scroll(event, sign * (1 if delta > 0 else -1) * max(1, abs(delta)) * SCROLL_UNITS, axis)
        return 'break'

    def accumulate_scroll(self, event, delta, per_detent, axis):
        self.inside = self.is_inside(event.x, event.y)
        if not self.active():
            self.scroll_remainder[axis] = 0.0
            return
        self.scroll_remainder[axis] += delta * SCROLL_UNITS / per_detent
        units = int(self.scroll_remainder[axis])
        if units:
            self.scroll_remainder[axis] -= units
            self.scroll(event, units, axis)

    def touchpad_scroll(self, event):
        self.inside = self.is_inside(event.x, event.y)
        if self.controlling():
            self.note_activity()
        self.accumulate_scroll(event, -touchpad_vertical_delta(event.delta), TOUCHPAD_DETENT, 0)
        self.accumulate_scroll(event, touchpad_horizontal_delta(event.delta), TOUCHPAD_DETENT, 1)
        return 'break'

    def scroll(self, event, units, axis=0):
        """Scroll in 1/8-detent units: axis 0 positive is up, axis 1 positive is right."""
        self.inside = self.is_inside(event.x, event.y)
        if self.controlling():
            self.note_activity()
        if self.active():
            while units:
                chunk = max(-MAX_SCROLL_UNITS, min(MAX_SCROLL_UNITS, units))
                self.send('smooth %d %d %d' % (axis, 1 if chunk > 0 else 0, abs(chunk)))
                units -= chunk
        return 'break'

    def key_down(self, event):
        usage = usage_from_event(event, self.root.tk.call("info", "patchlevel"))
        if self.controlling():
            self.note_activity()
        if (sys.platform != 'darwin' and usage == 11 and self.active()
                and any(u in self.keys.held for u in (224, 228))
                and all(u in (224, 228) for u in self.keys.held)):
            if not self.home_key_down:
                self.home_key_down = True
                self.send('keys 0')
                self.send('shortcut 8 11')
                self.send(self.keys.command())
            return 'break'
        if usage == 57 and self.caps_language.get():
            if self.active() and not self.language_key_down:
                self.language_key_down = True
                temporary = KeyState()
                temporary.held = list(self.keys.held)
                temporary.change(224, True)
                temporary.change(44, True)
                self.send(temporary.command())
            return 'break'
        if self.active() and self.keys.change(usage, True):
            self.send(self.keys.command())
        return 'break'

    def key_up(self, event):
        usage = usage_from_event(event, self.root.tk.call("info", "patchlevel"))
        if usage == 11 and self.home_key_down:
            self.home_key_down = False
            return 'break'
        if usage == 57 and self.language_key_down:
            self.language_key_down = False
            self.send(self.keys.command())
            return 'break'
        if self.keys.change(usage, False):
            if self.link:
                self.send(self.keys.command())
        return 'break'

    def poll(self):
        try:
            while True:
                source, kind, value = self.events.get_nowait()
                if source is not self.link:
                    continue
                if kind == 'ble':
                    if self.ble and not value:
                        self.reset_input()
                    if value and not self.ble:
                        # The firmware also announces the name when it finishes reading it.
                        self.send('peer')
                    elif not value:
                        self.set_peer(None)
                    if not self.sleeping:
                        self.status.set(self.connected_status() if value else '대상 Bluetooth 연결 대기')
                    self.ble = value
                    self.update_hires_status()
                elif kind == 'hires':
                    if value != self.hires:
                        self.hires = value
                        self.update_hires_status()
                elif kind == 'peer':
                    self.set_peer(value)
                    if self.ble and not self.sleeping:
                        self.status.set(self.connected_status())
                elif kind == 'sleep':
                    if value != self.sleeping and time.monotonic() - self.sleep_changed_at >= SLEEP_REPORT_GRACE:
                        self.sleeping = value
                        if value:
                            self.reset_input()
                        self.status.set('절전 모드: 제어 영역에서 조작하면 다시 연결합니다' if value
                                        else '대상 Bluetooth 연결 대기')
                elif kind == 'input_timeout':
                    self.dragging = False
                    self.keys.clear()
                    self.status.set('입력 시간 초과: 다시 조작하세요')
                elif kind == 'error':
                    self.link_error = value
                    self.status.set('오류: ' + value)
                elif kind == 'serial' and value == 'disconnected' and self.link:
                    self.link = None
                    self.ble = False
                    self.hires = None
                    self.set_peer(None)
                    self.update_hires_status()
                    self.dragging = False
                    self.keys.clear()
                    self.set_connect_label('연결')
                    self.status.set('오류: ' + self.link_error if self.link_error else 'USB 연결 끊김')
        except queue.Empty:
            pass
        self.check_idle()
        self.root.after(50, self.poll)

    def close(self):
        self.mac_hide.set_active(False)
        self.stop_capture()
        self.reset_input()
        if self.link:
            self.link.close()
            self.link.thread.join(timeout=1.5)
        self.root.destroy()

if __name__ == '__main__':
    App(tk.Tk()).root.mainloop()
