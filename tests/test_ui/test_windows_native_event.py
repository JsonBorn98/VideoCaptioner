import ctypes
import sys

import pytest

from videocaptioner.ui.common import windows_native_event

_qt_app = None


class FakeWindowsError(Exception):
    def __init__(self, winerror: int, funcname: str):
        super().__init__(winerror, funcname)
        self.winerror = winerror
        self.funcname = funcname


def _make_window(handler):
    class FakeWindow:
        nativeEvent = handler

    return FakeWindow


def test_guard_ignores_get_cursor_pos_access_denied():
    def native_event(self, event_type, message):
        raise FakeWindowsError(5, "GetCursorPos")

    window_class = _make_window(native_event)

    assert windows_native_event._patch_native_event(window_class, FakeWindowsError, 5)
    assert window_class().nativeEvent("windows_generic_MSG", object()) == (False, 0)


@pytest.mark.parametrize(
    ("error", "expected_type"),
    [
        (FakeWindowsError(6, "GetCursorPos"), FakeWindowsError),
        (FakeWindowsError(5, "ScreenToClient"), FakeWindowsError),
        (FakeWindowsError(5, "GetClientRect"), FakeWindowsError),
        (RuntimeError("unexpected"), RuntimeError),
    ],
)
def test_guard_reraises_unrelated_errors(error, expected_type):
    def native_event(self, event_type, message):
        raise error

    window_class = _make_window(native_event)
    windows_native_event._patch_native_event(window_class, FakeWindowsError, 5)

    with pytest.raises(expected_type):
        window_class().nativeEvent("windows_generic_MSG", object())


def test_guard_preserves_normal_result_and_is_idempotent():
    expected = (True, 123)

    def native_event(self, event_type, message):
        return expected

    window_class = _make_window(native_event)

    assert windows_native_event._patch_native_event(window_class, FakeWindowsError, 5)
    assert not windows_native_event._patch_native_event(window_class, FakeWindowsError, 5)
    assert window_class().nativeEvent("windows_generic_MSG", object()) == expected


def test_install_is_noop_outside_windows(monkeypatch):
    monkeypatch.setattr(windows_native_event.sys, "platform", "linux")

    assert not windows_native_event.install_windows_frameless_native_event_guard()


def test_install_degrades_gracefully_when_dependencies_change(monkeypatch):
    error = ImportError("missing optional Windows integration")
    reported = []

    monkeypatch.setattr(windows_native_event.sys, "platform", "win32")
    monkeypatch.setattr(
        windows_native_event,
        "_load_windows_dependencies",
        lambda: (_ for _ in ()).throw(error),
    )

    assert not windows_native_event.install_windows_frameless_native_event_guard(reported.append)
    assert reported == [error]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only dependency integration")
def test_guard_handles_real_pywintypes_error():
    import pywintypes
    import winerror

    def native_event(self, event_type, message):
        raise pywintypes.error(winerror.ERROR_ACCESS_DENIED, "GetCursorPos", "Access denied")

    window_class = _make_window(native_event)
    windows_native_event._patch_native_event(
        window_class,
        pywintypes.error,
        winerror.ERROR_ACCESS_DENIED,
    )

    assert window_class().nativeEvent("windows_generic_MSG", object()) == (False, 0)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only dependency integration")
def test_install_patches_windows_frameless_base_class(monkeypatch):
    from qframelesswindow.windows import WindowsFramelessWindow

    original = WindowsFramelessWindow.nativeEvent
    monkeypatch.setattr(WindowsFramelessWindow, "nativeEvent", original)

    assert windows_native_event.install_windows_frameless_native_event_guard()
    assert not windows_native_event.install_windows_frameless_native_event_guard()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only MRO integration")
def test_real_qfluentwidgets_chain_reaches_guard(monkeypatch):
    global _qt_app

    from ctypes.wintypes import MSG

    import pywintypes
    import winerror
    from PyQt5.QtWidgets import QApplication
    from qfluentwidgets.components.widgets.frameless_window import FramelessWindow
    from qframelesswindow.windows import WindowsFramelessWindow

    _qt_app = QApplication.instance() or QApplication([])

    def native_event(self, event_type, message):
        raise pywintypes.error(winerror.ERROR_ACCESS_DENIED, "GetCursorPos", "Access denied")

    monkeypatch.setattr(WindowsFramelessWindow, "nativeEvent", native_event)
    assert windows_native_event.install_windows_frameless_native_event_guard()

    native_message = MSG()

    class Message:
        def __int__(self):
            return ctypes.addressof(native_message)

    window = FramelessWindow.__new__(FramelessWindow)

    assert WindowsFramelessWindow in FramelessWindow.__mro__
    assert FramelessWindow.nativeEvent(window, "windows_generic_MSG", Message()) == (False, 0)
    assert _qt_app is not None
