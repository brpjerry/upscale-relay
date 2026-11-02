"""HDR output on Windows (prototype): a D3D11 HDR10 swap chain over the player.

Qt's WGL code never asks for a floating-point or 10-bit pixel format, so no
OpenGL window of Qt's can show HDR on Windows. This presenter is the
HdrPresenter of hdr_view.py with Direct3D 11 in place of the window's
OpenGL: a native child window over the player widget carries a flip-model
DXGI swap chain in R10G10B10A2 with the BT.2100 PQ colour space (DXGI
composes it as HDR10). mpv still renders through its OpenGL render API, in a
WGL context of our own, into a D3D11 texture shared through
WGL_NV_DX_interop2; each frame is composed from that texture and the
mirrored overlays (hdr_view.OverlayMirror), sRGB to PQ at the display's SDR
white level, by a small HLSL pass. Everything is reached through ctypes COM
calls: PySide6's QRhi has no way to wrap a native texture.

Opt-in (RELAY_HDR_WINDOWS=1, see hdr_output.presentation_supported) until it
has been measured as the Wayland path has; docs/HDR_OUTPUT.md has the plan.
"""

from __future__ import annotations

import ctypes
import time
from ctypes import POINTER, Structure, byref, c_char_p, c_float, c_int, c_size_t, c_uint, c_uint8, c_void_p

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QOffscreenSurface, QOpenGLContext, QWindow

from .hdr_output import _GUID, _com, _release
from .hdr_view import DEFAULT_SDR_WHITE_NITS, OverlayMirror, _fit

_RESCALE_LIMIT = 1.15
_DXGI_FORMAT_R10G10B10A2_UNORM = 24
_DXGI_FORMAT_R16G16B16A16_FLOAT = 10
_DXGI_FORMAT_R8G8B8A8_UNORM = 28
_DXGI_COLOR_SPACE_RGB_FULL_G2084_NONE_P2020 = 12
_D3D11_BIND_SHADER_RESOURCE = 0x8
_D3D11_BIND_RENDER_TARGET = 0x20
_D3D11_BIND_CONSTANT_BUFFER = 0x4
_GL_TEXTURE_2D = 0x0DE1
_GL_FRAMEBUFFER = 0x8D40
_GL_COLOR_ATTACHMENT0 = 0x8CE0
_GL_FRAMEBUFFER_COMPLETE = 0x8CD5
_WGL_ACCESS_WRITE_DISCARD_NV = 2

_IID_IDXGIDevice = "54ec77fa-1377-44e6-8c32-88fd5f44c84c"
_IID_IDXGIFactory2 = "50c83a1c-e072-4c48-87b0-3630fa36a6d0"
_IID_IDXGISwapChain3 = "94d99bdb-f1f8-4ab0-b236-7da0170edab1"
_IID_ID3D11Texture2D = "6f15aaf2-d208-4e89-9ab4-489535d34f9c"

_HLSL = b"""
cbuffer Params : register(b0) { float4 rect; float4 source; float4 params; };
struct VOut { float4 pos : SV_Position; float2 uv : TEXCOORD0; };
VOut vs_main(uint id : SV_VertexID) {
    float2 v = float2(id & 1, id >> 1);
    VOut o;
    o.uv = source.xy + v * source.zw;
    o.pos = float4(rect.xy + v * rect.zw, 0.0, 1.0);
    return o;
}
Texture2D tex : register(t0);
SamplerState smp : register(s0);
float3 to_linear(float3 c) {
    return lerp(c / 12.92, pow((c + 0.055) / 1.055, 2.4), step(0.04045, c));
}
float3 pq(float3 l) {
    float3 p = pow(max(l, 0.0), 0.1593017578125);
    return pow((0.8359375 + 18.8515625 * p) / (1.0 + 18.6875 * p), 78.84375);
}
float4 ps_main(VOut i) : SV_Target {
    float4 c = tex.Sample(smp, i.uv);
    if (params.z > 0.5 && c.a > 0.0) {
        float3x3 m = float3x3(0.627404, 0.329283, 0.043313,
                              0.069097, 0.919540, 0.011362,
                              0.016391, 0.088013, 0.895595);
        float3 rgb = mul(m, to_linear(c.rgb / c.a)) * params.x;
        c = float4(pq(rgb) * c.a, c.a);
    }
    return c * params.y;
}
"""


