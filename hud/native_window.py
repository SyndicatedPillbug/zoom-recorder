#!/usr/bin/env python3
"""Native macOS host for the live HUD.

The HUD content remains the existing local web UI. This small process gives it
an AppKit window instead of asking the user to manage a browser tab. Keeping
the host in its own process makes GUI failures non-fatal to recording and lets
the recorder close the surface cleanly when a session ends.

The capture-sharing setting is intentionally described as best effort. It is
honoured by a number of macOS capture paths, but modern capture frameworks and
third-party meeting applications may choose different paths.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
from typing import Any, Optional

from Foundation import NSObject


def _load_frameworks() -> dict[str, Any]:
    if sys.platform != "darwin":
        raise RuntimeError("native HUD windows are only available on macOS")
    import objc

    from AppKit import NSApplication, NSBackingStoreBuffered, NSWindow
    from Foundation import NSObject, NSURL, NSURLRequest

    objc.loadBundle("WebKit", globals(), "/System/Library/Frameworks/WebKit.framework")
    return {
        "NSApplication": NSApplication,
        "NSBackingStoreBuffered": NSBackingStoreBuffered,
        "NSWindow": NSWindow,
        "NSURL": NSURL,
        "NSURLRequest": NSURLRequest,
        "NSObject": NSObject,
        "WKWebView": globals()["WKWebView"],
        "WKWebViewConfiguration": globals()["WKWebViewConfiguration"],
    }


def native_window_available() -> bool:
    """Return whether the Python environment can load AppKit and WebKit."""
    try:
        _load_frameworks()
        return True
    except Exception:  # noqa: BLE001 - this is a capability probe
        return False


def capture_protection_label() -> str:
    """Human-readable status used by diagnostics and the HUD metadata."""
    return "best-effort macOS window capture exclusion"


class _WindowDelegate(NSObject):
    def windowShouldClose_(self, window: Any) -> bool:  # noqa: N802
        # Closing the HUD should hide it, not accidentally stop the recording.
        window.orderOut_(None)
        return False


def run(url: str, title: str = "Meeting HUD") -> None:
    fw = _load_frameworks()
    from AppKit import (
        NSFloatingWindowLevel,
        NSWindowCollectionBehaviorCanJoinAllSpaces,
        NSWindowCollectionBehaviorFullScreenAuxiliary,
        NSWindowSharingNone,
        NSWindowStyleMaskClosable,
        NSWindowStyleMaskResizable,
        NSWindowStyleMaskTitled,
        NSScreen,
    )
    from Foundation import NSMakeRect

    app = fw["NSApplication"].sharedApplication()
    app.setActivationPolicy_(1)  # accessory: no extra Dock icon

    screen = NSScreen.mainScreen()
    visible = screen.visibleFrame() if screen is not None else None
    if visible is None:
        rect = NSMakeRect(80, 80, 1180, 760)
    else:
        width = min(1180.0, max(860.0, visible.size.width * 0.62))
        height = min(760.0, max(560.0, visible.size.height * 0.68))
        rect = NSMakeRect(
            visible.origin.x + (visible.size.width - width) / 2.0,
            visible.origin.y + (visible.size.height - height) / 2.0,
            width,
            height,
        )

    style = NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskResizable
    window = fw["NSWindow"].alloc().initWithContentRect_styleMask_backing_defer_(
        rect, style, fw["NSBackingStoreBuffered"], False)
    window.setTitle_(title)
    window.setReleasedWhenClosed_(False)
    window.setLevel_(NSFloatingWindowLevel)
    window.setCollectionBehavior_(
        NSWindowCollectionBehaviorCanJoinAllSpaces
        | NSWindowCollectionBehaviorFullScreenAuxiliary
    )
    # This is a compatibility feature, not a security promise. Apple has
    # deprecated the old sharing enum for some modern capture paths.
    try:
        window.setSharingType_(NSWindowSharingNone)
    except Exception:  # noqa: BLE001 - old SDK/runtime variation
        pass

    config = fw["WKWebViewConfiguration"].alloc().init()
    webview = fw["WKWebView"].alloc().initWithFrame_configuration_(
        window.contentView().bounds(), config)
    webview.setAutoresizingMask_(18)  # width + height sizable
    request = fw["NSURLRequest"].requestWithURL_(fw["NSURL"].URLWithString_(url))
    webview.loadRequest_(request)
    window.setContentView_(webview)
    window.setDelegate_(_WindowDelegate.alloc().init())
    window.makeKeyAndOrderFront_(None)
    app.activateIgnoringOtherApps_(True)

    def stop(_signum: int, _frame: Optional[Any]) -> None:
        # A dedicated host has no durable state. Exit directly so a parent
        # stopping a recording never waits on AppKit delegate negotiation.
        os._exit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    app.run()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="native zoom-recorder HUD host")
    parser.add_argument("--url", required=True)
    parser.add_argument("--title", default="Meeting HUD")
    args = parser.parse_args(argv)
    try:
        run(args.url, args.title)
    except Exception as exc:  # noqa: BLE001 - caller falls back to browser
        print("native HUD unavailable: {}".format(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
