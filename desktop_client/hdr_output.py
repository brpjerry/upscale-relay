"""Whether the screen a window is on shows HDR, and how bright.

Qt reports none of this. On Wayland it comes from the compositor's
wp_color_management_v1 output image description (transfer function and
luminances), read on a connection queue of our own over Qt's wl_display
through libwayland-client (no pywayland). On Windows DXGI reports it
(IDXGIOutput6::GetDesc1: the colour space is BT.2100 PQ while HDR is on) and
the display configuration API the SDR white level.

Every query is a fresh snapshot (a DXGI factory caches its outputs; on
Wayland a query creates only the image descriptions), cheap enough to repeat
at each load and on a slow timer.

The same Wayland client describes HdrPresenter's surface
(``describe_pq_surface``): BT.2100 PQ with the display's peak and reference
white, which Qt's colour space tag cannot carry.
"""

from __future__ import annotations

import ctypes
import os
from ctypes import (
    CFUNCTYPE, POINTER, Structure, Union, byref, c_char_p, c_float, c_int, c_int32, c_uint16,
    c_uint32, c_ubyte, c_void_p, cast,
)
from dataclasses import dataclass

from PySide6.QtCore import QLibraryInfo, QVersionNumber
from PySide6.QtGui import QGuiApplication


@dataclass(frozen=True)
class DisplayHdr:
    """What one screen shows. ``peak_nits`` and ``sdr_white_nits`` are None
    when the platform does not say. ``auto``: not read but expected, an SDR
    screen the compositor is to switch to HDR (``expected_hdr``)."""

    hdr: bool
    peak_nits: float | None = None
    sdr_white_nits: float | None = None
    source: str = ""
    auto: bool = False


SDR = DisplayHdr(False)

# Each screen's last HDR reading, by source: what the compositor's own switch
# to HDR will most likely give it again.
_seen_hdr: dict[str, DisplayHdr] = {}
# Until a screen has been read in HDR: a common HDR peak, and PQ's reference
# white (which Hyprland's HDR mode reports too).
_EXPECTED_PEAK_NITS = 1000.0
_EXPECTED_WHITE_NITS = 203.0

# The Qt release whose Wayland plugin tags a surface with its format's colour
# space (wp_color_management_v1 set_image_description).
_QT_WAYLAND_COLOR = QVersionNumber(6, 10, 2)


def platform_kind(app=None) -> str:
    """'wayland', 'windows' or 'other'."""
    app = app or QGuiApplication.instance()
    name = app.platformName().lower() if app is not None else ""
    if name.startswith("wayland"):
        return "wayland"
    if name == "windows":
        return "windows"
    return "other"


def presentation_supported(app=None) -> bool:
    """Whether this build can put a PQ surface on screen: HdrPresenter on
    Wayland, the D3D11 prototype (hdr_d3d11.py) on Windows when
    RELAY_HDR_WINDOWS=1."""
    kind = platform_kind(app)
    if kind == "wayland":
        return QLibraryInfo.version() >= _QT_WAYLAND_COLOR
    if kind == "windows":
        return os.environ.get("RELAY_HDR_WINDOWS") == "1"
    return False


def query(window=None) -> DisplayHdr:
    """The HDR state of the screen ``window`` (a QWidget) is on."""
    try:
        kind = platform_kind()
        if kind == "wayland":
            screen = window.screen() if window is not None else None
            display = _wayland_query(screen.name() if screen is not None else None)
        elif kind == "windows":
            hwnd = int(window.winId()) if window is not None else None
            display = _windows_query(hwnd)
        else:
            return SDR
    except Exception:  # noqa: BLE001 - a failed query reads as SDR
        return SDR
    if display.hdr:
        _seen_hdr[display.source] = display
    return display


def expected_hdr(display: DisplayHdr) -> DisplayHdr:
    """What ``display`` (read as SDR) will most likely report once the
    compositor switches it to HDR: its last HDR reading, else a guess."""
    seen = _seen_hdr.get(display.source)
    return DisplayHdr(
        True,
        seen.peak_nits if seen is not None and seen.peak_nits else _EXPECTED_PEAK_NITS,
        seen.sdr_white_nits if seen is not None and seen.sdr_white_nits else _EXPECTED_WHITE_NITS,
        display.source, auto=True,
    )