class _SampleDesc(Structure):
    _fields_ = [("Count", c_uint), ("Quality", c_uint)]


class _Texture2DDesc(Structure):
    _fields_ = [("Width", c_uint), ("Height", c_uint), ("MipLevels", c_uint), ("ArraySize", c_uint),
                ("Format", c_uint), ("SampleDesc", _SampleDesc), ("Usage", c_uint), ("BindFlags", c_uint),
                ("CPUAccessFlags", c_uint), ("MiscFlags", c_uint)]


class _BufferDesc(Structure):
    _fields_ = [("ByteWidth", c_uint), ("Usage", c_uint), ("BindFlags", c_uint), ("CPUAccessFlags", c_uint),
                ("MiscFlags", c_uint), ("StructureByteStride", c_uint)]


class _SamplerDesc(Structure):
    _fields_ = [("Filter", c_uint), ("AddressU", c_uint), ("AddressV", c_uint), ("AddressW", c_uint),
                ("MipLODBias", c_float), ("MaxAnisotropy", c_uint), ("ComparisonFunc", c_uint),
                ("BorderColor", c_float * 4), ("MinLOD", c_float), ("MaxLOD", c_float)]


class _RtBlendDesc(Structure):
    _fields_ = [("BlendEnable", c_int), ("SrcBlend", c_uint), ("DestBlend", c_uint), ("BlendOp", c_uint),
                ("SrcBlendAlpha", c_uint), ("DestBlendAlpha", c_uint), ("BlendOpAlpha", c_uint),
                ("RenderTargetWriteMask", c_uint8)]


class _BlendDesc(Structure):
    _fields_ = [("AlphaToCoverageEnable", c_int), ("IndependentBlendEnable", c_int),
                ("RenderTarget", _RtBlendDesc * 8)]


class _RasterizerDesc(Structure):
    _fields_ = [("FillMode", c_uint), ("CullMode", c_uint), ("FrontCounterClockwise", c_int),
                ("DepthBias", c_int), ("DepthBiasClamp", c_float), ("SlopeScaledDepthBias", c_float),
                ("DepthClipEnable", c_int), ("ScissorEnable", c_int), ("MultisampleEnable", c_int),
                ("AntialiasedLineEnable", c_int)]


class _Viewport(Structure):
    _fields_ = [("TopLeftX", c_float), ("TopLeftY", c_float), ("Width", c_float), ("Height", c_float),
                ("MinDepth", c_float), ("MaxDepth", c_float)]


class _SwapChainDesc1(Structure):
    _fields_ = [("Width", c_uint), ("Height", c_uint), ("Format", c_uint), ("Stereo", c_int),
                ("SampleDesc", _SampleDesc), ("BufferUsage", c_uint), ("BufferCount", c_uint),
                ("Scaling", c_uint), ("SwapEffect", c_uint), ("AlphaMode", c_uint), ("Flags", c_uint)]


class _Box(Structure):
    _fields_ = [("left", c_uint), ("top", c_uint), ("front", c_uint), ("right", c_uint), ("bottom", c_uint),
                ("back", c_uint)]


def _check(hr: int, what: str) -> None:
    if hr < 0:
        raise OSError(f"{what} failed: HRESULT 0x{hr & 0xFFFFFFFF:08X}")


