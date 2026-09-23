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
