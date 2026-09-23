# Mac HUD overlay research and implementation notes

Updated: 2026-09-22

## Definition used here

For this project, a HUD is a non-modal visual layer that keeps information in
the user's primary field of view while the underlying application remains
visible and usable. The defining properties are not merely “a window” or
“always on top”; they are:

- transparent or translucent presentation over the live application;
- glanceable, high-contrast information that remains readable over changing
  backgrounds;
- low-interruption interaction, with movable/resizable placement and compact
  controls;
- persistence across Spaces and, where macOS allows it, alongside a full-screen
  application;
- a clear distinction between the presenter's private view and the audience's
  shared view.

Apple's AppKit documentation historically used `NSHUDWindowMask` for a
transparent panel and describes the HUD as a transparent panel. Modern AppKit
uses `NSPanel`, window levels, transparency, and collection behavior instead.
`NSWindowCollectionBehaviorCanJoinAllSpaces` places a window in all Spaces and
`NSWindowCollectionBehaviorFullScreenAuxiliary` allows it to appear alongside a
full-screen window where the host application permits it.

## Prior art reviewed

The following open-source projects confirm that this is an established native
Mac interaction pattern rather than something that needs to be invented here:

- [Stealth](https://github.com/vortechron/stealth) — a native meeting copilot
  using a floating `NSPanel`, two-sided transcript, draggable/resizable overlay,
  menu-bar operation, and hotkeys.
- [SubtitleOverlay](https://github.com/SweelLong/SubtitleOverlay) — a native
  speech/subtitle overlay using SwiftUI hosted in an AppKit `NSPanel`, with
  low-latency display and a separate overlay window controller.
- [CameraCue](https://github.com/TheoPsycheMedia/CameraCue) — useful “quiet
  glass” design ideas: near-camera and floating presets, persistent settings,
  font/background controls, lockable position, and a preflight readability/
  privacy check.
- [Teleprompter](https://github.com/pedrambayat/teleprompter) — persistent
  geometry, smooth time-delta scrolling, hover controls, font/opacity settings,
  and a dedicated `NSWindow` overlay controller.
- [Steadi](https://github.com/Siddharth-Khattar/Steadi) — a two-window model
  with a normal control surface and a separate transparent overlay, which is a
  good fit for keeping the recorder's established browser UI while adding a
  specialized presenter surface.

We are using these projects as architectural references only. The current
implementation keeps this repository's local HTTP/SSE data flow and does not
copy their source code.

## Decision for zoom-recorder

Keep two selectable native modes:

### Window mode

The existing decorated AppKit/WebKit window remains available as the stable
default. It is easier to inspect, interact with, and troubleshoot.

### Glass HUD mode

The new mode uses a borderless, translucent `NSPanel` with:

- a transparent WebKit surface;
- translucent, blurred content islands rather than an opaque page background;
- strong text shadow, contrast borders, and dark glass cards for changing video
  and slide backgrounds;
- persistent frame position/size through AppKit frame autosave;
- all-Spaces and full-screen auxiliary behavior;
- adjustable opacity and compact density from Settings;
- the same transcript, answer, speaker-label, pause, stop, and writeback
  behavior as Window mode.

The native host remains a separate process so a GUI failure cannot stop audio
capture, local STT, answer workers, or transcript writeback. If the native host
cannot start, the browser surface remains the fallback.

## Capture/privacy conclusion

Both native modes request macOS's window sharing exclusion. This is a
best-effort compatibility feature, not a security boundary. Apple's current
documentation marks the old AppKit sharing value as legacy, and different
meeting applications can choose different capture paths. The HUD therefore
reports its active mode and “best effort” status instead of claiming universal
invisibility.

The safe product behavior is:

1. Use Glass HUD when the presenter wants an unobtrusive overlay.
2. Verify it against the specific call application's full-monitor share path.
3. Use window sharing, a second display, or the future hardware-separated
   presenter mode when privacy must be guaranteed.

## Verification plan

For each supported macOS release and call application, verify:

- the HUD can be dragged and resized without losing the call window;
- text remains readable over light slides, dark video, high-motion video, and
  mixed backgrounds;
- transcript scrolling remains smooth during frequent SSE updates;
- controls remain reachable without covering the useful call area;
- full-screen Zoom/Meet/Teams behavior is recorded as supported, degraded, or
  unsupported;
- the audience sees neither the window nor the glass HUD when the tested
  capture path honors the exclusion request.

The current automated checks cover configuration, native-host startup, mode
selection, fallback, and the full backend regression suite. Actual audience
visibility still requires a manual call/capture matrix because the application
being shared controls the capture path.

## Sources

- [Apple: NSPanel](https://developer.apple.com/documentation/appkit/nspanel)
- [Apple: NSHUDWindowMask](https://developer.apple.com/documentation/appkit/nshudwindowmask)
- [Apple: NSWindow collection behavior](https://developer.apple.com/documentation/appkit/nswindow/collectionbehavior-swift.struct)
- [Apple: full-screen auxiliary windows](https://developer.apple.com/documentation/appkit/nswindow/collectionbehavior-swift.struct/fullscreenauxiliary)
- [Apple: legacy sharing behavior](https://developer.apple.com/documentation/appkit/nswindow/sharingtype-swift.enum/none?changes=_1)
