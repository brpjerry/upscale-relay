"""Desktop command-line option parsing."""

from desktop_client.app import parse_args


def test_relay_flags_are_consumed_and_qt_flags_are_preserved():
    options, qt_args = parse_args([
        "--debug", "--trace", "--mpv-osc", "--no-hwdec",
        "--mpv-scripts", "--headless", "--settings-scope", "smoke",
        "-platform", "offscreen",
    ])
    assert options.debug
    assert options.trace
    assert options.mpv_osc
    assert options.no_hwdec
    assert options.mpv_scripts
    assert options.headless
    assert options.settings_scope == "smoke"
    assert qt_args == ["-platform", "offscreen"]
