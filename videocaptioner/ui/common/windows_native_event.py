"""Windows-specific guards for third-party frameless-window native events."""

import sys
from functools import wraps
from typing import Any, Callable

_GUARD_MARKER = "__videocaptioner_cursor_access_guard__"
_TRANSIENT_CURSOR_FUNCTION = "GetCursorPos"


def _guard_native_event(
    native_event: Callable[..., Any],
    windows_error_type: type[Exception],
    access_denied_code: int,
) -> Callable[..., Any]:
    """Return a native-event handler tolerant of transient cursor access denial."""

    @wraps(native_event)
    def guarded(self, event_type, message):
        try:
            return native_event(self, event_type, message)
        except windows_error_type as exc:
            if (
                getattr(exc, "winerror", None) == access_denied_code
                and getattr(exc, "funcname", None) == _TRANSIENT_CURSOR_FUNCTION
            ):
                # Windows can temporarily deny cursor access while switching to
                # the secure desktop. Let Qt/Windows handle this hit test normally.
                return False, 0
            raise

    setattr(guarded, _GUARD_MARKER, True)
    return guarded


def _patch_native_event(
    window_class: type,
    windows_error_type: type[Exception],
    access_denied_code: int,
) -> bool:
    """Patch a frameless-window base class once; return whether it was changed."""

    native_event = window_class.nativeEvent
    if getattr(native_event, _GUARD_MARKER, False):
        return False

    window_class.nativeEvent = _guard_native_event(
        native_event,
        windows_error_type,
        access_denied_code,
    )
    return True


def _load_windows_dependencies() -> tuple[type, type[Exception], int]:
    import pywintypes
    import winerror
    from qframelesswindow.windows import WindowsFramelessWindow

    return WindowsFramelessWindow, pywintypes.error, winerror.ERROR_ACCESS_DENIED


def install_windows_frameless_native_event_guard(
    on_error: Callable[[Exception], None] | None = None,
) -> bool:
    """Guard every WindowsFramelessWindow subclass against cursor access denial.

    This guard only suppresses cosmetic native-event noise. If a future dependency
    changes or is unavailable, report the problem when requested and let the GUI
    continue starting without the patch.
    """

    if sys.platform != "win32":
        return False

    try:
        window_class, windows_error_type, access_denied_code = _load_windows_dependencies()
        return _patch_native_event(
            window_class,
            windows_error_type,
            access_denied_code,
        )
    except (ImportError, AttributeError) as exc:
        if on_error is not None:
            on_error(exc)
        return False
