"""A fake Chromium that speaks just enough DevTools protocol over fds 3 and 4 for `run_page`.

Written as an executable script into a test's temp folder; its behaviour comes from a JSON file
next to it, since the child gets no environment from the test. Each start appends its argv to
`starts.jsonl`. Modes:

- `"sandbox": "broken"`: a start without `--no-sandbox` exits at once, as Chromium does on a
  host whose kernel refuses the user namespaces its sandbox needs.
- `"read": "never"`: the browser stops reading commands (its pipe fills up).
- `"evaluate": "hang"`: the page's evaluation never answers (an endless shader), while the browser
  keeps reading and still quits on `Browser.close`.
"""

from __future__ import annotations

import json
import pathlib
import stat
import sys

SCRIPT = r'''
import json, os, sys, time
here = os.path.dirname(os.path.abspath(sys.argv[0]))
config = json.load(open(os.path.join(here, "fake-chromium.json")))
with open(os.path.join(here, "starts.jsonl"), "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\n")
if config.get("sandbox") == "broken" and "--no-sandbox" not in sys.argv:
    sys.stderr.write("No usable sandbox!\n")
    sys.exit(1)
if config.get("read") == "never":
    time.sleep(60)
    sys.exit(0)
inp, out = os.fdopen(3, "rb"), os.fdopen(4, "wb")
url, buf = "about:blank", b""
while True:
    chunk = inp.read1(1 << 16)
    if not chunk:
        break
    buf += chunk
    while b"\0" in buf:
        raw, buf = buf.split(b"\0", 1)
        msg = json.loads(raw)
        method, params, result = msg.get("method"), msg.get("params") or {}, {}
        if method == "Target.createTarget":
            result = {"targetId": "t1"}
        elif method == "Target.attachToTarget":
            result = {"sessionId": "s1"}
        elif method == "Page.navigate":
            url = params.get("url")
        elif method == "Runtime.evaluate":
            if "document.readyState" in params.get("expression", ""):
                value = json.dumps({"state": "complete", "href": url, "errors": []})
            elif config.get("evaluate") == "hang":
                continue
            else:
                value = {"status": "ok"}
            result = {"result": {"value": value}}
        elif method == "Browser.close":
            out.write(json.dumps({"id": msg["id"], "result": {}}).encode() + b"\0"); out.flush()
            sys.exit(0)
        out.write(json.dumps({"id": msg["id"], "result": result}).encode() + b"\0")
        out.flush()
'''


def install(folder: pathlib.Path, **config) -> pathlib.Path:
    """The fake browser in `folder`; point `SHADERBRUECKE_CHROMIUM` at the returned path."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "fake-chromium.json").write_text(json.dumps(config), encoding="utf-8")
    script = folder / "fake-chromium"
    script.write_text(f"#!{sys.executable}\n{SCRIPT}", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def starts(folder: pathlib.Path) -> list[list[str]]:
    log = folder / "starts.jsonl"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
