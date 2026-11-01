"""Temporarily remove the macOS application Hide menu shortcut."""
import ctypes
import ctypes.util
import sys


class MacHideShortcut:
    def __init__(self):
        self.item = None
        self.original = None
        if sys.platform != 'darwin':
            return
        objc = ctypes.CDLL(ctypes.util.find_library('objc'))
        self.objc = objc
        objc.objc_getClass.argtypes = [ctypes.c_char_p]
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]
        objc.sel_registerName.restype = ctypes.c_void_p
        objc.sel_getName.argtypes = [ctypes.c_void_p]
        objc.sel_getName.restype = ctypes.c_char_p
        self.send = objc.objc_msgSend
        self.send.restype = ctypes.c_void_p
        self.send.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

    def call(self, obj, selector):
        return self.send(obj, self.objc.sel_registerName(selector.encode()))

    def find_hide(self, menu):
        count = self.call(menu, 'numberOfItems') or 0
        item_at = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long)(('objc_msgSend', self.objc))
        selector = self.objc.sel_registerName(b'itemAtIndex:')
        for index in range(count):
            item = item_at(menu, selector, index)
            action = self.call(item, 'action')
            if action and self.objc.sel_getName(action) == b'hide:':
                return item
            submenu = self.call(item, 'submenu')
            if submenu:
                found = self.find_hide(submenu)
                if found:
                    return found
        return None

    def set_active(self, active):
        if sys.platform != 'darwin':
            return
        if active and not self.item:
            app = self.call(self.objc.objc_getClass(b'NSApplication'), 'sharedApplication')
            menu = self.call(app, 'mainMenu')
            if not menu:
                return
            item = self.find_hide(menu)
            if not item:
                return
            original = self.call(item, 'keyEquivalent')
            self.original = self.call(original, 'copy')
            self.item = self.call(item, 'retain')
            self._set_key(self.call(self.objc.objc_getClass(b'NSString'), 'string'))
        elif not active and self.item:
            self._set_key(self.original)
            self.call(self.original, 'release')
            self.call(self.item, 'release')
            self.item = self.original = None

    def _set_key(self, value):
        send = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(('objc_msgSend', self.objc))
        send(self.item, self.objc.sel_registerName(b'setKeyEquivalent:'), value)
