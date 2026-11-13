"""The render child sends nothing over the network, UDP included.

`--host-resolver-rules=MAP * ~NOTFOUND` blocks HTTP to host names and IP literals, but page
JavaScript could still open an RTCPeerConnection and send STUN over UDP: without the WebRTC
policy switch a listener received 5 binding requests on 127.0.0.1 and 5 on the host's own
address. Shaders cannot run JavaScript, so this only matters if an injection
ever reached the page script, but the README promises the network is blocked.
"""

import pathlib
import socket
import sys
import tempfile
import threading
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from shaderbruecke import renderer as RENDERER  # noqa: E402


def listen_udp(received: list) -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(0.3)

    def run():
        end = time.time() + 15
        while time.time() < end:
            try:
                received.append(sock.recvfrom(2048)[0])
            except socket.timeout:
                pass
        sock.close()

    threading.Thread(target=run, daemon=True).start()
    return sock.getsockname()[1]


@pytest.mark.chromium
def test_webrtc_sends_no_stun_packet():
    received: list = []
    port = listen_udp(received)
    script = ("(async () => { const pc = new RTCPeerConnection({iceServers: [{urls: 'stun:127.0.0.1:%d'}]});"
              " pc.createDataChannel('x'); await pc.setLocalDescription(await pc.createOffer());"
              " await new Promise(r => setTimeout(r, 3000)); return {status: 'ok'}; })()" % port)
    with tempfile.TemporaryDirectory(dir=RENDERER.short_temp_base()) as tmp:
        with RENDERER._Child(pathlib.Path(tmp), timeout_s=30.0) as child:
            session = child.open("about:blank")
            child.evaluate(script, session=session, await_promise=True)
    time.sleep(0.5)
    assert received == [], f"{len(received)} UDP packet(s) left the render child"
