import unittest
import collections
import threading
from unittest.mock import patch
from app import App, content_rect, normalized, touchpad_vertical_delta
from keymap import KeyState, usage_from_event, usage_from_keycode
from types import SimpleNamespace
from transport import SerialTransport
from video_capture import CaptureDevice, list_frame_rates, list_resolutions

class CoreTests(unittest.TestCase):
    def test_capture_fps_choices_exclude_avfoundation_range_minimum(self):
        device = CaptureDevice('test', 'avfoundation', 'test:none')
        result = SimpleNamespace(stderr=b'  640x480@[15.000000 30.000000]fps\n')
        with patch('video_capture._ffmpeg', return_value='ffmpeg'), patch('video_capture.subprocess.run', return_value=result):
            self.assertEqual(list_resolutions(device), [(640, 480)])
        self.assertEqual(list_frame_rates(device, (640, 480)), [30.0])

    def test_windows_control_h_sends_ipad_home(self):
        sent = []
        state = KeyState()
        state.change(224, True)
        app = SimpleNamespace(root=SimpleNamespace(tk=SimpleNamespace(call=lambda *args: '8.6')),
                              keys=state, home_key_down=False, active=lambda: True, send=sent.append)
        with patch('app.sys.platform', 'win32'), patch('app.usage_from_event', return_value=11):
            App.key_down(app, object())
            App.key_down(app, object())
            App.key_up(app, object())
        self.assertEqual(sent, ['keys 0', 'shortcut 8 11', state.command()])
        self.assertFalse(app.home_key_down)

    def test_toolbar_shortcut_does_not_require_pointer_inside(self):
        sent = []
        app = SimpleNamespace(
            root=SimpleNamespace(after_idle=lambda callback: None),
            canvas=SimpleNamespace(focus_set=lambda: None),
            link=object(), ble=True, inside=False,
            reset_input=lambda: sent.append('reset'), send=sent.append,
        )
        App.system_action(app, 43, 8)
        self.assertEqual(sent, ['reset', 'shortcut 8 43'])

    def test_letterbox_and_coordinate_mapping(self):
        rect = content_rect(1000, 1000, 1600, 900)
        self.assertEqual(rect, (0, 218.75, 1000, 562.5))
        self.assertEqual(normalized(0, 218.75, rect), (0, 0))
        self.assertEqual(normalized(999, 780.25, rect), (10000, 10000))
        self.assertIsNone(normalized(0, 0, (0, 0, 1, 1)))

    def test_physical_keys_and_combination(self):
        self.assertEqual(usage_from_keycode(0, 'darwin'), 4)
        self.assertEqual(usage_from_keycode(38, 'linux'), 4)
        self.assertEqual(usage_from_keycode(65, 'win32', scan_code=30), 4)
        state = KeyState()
        state.change(225, True)
        state.change(4, True)
        state.change(5, True)
        self.assertEqual(state.command(), 'keys 2 4 5 0 0 0 0')
        state.change(4, False)
        self.assertEqual(state.command(), 'keys 2 5 0 0 0 0 0')
        state.clear()
        self.assertEqual(state.command(), 'keys 0 0 0 0 0 0 0')

    def test_rollover(self):
        state = KeyState()
        for key in range(4, 11):
            state.change(key, True)
        self.assertEqual(state.command(), 'keys 0 1 1 1 1 1 1')

    def test_mac_tk_version_keycode_encoding(self):
        event = SimpleNamespace(keysym='s', keycode=1 << 24)
        self.assertEqual(usage_from_event(event, '9.1.0', 'darwin'), 22)
        event.keycode = 1 << 16
        self.assertEqual(usage_from_event(event, '8.5.9', 'darwin'), 22)

    def test_pointer_motion_coalesces_and_is_discarded_on_exit(self):
        transport = SerialTransport.__new__(SerialTransport)
        transport.lock = threading.Lock()
        transport.outgoing = collections.deque()
        for command in ('goto 1 1', 'goto 2 2', 'down 2 2', 'move 3 3', 'move 4 4'):
            transport.send(command)
        self.assertEqual(list(transport.outgoing), ['goto 2 2', 'down 2 2', 'move 4 4'])
        transport.discard_pointer_motion()
        self.assertEqual(list(transport.outgoing), ['down 2 2'])

    def test_touchpad_vertical_delta_is_signed_and_ignores_horizontal(self):
        self.assertEqual(touchpad_vertical_delta((42 << 16) | 7), 7)
        self.assertEqual(touchpad_vertical_delta((42 << 16) | 0xfff9), -7)
        self.assertEqual(touchpad_vertical_delta(42 << 16), 0)

if __name__ == '__main__':
    unittest.main()
