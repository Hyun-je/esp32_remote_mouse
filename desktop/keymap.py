"""Convert native physical key positions to USB HID keyboard usages."""
import sys

# X11 commonly exposes Linux evdev codes plus 8. Values are USB usage IDs.
_EVDEV = {
  1:41, 2:30,3:31,4:32,5:33,6:34,7:35,8:36,9:37,10:38,11:39,12:45,13:46,14:42,15:43,
  16:20,17:26,18:8,19:21,20:23,21:28,22:24,23:12,24:18,25:19,26:47,27:48,28:40,29:224,
  30:4,31:22,32:7,33:9,34:10,35:11,36:13,37:14,38:15,39:51,40:52,41:53,42:225,43:49,
  44:29,45:27,46:6,47:25,48:5,49:17,50:16,51:54,52:55,53:56,54:229,55:85,56:226,57:44,
  58:57,59:58,60:59,61:60,62:61,63:62,64:63,65:64,66:65,67:66,68:67,69:83,70:71,
  71:95,72:96,73:97,74:86,75:92,76:93,77:94,78:87,79:89,80:90,81:91,82:98,83:99,
  86:100,87:68,88:69,96:88,97:228,98:84,100:230,102:74,103:82,104:75,105:80,
  106:79,107:77,108:81,109:78,110:73,111:76,125:227,126:231,
}
_MAC = {
  0:4,1:22,2:7,3:9,4:11,5:10,6:29,7:27,8:6,9:25,11:5,12:20,13:26,14:8,
  15:21,16:28,17:23,18:30,19:31,20:32,21:33,22:35,23:34,24:46,25:38,26:36,
  27:45,28:37,29:39,30:48,31:18,32:24,33:47,34:12,35:19,36:40,37:15,38:13,
  39:52,40:14,41:51,42:49,43:54,44:56,45:17,46:16,47:55,48:43,49:44,50:53,
  51:42,53:41,54:231,55:227,56:225,57:57,58:226,59:224,60:229,61:230,62:228,
  65:99,67:85,69:87,71:83,75:84,76:88,78:86,81:103,82:98,83:89,84:90,85:91,
  86:92,87:93,88:94,89:95,91:96,92:97,96:62,97:63,98:64,99:60,100:65,101:66,
  103:68,109:67,111:69,114:73,115:74,116:75,117:76,118:61,119:77,120:59,
  121:78,122:58,123:80,124:79,125:81,126:82,
}
# Windows translates a virtual key to a set-1 scan code. Extended keys use a prefix.
_WIN_EXT = {0x25:80,0x26:82,0x27:79,0x28:81,0x21:75,0x22:78,0x23:77,0x24:74,
            0x2d:73,0x2e:76,0x6f:84,0x5b:227,0x5c:231,0xa3:228,0xa5:230}
_WIN_SCAN_TO_EVDEV = {i:i for i in range(1,89)}

def usage_from_keycode(keycode, platform=None, scan_code=None):
    platform = platform or sys.platform
    if platform == 'darwin':
        return _MAC.get(keycode)
    if platform.startswith('linux'):
        return _EVDEV.get(keycode - 8)
    if platform == 'win32':
        if keycode in _WIN_EXT:
            return _WIN_EXT[keycode]
        if scan_code is None:
            import ctypes
            scan_code = ctypes.windll.user32.MapVirtualKeyW(keycode, 0)
        return _EVDEV.get(_WIN_SCAN_TO_EVDEV.get(scan_code & 0xff, -1))
    return None


class KeyState:
    """Track held physical usages and produce a six-key boot-compatible report."""
    def __init__(self):
        self.held = []

    def change(self, usage, down):
        if usage is None:
            return False
        if down and usage not in self.held:
            self.held.append(usage)
        elif not down and usage in self.held:
            self.held.remove(usage)
        else:
            return False
        return True

    def clear(self):
        self.held.clear()

    def command(self):
        modifiers = sum(1 << (u - 224) for u in self.held if 224 <= u <= 231)
        keys = [u for u in self.held if u < 224]
        # HID rollover signals too many keys rather than silently releasing one.
        keys = ([1] * 6) if len(keys) > 6 else (keys + [0] * 6)[:6]
        return 'keys ' + ' '.join(map(str, [modifiers] + keys))

_MODIFIERS = {
    'Shift_L': 225, 'Shift_R': 229, 'Control_L': 224, 'Control_R': 228,
    'Alt_L': 226, 'Alt_R': 230, 'Option_L': 226, 'Option_R': 230,
    'Meta_L': 227, 'Meta_R': 231, 'Super_L': 227, 'Super_R': 231,
    'Command': 227, 'Command_L': 227, 'Command_R': 231,
}
# Tk Aqua may turn ordinary typing into text-input events lacking a native key.
# These US-layout positions are a best effort, and cannot preserve other layouts.
_ASCII = {chr(ord('a') + i): 4 + i for i in range(26)}
_ASCII.update({str((i + 1) % 10): 30 + i for i in range(10)})
_ASCII.update({' ':44, '-':45, '=':46, '[':47, ']':48, '\\':49,
               ';':51, "'":52, '`':53, ',':54, '.':55, '/':56})

def usage_from_event(event, tk_version='8.6', platform=None):
    platform = platform or sys.platform
    symbol = str(event.keysym)
    if symbol in _MODIFIERS:
        return _MODIFIERS[symbol]
    if platform == 'darwin':
        code = int(event.keycode)
        try:
            version = tuple(map(int, str(tk_version).split('.')[:2]))
        except (ValueError, IndexError):
            version = (8, 6)
        virtual = (code >> (16 if version < (8, 6) else 24)) & 0xff
        if virtual:
            return _MAC.get(virtual)
        # A native A has virtual keycode zero; for generated text events the
        # original key is unavailable, so fall back to a US-position guess.
        return _ASCII.get(symbol.lower())
    return usage_from_keycode(event.keycode, platform)
