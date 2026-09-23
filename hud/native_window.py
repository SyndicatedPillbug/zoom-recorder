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
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from Foundation import NSMakeRect, NSObject


def _load_frameworks() -> dict[str, Any]:
    if sys.platform != "darwin":
        raise RuntimeError("native HUD windows are only available on macOS")
    import objc

    from AppKit import NSApplication, NSBackingStoreBuffered, NSPanel, NSWindow
    from Foundation import NSObject, NSURL, NSURLRequest

    objc.loadBundle("WebKit", globals(), "/System/Library/Frameworks/WebKit.framework")
    return {
        "NSApplication": NSApplication,
        "NSBackingStoreBuffered": NSBackingStoreBuffered,
        "NSWindow": NSWindow,
        "NSPanel": NSPanel,
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

    def windowShouldBecomeKey_(self, window: Any) -> bool:  # noqa: N802
        # The Glass HUD is non-activating so clicks outside it return to the
        # meeting app, but its WebKit text field still needs keyboard focus.
        return True


class _GlassRegionBridge(NSObject):
    """Make only declared HUD controls receive mouse events.

    WebKit renders the whole overlay as one native view. The page reports its
    interactive rectangles here; the bridge converts them to screen space and
    toggles the panel's native mouse transparency as the pointer moves. This
    preserves true click-through over blank glass while keeping the question
    field and arrange handles clickable.
    """

    def initWithWindow_(self, window: Any) -> Any:  # noqa: N802
        import objc
        self = objc.super(_GlassRegionBridge, self).init()
        if self is not None:
            self.window = window
            self.webview = None
            self.regions = []
            self._ignores = False
        return self

    def setWebView_(self, webview: Any) -> None:  # noqa: N802
        self.webview = webview

    def userContentController_didReceiveScriptMessage_(  # noqa: N802
            self, _controller: Any, message: Any) -> None:
        body = message.body()
        try:
            values = body.get("regions", []) if hasattr(body, "get") else []
            self.regions = [
                (float(item.get("x", 0)), float(item.get("y", 0)),
                 float(item.get("width", 0)), float(item.get("height", 0)))
                for item in values if hasattr(item, "get")
            ]
        except (TypeError, ValueError):
            self.regions = []
        self.sync_mouse_transparency()

    def sync_mouse_transparency(self) -> None:
        if self.window is None or self.webview is None or not self.regions:
            return
        try:
            content = self.window.contentView()
            screen_rects = []
            for x, y, width, height in self.regions:
                local = NSMakeRect(x, y, width, height)
                in_content = self.webview.convertRect_toView_(local, content)
                screen_rects.append(self.window.convertRectToScreen_(in_content))
            point = __import__("AppKit").NSEvent.mouseLocation()
            interactive = any(
                rect.origin.x <= point.x <= rect.origin.x + rect.size.width
                and rect.origin.y <= point.y <= rect.origin.y + rect.size.height
                for rect in screen_rects)
            ignores = not interactive
            if ignores != self._ignores:
                self.window.setIgnoresMouseEvents_(ignores)
                self._ignores = ignores
        except Exception:  # noqa: BLE001 - click-through is non-critical
            self.window.setIgnoresMouseEvents_(False)
            self._ignores = False

    def poll_(self, _timer: Any) -> None:  # noqa: N802
        self.sync_mouse_transparency()


def _with_surface_query(url: str, mode: str, opacity: float, compact: bool) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update({"surface": mode, "opacity": str(opacity), "compact": "1" if compact else "0"})
    return urlunsplit((parts.scheme, parts.netloc, parts.path,
                       urlencode(query), parts.fragment))


def run(url: str, title: str = "Meeting HUD", mode: str = "window",
        opacity: float = 0.90, compact: bool = False) -> None:
    fw = _load_frameworks()
    from AppKit import (
        NSColor,
        NSFloatingWindowLevel,
        NSWindowCollectionBehaviorCanJoinAllApplications,
        NSWindowCollectionBehaviorCanJoinAllSpaces,
        NSWindowCollectionBehaviorFullScreenAuxiliary,
        NSWindowSharingNone,
        NSWindowStyleMaskBorderless,
        NSWindowStyleMaskNonactivatingPanel,
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

    glass = mode == "glass"
    if glass:
        style = NSWindowStyleMaskBorderless | NSWindowStyleMaskResizable | NSWindowStyleMaskNonactivatingPanel
        window = fw["NSPanel"].alloc().initWithContentRect_styleMask_backing_defer_(
            rect, style, fw["NSBackingStoreBuffered"], False)
        window.setFloatingPanel_(True)
        # This panel contains an editable question field. Apple specifically
        # recommends false when a panel has text fields; true can leave the
        # WebKit input visible but unable to become first responder.
        window.setBecomesKeyOnlyIfNeeded_(False)
        # The page supplies its own drag handles for the independent HUD
        # modules. Letting AppKit move the whole borderless panel from any
        # background click prevents reliable click-through and text entry.
        window.setMovableByWindowBackground_(False)
        window.setHasShadow_(True)
        window.setHidesOnDeactivate_(False)
        window.setOpaque_(False)
        window.setBackgroundColor_(NSColor.clearColor())
        window.setAlphaValue_(min(1.0, max(0.60, float(opacity))))
        window.setFrameAutosaveName_("zoom-recorder-glass-hud")
        if not window.setFrameUsingName_("zoom-recorder-glass-hud"):
            window.center()
    else:
        style = NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskResizable
        window = fw["NSWindow"].alloc().initWithContentRect_styleMask_backing_defer_(
            rect, style, fw["NSBackingStoreBuffered"], False)
        window.setTitle_(title)
        window.setFrameAutosaveName_("zoom-recorder-window-hud")
        if not window.setFrameUsingName_("zoom-recorder-window-hud"):
            window.center()
    window.setReleasedWhenClosed_(False)
    window.setLevel_(NSFloatingWindowLevel)
    # FullScreenAuxiliary keeps the panel in the call's full-screen Space.
    # Glass also opts into all-app visibility; both behaviors are intentional.
    collection = (NSWindowCollectionBehaviorCanJoinAllSpaces
                  | NSWindowCollectionBehaviorFullScreenAuxiliary)
    if glass:
        collection |= NSWindowCollectionBehaviorCanJoinAllApplications
    window.setCollectionBehavior_(collection)
    # This is a compatibility feature, not a security promise. Apple has
    # deprecated the old sharing enum for some modern capture paths.
    try:
        window.setSharingType_(NSWindowSharingNone)
    except Exception:  # noqa: BLE001 - old SDK/runtime variation
        pass

    config = fw["WKWebViewConfiguration"].alloc().init()
    bridge = None
    if glass:
        bridge = _GlassRegionBridge.alloc().initWithWindow_(window)
        config.userContentController().addScriptMessageHandler_name_(bridge, "hudRegions")
    webview = fw["WKWebView"].alloc().initWithFrame_configuration_(
        window.contentView().bounds(), config)
    webview.setAutoresizingMask_(18)  # width + height sizable
    if glass:
        try:
            webview.setValue_forKey_(False, "drawsBackground")
        except Exception:  # noqa: BLE001 - WebKit runtime variation
            pass
    request = fw["NSURLRequest"].requestWithURL_(
        fw["NSURL"].URLWithString_(_with_surface_query(url, mode, opacity, compact)))
    webview.loadRequest_(request)
    window.setContentView_(webview)
    if bridge is not None:
        bridge.setWebView_(webview)
        # Keep polling even while another app owns the mouse. The native
        # window becomes transparent over non-interactive page regions and
        # re-enables events when the pointer reaches a control.
        from AppKit import NSTimer
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.08, bridge, "poll:", None, True)
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
    parser.add_argument("--mode", choices=("window", "glass"), default="window")
    parser.add_argument("--opacity", type=float, default=0.90)
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args(argv)
    try:
        run(args.url, args.title, args.mode, args.opacity, args.compact)
    except Exception as exc:  # noqa: BLE001 - caller falls back to browser
        print("native HUD unavailable: {}".format(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