class _D3D:
    """The device, the swap chain on one window, and the composing pass."""

    def __init__(self, hwnd: int, width: int, height: int):
        self.device = c_void_p()
        self.context = c_void_p()
        level = c_uint()
        d3d11 = ctypes.WinDLL("d3d11")
        _check(d3d11.D3D11CreateDevice(None, 1, None, 0x20, None, 0, 7,
                                       byref(self.device), byref(level), byref(self.context)), "D3D11CreateDevice")
        dxgi_device, adapter, factory = c_void_p(), c_void_p(), c_void_p()
        _check(_com(self.device, 0, c_int, [POINTER(_GUID), POINTER(c_void_p)],
                    byref(_GUID.parse(_IID_IDXGIDevice)), byref(dxgi_device)), "QueryInterface(IDXGIDevice)")
        try:
            _check(_com(dxgi_device, 7, c_int, [POINTER(c_void_p)], byref(adapter)), "GetAdapter")
            _check(_com(adapter, 6, c_int, [POINTER(_GUID), POINTER(c_void_p)],
                        byref(_GUID.parse(_IID_IDXGIFactory2)), byref(factory)), "GetParent(IDXGIFactory2)")
            desc = _SwapChainDesc1(width, height, _DXGI_FORMAT_R10G10B10A2_UNORM, 0, _SampleDesc(1, 0),
                                   0x20, 2, 0, 4, 0, 0)  # render target output, stretch, flip discard
            swapchain1 = c_void_p()
            _check(_com(factory, 15, c_int, [c_void_p, c_void_p, POINTER(_SwapChainDesc1), c_void_p, c_void_p,
                                             POINTER(c_void_p)],
                        self.device, c_void_p(hwnd), byref(desc), None, None, byref(swapchain1)),
                   "CreateSwapChainForHwnd")
        finally:
            for obj in (factory, adapter, dxgi_device):
                _release(obj)
        self.swapchain = c_void_p()
        try:
            _check(_com(swapchain1, 0, c_int, [POINTER(_GUID), POINTER(c_void_p)],
                        byref(_GUID.parse(_IID_IDXGISwapChain3)), byref(self.swapchain)), "IDXGISwapChain3")
        finally:
            _release(swapchain1)
        _check(_com(self.swapchain, 38, c_int, [c_uint], _DXGI_COLOR_SPACE_RGB_FULL_G2084_NONE_P2020),
               "SetColorSpace1(G2084 P2020)")
        self.size = (width, height)
        self.rtv = c_void_p()
        self._make_rtv()
        self._compile()

    def _make_rtv(self) -> None:
        backbuffer = c_void_p()
        _check(_com(self.swapchain, 9, c_int, [c_uint, POINTER(_GUID), POINTER(c_void_p)],
                    0, byref(_GUID.parse(_IID_ID3D11Texture2D)), byref(backbuffer)), "GetBuffer")
        try:
            _check(_com(self.device, 9, c_int, [c_void_p, c_void_p, POINTER(c_void_p)],
                        backbuffer, None, byref(self.rtv)), "CreateRenderTargetView")
        finally:
            _release(backbuffer)

    def resize(self, width: int, height: int) -> None:
        if (width, height) == self.size or width <= 0 or height <= 0:
            return
        _com(self.context, 33, None, [c_uint, c_void_p, c_void_p], 0, None, None)  # unbind targets
        _release(self.rtv)
        self.rtv = c_void_p()
        _check(_com(self.swapchain, 13, c_int, [c_uint, c_uint, c_uint, c_uint, c_uint],
                    0, width, height, 0, 0), "ResizeBuffers")
        self.size = (width, height)
        self._make_rtv()

    def _blob(self, entry: bytes, target: bytes) -> bytes:
        compiler = ctypes.WinDLL("d3dcompiler_47")
        code, errors = c_void_p(), c_void_p()
        hr = compiler.D3DCompile(c_char_p(_HLSL), c_size_t(len(_HLSL)), None, None, None, c_char_p(entry),
                                 c_char_p(target), 0, 0, byref(code), byref(errors))
        if hr < 0:
            message = b""
            if errors:
                pointer = _com(errors, 3, c_void_p, [])
                message = ctypes.string_at(pointer, _com(errors, 4, c_size_t, []))
                _release(errors)
            raise OSError(f"D3DCompile {entry.decode()}: {message.decode(errors='replace')}")
        data = ctypes.string_at(_com(code, 3, c_void_p, []), _com(code, 4, c_size_t, []))
        _release(code)
        return data

    def _compile(self) -> None:
        vs, ps = self._blob(b"vs_main", b"vs_5_0"), self._blob(b"ps_main", b"ps_5_0")
        self.vs, self.ps = c_void_p(), c_void_p()
        _check(_com(self.device, 12, c_int, [c_char_p, c_size_t, c_void_p, POINTER(c_void_p)],
                    vs, len(vs), None, byref(self.vs)), "CreateVertexShader")
        _check(_com(self.device, 15, c_int, [c_char_p, c_size_t, c_void_p, POINTER(c_void_p)],
                    ps, len(ps), None, byref(self.ps)), "CreatePixelShader")
        self.constants = c_void_p()
        _check(_com(self.device, 3, c_int, [POINTER(_BufferDesc), c_void_p, POINTER(c_void_p)],
                    byref(_BufferDesc(48, 0, _D3D11_BIND_CONSTANT_BUFFER, 0, 0, 0)), None, byref(self.constants)),
               "CreateBuffer")
        sampler = _SamplerDesc(0x15, 3, 3, 3, 0.0, 1, 0, (c_float * 4)(), 0.0, 3.4e38)  # linear, clamp
        self.sampler = c_void_p()
        _check(_com(self.device, 23, c_int, [POINTER(_SamplerDesc), POINTER(c_void_p)],
                    byref(sampler), byref(self.sampler)), "CreateSamplerState")
        blend = _BlendDesc()
        blend.RenderTarget[0] = _RtBlendDesc(1, 2, 6, 1, 2, 6, 1, 0x0F)  # ONE, INV_SRC_ALPHA, ADD
        self.blend = c_void_p()
        _check(_com(self.device, 20, c_int, [POINTER(_BlendDesc), POINTER(c_void_p)],
                    byref(blend), byref(self.blend)), "CreateBlendState")
        raster = _RasterizerDesc(3, 1, 0, 0, 0.0, 0.0, 1, 0, 0, 0)  # solid, no culling
        self.raster = c_void_p()
        _check(_com(self.device, 22, c_int, [POINTER(_RasterizerDesc), POINTER(c_void_p)],
                    byref(raster), byref(self.raster)), "CreateRasterizerState")

    def texture(self, width: int, height: int, fmt: int, render_target: bool = False):
        desc = _Texture2DDesc(width, height, 1, 1, fmt, _SampleDesc(1, 0), 0,
                              _D3D11_BIND_SHADER_RESOURCE | (_D3D11_BIND_RENDER_TARGET if render_target else 0),
                              0, 0)
        texture, view = c_void_p(), c_void_p()
        _check(_com(self.device, 5, c_int, [POINTER(_Texture2DDesc), c_void_p, POINTER(c_void_p)],
                    byref(desc), None, byref(texture)), "CreateTexture2D")
        _check(_com(self.device, 7, c_int, [c_void_p, c_void_p, POINTER(c_void_p)],
                    texture, None, byref(view)), "CreateShaderResourceView")
        return texture, view

    def upload(self, texture, x: int, y: int, width: int, height: int, data, pitch: int) -> None:
        box = _Box(x, y, 0, x + width, y + height, 1)
        _com(self.context, 48, None, [c_void_p, c_uint, POINTER(_Box), c_void_p, c_uint, c_uint],
             texture, 0, byref(box), data, pitch, 0)

    def begin(self) -> None:
        context = self.context
        width, height = self.size
        _com(context, 33, None, [c_uint, POINTER(c_void_p), c_void_p], 1, byref(self.rtv), None)
        _com(context, 50, None, [c_void_p, POINTER(c_float)], self.rtv, (c_float * 4)(0.0, 0.0, 0.0, 1.0))
        _com(context, 44, None, [c_uint, POINTER(_Viewport)], 1, byref(_Viewport(0, 0, width, height, 0, 1)))
        _com(context, 24, None, [c_uint], 5)  # triangle strip
        _com(context, 17, None, [c_void_p], None)  # no input layout: SV_VertexID
        _com(context, 11, None, [c_void_p, c_void_p, c_uint], self.vs, None, 0)
        _com(context, 9, None, [c_void_p, c_void_p, c_uint], self.ps, None, 0)
        _com(context, 7, None, [c_uint, c_uint, POINTER(c_void_p)], 0, 1, byref(self.constants))
        _com(context, 16, None, [c_uint, c_uint, POINTER(c_void_p)], 0, 1, byref(self.constants))
        _com(context, 10, None, [c_uint, c_uint, POINTER(c_void_p)], 0, 1, byref(self.sampler))
        _com(context, 43, None, [c_void_p], self.raster)

    def draw(self, view, rect, source, white: float, opacity: float, convert: bool, blend: bool) -> None:
        context = self.context
        values = (c_float * 12)(*rect, *source, white, opacity, 1.0 if convert else 0.0, 0.0)
        _com(context, 48, None, [c_void_p, c_uint, c_void_p, c_void_p, c_uint, c_uint],
             self.constants, 0, None, values, 0, 0)
        _com(context, 8, None, [c_uint, c_uint, POINTER(c_void_p)], 0, 1, byref(view))
        _com(context, 35, None, [c_void_p, c_void_p, c_uint], self.blend if blend else None, None, 0xFFFFFFFF)
        _com(context, 13, None, [c_uint, c_uint], 4, 0)

    def present(self) -> None:
        _com(self.swapchain, 8, c_int, [c_uint, c_uint], 1, 0)

    def close(self) -> None:
        for name in ("raster", "blend", "sampler", "constants", "ps", "vs", "rtv", "swapchain", "context", "device"):
            _release(getattr(self, name, None))
            setattr(self, name, None)