def compositor_auto_hdr() -> bool:
    """Whether the compositor switches a screen to HDR by itself while a
    fullscreen window shows an HDR surface, and back once it leaves:
    Hyprland's ``render:cm_auto_hdr`` (on by default)."""
    signature = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
    if not signature or platform_kind() != "wayland":
        return False
    value = _hyprland_option(signature, "render:cm_auto_hdr")
    return value != 0  # unknown: Hyprland's default, on


def configured_hdr(screen_name: str | None) -> bool | None:
    """Whether the compositor's own setting puts this screen in HDR mode
    (Hyprland's ``cm = "hdr"``/``"hdredid"``). Its switch for a fullscreen
    HDR surface leaves that setting as it is, so a screen that reads HDR
    with this False is in HDR only for the fullscreen window. None: unknown."""
    signature = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
    if not signature or not screen_name:
        return None
    monitors = _hyprland_request(signature, "j/monitors")
    if not isinstance(monitors, list):
        return None
    for monitor in monitors:
        if isinstance(monitor, dict) and monitor.get("name") == screen_name:
            preset = monitor.get("colorManagementPreset")
            return preset in ("hdr", "hdredid") if isinstance(preset, str) else None
    return None


def _hyprland_option(signature: str, name: str) -> int | None:
    """An integer option over Hyprland's IPC socket (``hyprctl getoption``)."""
    return _option_value(_hyprland_request(signature, f"j/getoption {name}"))


def _hyprland_request(signature: str, command: str):
    """A JSON reply over Hyprland's IPC socket, or None."""
    import json  # noqa: PLC0415
    import socket  # noqa: PLC0415

    runtime = os.environ.get("XDG_RUNTIME_DIR", "")
    for path in (os.path.join(runtime, "hypr", signature, ".socket.sock"),
                 os.path.join("/tmp", "hypr", signature, ".socket.sock")):
        if not os.path.exists(path):
            continue
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(0.5)
                connection.connect(path)
                connection.sendall(command.encode())
                reply = b""
                while chunk := connection.recv(4096):
                    reply += chunk
            return json.loads(reply)
        except (OSError, ValueError):
            continue  # a stale socket: try the other place
    return None


def _option_value(reply) -> int | None:
    if not isinstance(reply, dict):
        return None
    for key in ("int", "value"):
        if isinstance(reply.get(key), int) and not isinstance(reply.get(key), bool):
            return reply[key]
    return None


# -- Wayland -------------------------------------------------------------------

class _WlMessage(Structure):
    _fields_ = [("name", c_char_p), ("signature", c_char_p), ("types", POINTER(c_void_p))]


class _WlInterface(Structure):
    pass


_WlInterface._fields_ = [
    ("name", c_char_p), ("version", c_int),
    ("method_count", c_int), ("methods", POINTER(_WlMessage)),
    ("event_count", c_int), ("events", POINTER(_WlMessage)),
]


class _WlArgument(Union):
    _fields_ = [
        ("i", c_int32), ("u", c_uint32), ("f", c_int32), ("s", c_char_p),
        ("o", c_void_p), ("n", c_uint32), ("a", c_void_p), ("h", c_int32),
    ]


_WL_MARSHAL_FLAG_DESTROY = 1
_TF_ST2084_PQ = 11
_TF_HLG = 13
_PRIMARIES_BT2020 = 6
_INTENT_PERCEPTUAL = 0
_FEATURE_PARAMETRIC = 1
_FEATURE_SET_LUMINANCES = 4
_FEATURE_SET_MASTERING_DISPLAY_PRIMARIES = 5
_PQ_MIN_NITS = 0.005  # st2084_pq's default minimum luminance
# Event argument types for listener callbacks, by signature letter.
_CTYPE = {"i": c_int32, "u": c_uint32, "f": c_int32, "s": c_char_p, "o": c_void_p,
          "n": c_void_p, "a": c_void_p, "h": c_int32}


