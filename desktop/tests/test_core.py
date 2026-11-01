import unittest
import collections
import os
import tempfile
import threading
from unittest.mock import patch
from app import (App, MEDIA_ACTIONS, wheel_detent, content_rect, load_settings, normalized, save_settings,
                 touchpad_horizontal_delta, touchpad_vertical_delta)
from keymap import KeyState, usage_from_event, usage_from_keycode
from types import SimpleNamespace
from transport import LinkLog, SerialTransport, parse_hires, parse_peer, peer_label
from video_capture import CaptureDevice, list_frame_rates, list_resolutions

class CoreTests(unittest.TestCase):
    def test_bluetooth_recovery_requires_usb_and_sends_command(self):
        sent = []
        app = SimpleNamespace(link=None, send=sent.append,
                              status=SimpleNamespace(set=lambda value: None))
        with patch('app.messagebox.showerror') as error:
            App.recover_bluetooth(app)
        error.assert_called_once()
        self.assertEqual(sent, [])
        app.link = object()
        App.recover_bluetooth(app)
        self.assertEqual(sent, ['ble_recover'])

    def test_peer_line_parsing_and_label(self):
        peer = parse_peer('PEER name="재현의 iPad" manufacturer="Apple Inc." model="iPad13,4"')
        self.assertEqual(peer, {'name': '재현의 iPad', 'manufacturer': 'Apple Inc.', 'model': 'iPad13,4'})
        self.assertEqual(peer_label(peer), '재현의 iPad (iPad13,4)')
        self.assertEqual(peer_label(parse_peer('PEER name="LAPTOP-1" manufacturer="" model=""')), 'LAPTOP-1')
        self.assertEqual(peer_label(parse_peer('PEER name="" manufacturer="Apple Inc." model=""')), 'Apple Inc.')
        self.assertIsNone(parse_peer('PEER unknown'))
        self.assertIsNone(parse_peer('PEER name="" manufacturer="" model=""'))

    def test_hires_status_parsing_and_text(self):
        self.assertEqual(parse_hires('CONNECTED sleeping=0 wheel_hires=1 pan_hires=0'), (True, False))
        self.assertIsNone(parse_hires('CONNECTED sleeping=0 mouse_subscribers=1'))
        texts = []
        app = SimpleNamespace(link=None, ble=False, hires=None, hires_status=SimpleNamespace(set=texts.append))
        App.update_hires_status(app)
        app.link = object()
        App.update_hires_status(app)
        app.ble = True
        App.update_hires_status(app)
        app.hires = (True, False)
        App.update_hires_status(app)
        self.assertEqual(texts, ['고해상도 휠: USB 연결 후 확인', '고해상도 휠: 대상 연결 후 확인',
                                 '고해상도 휠: 펌웨어가 지원하지 않음',
                                 '고해상도 휠: 세로 켜짐 · 가로 꺼짐 (한 칸 단위)'])

    def test_ble_connect_requests_peer_and_shows_name(self):
        import queue
        sent, statuses, titles = [], [], []
        app = SimpleNamespace(events=queue.Queue(), link=object(), ble=False, peer=None, sleeping=False,
                              send=sent.append, reset_input=lambda: None, check_idle=lambda: None, poll=None,
                              status=SimpleNamespace(set=statuses.append),
                              root=SimpleNamespace(title=titles.append, after=lambda *a: None))
        app.set_peer = lambda peer: App.set_peer(app, peer)
        app.update_hires_status = lambda: None
        app.connected_status = lambda: App.connected_status(app)
        app.events.put((app.link, 'ble', True))
        app.events.put((app.link, 'ble', True))
        app.events.put((app.link, 'peer', {'name': 'LAPTOP-1', 'manufacturer': '', 'model': ''}))
        App.poll(app)
        self.assertEqual(sent, ['peer'])
        self.assertEqual(statuses[-1], '연결됨: LAPTOP-1')
        self.assertEqual(titles[-1], 'ESP32 Remote Input — LAPTOP-1')
        app.events.put((app.link, 'ble', False))
        App.poll(app)
        self.assertIsNone(app.peer)
        self.assertEqual(titles[-1], 'ESP32 Remote Input')

    def scroll_app(self, sent):
        app = SimpleNamespace(scroll_remainder=[0.0, 0.0], send=sent.append, note_activity=lambda: None,
                              is_inside=lambda x, y: True, controlling=lambda: True, active=lambda: True)
        app.scroll = lambda event, units, axis=0: App.scroll(app, event, units, axis)
        app.accumulate_scroll = lambda *args: App.accumulate_scroll(app, *args)
        return app

    def test_scroll_sends_eighth_detent_units_and_splits_large_amounts(self):
        sent = []
        app = self.scroll_app(sent)
        event = SimpleNamespace(x=0, y=0)
        App.scroll(app, event, 8)
        App.scroll(app, event, -3, 1)
        App.scroll(app, event, 1100)
        self.assertEqual(sent, ['smooth 0 1 8', 'smooth 1 0 3', 'smooth 0 1 1016', 'smooth 0 1 84'])

    def test_detent_wheel_keeps_fractional_deltas_and_shift_scrolls_left(self):
        sent = []
        app = self.scroll_app(sent)
        app.wheel_detent = 120
        App.mouse_wheel(app, SimpleNamespace(x=0, y=0, delta=120))
        App.mouse_wheel(app, SimpleNamespace(x=0, y=0, delta=10))
        App.mouse_wheel(app, SimpleNamespace(x=0, y=0, delta=10))
        App.mouse_wheel(app, SimpleNamespace(x=0, y=0, delta=120), 1)
        self.assertEqual(sent, ['smooth 0 1 8', 'smooth 0 1 1', 'smooth 1 0 8'])

    def test_wheel_detent_by_platform_and_tk_version(self):
        self.assertEqual(wheel_detent('win32', '8.6.13'), 120)
        self.assertEqual(wheel_detent('linux', '9.0.1'), 120)
        self.assertIsNone(wheel_detent('darwin', '8.6.12'))
        sent = []
        app = self.scroll_app(sent)
        app.wheel_detent = None
        App.mouse_wheel(app, SimpleNamespace(x=0, y=0, delta=-2))
        self.assertEqual(sent, ['smooth 0 0 16'])

    def test_touchpad_scroll_uses_both_axes(self):
        sent = []
        app = self.scroll_app(sent)
        App.touchpad_scroll(app, SimpleNamespace(x=0, y=0, delta=(30 << 16) | (0x10000 - 15)))
        self.assertEqual(sent, ['smooth 0 1 4', 'smooth 1 1 8'])

    def test_extra_mouse_buttons_ignore_alternate_click(self):
        sent = []
        app = SimpleNamespace(inside=False, dragging=False, rect=(0, 0, 100, 100), link=object(),
                              send=sent.append, note_activity=lambda: None, has_focus=lambda: True,
                              active=lambda: True, is_inside=lambda x, y: True,
                              canvas=SimpleNamespace(focus_set=lambda: None),
                              alternate_click=SimpleNamespace(get=lambda: True))
        event = SimpleNamespace(x=50, y=50)
        App.mouse_down(app, event, 6)
        self.assertEqual(sent[-1], 'down 5051 5051 6')
        app.dragging = False
        App.mouse_down(app, event, 2)
        self.assertEqual(sent[-1], 'down 5051 5051 4')

    def test_media_menu_actions_are_valid_firmware_commands(self):
        for action in filter(None, MEDIA_ACTIONS):
            _label, command, usage = action
            if command == 'media':
                self.assertTrue(1 <= usage <= 0x3ff)
            else:
                self.assertEqual(command, 'system')
                self.assertTrue(0x81 <= usage <= 0x83)

    def idle_app(self, sent):
        app = SimpleNamespace(link=object(), sleeping=False, idle_minutes=1, last_activity=0.0,
                              sleep_changed_at=0.0, send=sent.append,
                              reset_input=lambda: sent.append('reset'),
                              status=SimpleNamespace(set=lambda value: None))
        app.note_activity = lambda: App.note_activity(app)
        return app

    def test_idle_timeout_sleeps_once_and_activity_wakes(self):
        sent = []
        app = self.idle_app(sent)
        App.check_idle(app, now=59)
        self.assertEqual(sent, [])
        App.check_idle(app, now=60)
        App.check_idle(app, now=61)
        self.assertEqual(sent, ['reset', 'sleep'])
        self.assertTrue(app.sleeping)
        App.note_activity(app)
        App.note_activity(app)
        self.assertEqual(sent, ['reset', 'sleep', 'wake'])
        self.assertFalse(app.sleeping)

    def test_idle_timeout_zero_disables_sleep(self):
        sent = []
        app = self.idle_app(sent)
        app.idle_minutes = 0
        App.check_idle(app, now=10_000)
        self.assertEqual(sent, [])

    def test_toolbar_action_wakes_sleeping_esp32(self):
        sent = []
        app = self.idle_app(sent)
        app.sleeping, app.ble = True, False
        app.root = SimpleNamespace(after_idle=lambda callback: None)
        app.canvas = SimpleNamespace(focus_set=lambda: None)
        app.target_command = lambda command: App.target_command(app, command)
        App.system_action(app, 11, 8)
        self.assertEqual(sent, ['wake'])

    def test_settings_round_trip_and_merge(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, 'nested', 'settings.json')
            self.assertEqual(load_settings(path), {})
            self.assertTrue(save_settings({'other': 1}, path))
            self.assertTrue(save_settings({'idle_minutes': 2.5}, path))
            self.assertEqual(load_settings(path), {'other': 1, 'idle_minutes': 2.5})
            with open(path, 'w', encoding='utf-8') as file:
                file.write('not json')
            self.assertEqual(load_settings(path), {})

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
                              keys=state, home_key_down=False, active=lambda: True, send=sent.append,
                              controlling=lambda: False)
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
            link=object(), ble=True, inside=False, note_activity=lambda: None,
            reset_input=lambda: sent.append('reset'), send=sent.append,
        )
        app.target_command = lambda command: App.target_command(app, command)
        App.system_action(app, 43, 8)
        self.assertEqual(sent, ['reset', 'shortcut 8 43'])
        App.target_command(app, 'media 233')
        self.assertEqual(sent[-2:], ['reset', 'media 233'])

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

    def test_link_log_keeps_events_and_status_changes_only(self):
        with tempfile.TemporaryDirectory() as folder:
            log = LinkLog(os.path.join(folder, 'link.log'))
            for line in ('CONNECTED a', 'CONNECTED a', 'OK', 'BLE disconnect reason=0x213',
                         'DISCONNECTED a', 'DISCONNECTED a', 'waiting for download'):
                log.line(line)
            with open(log.path, encoding='utf-8') as file:
                logged = [entry.split(' ', 2)[2] for entry in file.read().splitlines()]
        self.assertEqual(logged, ['CONNECTED a', 'BLE disconnect reason=0x213',
                                  'DISCONNECTED a', 'waiting for download'])

    def test_touchpad_vertical_delta_is_signed_and_ignores_horizontal(self):
        self.assertEqual(touchpad_vertical_delta((42 << 16) | 7), 7)
        self.assertEqual(touchpad_vertical_delta((42 << 16) | 0xfff9), -7)
        self.assertEqual(touchpad_vertical_delta(42 << 16), 0)
        self.assertEqual(touchpad_horizontal_delta((42 << 16) | 0xfff9), 42)
        self.assertEqual(touchpad_horizontal_delta((0xffd6 << 16) | 7), -42)

if __name__ == '__main__':
    unittest.main()
