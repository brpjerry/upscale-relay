"""The desktop's "Use passthrough for 4K or higher resolution videos" switch."""
import asyncio

import pytest
pytest.importorskip("PySide6")

from relay_client_core import is_4k_or_higher
from test_desktop_features_gui import LifecycleClient, setup_lifecycle
from test_server_library_gui import window  # noqa: F401


class SizedClient(LifecycleClient):
    """Applies passthrough_4k as RelayClient does, for one source size."""

    size = (1920, 1080)

    async def open_session(self, config):
        session = await super().open_session(config)
        swap = config.passthrough_4k and config.model != "passthrough" and is_4k_or_higher(*self.size)
        session.model = "passthrough" if swap else config.model
        session.source_size = self.size
        return session


def setup_sized(window, monkeypatch, size, model="2x_Model"):
    events, errors = setup_lifecycle(window, monkeypatch)
    SizedClient.size = size
    window.client = SizedClient(events)
    window.model_combo.addItem(model)
    window.model_combo.setCurrentText(model)
    restarts = []

    async def restart():
        restarts.append(window.settings.passthrough_4k)

    monkeypatch.setattr(window, "_restart_for_playback_setting", restart)
    return errors, restarts


def test_switch_persists_and_reaches_the_session(window, monkeypatch):
    errors, _ = setup_sized(window, monkeypatch, (3840, 2160))

    async def scenario():
        assert not window.passthrough_4k_check.isChecked()
        window.passthrough_4k_check.setChecked(True)
        await asyncio.sleep(0)
        assert window.settings.passthrough_4k
        await window._start_session("film.mkv", "server_file")
        assert window.client.opened_config.passthrough_4k
        assert window.client.opened_config.model == "2x_Model"
        assert window.now_detail.text().startswith("passthrough (4K source) · ")
        assert ("Upscale model", "passthrough (4K source)") in window._now_info[1]
        # The chosen model stays the remembered one.
        assert window.settings.model == "2x_Model"
        assert not errors

    asyncio.run(scenario())


def test_below_4k_now_playing_names_the_model(window, monkeypatch):
    errors, _ = setup_sized(window, monkeypatch, (1920, 1080))

    async def scenario():
        window.passthrough_4k_check.setChecked(True)
        await asyncio.sleep(0)
        await window._start_session("show.mkv", "server_file")
        assert window.now_detail.text().startswith("2x_Model · ")
        assert not errors

    asyncio.run(scenario())


@pytest.mark.parametrize(("size", "model", "expected"), [
    ((3840, 2160), "2x_Model", [True, False]),  # the model changes both ways
    ((1920, 1080), "2x_Model", []),             # below 4K: never
    (None, "2x_Model", [True]),                 # unmeasured server file: on restarts
    ((3840, 2160), "passthrough", []),          # passthrough chosen anyway
])
def test_toggle_restarts_only_a_stream_whose_model_changes(window, monkeypatch, size, model, expected):
    errors, restarts = setup_sized(window, monkeypatch, size or (1920, 1080), model)

    async def scenario():
        await window._start_session("film.mkv", "server_file")
        if size is None:
            window.client.session.source_size = None
        await window.on_passthrough_4k_changed(True)
        window.client.session.model = (
            "passthrough" if size and is_4k_or_higher(*size) else model
        )
        await window.on_passthrough_4k_changed(False)
        assert restarts == expected
        assert not errors

    asyncio.run(scenario())
