# HDR output in the desktop player

The relay carries HDR end to end (10-bit PQ/HLG downlinks with their metadata,
PROTOCOL §3.5). This document is about showing it: getting a PQ signal to an
HDR display instead of mpv tone-mapping it to SDR.

## Why the player widget cannot do it

Qt Widgets compose every top-level window in SDR on every platform. The RHI
backing store never asks for an HDR swap chain format, its composition copies
values through unchanged, and Qt's OpenGL swap chain is 8-bit SDR.
`QOpenGLWidget.setTextureFormat` and the formats of `QRhiWidget` only change
the offscreen texture. Anything drawn inside the window's widgets, video
included, reaches the screen as SDR.

## The design: a native surface over the player, overlays mirrored

`desktop_client/hdr_view.py`:

- **HdrPresenter** is a native child window (`QOpenGLWindow`) over the
  player widget, 10 bits a channel. It is transparent for input
  (`Qt.WindowTransparentForInput`: an empty input region) and takes no focus.
- **On Wayland it describes itself to the compositor**
  (`hdr_output.describe_pq_surface`, before its first frame and again when
  the display's luminances change). The `wp_color_management_surface_v1`
  image description it sets is BT.2020 primaries and st2084_pq, with these
  luminances:
  - `set_luminances`: 0.005 nits up to the display's peak, reference white
    at the display's SDR white.
  - `set_mastering_luminance`: the same range.

  Qt can tag a surface too (from 6.10.2 it sends a format's `QColorSpace` as
  an image description), but a colour space carries no luminances. Qt's
  `Bt2100Pq` tag left the PQ defaults, a 10000-nit range, and Hyprland
  tone-mapped that into the panel's 1107 nits after mpv already had (see
  Hyprland below). So the format carries no colour space: Qt creates its own
  colour management surface only for one that does, and a second one for the
  same `wl_surface` is a protocol error (`surface_exists`). Qt's
  `QWindow.winId()` on Wayland is the `wl_surface`. A compositor without
  parametric descriptions with luminances gets Qt's tag instead.
- **On Wayland it spans the whole window and stays put** ("cover" mode). It
  is clear everywhere but the player's rectangle, where it draws the picture
  and the overlays, clipped to it; the window beneath shows through
  elsewhere. A subsurface that followed the player instead made each layout
  step re-present the main window in full: Qt exposes the parent window on
  every geometry change of a subsurface. A Windows child window cannot be
  clear, so the Windows prototype follows the player's geometry.
- **The render context moves.** mpv's render context is bound to one GL
  context, and freeing it while a video plays tears the video down. It moves
  between the widget and the surface only between loads
  (`MpvPlayerView._prepare_surface`, before every `loadfile`, with mpv
  stopped). mpv then outputs PQ (`target-trc=pq`, `target-prim=bt.2020`,
  `target-peak=<display peak>`), rendering into a 10-bit framebuffer (mpv
  dithers to the depth of the attachment). The surface draws that picture,
  scaled during resizes as the widget does.
- **The player widget stays in charge.** It keeps its place in the layout,
  input, focus, the cursor and every signal. It paints black underneath.
- **The overlays are mirrored.** The control bar in fullscreen, the track and
  info cards, the settings sheet, the busy ring and the seek tip are widgets
  of the main window, which the surface covers. `OverlayMirror` finds every
  widget stacked above the player, renders each into an image when it
  repaints (only the region it repainted), and the surface composites them
  over the video. The shader converts sRGB to linear light at the display's
  SDR white, BT.709 to BT.2020 primaries, then applies PQ. The real widgets
  underneath still take the pointer and the keys, so their behaviour does not
  change. Moves and fade steps reuse the picture, and opacity is applied
  when compositing. `QWidget.render` draws nothing for a widget that carries
  a graphics effect, so the fade's effect is switched off around the render.
  A widget that paints only part of itself names that part (`mirror_rect`):
  the busy ring's widget spans the player, and picturing all of it at every
  resize step cost 1.3-5 ms of each 8.3 ms frame.

