"""Explicit local desktop control. Frames and text are transient and model-free.

No endpoint accepts executable code. Input requires a short-lived manual grant,
a current frame and a target selected by clicking that frame. A global emergency
hotkey revokes the grant even when the browser has lost keyboard focus.
"""
import ctypes
import os
import secrets
import struct
import threading
import time
import zlib
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from .auth import is_human
from .local_console import allows_local_console


class ControlError(Exception):
    pass


class ControlBody(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)


class GrantIn(ControlBody):
    allow_input: bool = False


class SessionIn(ControlBody):
    session_id: str = Field(min_length=12, max_length=128)


class ClickIn(SessionIn):
    frame_id: str = Field(min_length=8, max_length=128)
    x: int
    y: int
    button: str = Field(pattern=r'^(left|right)$', default='left')


class TextIn(SessionIn):
    text: str = Field(min_length=1, max_length=2000)


class KeyIn(SessionIn):
    key: str = Field(min_length=1, max_length=30)


KEYS = {'enter': (0x0D,), 'tab': (0x09,), 'escape': (0x1B,), 'backspace': (0x08,), 'delete': (0x2E,),
        'up': (0x26,), 'down': (0x28,), 'left': (0x25,), 'right': (0x27,), 'home': (0x24,), 'end': (0x23,),
        'ctrl+a': (0x11, 0x41), 'ctrl+c': (0x11, 0x43), 'ctrl+v': (0x11, 0x56), 'ctrl+z': (0x11, 0x5A)}


def png_rgb(width, height, bgra):
    """Encode native top-down BGRA without adding an imaging dependency."""
    if len(bgra) != width * height * 4:
        raise ControlError('SCREEN_FRAME_INVALID')
    rgb = bytearray(width * height * 3)
    rgb[0::3], rgb[1::3], rgb[2::3] = bgra[2::4], bgra[1::4], bgra[0::4]
    compressor = zlib.compressobj(2)
    parts = []
    stride = width * 3
    for row in range(height):
        parts.append(compressor.compress(b'\0' + rgb[row * stride:(row + 1) * stride]))
    parts.append(compressor.flush())

    def chunk(kind, data):
        return struct.pack('!I', len(data)) + kind + data + struct.pack('!I', zlib.crc32(kind + data) & 0xFFFFFFFF)
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!2I5B', width, height, 8, 2, 0, 0, 0)) + chunk(b'IDAT', b''.join(parts)) + chunk(b'IEND', b'')