class _Protocol:
    """The color-management-v1 interfaces a query and a surface description
    need, as libwayland interface descriptions. Requests and events must be listed completely
    and in protocol order (they are dispatched by opcode)."""

    def __init__(self):
        self._keep = []
        info = self._interface("wp_image_description_info_v1", 1, [], [
            ("done", ""), ("icc_file", "hu"), ("primaries", "iiiiiiii"), ("primaries_named", "u"),
            ("tf_power", "u"), ("tf_named", "u"), ("luminances", "uuu"),
            ("target_primaries", "iiiiiiii"), ("target_luminance", "uu"),
            ("target_max_cll", "u"), ("target_max_fall", "u"),
        ])
        description = self._interface("wp_image_description_v1", 1, [
            ("destroy", ""), ("get_information", "n", [info]),
        ], [("failed", "us"), ("ready", "u")])
        output = self._interface("wp_color_management_output_v1", 1, [
            ("destroy", ""), ("get_image_description", "n", [description]),
        ], [("image_description_changed", "")])
        creator = self._interface("wp_image_description_creator_params_v1", 1, [
            ("create", "n", [description]), ("set_tf_named", "u"), ("set_tf_power", "u"),
            ("set_primaries_named", "u"), ("set_primaries", "iiiiiiii"), ("set_luminances", "uuu"),
            ("set_mastering_display_primaries", "iiiiiiii"), ("set_mastering_luminance", "uu"),
            ("set_max_cll", "u"), ("set_max_fall", "u"),
        ], [])
        surface = self._interface("wp_color_management_surface_v1", 1, [
            ("destroy", ""), ("set_image_description", "ou"), ("unset_image_description", ""),
        ], [])
        manager = self._interface("wp_color_manager_v1", 1, [
            ("destroy", ""), ("get_output", "no", [output, None]), ("get_surface", "no", [surface, None]),
            ("get_surface_feedback", "no"), ("create_icc_creator", "n"),
            ("create_parametric_creator", "n", [creator]), ("create_windows_scrgb", "n"),
        ], [("supported_intent", "u"), ("supported_feature", "u"), ("supported_tf_named", "u"),
            ("supported_primaries_named", "u"), ("done", "")])
        self.info, self.description, self.output, self.manager = info, description, output, manager
        self.creator, self.surface = creator, surface

    def _messages(self, specs):
        array = (_WlMessage * max(1, len(specs)))()
        for i, spec in enumerate(specs):
            name, signature = spec[0], spec[1]
            count = sum(1 for c in signature if c.isalpha())
            types = list(spec[2]) if len(spec) > 2 else []
            types += [None] * (count - len(types))
            type_array = (c_void_p * max(1, count))(
                *[ctypes.addressof(t) if t is not None else None for t in types])
            self._keep.append(type_array)
            array[i] = _WlMessage(name.encode(), signature.encode(), cast(type_array, POINTER(c_void_p)))
        self._keep.append(array)
        return array

    def _interface(self, name, version, methods, events):
        interface = _WlInterface()
        self._keep.append(interface)
        interface.name = name.encode()
        interface.version = version
        interface.method_count = len(methods)
        interface.methods = cast(self._messages(methods), POINTER(_WlMessage))
        interface.event_count = len(events)
        interface.events = cast(self._messages(events), POINTER(_WlMessage))
        return interface


_wayland = None


def _libwayland():
    global _wayland
    if _wayland is None:
        lib = ctypes.CDLL("libwayland-client.so.0")
        lib.wl_display_create_queue.restype = c_void_p
        lib.wl_display_create_queue.argtypes = [c_void_p]
        lib.wl_event_queue_destroy.argtypes = [c_void_p]
        lib.wl_proxy_create_wrapper.restype = c_void_p
        lib.wl_proxy_create_wrapper.argtypes = [c_void_p]
        lib.wl_proxy_wrapper_destroy.argtypes = [c_void_p]
        lib.wl_proxy_set_queue.argtypes = [c_void_p, c_void_p]
        lib.wl_proxy_marshal_array_flags.restype = c_void_p
        lib.wl_proxy_marshal_array_flags.argtypes = [
            c_void_p, c_uint32, c_void_p, c_uint32, c_uint32, POINTER(_WlArgument)]
        lib.wl_proxy_add_listener.restype = c_int
        lib.wl_proxy_add_listener.argtypes = [c_void_p, c_void_p, c_void_p]
        lib.wl_proxy_destroy.argtypes = [c_void_p]
        lib.wl_proxy_get_version.restype = c_uint32
        lib.wl_proxy_get_version.argtypes = [c_void_p]
        lib.wl_display_roundtrip_queue.restype = c_int
        lib.wl_display_roundtrip_queue.argtypes = [c_void_p, c_void_p]
        lib.wl_display_flush.restype = c_int
        lib.wl_display_flush.argtypes = [c_void_p]
        lib.wl_proxy_get_class.restype = c_char_p
        lib.wl_proxy_get_class.argtypes = [c_void_p]
        lib.wl_proxy_get_id.restype = c_uint32
        lib.wl_proxy_get_id.argtypes = [c_void_p]
        _wayland = (lib, _Protocol(),
                    _WlInterface.in_dll(lib, "wl_registry_interface"),
                    _WlInterface.in_dll(lib, "wl_output_interface"))
    return _wayland


