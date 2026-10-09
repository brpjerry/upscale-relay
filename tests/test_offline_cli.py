from pathlib import Path

import pytest

from upscale_cli.cli import main


@pytest.mark.parametrize("alias", ["same", "relative", "symlink", "hardlink"])
def test_run_rejects_input_alias_before_loading_model(tmp_path, monkeypatch, capsys, alias):
    source = tmp_path / "source.mkv"
    original = b"source media must remain unchanged"
    source.write_bytes(original)
    target = tmp_path / "output.mkv"
    if alias == "same":
        target = source
    elif alias == "relative":
        monkeypatch.chdir(tmp_path)
        target = Path("source.mkv")
    else:
        try:
            if alias == "symlink":
                target.symlink_to(source)
            else:
                target.hardlink_to(source)
        except OSError as error:
            pytest.skip(f"filesystem does not support {alias}: {error}")
    with pytest.raises(SystemExit) as error:
        main(["run", str(source), str(target), "--model", "missing-model.onnx"])
    assert error.value.code == 2
    assert "input and output must be different files" in capsys.readouterr().err
    assert source.read_bytes() == original


def test_frame_sink_times_frames_that_carry_no_time_base(tmp_path):
    """A frame without a time base (PyAV 18 left frames flushed from a
    decoder that way) is timed by the sink's source time base, not stamped 0.
    PyAV 19 reads unset packet and stream time bases as 0/1 instead of None;
    frames still read None, and the sink accepts either."""
    from fractions import Fraction

    import av

    from upscale_cli.stages import FrameSink

    path = tmp_path / "out.mkv"
    sink = FrameSink(str(path), Fraction(1, 24), rate=Fraction(24), options={"preset": "ultrafast"})
    for index in range(3):
        frame = av.VideoFrame(64, 64, "yuv420p")
        for plane in frame.planes:
            plane.update(bytes(plane.buffer_size))
        frame.pts = index
        assert not frame.time_base
        sink.write(frame)
    sink.close()
    assert sink.pts_written == [0.0, 1 / 24, 2 / 24]
    with av.open(str(path)) as written:
        stream = written.streams.video[0]
        stamps = sorted(float(p.pts * p.time_base) for p in written.demux(stream) if p.pts is not None)
    assert stamps == pytest.approx([0.0, 1 / 24, 2 / 24], abs=0.002)
