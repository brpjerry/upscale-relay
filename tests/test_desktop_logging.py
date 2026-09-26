import json
import time

from desktop_client.diagnostics import ClientLog, redact


def test_log_redacts_before_queueing_and_bounds_queue_and_lines(tmp_path):
    log = ClientLog(tmp_path, capacity=2)
    log.enabled = True  # inspect the producer queue without starting a writer
    for _ in range(5):
        log.record("error", detail="https://host/media/secret?token=abc token=abc Bearer xyz", large="x" * 9000)
    assert log.queue.qsize() == 2
    line = log.queue.get_nowait()
    assert len(line) <= 8193
    assert "secret" not in line and "abc" not in line and "xyz" not in line


def test_rollover_retains_ten_client_files_and_flushes_on_close(tmp_path):
    unrelated = tmp_path / "upscale-relay-server.log"
    unrelated.write_text("server")
    log = ClientLog(tmp_path, max_bytes=200)
    log.enable(True)
    for index in range(30):
        log.record("sample", index=index, detail="x" * 110)
    log.close()
    files = list(tmp_path.glob("upscale-relay-client-*.log"))
    assert len(files) == 10
    assert log.error is None
    assert unrelated.read_text() == "server"
    assert "application_close" in sorted(files)[-1].read_text()


def test_writer_failure_does_not_raise_into_producer(tmp_path):
    root = tmp_path / "file"
    root.write_text("cannot be a directory")
    log = ClientLog(root)
    log.enable(True)
    log.record("playback")
    log.close()
    assert log.error
    log.record("still_playing")


def test_log_handles_quoted_authorization_nested_tokens_and_valid_truncation(tmp_path):
    log = ClientLog(tmp_path)
    log.enabled = True
    log.record("error", headers={"Authorization": "Basic secret", "X-Access-Token": "also-secret"},
               message='Authorization: Basic hidden "token": "private" wss://host/media/private',
               large="'\\\"" * 9000)
    line = log.queue.get_nowait()
    parsed = json.loads(line)
    assert parsed["truncated"]
    assert len(line) <= 8193
    for secret in ("secret", "hidden", "private"):
        assert secret not in line