class _WaylandOutputs:
    """The outputs' image descriptions, on an event queue of our own.

    The registry, the colour manager and the outputs are bound once for the
    life of the connection: wl_registry has no destroy request, so a registry
    per query stayed with the compositor (and kept receiving global events)
    until the client went. A query creates only the image descriptions.
    """

    def __init__(self, display: int):
        self.lib, self.protocol, self.registry_interface, self.output_interface = _libwayland()
        self.display = display
        self.callbacks = []  # ctypes callbacks and listener arrays of the lasting objects
        self.query_callbacks = []  # those of the last query's objects
        self.manager = None
        # What the colour manager supports (its supported_* events).
        self.intents, self.features, self.tfs, self.primaries = set(), set(), set(), set()
        self.outputs: dict[int, dict] = {}  # by global name
        self.queue = self.lib.wl_display_create_queue(display)
        self.wrapper = self.lib.wl_proxy_create_wrapper(display)
        self.lib.wl_proxy_set_queue(self.wrapper, self.queue)
        # wl_display.get_registry (opcode 1)
        self.registry = self.request(self.wrapper, 1, self.registry_interface, ("n", 0))
        self.listen(self.registry, ["usu", "u"], [self.on_global, self.on_global_remove], self.callbacks)
        self.roundtrip()  # the globals, binding as they arrive
        self.roundtrip()  # the outputs' names

    def listen(self, proxy, signatures, handlers, keep):
        functions = []
        for signature, handler in zip(signatures, handlers):
            argtypes = [c_void_p, c_void_p] + [_CTYPE[c] for c in signature if c.isalpha()]
            functions.append(CFUNCTYPE(None, *argtypes)(handler))
        array = (c_void_p * len(functions))(*[cast(f, c_void_p) for f in functions])
        keep += [functions, array]
        self.lib.wl_proxy_add_listener(proxy, array, None)

    def request(self, proxy, opcode, interface=None, *args, destroy=False, version=None):
        values = (_WlArgument * max(1, len(args)))()
        for i, (kind, value) in enumerate(args):
            setattr(values[i], kind, value)
        if version is None:  # a new object takes its factory's version
            version = self.lib.wl_proxy_get_version(proxy)
        return self.lib.wl_proxy_marshal_array_flags(
            proxy, opcode, ctypes.addressof(interface) if interface is not None else None,
            version, _WL_MARSHAL_FLAG_DESTROY if destroy else 0, values)

    def roundtrip(self) -> None:
        if self.lib.wl_display_roundtrip_queue(self.display, self.queue) < 0:
            raise OSError("wayland roundtrip failed")

    def bind(self, name, interface, version):
        # wl_registry.bind (opcode 0, "usun"): the interface name rides along.
        return self.request(self.registry, 0, interface,
                            ("u", name), ("s", interface.name), ("u", version), ("n", 0), version=version)

    def on_global(self, _data, _registry, name, interface, version):
        interface = interface.decode()
        ignore = lambda *_: None  # noqa: E731
        if interface == "wp_color_manager_v1" and self.manager is None:
            self.manager = self.bind(name, self.protocol.manager, 1)
            self.listen(self.manager, ["u", "u", "u", "u", ""], [
                lambda _d, _p, value: self.intents.add(value),
                lambda _d, _p, value: self.features.add(value),
                lambda _d, _p, value: self.tfs.add(value),
                lambda _d, _p, value: self.primaries.add(value),
                ignore,
            ], self.callbacks)
            for output in self.outputs.values():
                self.manage(output)
        elif interface == "wl_output":
            version = min(version, 4)
            proxy = self.bind(name, self.output_interface, version)
            output = self.outputs[name] = {"proxy": proxy, "version": version, "name": None}
            signatures = ["iiiiissi", "uiii", "", "i", "s", "s"][:6 if version >= 4 else 4 if version >= 2 else 2]
            handlers = [ignore, ignore, ignore, ignore,
                        lambda _d, _p, value: output.update(name=(value or b"").decode()), ignore]
            self.listen(proxy, signatures, handlers[:len(signatures)], self.callbacks)
            if self.manager is not None:
                self.manage(output)

    def manage(self, output):
        """The output's wp_color_management_output_v1 (get_output, opcode 1)."""
        output["cm_output"] = self.request(self.manager, 1, self.protocol.output, ("n", 0), ("o", output["proxy"]))
        self.listen(output["cm_output"], [""], [lambda *_: None], self.callbacks)

    def on_global_remove(self, _data, _registry, name):
        output = self.outputs.pop(name, None)
        if output is None:
            return
        if output.get("cm_output"):
            self.request(output["cm_output"], 0, destroy=True)
        if output["version"] >= 3:
            self.request(output["proxy"], 0, destroy=True)  # wl_output.release
        else:
            self.lib.wl_proxy_destroy(output["proxy"])

    def query(self) -> list[dict]:
        """Every output's image description, as the compositor reports it now."""
        self.roundtrip()  # outputs that came or went meanwhile
        keep = self.query_callbacks = []  # the previous query's objects are gone
        states = []
        for output in list(self.outputs.values()):
            state = {"name": output["name"], "version": output["version"]}
            states.append(state)
            if not output.get("cm_output"):
                continue
            state["description"] = self.request(output["cm_output"], 1, self.protocol.description, ("n", 0))
            self.listen(state["description"], ["us", "u"], [
                lambda _d, _p, _cause, msg, state=state: state.update(failed=(msg or b"").decode()),
                lambda _d, _p, _identity, state=state: state.update(ready=True),
            ], keep)
        try:
            self.roundtrip()
            for state in states:
                if state.get("ready"):
                    self.read_information(state, keep)
            self.roundtrip()
        finally:
            for state in states:
                if state.get("info"):
                    self.lib.wl_proxy_destroy(state.pop("info"))
                state.pop("info", None)
                if state.get("description"):
                    self.request(state.pop("description"), 0, destroy=True)
        return states

    def read_information(self, state, keep):
        state["info"] = self.request(state["description"], 1, self.protocol.info, ("n", 0))

        def done(_d, proxy):
            state["complete"] = True
            self.lib.wl_proxy_destroy(proxy)  # a destructor event
            state["info"] = None

        def icc(_d, _p, fd, _size):
            os.close(fd)

        set_ = state.__setitem__
        ignore = lambda *_: None  # noqa: E731
        self.listen(state["info"], ["", "hu", "iiiiiiii", "u", "u", "u", "uuu", "iiiiiiii", "uu", "u", "u"], [
            done, icc, ignore, lambda _d, _p, v: set_("primaries_named", v), ignore,
            lambda _d, _p, v: set_("tf_named", v),
            lambda _d, _p, lo, hi, ref: state.update(min_lum=lo / 10000, max_lum=hi, reference_lum=ref),
            ignore,
            lambda _d, _p, lo, hi: state.update(target_min_lum=lo / 10000, target_max_lum=hi),
            lambda _d, _p, v: set_("target_max_cll", v), lambda _d, _p, v: set_("target_max_fall", v),
        ], keep)


    def can_describe_pq(self) -> bool:
        """Whether a surface can be described as BT.2100 PQ with luminances."""
        return (self.manager is not None
                and {_FEATURE_PARAMETRIC, _FEATURE_SET_LUMINANCES} <= self.features
                and _TF_ST2084_PQ in self.tfs and _PRIMARIES_BT2020 in self.primaries
                and _INTENT_PERCEPTUAL in self.intents)

    def describe_pq(self, surface: int, cm_surface: int | None, peak_nits: int, reference_nits: int) -> int:
        """Describe ``surface`` (a wl_surface) as BT.2100 PQ for a display
        that peaks at ``peak_nits`` with SDR white at ``reference_nits``.

        Returns the surface's wp_color_management_surface_v1: ``cm_surface``
        when given (new luminances), else a new one. The description is
        double-buffered: the surface's next commit applies it.
        """
        if self.lib.wl_proxy_get_class(surface) != b"wl_surface":
            raise ValueError("not a wl_surface")
        minimum = round(_PQ_MIN_NITS * 10000)
        creator = self.request(self.manager, 5, self.protocol.creator, ("n", 0))  # create_parametric_creator
        self.request(creator, 1, None, ("u", _TF_ST2084_PQ))  # set_tf_named
        self.request(creator, 3, None, ("u", _PRIMARIES_BT2020))  # set_primaries_named
        # The primary volume's maximum is what Hyprland compares with the
        # output's peak to decide whether to tone-map; the protocol takes it
        # as 10000 nits for PQ, so compositors that follow it read the
        # mastering luminance (the target volume) instead.
        self.request(creator, 5, None, ("u", minimum), ("u", peak_nits), ("u", reference_nits))  # set_luminances
        if _FEATURE_SET_MASTERING_DISPLAY_PRIMARIES in self.features:
            self.request(creator, 7, None, ("u", minimum), ("u", peak_nits))  # set_mastering_luminance
        description = self.request(creator, 0, self.protocol.description, ("n", 0), destroy=True)  # create
        state, keep = {}, []
        self.listen(description, ["us", "u"], [
            lambda _d, _p, _cause, msg: state.update(failed=(msg or b"").decode()),
            lambda _d, _p, _identity: state.update(ready=True),
        ], keep)
        try:
            for _ in range(3):  # 'ready' or 'failed' within a roundtrip or two
                if state:
                    break
                self.roundtrip()
            if not state.get("ready"):
                raise RuntimeError(f"image description: {state.get('failed', 'no answer')}")
            if cm_surface is None:
                cm_surface = self.request(self.manager, 2, self.protocol.surface, ("n", 0), ("o", surface))
            # set_image_description (copied: the description can go at once)
            self.request(cm_surface, 1, None, ("o", description), ("u", _INTENT_PERCEPTUAL))
        finally:
            self.request(description, 0, destroy=True)
            self.lib.wl_display_flush(self.display)
        return cm_surface

    def release_surface(self, cm_surface: int) -> None:
        """Drop a surface's description (wp_color_management_surface_v1.destroy)."""
        self.request(cm_surface, 0, destroy=True)
        self.lib.wl_display_flush(self.display)


