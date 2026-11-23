"""Run with python -I after pacman installs the server package, outside the checkout.

CI has no NVIDIA GPU and installs the package with CUDA, cuDNN and the driver
only assumed (pacman --assume-installed), plus Arch's CPU ONNX Runtime. So
this proves the packaged code, commands and unit; TensorRT and NVENC are
verified on real hardware (docs/SERVER_LINUX.md).
"""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import urllib.request

PORT = 18590


def run(*command, env, check=True, timeout=300):
    result = subprocess.run(
        command, env=env, capture_output=True, text=True, timeout=timeout,
    )
    if check and result.returncode != 0:
        raise AssertionError(f"{command} exited {result.returncode}:\n{result.stdout}\n{result.stderr}")
    return result


def make_model(path: Path) -> None:
    """A 2x DepthToSpace network small enough for the CPU provider."""
    import numpy as np
    import onnx
    from onnx import TensorProto, helper, numpy_helper

    weight = (np.random.default_rng(0).standard_normal((12, 3, 3, 3)) * 0.05).astype(np.float32)
    graph = helper.make_graph(
        [
            helper.make_node("Conv", ["input", "w"], ["features"], pads=[1, 1, 1, 1]),
            helper.make_node("DepthToSpace", ["features"], ["output"], blocksize=2, mode="CRD"),
        ],
        "smoke2x",
        [helper.make_tensor_value_info("input", TensorProto.FLOAT, [1, 3, "h", "w"])],
        [helper.make_tensor_value_info("output", TensorProto.FLOAT, [1, 3, "h2", "w2"])],
        [numpy_helper.from_array(weight, "w")],
    )
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)], ir_version=10)
    onnx.save(model, path)
    path.with_suffix(".json").write_text(json.dumps(
        {"scale_factor": 2, "channel_order": "rgb", "value_range": [0.0, 1.0]}
    ))


def status() -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/status", timeout=2) as response:
        return json.load(response)


def stream(sample: Path, model: str, env) -> None:
    result = run(
        "relay-client", str(sample), "--port", str(PORT), "--model", model,
        "--display", "640x360", "--decode", env=env,
    )
    assert "decoded frames: 48" in result.stdout, result.stdout + result.stderr


def main():
    for module in ("relay_server", "upscale_cli", "relay_protocol", "relay_media"):
        spec = importlib.util.find_spec(module)
        assert spec is not None and Path(spec.origin).is_relative_to("/usr/lib"), module
    unit = Path("/usr/lib/systemd/user/upscale-relay-server.service")
    assert "ExecStart=/usr/bin/relay-server" in unit.read_text()

    with tempfile.TemporaryDirectory(prefix="relay-server-smoke-") as home:
        home = Path(home)
        env = dict(
            os.environ, HOME=str(home), XDG_DATA_HOME=str(home / "data"),
            XDG_CONFIG_HOME=str(home / "config"), XDG_STATE_HOME=str(home / "state"),
            QT_QPA_PLATFORM="offscreen",
            # No NVENC on CI runners.
            RELAY_LOSSLESS_HEVC_PROFILE="x265-ultrafast",
        )

        # Setup is explicit, and stops before a 4 GB download that could not
        # load: CUDA and cuDNN are only assumed here.
        result = run("relay-server", "--install-nvidia-runtime", env=env, check=False)
        assert result.returncode == 1, result.stdout + result.stderr
        assert "(package cuda)" in result.stderr, result.stderr
        assert not (home / "data" / "upscale-relay" / "runtimes").exists()

        run("relay-server-gui", "--check", env=env)

        models = home / "models"
        models.mkdir()
        make_model(models / "smoke_2x.onnx")
        sample = home / "sample.mkv"
        run("upscale-cli", "sample", str(sample), "--frames", "48", "--size", "320x180",
            "--fps", "24", env=env)

        server = subprocess.Popen(
            ["relay-server", "--models-dir", str(models), "--port", str(PORT),
             "--ep", "cpu", "--no-mdns"],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        try:
            deadline = time.monotonic() + 60
            while True:
                try:
                    info = status()
                    break
                except OSError:
                    if server.poll() is not None or time.monotonic() > deadline:
                        raise AssertionError(server.stdout.read())
                    time.sleep(0.25)
            assert set(info["models"]) == {"passthrough", "smoke_2x"}, info["models"]
            stream(sample, "passthrough", env)
            stream(sample, "smoke_2x", env)
        finally:
            server.terminate()
            output, _ = server.communicate(timeout=30)
        print(output[-3000:])
    print("Installed server: setup guard, tray import, passthrough and model sessions passed")


if __name__ == "__main__":
    sys.exit(main())