class WindowsDesktop:
    def __init__(self):
        if os.name != 'nt':
            raise ControlError('COMPUTER_CONTROL_WINDOWS_ONLY')
        from ctypes import wintypes as w
        self.w = w
        self.u = ctypes.WinDLL('user32', use_last_error=True)
        self.g = ctypes.WinDLL('gdi32', use_last_error=True)
        self.k = ctypes.WinDLL('kernel32', use_last_error=True)
        u, g = self.u, self.g
        u.GetDC.argtypes = [w.HWND]; u.GetDC.restype = w.HDC
        u.ReleaseDC.argtypes = [w.HWND, w.HDC]
        u.GetSystemMetrics.argtypes = [ctypes.c_int]; u.GetSystemMetrics.restype = ctypes.c_int
        u.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]; u.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        g.CreateCompatibleDC.argtypes = [w.HDC]; g.CreateCompatibleDC.restype = w.HDC
        g.CreateCompatibleBitmap.argtypes = [w.HDC, ctypes.c_int, ctypes.c_int]; g.CreateCompatibleBitmap.restype = w.HBITMAP
        g.SelectObject.argtypes = [w.HDC, w.HANDLE]; g.SelectObject.restype = w.HANDLE
        g.DeleteObject.argtypes = [w.HANDLE]; g.DeleteDC.argtypes = [w.HDC]
        g.BitBlt.argtypes = [w.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.HDC, ctypes.c_int, ctypes.c_int, w.DWORD]
        g.BitBlt.restype = w.BOOL
        g.GetDIBits.argtypes = [w.HDC, w.HBITMAP, w.UINT, w.UINT, ctypes.c_void_p, ctypes.c_void_p, w.UINT]
        g.GetDIBits.restype = ctypes.c_int
        u.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]; u.SetCursorPos.restype = w.BOOL
        u.GetAncestor.argtypes = [w.HWND, w.UINT]; u.GetAncestor.restype = w.HWND
        u.WindowFromPoint.argtypes = [w.POINT]; u.WindowFromPoint.restype = w.HWND
        u.GetForegroundWindow.restype = w.HWND
        u.SetForegroundWindow.argtypes = [w.HWND]; u.SetForegroundWindow.restype = w.BOOL
        u.IsWindow.argtypes = [w.HWND]; u.IsWindow.restype = w.BOOL
        u.RegisterHotKey.argtypes = [w.HWND, ctypes.c_int, w.UINT, w.UINT]; u.RegisterHotKey.restype = w.BOOL
        u.UnregisterHotKey.argtypes = [w.HWND, ctypes.c_int]
        u.GetMessageW.argtypes = [ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT]
        u.GetMessageW.restype = ctypes.c_int
        u.PeekMessageW.argtypes = [ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT, w.UINT]
        u.PostThreadMessageW.argtypes = [w.DWORD, w.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
        self.k.GetCurrentThreadId.restype = w.DWORD

        class Mouse(ctypes.Structure):
            _fields_ = [('dx', w.LONG), ('dy', w.LONG), ('data', w.DWORD), ('flags', w.DWORD), ('time', w.DWORD), ('extra', ctypes.c_size_t)]
        class Keyboard(ctypes.Structure):
            _fields_ = [('vk', w.WORD), ('scan', w.WORD), ('flags', w.DWORD), ('time', w.DWORD), ('extra', ctypes.c_size_t)]
        class Hardware(ctypes.Structure):
            _fields_ = [('message', w.DWORD), ('low', w.WORD), ('high', w.WORD)]
        class Union(ctypes.Union):
            _fields_ = [('mouse', Mouse), ('key', Keyboard), ('hardware', Hardware)]
        class Input(ctypes.Structure):
            _anonymous_ = ('value',)
            _fields_ = [('type', w.DWORD), ('value', Union)]
        self.Input, self.Keyboard, self.Mouse = Input, Keyboard, Mouse
        u.SendInput.argtypes = [w.UINT, ctypes.POINTER(Input), ctypes.c_int]; u.SendInput.restype = w.UINT
        self.hotkey_thread = None
        self.hotkey_tid = 0
        self.target = None

    def geometry(self):
        previous = self.u.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        try:
            x, y, width, height = [self.u.GetSystemMetrics(i) for i in (76, 77, 78, 79)]
            if width < 1 or height < 1 or width * height > 32_000_000:
                raise ControlError('SCREEN_DIMENSIONS_UNSUPPORTED')
            return dict(left=x, top=y, width=width, height=height)
        finally:
            if previous:
                self.u.SetThreadDpiAwarenessContext(previous)

    def capture(self):
        previous = self.u.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        source = memory = bitmap = old = None
        try:
            geometry = self.geometry()
            width, height = geometry['width'], geometry['height']
            source = self.u.GetDC(None)
            memory = self.g.CreateCompatibleDC(source)
            bitmap = self.g.CreateCompatibleBitmap(source, width, height)
            if not source or not memory or not bitmap:
                raise ControlError('SCREEN_CAPTURE_UNAVAILABLE')
            old = self.g.SelectObject(memory, bitmap)
            if not self.g.BitBlt(memory, 0, 0, width, height, source, geometry['left'], geometry['top'], 0x40CC0020):
                raise ControlError('SCREEN_CAPTURE_UNAVAILABLE')
            self.g.SelectObject(memory, old); old = None
            # BITMAPINFOHEADER: 40 bytes, negative height requests top-down rows.
            header = struct.pack('<IiiHHIIiiII', 40, width, -height, 1, 32, 0, width * height * 4, 0, 0, 0, 0)
            info = ctypes.create_string_buffer(header + b'\0' * 16)
            pixels = ctypes.create_string_buffer(width * height * 4)
            if self.g.GetDIBits(memory, bitmap, 0, height, pixels, info, 0) != height:
                raise ControlError('SCREEN_CAPTURE_UNAVAILABLE')
            return png_rgb(width, height, pixels.raw), geometry
        finally:
            if memory and old:
                self.g.SelectObject(memory, old)
            if bitmap: self.g.DeleteObject(bitmap)
            if memory: self.g.DeleteDC(memory)
            if source: self.u.ReleaseDC(None, source)
            if previous: self.u.SetThreadDpiAwarenessContext(previous)

    def start_emergency_stop(self, callback):
        ready = threading.Event()
        registered = []
        def run():
            self.hotkey_tid = self.k.GetCurrentThreadId()
            message = self.w.MSG()
            self.u.PeekMessageW(ctypes.byref(message), None, 0, 0, 0)
            ok = bool(self.u.RegisterHotKey(None, 0x5645, 0x4003, 0x1B))
            registered.append(ok); ready.set()
            if not ok:
                return
            try:
                while self.u.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                    if message.message == 0x0312 and message.wParam == 0x5645:
                        callback()
                        break
            finally:
                self.u.UnregisterHotKey(None, 0x5645)
        self.hotkey_thread = threading.Thread(target=run, name='verilo-computer-stop', daemon=True)
        self.hotkey_thread.start()
        if not ready.wait(2) or not registered or not registered[0]:
            self.close()
            raise ControlError('EMERGENCY_HOTKEY_UNAVAILABLE')

    def _send(self, records):
        native = (self.Input * len(records))(*records)
        if self.u.SendInput(len(records), native, ctypes.sizeof(self.Input)) != len(records):
            raise ControlError('WINDOWS_INPUT_REJECTED')

    def click(self, x, y, button):
        previous = self.u.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        try:
            target = self.u.WindowFromPoint(self.w.POINT(x, y))
            target = self.u.GetAncestor(target, 2) if target else None
            if not target or not self.u.SetCursorPos(x, y):
                raise ControlError('TARGET_WINDOW_UNAVAILABLE')
            down, up = (0x0002, 0x0004) if button == 'left' else (0x0008, 0x0010)
            self._send([self.Input(type=0, value=self.Input._fields_[1][1](mouse=self.Mouse(0, 0, 0, f, 0, 0))) for f in (down, up)])
            self.target = target
        finally:
            if previous: self.u.SetThreadDpiAwarenessContext(previous)

    def _focus_target(self):
        if not self.target or not self.u.IsWindow(self.target):
            raise ControlError('CLICK_TARGET_FIRST')
        if self.u.GetForegroundWindow() != self.target:
            self.u.SetForegroundWindow(self.target)
        if self.u.GetForegroundWindow() != self.target:
            raise ControlError('TARGET_FOCUS_REJECTED')

    def type_text(self, text):
        self._focus_target()
        units = struct.unpack('<' + 'H' * (len(text.encode('utf-16-le')) // 2), text.encode('utf-16-le'))
        records = []
        for unit in units:
            for flags in (0x0004, 0x0006):
                record = self.Input(type=1); record.key = self.Keyboard(0, unit, flags, 0, 0); records.append(record)
        self._send(records)

    def press(self, key):
        self._focus_target()
        records = []
        for vk in KEYS[key]:
            record = self.Input(type=1); record.key = self.Keyboard(vk, 0, 0, 0, 0); records.append(record)
        for vk in reversed(KEYS[key]):
            record = self.Input(type=1); record.key = self.Keyboard(vk, 0, 0x0002, 0, 0); records.append(record)
        self._send(records)

    def close(self):
        if self.hotkey_tid:
            self.u.PostThreadMessageW(self.hotkey_tid, 0x0012, 0, 0)
        if self.hotkey_thread and threading.current_thread() is not self.hotkey_thread:
            self.hotkey_thread.join(timeout=2)
        self.target = None


class ComputerControl:
    def __init__(self, driver_factory=WindowsDesktop, clock=time.monotonic):
        self.driver_factory, self.clock = driver_factory, clock
        self.lock = threading.RLock()
        self.cancelled = threading.Event()
        self.session = self.driver = None
        self.last_frame = None

    @staticmethod
    def owner(identity):
        return identity.tenant_id, identity.subject_id

    def _clear(self):
        self.cancelled.set()
        driver = self.driver
        self.session = self.driver = self.last_frame = None
        if driver:
            driver.close()

    def emergency_stop(self):
        self.cancelled.set()
        with self.lock:
            self._clear()

    def _get(self, identity, session_id=None, *, input_required=False):
        if not self.session or self.cancelled.is_set() or self.clock() >= self.session['expires']:
            self._clear()
            raise ControlError('COMPUTER_CONTROL_DISABLED')
        if self.session['owner'] != self.owner(identity) or (session_id is not None and self.session['id'] != session_id):
            raise ControlError('COMPUTER_SESSION_NOT_FOUND')
        if input_required and not self.session['allow_input']:
            raise ControlError('COMPUTER_PREVIEW_ONLY')
        return self.session

    def enable(self, identity, allow_input):
        with self.lock:
            if self.session and self.clock() < self.session['expires']:
                if self.session['owner'] != self.owner(identity):
                    raise ControlError('COMPUTER_ALREADY_IN_USE')
            self._clear()
            driver = self.driver_factory()
            self.cancelled.clear()
            try:
                driver.start_emergency_stop(self.emergency_stop)
                self.driver = driver
                self.session = dict(id=secrets.token_urlsafe(24), owner=self.owner(identity), allow_input=allow_input, expires=self.clock() + 300)
                return self.status(identity)
            except Exception:
                driver.close(); self._clear()
                raise

    def status(self, identity):
        with self.lock:
            enabled = bool(self.session and self.session['owner'] == self.owner(identity) and not self.cancelled.is_set() and self.clock() < self.session['expires'])
            if self.session and self.clock() >= self.session['expires']:
                self._clear()
            return dict(available=os.name == 'nt' or self.driver_factory is not WindowsDesktop, enabled=enabled,
                        session_id=self.session['id'] if enabled else None,
                        allow_input=self.session['allow_input'] if enabled else False,
                        expires_in=max(0, int(self.session['expires'] - self.clock())) if enabled else 0,
                        emergency_stop='Ctrl+Alt+Esc', scope='local_manual_only', screenshots_persisted=False, model_access=False)

    def disable(self, identity):
        with self.lock:
            if self.session and self.session['owner'] != self.owner(identity):
                raise ControlError('COMPUTER_SESSION_NOT_FOUND')
            self._clear()
            return self.status(identity)

    def frame(self, identity, session_id):
        with self.lock:
            self._get(identity, session_id)
            image, geometry = self.driver.capture()
            self._get(identity, session_id)
            frame_id = secrets.token_urlsafe(16)
            self.last_frame = dict(id=frame_id, geometry=geometry, time=self.clock())
            return image, dict(frame_id=frame_id, **geometry)

    def click(self, identity, body):
        with self.lock:
            self._get(identity, body.session_id, input_required=True)
            frame = self.last_frame
            if not frame or frame['id'] != body.frame_id or self.clock() - frame['time'] > 30 or self.driver.geometry() != frame['geometry']:
                raise ControlError('REFRESH_SCREEN_FIRST')
            g = frame['geometry']
            if not g['left'] <= body.x < g['left'] + g['width'] or not g['top'] <= body.y < g['top'] + g['height']:
                raise ControlError('POINT_OUTSIDE_SCREEN')
            self.driver.click(body.x, body.y, body.button)

    def text(self, identity, body):
        with self.lock:
            self._get(identity, body.session_id, input_required=True)
            if any(ord(c) < 32 and c not in '\t\n\r' for c in body.text):
                raise ControlError('TEXT_CONTAINS_CONTROL_CODES')
            self.driver.type_text(body.text)

    def key(self, identity, body):
        with self.lock:
            self._get(identity, body.session_id, input_required=True)
            if body.key not in KEYS:
                raise ControlError('KEY_NOT_SUPPORTED')
            self.driver.press(body.key)


def install_computer_api(app, store, *, Reader, Writer, local_console=False, driver_factory=WindowsDesktop, clock=time.monotonic):
    control = ComputerControl(driver_factory, clock)
    app.state.computer_control = control

    def guard(request, identity):
        if not local_console or not is_human(identity) or not allows_local_console(request) or urlsplit(str(request.url)).hostname not in {'127.0.0.1', '::1'}:
            raise HTTPException(403, 'COMPUTER_CONTROL_LOCAL_HUMAN_ONLY')

    def translate(error):
        code = str(error)
        status = 404 if code == 'COMPUTER_SESSION_NOT_FOUND' else 409
        raise HTTPException(status, code)

    @app.get('/v1/computer/status')
    def status(request: Request, identity: Reader):
        guard(request, identity)
        return control.status(identity)

    @app.post('/v1/computer/enable')
    def enable(body: GrantIn, request: Request, identity: Writer):
        guard(request, identity)
        try:
            return control.enable(identity, body.allow_input)
        except ControlError as error:
            translate(error)

    @app.post('/v1/computer/disable')
    def disable(request: Request, identity: Writer):
        guard(request, identity)
        try:
            return control.disable(identity)
        except ControlError as error:
            translate(error)

    @app.get('/v1/computer/frame')
    def frame(request: Request, identity: Reader):
        guard(request, identity)
        session_id = request.headers.get('x-verilo-computer-session', '')
        try:
            image, info = control.frame(identity, session_id)
        except ControlError as error:
            translate(error)
        return Response(image, media_type='image/png', headers={'Cache-Control': 'no-store', 'X-Frame-Id': info['frame_id'],
            **{'X-Desktop-' + key: str(info[key]) for key in ('left', 'top', 'width', 'height')}})

    @app.post('/v1/computer/click')
    def click(body: ClickIn, request: Request, identity: Writer):
        guard(request, identity)
        try: control.click(identity, body)
        except ControlError as error: translate(error)
        return dict(sent=True)

    @app.post('/v1/computer/text')
    def text(body: TextIn, request: Request, identity: Writer):
        guard(request, identity)
        try: control.text(identity, body)
        except ControlError as error: translate(error)
        return dict(sent=True)

    @app.post('/v1/computer/key')
    def key(body: KeyIn, request: Request, identity: Writer):
        guard(request, identity)
        try: control.key(identity, body)
        except ControlError as error: translate(error)
        return dict(sent=True)

    previous = app.router.lifespan_context
    from contextlib import asynccontextmanager
    @asynccontextmanager
    async def lifespan(instance):
        try:
            async with previous(instance):
                yield
        finally:
            control.emergency_stop()
    app.router.lifespan_context = lifespan
    return control