_outputs: _WaylandOutputs | None = None


def _wayland_client(display: int | None = None) -> _WaylandOutputs:
    global _outputs
    if display is None:
        app = QGuiApplication.instance()
        display = int(app.nativeInterface().display())
    if _outputs is None or _outputs.display != display:
        _outputs = _WaylandOutputs(display)
    return _outputs


def wayland_outputs(display: int | None = None) -> list[dict]:
    """Every output's image description: name, tf_named, luminances."""
    return _wayland_client(display).query()


def can_describe_surfaces() -> bool:
    """Whether a surface can describe itself as BT.2100 PQ with the display's
    luminances (Wayland, a compositor with parametric descriptions)."""
    if platform_kind() != "wayland":
        return False
    try:
        return _wayland_client().can_describe_pq()
    except Exception:  # noqa: BLE001 - Qt's colour space tag instead
        return False


def surface_identity(surface: int) -> tuple[int, int]:
    """A wl_surface's pointer and protocol id: a surface Qt re-creates can
    reuse the old one's memory, but not (soon) its id."""
    return surface, _libwayland()[0].wl_proxy_get_id(surface)


def describe_pq_surface(surface: int, cm_surface: int | None, peak_nits: int, reference_nits: int) -> int:
    """See _WaylandOutputs.describe_pq."""
    return _wayland_client().describe_pq(surface, cm_surface, peak_nits, reference_nits)