class _Interop:
    """WGL_NV_DX_interop2: GL framebuffers whose colour is a D3D11 texture."""

    def __init__(self, context: QOpenGLContext, device):
        def fn(name, restype, *argtypes):
            address = int(context.getProcAddress(name))
            if not address:
                raise OSError(f"{name.decode()} is not available")
            return ctypes.WINFUNCTYPE(restype, *argtypes)(address)

        self.open_device = fn(b"wglDXOpenDeviceNV", c_void_p, c_void_p)
        self.close_device = fn(b"wglDXCloseDeviceNV", c_int, c_void_p)
        self.register = fn(b"wglDXRegisterObjectNV", c_void_p, c_void_p, c_void_p, c_uint, c_uint, c_uint)
        self.unregister = fn(b"wglDXUnregisterObjectNV", c_int, c_void_p, c_void_p)
        self.lock = fn(b"wglDXLockObjectsNV", c_int, c_void_p, c_int, POINTER(c_void_p))
        self.unlock = fn(b"wglDXUnlockObjectsNV", c_int, c_void_p, c_int, POINTER(c_void_p))
        self.gen_framebuffers = fn(b"glGenFramebuffers", None, c_int, POINTER(c_uint))
        self.delete_framebuffers = fn(b"glDeleteFramebuffers", None, c_int, POINTER(c_uint))
        self.bind_framebuffer = fn(b"glBindFramebuffer", None, c_uint, c_uint)
        self.framebuffer_texture = fn(b"glFramebufferTexture2D", None, c_uint, c_uint, c_uint, c_uint, c_int)
        self.framebuffer_status = fn(b"glCheckFramebufferStatus", c_uint, c_uint)
        self.handle = self.open_device(device)
        if not self.handle:
            raise OSError("wglDXOpenDeviceNV failed")


