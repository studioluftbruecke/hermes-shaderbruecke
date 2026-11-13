"""A browser that stops reading its DevTools pipe cannot hold a call past the deadline.

The page server answers each request with up to megabytes of base64 on the same pipe; a write that
blocks until the browser reads would hang the render beyond its hard deadline.
"""

import os
import pathlib
import sys
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from shaderbruecke import renderer  # noqa: E402


def test_a_large_send_to_a_reader_that_never_reads_ends_at_the_deadline():
    read_end, write_end = os.pipe()
    reply_read, reply_write = os.pipe()
    pipe = renderer._Pipe(write_end, reply_read, time.monotonic() + 0.5)
    outcome = []

    def send():
        try:
            pipe.send("Fetch.fulfillRequest", {"body": "A" * 2_000_000})
            outcome.append("sent")
        except renderer._Deadline:
            outcome.append("deadline")

    worker = threading.Thread(target=send, daemon=True)
    started = time.monotonic()
    worker.start()
    worker.join(5)
    assert not worker.is_alive(), "the write blocked past the deadline"
    assert outcome == ["deadline"] and time.monotonic() - started < 3
    for fd in (read_end, write_end, reply_read, reply_write):
        os.close(fd)


def test_a_reader_that_reads_gets_the_whole_message():
    read_end, write_end = os.pipe()
    reply_read, reply_write = os.pipe()
    pipe = renderer._Pipe(write_end, reply_read, time.monotonic() + 5)
    received = bytearray()

    def drain():
        while not received.endswith(b"\0"):
            received.extend(os.read(read_end, 1 << 16))

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    pipe.send("Fetch.fulfillRequest", {"body": "B" * 300_000})
    reader.join(5)
    assert received.endswith(b"\0") and received.count(b"B") == 300_000
    for fd in (read_end, write_end, reply_read, reply_write):
        os.close(fd)


def _page(tmp_path):
    page = tmp_path / "site" / "probe.html"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("<!doctype html><title>probe</title>")
    return page


def test_a_timed_out_render_closes_its_browser_and_answers_right_after_the_deadline(tmp_path, monkeypatch):
    import sb_fake_chromium as fake

    browser = fake.install(tmp_path / "bin", evaluate="hang")
    monkeypatch.setenv("SHADERBRUECKE_CHROMIUM", str(browser))
    started = time.monotonic()
    answer = renderer.run_page(_page(tmp_path), "1", timeout_s=2.0, out_dir=tmp_path / "out")
    wall = time.monotonic() - started
    assert answer["status"] == "timeout"
    # The browser is told to close even though the deadline has passed, so no grace period is
    # spent waiting for a browser that was never asked to quit.
    assert wall < 2.0 + 1.0, f"answered {wall - 2.0:.2f} s after the deadline"


def test_an_ordinary_render_still_ends_with_its_browser_closed(tmp_path, monkeypatch):
    import sb_fake_chromium as fake

    browser = fake.install(tmp_path / "bin")
    monkeypatch.setenv("SHADERBRUECKE_CHROMIUM", str(browser))
    started = time.monotonic()
    assert renderer.run_page(_page(tmp_path), "1", timeout_s=10.0, out_dir=tmp_path / "out")["status"] == "ok"
    assert time.monotonic() - started < 3