def release_surface(cm_surface: int) -> None:
    _wayland_client().release_surface(cm_surface)


def _wayland_query(screen_name: str | None) -> DisplayHdr:
    outputs = [o for o in wayland_outputs() if o.get("complete")]
    match = next((o for o in outputs if o.get("name") == screen_name), None)
    if match is None and len(outputs) == 1:
        match = outputs[0]
    if match is None:
        return SDR
    hdr = match.get("tf_named") in (_TF_ST2084_PQ, _TF_HLG)
    peak = match.get("target_max_lum") or match.get("max_lum")
    return DisplayHdr(
        hdr, float(peak) if peak else None,
        float(match["reference_lum"]) if match.get("reference_lum") else None,
        f"wayland {match.get('name')}",
    )


# -- Windows -------------------------------------------------------------------

class _GUID(Structure):
    _fields_ = [("Data1", c_uint32), ("Data2", c_uint16), ("Data3", c_uint16), ("Data4", c_ubyte * 8)]

    @classmethod
    def parse(cls, text: str) -> "_GUID":
        import uuid  # noqa: PLC0415
        u = uuid.UUID(text)
        return cls(u.time_low, u.time_mid, u.time_hi_version, (c_ubyte * 8)(*u.bytes[8:]))


class _RECT(Structure):
    _fields_ = [("left", c_int32), ("top", c_int32), ("right", c_int32), ("bottom", c_int32)]


