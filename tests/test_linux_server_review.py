"""Linux server runtime selection, shutdown and setup behaviour."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from ports import free_port_pair
from relay_server import runtime_bootstrap as runtime

ROOT = Path(__file__).resolve().parents[1]


# Bugs caught: 1, False positives: 0
def test_tray_import_leaves_onnxruntime_unimported():
    # Importing the tray (and reading the saved provider) used to import the
    # system onnxruntime before the tray activated the managed runtime, and a
    # module once imported cannot be replaced by putting the runtime first.
    pytest.importorskip("PySide6")
    pytest.importorskip("qasync")
    code = (
        "import sys\n"
        "import relay_server.tray\n"
        "from relay_server.gui_settings import ServerSettings\n"
        "ServerSettings(scope='test-import-order').requested_ep\n"
        "assert 'onnxruntime' not in sys.modules, 'onnxruntime imported early'\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True,
        env=dict(os.environ, QT_QPA_PLATFORM="offscreen"), timeout=60,
    )
    assert result.returncode == 0, result.stderr


# Bugs caught: 1, False positives: 0
def test_linux_source_probe_needs_a_gpu_unless_cpu_was_asked_for(monkeypatch):
    # A CPU-only system onnxruntime passed the probe under "auto", so the
    # Linux tray skipped the GPU setup while the pipeline refused CPU.
    ort = pytest.importorskip("onnxruntime")
    pytest.importorskip("onnx")
    from upscale_cli.infer_worker import is_gpu_provider

    if any(is_gpu_provider(name) for name in ort.get_available_providers()):
        pytest.skip("needs a CPU-only onnxruntime")
    monkeypatch.setattr(runtime, "_LINUX", True)
    with pytest.raises(RuntimeError, match="GPU inference"):
        runtime._validate_source_runtime("auto")
    runtime._validate_source_runtime("cpu")


# Bugs caught: 1, False positives: 0
def test_linux_validation_exercises_tensorrt_and_cuda(monkeypatch, tmp_path):
    import types

    monkeypatch.setattr(runtime, "_LINUX", True)
    monkeypatch.setattr(runtime, "activate_runtime", lambda *a, **k: True)
    fake = types.SimpleNamespace(get_available_providers=lambda: sorted(runtime._REQUIRED_PROVIDERS))
    monkeypatch.setitem(sys.modules, "onnxruntime", fake)
    monkeypatch.setattr(runtime, "verify_native_runtime", lambda target: None)
    checked = []
    monkeypatch.setattr(runtime, "_validate_source_runtime", checked.append)
    runtime._validate_runtime(tmp_path)
    assert checked == ["tensorrt", "cuda"]


# Bugs caught: 1, False positives: 1
def test_relative_xdg_data_home_is_ignored(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime, "_LINUX", True)
    monkeypatch.delenv("UPSCALE_RELAY_RUNTIME_DIR", raising=False)
    # Not HOME: Windows' Path.home() reads USERPROFILE.
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", "relative/data")
    assert runtime.runtime_base_dir() == tmp_path / ".local/share/upscale-relay/runtimes"
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    assert runtime.runtime_base_dir() == tmp_path / "data/upscale-relay/runtimes"


# Bugs caught: 1, False positives: 0
def test_install_flag_exists_only_on_linux(monkeypatch):
    from relay_server.server import build_arg_parser

    monkeypatch.setattr(sys, "platform", "linux")
    assert build_arg_parser().parse_args(["--install-nvidia-runtime"]).install_nvidia_runtime
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(SystemExit):
        build_arg_parser().parse_args(["--install-nvidia-runtime"])


# Bugs caught: 1, False positives: 0
@pytest.mark.skipif(sys.platform == "win32", reason="SIGTERM is POSIX")
def test_sigterm_stops_the_server_cleanly(tmp_path):
    # systemctl --user stop sends SIGTERM, which ended the process without
    # RelayServer.stop(): no session teardown, no mDNS goodbye, temporary
    # subtitle indexes left behind.
    port = free_port_pair()
    (tmp_path / "models").mkdir()
    server = subprocess.Popen(
        [sys.executable, "-m", "relay_server.server", "--models-dir", str(tmp_path / "models"),
         "--port", str(port), "--no-mdns", "--no-display-mapping"],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        env=dict(os.environ, UPSCALE_RELAY_LOCK_DIR=str(tmp_path / "state"),
                 UPSCALE_RELAY_RUNTIME_DIR=str(tmp_path / "runtimes")),
    )
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/status", timeout=1) as response:
                    assert json.load(response)["server"] == "upscale-relay"
                break
            except OSError:
                assert server.poll() is None and time.monotonic() < deadline
                time.sleep(0.1)
        server.send_signal(signal.SIGTERM)
        output, _ = server.communicate(timeout=30)
    finally:
        if server.poll() is None:
            server.kill()
            server.communicate()
    assert server.returncode == 0, output
    assert "server stopped" in output


# Bugs caught: 0, False positives: 0
def test_model_sessions_cache_engines_beside_the_model(tmp_path, monkeypatch):
    # The cache moved from ./models to the model's folder; nothing else
    # checked that OnnxUpscaler forwards it.
    pytest.importorskip("onnxruntime")
    onnx = pytest.importorskip("onnx")
    import numpy as np
    from onnx import TensorProto, helper, numpy_helper

    from upscale_cli import infer

    models = tmp_path / "models"
    models.mkdir()
    weight = np.zeros((12, 3, 1, 1), np.float32)
    graph = helper.make_graph(
        [helper.make_node("Conv", ["input", "w"], ["f"]),
         helper.make_node("DepthToSpace", ["f"], ["output"], blocksize=2, mode="CRD")],
        "g",
        [helper.make_tensor_value_info("input", TensorProto.FLOAT, [1, 3, "h", "w"])],
        [helper.make_tensor_value_info("output", TensorProto.FLOAT, [1, 3, "h2", "w2"])],
        [numpy_helper.from_array(weight, "w")],
    )
    onnx.save(helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)],
                                ir_version=10), models / "x2.onnx")
    (models / "x2.json").write_text(json.dumps(
        {"scale_factor": 2, "channel_order": "rgb", "value_range": [0.0, 1.0]}))
    seen = []
    real = infer.resolve_providers
    monkeypatch.setattr(infer, "resolve_providers",
                        lambda ep, trt_cache=None: seen.append(trt_cache) or real(ep, trt_cache))
    infer.OnnxUpscaler(str(models / "x2.onnx"), ep="cpu")
    assert seen == [models / ".trt_cache"]


# Bugs caught: 1, False positives: 0
@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0,
                    reason="POSIX permissions; root writes anywhere")
def test_unwritable_models_folder_disables_the_engine_cache(tmp_path):
    # A read-only models folder used to fail every TensorRT session at mkdir.
    import importlib.util
    import types

    spec = importlib.util.spec_from_file_location(
        "upscale_cli._infer_cache_check", ROOT / "upscale_cli" / "infer.py")
    module = importlib.util.module_from_spec(spec)
    saved = sys.modules.get("onnxruntime")
    sys.modules["onnxruntime"] = types.ModuleType("onnxruntime")
    try:
        spec.loader.exec_module(module)
    finally:
        if saved is None:
            sys.modules.pop("onnxruntime", None)
        else:
            sys.modules["onnxruntime"] = saved
    models = tmp_path / "models"
    models.mkdir()
    models.chmod(0o555)
    try:
        options = module._trt_options(models / ".trt_cache")
    finally:
        models.chmod(0o755)
    assert options["trt_engine_cache_enable"] is False
    assert options["trt_timing_cache_enable"] is False
    assert "trt_engine_cache_path" not in options


# Bugs caught: 0, False positives: 0
def test_saved_provider_missing_from_this_build_probes_as_auto(monkeypatch):
    # The tray hands the probe its saved preference. A saved "tensorrt" in a
    # build without it used to fail the probe, and the tray then offered (on
    # Windows, started) the multi-gigabyte runtime download although the
    # server would simply run auto on what is installed.
    pytest.importorskip("onnxruntime")
    pytest.importorskip("onnx")
    monkeypatch.setattr(runtime, "_LINUX", False)
    runtime._validate_source_runtime("dml", saved_choice=True)
    with pytest.raises(RuntimeError, match="unavailable"):
        runtime._validate_source_runtime("dml")


def test_a_marker_of_the_wrong_type_is_not_ready(monkeypatch, tmp_path):
    monkeypatch.setenv("UPSCALE_RELAY_RUNTIME_DIR", str(tmp_path))
    target = tmp_path / runtime.RUNTIME_STACK_ID
    target.mkdir()
    for text in ("null", "[]", '"ready"'):
        (target / ".runtime-ready.json").write_text(text)
        assert runtime.runtime_ready() is False


def test_activation_moves_a_listed_runtime_first(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime, "_LINUX", True)
    monkeypatch.setenv("UPSCALE_RELAY_RUNTIME_DIR", str(tmp_path))
    target = tmp_path / runtime.RUNTIME_STACK_ID
    target.mkdir()
    (target / ".runtime-ready.json").write_text(json.dumps({"stack_id": runtime.RUNTIME_STACK_ID}))
    monkeypatch.setattr(sys, "path", ["/usr/lib/site-packages", str(target)])
    monkeypatch.delenv("LD_LIBRARY_PATH", raising=False)
    assert runtime.activate_runtime()
    assert sys.path == [str(target), "/usr/lib/site-packages"]


def test_relative_xdg_state_home_is_ignored(monkeypatch, tmp_path):
    from relay_server import instance_lock

    if sys.platform == "win32":
        pytest.skip("XDG on Linux")
    monkeypatch.delenv("UPSCALE_RELAY_LOCK_DIR", raising=False)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", "relative")
    assert instance_lock.lock_dir() == tmp_path / ".local/state/upscale-relay"


# Bugs caught: 0, False positives: 0
def test_tray_models_default_is_the_units_folder(monkeypatch, tmp_path):
    # The unit cannot follow XDG_DATA_HOME, so neither may the tray's
    # default, or switching from the service to the tray loses the models.
    pytest.importorskip("PySide6")
    if not sys.platform.startswith("linux"):
        pytest.skip("Linux default")
    from relay_server.gui_settings import default_models_dir

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "elsewhere"))
    unit = (ROOT / "packaging/arch/upscale-relay-server.service").read_text()
    assert "--models-dir %h/.local/share/upscale-relay/models" in unit
    assert default_models_dir() == str(tmp_path / ".local/share/upscale-relay/models")
