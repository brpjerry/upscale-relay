"""Which of libmpv's renderers the embedded render path asks for."""

import pytest

pytest.importorskip("PySide6")
# Through the player module: on Windows it puts mpv-dev's libmpv on the path
# before python-mpv loads it.
mpv_view = pytest.importorskip("desktop_client.mpv_view")

from desktop_client.mpv_view import _open_render_context, _render_renderer, mpv


# Bugs caught: 0, False positives: 0
def test_libmpv_before_client_api_2_7_has_no_renderer_to_choose():
    assert _render_renderer({}, (2, 6)) is None
    assert _render_renderer({"RELAY_RENDERER": "gpu-next"}, (2, 6)) is None


# Bugs caught: 0, False positives: 0
def test_gpu_stays_pinned_where_libmpv_would_default_to_gpu_next():
    assert _render_renderer({}, (2, 7)) == "gpu"
    assert _render_renderer({"RELAY_RENDERER": " "}, (3, 0)) == "gpu"
    assert _render_renderer({"RELAY_RENDERER": "vulkan"}, (2, 7)) == "gpu"


# Bugs caught: 0, False positives: 0
def test_relay_renderer_selects_gpu_next_for_testing():
    assert _render_renderer({"RELAY_RENDERER": "gpu-next"}, (2, 7)) == "gpu-next"
    assert _render_renderer({"RELAY_RENDERER": "gpu"}, (2, 7)) == "gpu"


# Bugs caught: 0, False positives: 0
def test_python_mpv_can_pass_the_renderer_parameter():
    param = mpv.MpvRenderParam("renderer", "gpu-next")
    assert param.type_id == 23


class _Contexts:
    """Stands in for mpv.MpvRenderContext: records each create, fails for
    the renderers named."""

    def __init__(self, failing=()):
        self.failing = set(failing)
        self.created = []

    def __call__(self, handle, api_type, **params):
        assert api_type == "opengl"
        self.created.append(params.get("renderer"))
        if params.get("renderer") in self.failing:
            raise SystemError("MPV_ERROR_UNSUPPORTED")
        return ("ctx", params)


# Bugs caught: 0, False positives: 0
def test_renderer_is_omitted_for_older_libmpv(monkeypatch):
    contexts = _Contexts()
    monkeypatch.setattr(mpv_view.mpv, "MpvRenderContext", contexts)
    ctx, renderer = _open_render_context("handle", {"opengl_init_params": 1}, None, pytest.fail)
    assert renderer == "gpu"
    assert contexts.created == [None]
    assert ctx[1] == {"opengl_init_params": 1}


# Bugs caught: 0, False positives: 0
@pytest.mark.parametrize("renderer", ["gpu", "gpu-next"])
def test_renderer_that_starts_is_asked_for_once(monkeypatch, renderer):
    contexts = _Contexts()
    monkeypatch.setattr(mpv_view.mpv, "MpvRenderContext", contexts)
    ctx, used = _open_render_context("handle", {"opengl_init_params": 1}, renderer, pytest.fail)
    assert used == renderer
    assert contexts.created == [renderer]
    assert ctx[1] == {"opengl_init_params": 1, "renderer": renderer}


# Bugs caught: 0, False positives: 0
def test_gpu_next_that_does_not_start_falls_back_to_gpu(monkeypatch):
    contexts = _Contexts(failing={"gpu-next"})
    warnings = []
    monkeypatch.setattr(mpv_view.mpv, "MpvRenderContext", contexts)
    ctx, renderer = _open_render_context("handle", {"opengl_init_params": 1}, "gpu-next", warnings.append)
    assert renderer == "gpu"
    assert contexts.created == ["gpu-next", "gpu"]
    assert ctx[1] == {"opengl_init_params": 1, "renderer": "gpu"}
    assert len(warnings) == 1 and "gpu-next" in warnings[0]


# Bugs caught: 0, False positives: 0
def test_gpu_that_does_not_start_is_an_error(monkeypatch):
    monkeypatch.setattr(mpv_view.mpv, "MpvRenderContext", _Contexts(failing={"gpu"}))
    with pytest.raises(SystemError):
        _open_render_context("handle", {}, "gpu", pytest.fail)


# Bugs caught: 0, False positives: 0
def test_turning_logging_on_records_the_renderer_chosen_before_it():
    from types import SimpleNamespace

    from desktop_client.features import DesktopFeatures

    records = []
    log = SimpleNamespace(enable=lambda value: None, record=lambda event, **fields: records.append(fields))
    features = SimpleNamespace(settings=SimpleNamespace(), client_log=log,
                               player=SimpleNamespace(renderer="gpu"))
    DesktopFeatures._set_logging(features, False)
    assert records == []
    DesktopFeatures._set_logging(features, True)
    assert [r["message"] for r in records] == ["mpv renders with its gpu renderer"]
    features.player.renderer = None  # headless: no render context
    DesktopFeatures._set_logging(features, True)
    assert len(records) == 1