class _DXGI_OUTPUT_DESC1(Structure):
    _fields_ = [
        ("DeviceName", ctypes.c_wchar * 32), ("DesktopCoordinates", _RECT), ("AttachedToDesktop", c_int32),
        ("Rotation", c_uint32), ("Monitor", c_void_p), ("BitsPerColor", c_uint32), ("ColorSpace", c_uint32),
        ("RedPrimary", c_float * 2), ("GreenPrimary", c_float * 2), ("BluePrimary", c_float * 2),
        ("WhitePoint", c_float * 2), ("MinLuminance", c_float), ("MaxLuminance", c_float),
        ("MaxFullFrameLuminance", c_float),
    ]


_IID_IDXGIFactory1 = "770aae78-f26f-4dba-a829-253c83d1b387"
_IID_IDXGIOutput6 = "068346e8-aaec-4b84-add7-137f513f77a1"
_DXGI_COLOR_SPACE_RGB_FULL_G2084_NONE_P2020 = 12


def _com(obj: c_void_p, index: int, restype, argtypes, *args):
    vtable = cast(obj, POINTER(POINTER(c_void_p)))[0]
    function = ctypes.WINFUNCTYPE(restype, c_void_p, *argtypes)(vtable[index])
    return function(obj, *args)


def _release(obj) -> None:
    if obj:
        _com(obj, 2, c_uint32, [])


def windows_outputs() -> list[dict]:
    """Every desktop output: device name, monitor, HDR colour space, luminance."""
    dxgi = ctypes.WinDLL("dxgi")
    factory = c_void_p()
    iid = _GUID.parse(_IID_IDXGIFactory1)
    if dxgi.CreateDXGIFactory1(byref(iid), byref(factory)) != 0:
        return []
    outputs = []
    try:
        adapter_index = 0
        while True:
            adapter = c_void_p()
            # IDXGIFactory1::EnumAdapters1
            if _com(factory, 12, c_int32, [c_uint32, POINTER(c_void_p)],
                    adapter_index, byref(adapter)) != 0:
                break
            try:
                output_index = 0
                while True:
                    output = c_void_p()
                    # IDXGIAdapter::EnumOutputs
                    if _com(adapter, 7, c_int32, [c_uint32, POINTER(c_void_p)], output_index, byref(output)) != 0:
                        break
                    try:
                        output6 = c_void_p()
                        iid6 = _GUID.parse(_IID_IDXGIOutput6)
                        if _com(output, 0, c_int32, [POINTER(_GUID), POINTER(c_void_p)], byref(iid6), byref(output6)) == 0:
                            try:
                                desc = _DXGI_OUTPUT_DESC1()
                                if _com(output6, 27, c_int32, [POINTER(_DXGI_OUTPUT_DESC1)], byref(desc)) == 0:
                                    outputs.append({
                                        "device": desc.DeviceName, "monitor": desc.Monitor or 0,
                                        "hdr": desc.ColorSpace == _DXGI_COLOR_SPACE_RGB_FULL_G2084_NONE_P2020,
                                        "bits": desc.BitsPerColor, "max_lum": desc.MaxLuminance,
                                        "max_full_frame_lum": desc.MaxFullFrameLuminance,
                                    })
                            finally:
                                _release(output6)
                    finally:
                        _release(output)
                    output_index += 1
            finally:
                _release(adapter)
            adapter_index += 1
    finally:
        _release(factory)
    return outputs


class _LUID(Structure):
    _fields_ = [("LowPart", c_uint32), ("HighPart", c_int32)]


