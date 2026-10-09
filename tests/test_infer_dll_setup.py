import importlib.util
import os
import sys
import types
from pathlib import Path

import upscale_cli

INFER = Path(upscale_cli.__file__).with_name("infer.py")


def load_infer(monkeypatch):
    """Execute upscale_cli/infer.py under a private name, so the import-time
    DLL setup runs without onnxruntime and without replacing the real
    module other tests import."""
    monkeypatch.setitem(sys.modules, "onnxruntime", types.ModuleType("onnxruntime"))
    spec = importlib.util.spec_from_file_location("upscale_cli._infer_dll_setup", INFER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fake_site(tmp_path):
    site = tmp_path / "site-packages"
    (site / "numpy").mkdir(parents=True)
    (site / "tensorrt_libs").mkdir()
    return site


def test_tensorrt_wheel_dirs_are_registered_on_windows_only(tmp_path, monkeypatch):
    infer = load_infer(monkeypatch)
    site = fake_site(tmp_path)
    monkeypatch.setattr(infer, "np", types.SimpleNamespace(__file__=str(site / "numpy" / "__init__.py")))
    added = []
    monkeypatch.setattr(os, "add_dll_directory", added.append, raising=False)
    monkeypatch.setenv("PATH", "original")

    infer._add_nvidia_dll_dirs()

    if os.name == "nt":
        libs = str(site / "tensorrt_libs")
        assert os.environ["PATH"] == libs + os.pathsep + "original"
        assert added == [libs]
    else:
        # Linux wheels hold .so files the dynamic loader never finds on PATH;
        # the helper used to call the Windows-only os.add_dll_directory here.
        assert os.environ["PATH"] == "original"
        assert added == []