The other designs considered, and why each was set aside:

- **Overlays as native subsurfaces of their own.** qtwayland never sends
  `place_above`/`place_below`, so a surface stacks above the video only if it
  was created after it (each `show()` recreates it). A translucent native
  child widget shares the window's backing store and drew its 50% panel
  twice. Separate top-level windows would have taken focus and activation
  away from the main window.
- **`overlay-add` into mpv.** During playback mpv shows OSD changes only with
  the next video frame, so overlays would have moved at 24 steps a second.
- **QRhi for the compositing.** PySide6 binds neither
  `QRhiTexture.createFrom` nor `nativeTexture`, so mpv's texture cannot reach
  it.

### When it is used

The surface is used for a PQ or HLG video (the relay's `downlink_color`, or the
source the server read for local fallback) on a screen that is in HDR mode,
while **Show HDR videos in HDR** is on (the default). In every other case the
video plays on the widget exactly as before: mpv tone-maps, with the same
code and the same pacing.

`desktop_client/hdr_output.py` reads the screen's state:

- **Wayland:** the output's `wp_color_management_output_v1` image
  description, read on an event queue of our own over Qt's `wl_display`
  through libwayland-client via ctypes (pywayland is not needed). HDR means
  transfer function st2084_pq or hlg. The peak comes from `target_luminance`
  and the SDR white from `reference_lum`. The registry and the objects bound
  through it live as long as the process, and a query creates only image
  descriptions.
- **Windows:** DXGI `IDXGIOutput6::GetDesc1` (colour space G2084/P2020 means
  HDR, plus `MaxLuminance`) and the display configuration's SDR white level.

Each load reads it again. During an HDR video the player rereads it every
3 seconds and when the window changes screen. If the surface no longer fits
(HDR switched on or off, a screen of the other kind, the setting changed),
the player reloads at the current position: a relay seek, or the local file
again.

`python -m desktop_client.hdr_output` prints what each screen reports.
`RELAY_HDR_SURFACE=force` puts every video on the surface, on any display
(an SDR surface on an SDR screen), to test the mirroring and its pacing where
no HDR display is at hand. `=off` never uses the surface.

### Hyprland

Hyprland shows a PQ surface as HDR only on a monitor with `cm = "hdr"`.
Without it, Hyprland tone-maps the surface to SDR, and the player keeps the
widget anyway, because the output reports sRGB. `hyprctl` can switch it at
runtime. Switching back needs an explicit `cm = "srgb"`, since leaving `cm`
out keeps HDR on. In HDR mode the eDP panel (Samsung ATNA53JB01-0) reports a
peak of 1107 nits and an SDR white of 203 nits. In SDR mode it reports 80 and
80.

**Hyprland tone-maps any surface whose range exceeds the output's.**
`IHyprRenderer::getCMSettings` (0.56.2) tone-maps when the surface's
`luminances.max`, scaled by the ratio of the two reference whites, reaches
1.01 times the output's. The protocol says a PQ description's maximum is
always 10000 nits whatever `set_luminances` gives; Hyprland takes the value
given. Compositors that follow the protocol read the target volume instead,
hence `set_mastering_luminance` as well.

With Qt's tag (the PQ defaults) the laptop session measured what reached the
panel (2026-11-02, 1107-nit peak, reference 203):

| signal (nits) | 203 | 600 | 1000 | 1107 | 2000 | 4000 | 10000 |
|---|---|---|---|---|---|---|---|
| on the panel | 111 | 193 | 246 | 258 | 353 | 546 | 1107 |

mpv had already tone-mapped the video to its 1107-nit `target-peak`, so a
highlight ended near 258 nits, and the video's white (203) showed at 111
beside the interface's 203. A 10000-nit test square looked far dimmer than
the hardware cursor, which skips Hyprland's colour management. Raising the
monitor's `max_luminance` to 10000, so that nothing was tone-mapped, made the
square match the cursor. The surface now declares the panel's peak, which
Hyprland only clamps.