class _VideoTarget:
    """mpv's picture: a D3D11 texture that is a GL framebuffer for mpv."""

    def __init__(self, d3d: _D3D, interop: _Interop, width: int, height: int):
        from PySide6.QtOpenGL import QOpenGLTexture  # noqa: PLC0415

        self.width, self.height = width, height
        self.texture, self.view = d3d.texture(width, height, _DXGI_FORMAT_R16G16B16A16_FLOAT, render_target=True)
        self._interop = interop
        self._gl_texture = QOpenGLTexture(QOpenGLTexture.Target2D)
        self._gl_texture.create()  # a name with no storage: the interop supplies it
        self.object = interop.register(interop.handle, self.texture, self._gl_texture.textureId(),
                                       _GL_TEXTURE_2D, _WGL_ACCESS_WRITE_DISCARD_NV)
        if not self.object:
            raise OSError("wglDXRegisterObjectNV failed")
        fbo = c_uint()
        interop.gen_framebuffers(1, byref(fbo))
        self.fbo = fbo.value
        self.lock()
        try:
            interop.bind_framebuffer(_GL_FRAMEBUFFER, self.fbo)
            interop.framebuffer_texture(_GL_FRAMEBUFFER, _GL_COLOR_ATTACHMENT0, _GL_TEXTURE_2D,
                                        self._gl_texture.textureId(), 0)
            status = interop.framebuffer_status(_GL_FRAMEBUFFER)
            interop.bind_framebuffer(_GL_FRAMEBUFFER, 0)
        finally:
            self.unlock()
        if status != _GL_FRAMEBUFFER_COMPLETE:
            raise OSError(f"interop framebuffer incomplete: 0x{status:04X}")

    def lock(self) -> None:
        objects = (c_void_p * 1)(self.object)
        if not self._interop.lock(self._interop.handle, 1, objects):
            raise OSError("wglDXLockObjectsNV failed")

    def unlock(self) -> None:
        objects = (c_void_p * 1)(self.object)
        self._interop.unlock(self._interop.handle, 1, objects)

    def close(self) -> None:
        fbo = c_uint(self.fbo)
        self._interop.delete_framebuffers(1, byref(fbo))
        self._interop.unregister(self._interop.handle, self.object)
        self._gl_texture.destroy()
        _release(self.view)
        _release(self.texture)


