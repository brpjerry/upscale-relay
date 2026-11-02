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
  player widget. Its format is 10 bits a channel tagged
  `QColorSpace.Bt2100Pq`. From Qt 6.10.2 the Wayland plugin sends that colour
  space as a `wp_color_management_surface_v1` image description (bt2020
  primaries, st2084_pq), and the compositor shows the surface as HDR. It is
  transparent for input (`Qt.WindowTransparentForInput`: an empty input
  region) and takes no focus.
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
window floated at 1401×860, two runs each.

<!-- MEASUREMENTS -->

## Windows

Qt's WGL code never asks for a float or 10-bit pixel format, so no OpenGL
window of Qt's can show HDR on Windows. The prototype
(`desktop_client/hdr_d3d11.py`, opt-in with `RELAY_HDR_WINDOWS=1`) keeps the
same design with Direct3D 11 for the surface:

- The native child window carries a flip-model DXGI swap chain,
  R10G10B10A2, in the BT.2100 PQ colour space
  (`DXGI_COLOR_SPACE_RGB_FULL_G2084_NONE_P2020`).
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
Steady playback composes at 24 frames a second with no drops. The overlays
move step for step: the fullscreen bar ~220 steps a second, the track card
~250, the settings sheet 160-170 (the widget path: 220-265). The picture
looked right in SDR screen captures, but its brightness has not yet been
judged by eye.

| scenario (rates per second) | surface paints | main window frames | widget path: main window frames |
|---|---|---|---|
| fullscreen enter | 47-49 | 50-52 | 102-108 |
| fullscreen leave | 95-102 | 72-76 | 162 |
| sidebar hide / show | 121 / 87 | 145 / 56 | 207 / 207 |
| splitter drag | 99 | 75 | 165 |

Open before Windows can leave the prototype:

1. **Transitions run at half the widget path's rate.** Growing is the
   worst case: each 15% of growth re-allocates the shared texture and renders
   mpv at the new size (~11 ms a paint). The surface's presents also share the
   GUI thread with the window's own.
   - Windows delivers `requestUpdate()` on a `WM_TIMER`, which it generates
     only once the message queue is empty. Before the surface followed the
     player once per event-loop turn, it got almost no update requests while
     growing (11-14 a second); a queued paint fared worse still.
   - Things to try: a waitable swap chain
     (`DXGI_SWAP_CHAIN_FLAG_FRAME_LATENCY_WAITABLE_OBJECT`) so Present never
     blocks; keeping the shared texture at the screen's size and viewporting
     into it; a render thread for the surface, with the mirror's images
     handed across.
2. **Spikes.** Single paints reached 40-60 ms during the sidebar slide and
   the splitter drag, probably the interop re-registering a texture. Not
   investigated yet.
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