Hyprland also switches a panel in SDR mode to HDR on its own while a
fullscreen window shows a PQ surface (`render:cm_auto_hdr`, on by default):
the connector's colorspace became BT2020_RGB with PQ metadata ~1.2 s after
fullscreen, and the output's image description read PQ with 1107/203. The
player does not use this yet: it picks the HDR surface only on a screen that
already reports HDR.

### What mpv does with the metadata

The render API runs mpv's `gpu` renderer. gpu-next is not available through
it. `gpu` uses static HDR10 metadata only, so the Dolby Vision RPU and HDR10+
still ride along unused. It tone-maps content brighter than `target-peak` (the
display's peak) and passes the rest through.

## Measurements

`tools/hdr_desktop_check.py --file hdr.mkv --pacing` plays a clip from
`upscale-cli sample hdr.mkv --hdr` in the real window and times the CLAUDE.md
scenarios. `--surface off` measures the widget for comparison. Rates are per
second, measured over the span of the motion. "Main" is the main window's
presented frames. Resizes and steps are the layout and animation steps that
reached the player or the overlay.

Laptop, 2026-11-02: Hyprland 0.56.2 with `cm = "hdr"`, Samsung OLED
2880×1800 at 120 Hz, scale 2, Qt 6.12.0, a 1080p24 HDR10 clip played locally,
window floated at 1401×860. The surface's numbers come from two runs, the
widget's from one, in the same session.

| scenario | HDR surface: main window fps | surface paints/s (median ms) | widget: main window fps |
|---|---|---|---|
| fullscreen enter | 101-110 | 104-113 (0.6) | 118 |
| fullscreen leave | 84-99 | 99-110 (0.9-1.4) | 111-112 |
| sidebar hide / show | 113-123 | 120-126 (0.24) | 124-127 |
| splitter drag | 120-122 | 121-122 (0.22) | 121 |
| steady playback | 26 paints/s, 0 dropped frames | | 0 dropped frames |

Overlay motion was measured in the earlier rounds and matched the widget:
107-140 steps a second for the cards, the settings sheet and the fullscreen
bar, against 110-145. The rounds that led here, on the same scenarios:

1. The surface followed the player on each Move/Resize of the player and its
   ancestors: 15-57 resizes a second during slides.
2. It followed once per event-loop turn: the main window ran at 62-89 fps.
3. It spanned the window, clear around the picture (cover mode): no change,
   55-92 fps. Splitting each paint into phases showed why. The video took
   0.01-0.6 ms; the mirror took 1.3-5 ms, re-rendering the busy ring's
   full-size clear picture at every step.
4. The mirror pictured only what the busy ring paints (`mirror_rect`): the
   rates above.

Fullscreen leave is the one place still behind, at 75-90% of the widget's
rate. Its paints are the longest of any span, probably because the control
bar re-docks over the shrinking picture and is pictured anew at each width.

The overlays and the window looked identical, pixel for pixel, in screenshots
of every round. Screenshots in HDR mode are converted to 8 bits, though, so
nobody has yet judged by eye that the 400-1000-nit patches are brighter than
SDR white on the panel.

## Windows

Qt's WGL code never asks for a float or 10-bit pixel format, so no OpenGL
window of Qt's can show HDR on Windows. The prototype
(`desktop_client/hdr_d3d11.py`, opt-in with `RELAY_HDR_WINDOWS=1`) keeps the
same design with Direct3D 11 for the surface:

- The native child window carries a flip-model DXGI swap chain,
  R10G10B10A2, in the BT.2100 PQ colour space
  (`DXGI_COLOR_SPACE_RGB_FULL_G2084_NONE_P2020`). Its HDR10 metadata
  (`IDXGISwapChain4::SetHDRMetaData`) gives BT.2020 primaries and the
  display's peak as the mastering maximum. DWM composes HDR10 swap chains
  without tone-mapping them, so nothing on Windows maps the picture a second
  time. The metadata is for a display that tone-maps by it.
- mpv still renders through its OpenGL render API, in a WGL context of our
  own, into a D3D11 texture (RGBA16F) shared through `WGL_NV_DX_interop2`.
  Through the interop GL's bottom row is the texture's last, so mpv renders
  unflipped.
- A small HLSL pass, compiled at runtime with `D3DCompile`, composes the
  picture with the mirrored overlays. The overlays reuse `OverlayMirror` and
  the same sRGB to PQ conversion, at Windows' SDR content brightness.
- PySide6's QRhi cannot wrap a native texture, so everything goes through
  ctypes COM calls.

Measured on the RTX 5090 box (2026-11-02, Odyssey G80SH at 240 Hz in HDR
mode, 500 nits, SDR white 80): the HDR clip plays on the swap chain with mpv
writing PQ at a 500-nit peak, and the overlays are drawn over it and aligned.
Steady playback composes at 24-26 frames a second with no drops. The
overlays move step for step: the fullscreen bar ~240 steps a second, the
track card 245-270, the settings sheet 190-195 (the widget path: 220-265).
The picture looked right in SDR screen captures, but its brightness has not
yet been judged by eye.

| scenario (rates per second) | surface paints | main window frames | widget path: main window frames |
|---|---|---|---|
| fullscreen enter | 82-87 | 85-87 | 102-108 |
| fullscreen leave | 136-138 | 98-100 | 162 |
| sidebar hide / show | 171 / 132 | 171 / 88 | 207 / 207 |
| splitter drag | 150 | 120 | 165 |

Open before Windows can leave the prototype:

1. **Transitions run at 60-85% of the widget path's rate.** The surface
   follows the player's geometry: a child window cannot be clear, so cover
   mode is out. Its presents also share the GUI thread with the window's own.
   - Windows delivers `requestUpdate()` on a `WM_TIMER`, which it generates
     only once the message queue is empty. Before the surface followed the
     player once per event-loop turn, it got almost no update requests while
     growing (11-14 a second); a queued paint fared worse still.
   - Things to try: a waitable swap chain
     (`DXGI_SWAP_CHAIN_FLAG_FRAME_LATENCY_WAITABLE_OBJECT`) so Present never
     blocks; keeping the shared texture at the screen's size and viewporting
     into it; DirectComposition, so a clear surface can span the window as on
     Wayland.
2. **Spikes.** Single paints reach 16-20 ms during transitions, probably the
   interop re-registering a texture as the picture grows. Not investigated
   yet.
3. **Input transparency.** It works through `Qt.WindowTransparentForInput`
   (`HTTRANSPARENT` on the child window). It still has to be checked against
   every overlay control, including combo popups and the slider tip.
4. **Display changes.** A move between an HDR and an SDR screen reloads the
   video, as on Wayland. Windows' HDR toggle (Win+Alt+B) while playing has not
   been tried.
5. **Other GPUs.** `d3dcompiler_47.dll` ships with Windows 10 and 11, but the
   NV interop has only been tried on NVIDIA. AMD and Intel drivers often
   expose `WGL_NV_DX_interop2` too; a missing entry point has to be detected
   at start (it raises now, which falls back to the widget).

The alternative is mpv's own HDR output (`--wid`, `vo=gpu-next`,
`gpu-api=d3d11`, `target-colorspace-hint`). It gains gpu-next (Dolby Vision
and HDR10+), but it loses the render API, so there would be no scaled resizes
and no mirrored overlays. mpv's window is on mpv's thread, so it cannot pass
the pointer through to the widgets beneath. The overlays would have to be
separate top-level windows, with the focus and activation problems described
above.