class D3D11Presenter(QWindow):
    """HdrPresenter's counterpart on Windows (see the module docstring).

    The same surface for MpvPlayerView: it follows the player widget, takes
    no input or focus, owns the GL context mpv's render context lives in
    (makeCurrent/doneCurrent), and composes on update().
    """

    ready = Signal()
    failed = Signal(str)

    def __init__(self, view, parent: QWindow, *, pq: bool = True, sdr_white_nits: float | None = None):
        super().__init__(parent)
        self._view = view
        self.pq = True
        self.sdr_white_nits = sdr_white_nits or DEFAULT_SDR_WHITE_NITS
        self.setFlags(self.flags() | Qt.WindowTransparentForInput | Qt.WindowDoesNotAcceptFocus)
        self.ctx = None
        self._d3d: _D3D | None = None
        self._gl: QOpenGLContext | None = None
        self._surface: QOffscreenSurface | None = None
        self._interop: _Interop | None = None
        self._video: _VideoTarget | None = None
        self._textures: dict = {}
        self._picture_due = False
        self._broken = False
        self._place_posted = False
        self._mirror = OverlayMirror(view, self._overlays_changed)
        self._followed = []
        self._tracker_obj = None
        self.frames = 0
        self.renders = 0
        self.last_paint_ms = 0.0

    # -- the HdrPresenter surface ------------------------------------------------

    @property
    def initialized(self) -> bool:
        return self._d3d is not None and not self._broken

    @property
    def broken(self) -> bool:
        return self._broken

    def makeCurrent(self) -> bool:
        return self._gl.makeCurrent(self._surface)

    def doneCurrent(self) -> None:
        self._gl.doneCurrent()

    def update(self) -> None:
        # Open: while the window grows (entering fullscreen, hiding the
        # sidebar) Windows delivers no update request to the surface until
        # the slide ends (WM_TIMER waits for an empty message queue). A queued
        # paint instead fared worse still; see docs/HDR_OUTPUT.md.
        self.requestUpdate()

    def _paint_now(self) -> None:
        try:
            if self._d3d is None and not self._broken and self.isExposed():
                self._start()
            if self._d3d is not None and self.isExposed():
                self._paint()
        except Exception as error:  # noqa: BLE001 - a native crash otherwise
            self._broken = True
            self.failed.emit(f"HDR surface (D3D11): {error!r}")

    def picture_due(self) -> None:
        self._picture_due = True

    def follow_view(self) -> None:
        if self._tracker_obj is None:
            self._tracker_obj = _Tracker(self)
        widget = self._view
        while widget is not None and not widget.isWindow():
            if widget not in self._followed:
                widget.installEventFilter(self._tracker_obj)
                self._followed.append(widget)
            widget = widget.parentWidget()
        self._place_now()

    def place(self) -> None:
        # Once per turn of the event loop, as HdrPresenter.place.
        if not self._place_posted:
            self._place_posted = True
            QTimer.singleShot(0, self._place_now)

    def _place_now(self) -> None:
        self._place_posted = False
        view = self._view
        geometry = QRect(view.mapTo(view.window(), QPoint(0, 0)), view.size())
        if geometry != self.geometry():
            self.setGeometry(geometry)
            self.update()

    def frame_ready(self) -> None:
        if self.ctx is None:
            return
        if self.isExposed():
            self.update()
            return
        self.makeCurrent()
        try:
            if self.ctx is not None and self.ctx.update():
                self.ctx.render(skip_rendering=True)
        finally:
            self.doneCurrent()

    def release_render_context(self) -> None:
        if self.ctx is None:
            return
        self.makeCurrent()
        try:
            self.ctx.free()
            self.ctx = None
            if self._video is not None:
                self._video.close()
                self._video = None
        finally:
            self.doneCurrent()

    def _overlays_changed(self) -> None:
        if self.isVisible():
            self.update()

    # -- set-up ------------------------------------------------------------------

    def event(self, event) -> bool:
        if event.type() in (QEvent.Expose, QEvent.UpdateRequest):
            self._paint_now()
        return super().event(event)

    def _start(self) -> None:
        width, height = self._pixel_size()
        self._d3d = _D3D(int(self.winId()), width, height)
        self._gl = QOpenGLContext()
        if not self._gl.create():
            raise OSError("no OpenGL context for mpv")
        self._surface = QOffscreenSurface()
        self._surface.setFormat(self._gl.format())
        self._surface.create()
        self.makeCurrent()
        try:
            self._interop = _Interop(self._gl, self._d3d.device)
        finally:
            self.doneCurrent()
        self.ready.emit()

    def _pixel_size(self) -> tuple[int, int]:
        dpr = self.devicePixelRatio()
        return max(1, round(self.width() * dpr)), max(1, round(self.height() * dpr))

    # -- frames ------------------------------------------------------------------

    def _paint(self) -> None:
        started = time.perf_counter()
        d3d = self._d3d
        width, height = self._pixel_size()
        d3d.resize(width, height)
        dpr = self.devicePixelRatio()
        layers = self._mirror.layers(dpr)
        if self.ctx is not None:
            self._render_video(width, height)
        d3d.begin()
        if self._video is not None:
            self._draw_video(width, height)
        white = self.sdr_white_nits / 10000.0
        for layer, pos, opacity in layers:
            if opacity <= 0.0 or layer.image is None:
                continue
            view = self._overlay_texture(layer)
            x, y = round(pos.x() * dpr), round(pos.y() * dpr)
            w, h = layer.image.width(), layer.image.height()
            rect = (2.0 * x / width - 1.0, 1.0 - 2.0 * (y + h) / height, 2.0 * w / width, 2.0 * h / height)
            d3d.draw(view, rect, (0.0, 1.0, 1.0, -1.0), white, float(opacity), True, True)
        live = {layer for layer, _pos, _opacity in layers}
        for layer in [layer for layer in self._textures if layer not in live]:
            texture, view, _size = self._textures.pop(layer)
            _release(view)
            _release(texture)
        d3d.present()
        self.frames += 1
        self.last_paint_ms = (time.perf_counter() - started) * 1000

    def _render_video(self, width: int, height: int) -> None:
        view = self._view
        aspect = view._picture_aspect()
        scaling = view._scaling and aspect is not None
        video = self._video
        if scaling:
            stale = video is None or not (
                1 / _RESCALE_LIMIT <= width / video.width <= _RESCALE_LIMIT
                and 1 / _RESCALE_LIMIT <= height / video.height <= _RESCALE_LIMIT)
        else:
            stale = video is None or (video.width, video.height) != (width, height)
        self.makeCurrent()
        try:
            if stale:
                if video is not None:
                    video.close()
                video = self._video = _VideoTarget(self._d3d, self._interop, width, height)
            new_frame = self.ctx.update()
            if stale or new_frame or self._picture_due:
                video.lock()
                try:
                    # Unflipped: through the interop GL's bottom row is the
                    # texture's last, so it samples top-down like a QImage.
                    self.ctx.render(flip_y=False, opengl_fbo={"fbo": video.fbo, "w": video.width, "h": video.height})
                finally:
                    video.unlock()
                self._picture_due = False
                self.renders += 1
        finally:
            self.doneCurrent()

    def _draw_video(self, width: int, height: int) -> None:
        video = self._video
        aspect = self._view._picture_aspect()
        if (video.width, video.height) == (width, height) or aspect is None:
            rect = (-1.0, -1.0, 2.0, 2.0)
        else:
            src_x, src_y, src_w, _src_h = _fit(video.width, video.height, aspect)
            dst_x, dst_y, dst_w, _dst_h = _fit(width, height, aspect)
            scale = dst_w / src_w
            left, top = dst_x - src_x * scale, dst_y - src_y * scale
            w, h = video.width * scale, video.height * scale
            rect = (2.0 * left / width - 1.0, 1.0 - 2.0 * (top + h) / height, 2.0 * w / width, 2.0 * h / height)
        # mpv already wrote PQ (target-trc=pq): drawn as it is, unblended.
        self._d3d.draw(video.view, rect, (0.0, 1.0, 1.0, -1.0), 0.0, 1.0, False, False)

    def _overlay_texture(self, layer):
        image = layer.image
        entry = self._textures.get(layer)
        if entry is not None and entry[2] != (image.width(), image.height()):
            texture, view, _size = self._textures.pop(layer)
            _release(view)
            _release(texture)
            entry = None
        if entry is None:
            texture, view = self._d3d.texture(image.width(), image.height(), _DXGI_FORMAT_R8G8B8A8_UNORM)
            entry = self._textures[layer] = (texture, view, (image.width(), image.height()))
            layer.upload = image.rect()
        rect = layer.upload
        if not rect.isNull() and not rect.isEmpty():
            part = image if rect == image.rect() else image.copy(rect)
            self._d3d.upload(entry[0], rect.x(), rect.y(), rect.width(), rect.height(),
                             bytes(part.constBits()), part.bytesPerLine())
        layer.upload = QRect()
        return entry[1]

    def destroy_d3d(self) -> None:
        self.release_render_context()
        for texture, view, _size in self._textures.values():
            _release(view)
            _release(texture)
        self._textures.clear()
        if self._interop is not None and self._gl is not None:
            self.makeCurrent()
            self._interop.close_device(self._interop.handle)
            self.doneCurrent()
            self._interop = None
        if self._d3d is not None:
            self._d3d.close()
            self._d3d = None


class _Tracker(QObject):
    def __init__(self, presenter: D3D11Presenter):
        super().__init__(presenter)
        self._presenter = presenter

    def eventFilter(self, obj, event) -> bool:
        try:
            if event.type() in (QEvent.Move, QEvent.Resize) and self._presenter.isVisible():
                self._presenter.place()
        except Exception:  # noqa: BLE001 - a native crash otherwise
            pass
        return False