class _DisplayConfigHeader(Structure):
    _fields_ = [("type", c_uint32), ("size", c_uint32), ("adapterId", _LUID), ("id", c_uint32)]


class _SourceDeviceName(Structure):
    _fields_ = [("header", _DisplayConfigHeader), ("viewGdiDeviceName", ctypes.c_wchar * 32)]


class _SdrWhiteLevel(Structure):
    _fields_ = [("header", _DisplayConfigHeader), ("SDRWhiteLevel", c_uint32)]


class _PathInfo(Structure):
    _fields_ = [
        ("sourceAdapterId", _LUID), ("sourceId", c_uint32), ("sourceModeInfoIdx", c_uint32),
        ("sourceStatusFlags", c_uint32),
        ("targetAdapterId", _LUID), ("targetId", c_uint32), ("targetModeInfoIdx", c_uint32),
        ("outputTechnology", c_uint32), ("rotation", c_uint32), ("scaling", c_uint32),
        ("refreshNumerator", c_uint32), ("refreshDenominator", c_uint32), ("scanLineOrdering", c_uint32),
        ("targetAvailable", c_int32), ("targetStatusFlags", c_uint32), ("flags", c_uint32),
    ]


def windows_sdr_white(device: str) -> float | None:
    """The SDR white level (nits) Windows composes SDR content at on ``device``
    ("\\\\.\\DISPLAY1") while HDR is on: the "SDR content brightness" slider."""
    user32 = ctypes.WinDLL("user32")
    paths, modes = c_uint32(), c_uint32()
    if user32.GetDisplayConfigBufferSizes(2, byref(paths), byref(modes)) != 0:  # QDC_ONLY_ACTIVE_PATHS
        return None
    path_array = (_PathInfo * paths.value)()
    mode_array = (c_ubyte * (64 * max(1, modes.value)))()
    if user32.QueryDisplayConfig(2, byref(paths), path_array, byref(modes), mode_array, None) != 0:
        return None
    for path in path_array[:paths.value]:
        name = _SourceDeviceName()
        name.header = _DisplayConfigHeader(1, ctypes.sizeof(_SourceDeviceName), path.sourceAdapterId, path.sourceId)
        if user32.DisplayConfigGetDeviceInfo(byref(name)) != 0 or name.viewGdiDeviceName != device:
            continue
        level = _SdrWhiteLevel()
        level.header = _DisplayConfigHeader(11, ctypes.sizeof(_SdrWhiteLevel), path.targetAdapterId, path.targetId)
        if user32.DisplayConfigGetDeviceInfo(byref(level)) == 0 and level.SDRWhiteLevel:
            return level.SDRWhiteLevel / 1000 * 80
    return None


def _windows_query(hwnd: int | None) -> DisplayHdr:
    outputs = windows_outputs()
    monitor = 0
    if hwnd:
        user32 = ctypes.WinDLL("user32")
        user32.MonitorFromWindow.restype = c_void_p
        user32.MonitorFromWindow.argtypes = [c_void_p, c_uint32]
        monitor = user32.MonitorFromWindow(hwnd, 2) or 0  # MONITOR_DEFAULTTONEAREST
    match = next((o for o in outputs if o["monitor"] == monitor), None)
    if match is None:
        return SDR
    white = windows_sdr_white(match["device"]) if match["hdr"] else None
    return DisplayHdr(match["hdr"], float(match["max_lum"]) or None, white, f"dxgi {match['device']}")


def main() -> None:
    """``python -m desktop_client.hdr_output``: what each screen reports."""
    import json  # noqa: PLC0415

    from PySide6.QtWidgets import QApplication, QWidget  # noqa: PLC0415

    app = QApplication([])
    print(json.dumps({"platform": platform_kind(app), "qt": QLibraryInfo.version().toString(),
                      "presentation_supported": presentation_supported(app)}))
    if platform_kind(app) == "wayland":
        print(json.dumps({"outputs": wayland_outputs()}, default=str))
    elif platform_kind(app) == "windows":
        print(json.dumps({"outputs": windows_outputs()}, default=str))
    window = QWidget()
    window.show()
    app.processEvents()
    for screen in app.screens():
        window.move(screen.availableGeometry().center())
        app.processEvents()
        print(json.dumps({"screen": screen.name(), **query(window).__dict__}))


if __name__ == "__main__":
    main()
