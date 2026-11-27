import http.server
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from textfold.agents import CODEX_DOCUMENT_CONFIG, CODEX_LEAN

npm_modules = Path(os.environ["RUNNER_TEMP"]) / "textfold-codex" / "node_modules"
name = "codex.exe" if os.name == "nt" else "codex"
(executable,) = [p for p in npm_modules.rglob(name) if p.is_file() and "vendor" in p.parts]
seen = []
reply = '{"ok": true}'
image_path = None
image_receipts = []
example_png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNgYAAAAAMAASsJTYQAAAAASUVORK5CYII=")

class Fixture(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def response(self, media, body):
        self.send_response(200)
        self.send_header("Content-Type", media)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.response("application/json", b'{"models":[],"data":[]}')

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        seen.append([tool.get("name", tool.get("type", "")) for tool in payload.get("tools", [])])
        item = {"type":"message","id":"msg_fixture","role":"assistant","status":"completed",
                "content":[{"type":"output_text","text":reply,"annotations":[]}]}
        if image_path:
            receipt = next((part for part in payload.get("input", [])
                            if part.get("type") == "function_call_output" and part.get("call_id") == "fixture_image"), None)
            if receipt is None:
                item = {"type": "function_call", "id": "fc_fixture", "call_id": "fixture_image",
                        "name": "view_image", "status": "completed", "arguments": json.dumps({"path": str(image_path)})}
            else:
                image_receipts.append(receipt.get("output"))
        messages = [
            {"type":"response.created","response":{"id":"resp_fixture","status":"in_progress","output":[]}},
            {"type":"response.output_item.added","output_index":0,"item":item},
            {"type":"response.output_item.done","output_index":0,"item":item},
            {"type":"response.completed","response":{"id":"resp_fixture","status":"completed","output":[item],
                         "usage":{"input_tokens":40,"output_tokens":10,"total_tokens":50}}},
        ]
        body = b"".join(b"data: " + json.dumps(event).encode() + b"\n\n" for event in messages)
        self.response("text/event-stream", body)

env = dict(os.environ)
for key in list(env):
    if (key.startswith(("OPENAI_", "CHATGPT_")) and any(x in key for x in ("KEY", "TOKEN", "SECRET", "BASE", "URL"))):
        del env[key]
env["NO_PROXY"] = "127.0.0.1,localhost"
env["no_proxy"] = env["NO_PROXY"]

with http.server.ThreadingHTTPServer(("127.0.0.1", 0), Fixture) as server:
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    with tempfile.TemporaryDirectory() as tmp:
        cwd = Path(tmp)
        schema = cwd / "schema.json"
        schema.write_text('{"type":"object","properties":{"ok":{"type":"boolean"}},"required":["ok"],"additionalProperties":false}')
        route = f'model_providers.fixture={{name="Fixture",base_url="http://127.0.0.1:{server.server_port}/v1",wire_api="responses",requires_openai_auth=false}}'
        sibling = cwd.parent / f"{cwd.name}-isolated-profile"
        sibling.mkdir()
        inner_image = cwd / "sample.png"
        outer_image = sibling / "sample.png"
        inner_image.write_bytes(example_png)
        outer_image.write_bytes(example_png)
        for use_web, path in ((False, None), (True, None), (False, inner_image), (False, outer_image)):
            seen.clear()
            image_receipts.clear()
            image_path = path
            opts = list(CODEX_DOCUMENT_CONFIG) + ['model_provider="fixture"',route,'model_reasoning_effort="low"']
            if not use_web:
                opts.append('web_search="disabled"')
            opts.extend([f'sqlite_home={json.dumps(str(cwd / "state"))}', f'log_dir={json.dumps(str(cwd / "logs"))}'])
            configs = [part for option in opts for part in ("-c", option)]
            args = [str(executable), *(["--search"] if use_web else []), "exec", *CODEX_LEAN, *configs,
                    "--strict-config","--ignore-user-config","--ignore-rules","--ephemeral","--skip-git-repo-check",
                    "-m","gpt-5.5","--output-schema",str(schema),"-"]
            run = subprocess.run(args, input="Return the example boolean as JSON.", capture_output=True,
                                 timeout=20, encoding="utf-8", cwd=tmp, env=env)
            print("Web request enabled:", use_web, "exit:", run.returncode, "tool lists:", seen)
            if run.returncode:
                print("\n".join(line for line in run.stderr.splitlines()
                     if any(s in line.lower() for s in ("failed", "error", "sandbox", "invalid")))[-1200:])

            assert run.returncode == 0 and seen
            tools = {tool for request in seen for tool in request}
            assert not tools.intersection({"shell", "shell_command", "exec_command", "write_stdin", "create_goal", "update_goal"})
            assert any(tool.startswith("web_search") for tool in tools) == use_web
            if path:
                assert len(image_receipts) == 1
                receipt = json.dumps(image_receipts[0])
                received_image = "data:image/" in receipt
                print("Fixture in scratch:", path == inner_image, "image returned:", received_image, flush=True)
                if path == outer_image:
                    assert not received_image
                    print("Outside fixture response:", receipt[:220].replace(str(outer_image), "(fixture)"), flush=True)
        outer_image.unlink()
        sibling.rmdir()

    server.shutdown()

if os.name == "nt":
    # The native file tool above exercises the Windows path through the actual Codex generation.
    raise SystemExit(0)

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    scratch = root / "scratch"
    scratch.mkdir()
    inside = scratch / "allowed.txt"
    outside = root / "protected.txt"
    inside.write_text("scratch fixture")
    outside.write_text("another fixture")
    runtimes = {Path(sys.executable).resolve().parent, executable.resolve().parent}
    runtime_rules = ",".join(json.dumps(str(path)) + '="read"' for path in sorted(runtimes))
    profile = 'permissions.textfold-document.filesystem={":minimal"="read",":workspace_roots"="read",' + runtime_rules + '}'
    check = """from pathlib import Path
import sys
assert Path('allowed.txt').read_text() == 'scratch fixture'
try:
    Path('allowed.txt').write_text('must stay read-only')
except OSError:
    pass
else:
    raise SystemExit('The profile allowed a write in the scratch directory')
try:
    Path(sys.argv[1]).read_text()
except OSError:
    pass
else:
    raise SystemExit('The profile allowed a read of the outside fixture')
"""
    subprocess.run([str(executable), "sandbox", "-P", "textfold-document", "-C", str(scratch),
                    "-c", profile, "-c", "permissions.textfold-document.network.enabled=false",
                    "--", sys.executable, "-I", "-S", "-c", check, str(outside)],
                   timeout=60, check=True)
print("The scratch fixture was read-only and the separate fixture was denied.")
