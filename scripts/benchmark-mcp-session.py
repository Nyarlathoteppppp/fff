#!/usr/bin/env python3
import argparse
from concurrent.futures import Future
from functools import partial
import json
import os
import select
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(
        description="Keep one FFF MCP index resident across benchmark clients."
    )
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--connect", action="store_true")
    parser.add_argument("--setup", type=Path)
    parser.add_argument("--repo", type=Path)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--fff", type=Path)
    args = parser.parse_args()
    if args.connect:
        connect(args.socket, json.loads(args.setup.read_text()) if args.setup else None)
        return
    if not all([args.repo, args.state, args.fff]):
        parser.error("Server mode requires --repo, --state, and --fff")
    if args.socket.exists():
        parser.error(
            "Socket already exists; stop its resident server before restarting"
        )
    args.state.mkdir(parents=True, exist_ok=True)
    (args.state / "ready.json").unlink(missing_ok=True)
    log_offsets = {p: p.stat().st_size for p in args.state.glob("fff+*.log")}
    has_git = (
        subprocess.run(
            ["git", "-C", str(args.repo), "rev-parse", "--is-inside-work-tree"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode
        == 0
    )
    command = [
        str(args.fff.resolve()),
        str(args.repo.resolve()),
        "--no-update-check",
        "--log-file",
        str(args.state / "fff.log"),
        "--frecency-db",
        str(args.state / "frecency"),
        "--history-db",
        str(args.state / "history"),
    ]
    with (args.state / "stderr.log").open("w") as stderr:
        backend = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr,
            text=True,
            bufsize=1,
        )

        def stop(_signal, _frame):
            raise SystemExit(0)

        signal.signal(signal.SIGTERM, stop)
        try:
            initialized = rpc(
                backend,
                {
                    "jsonrpc": "2.0",
                    "id": 0,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "fff-search-benchmark", "version": "1"},
                    },
                },
            )
            rpc(backend, {"jsonrpc": "2.0", "method": "notifications/initialized"})
            deadline = time.monotonic() + 600
            while True:
                logs = read_new_logs(args.state, log_offsets)
                content_ready = any(
                    "run_post_scan" in line and "close time" in line
                    for line in logs.splitlines()
                )
                git_ready = not has_git or any(
                    "refresh_git_status: fff_search::shared: close" in line
                    for line in logs.splitlines()
                )
                if content_ready and git_ready:
                    break
                if backend.poll() is not None:
                    raise RuntimeError("FFF exited during indexing")
                if time.monotonic() > deadline:
                    raise TimeoutError(
                        "FFF content indexing did not finish within 10 minutes"
                    )
                time.sleep(0.5)

            bridge = RpcBridge(backend)

            class Handler(socketserver.StreamRequestHandler):
                def handle(self):
                    write_lock = threading.Lock()
                    for line in self.rfile:
                        request = json.loads(line)
                        if request.get("method") == "initialize":
                            response = dict(initialized, id=request["id"])
                            response["result"] = dict(
                                response["result"],
                                _meta={"fff_benchmark_bridge": "multiplexed"},
                            )
                            self.send_response(write_lock, response)
                        elif request.get("method") == "notifications/initialized":
                            continue
                        else:
                            future = bridge.send(request)
                            if future is not None:
                                future.add_done_callback(
                                    partial(self.complete, write_lock, request["id"])
                                )

                def complete(self, write_lock, request_id, future):
                    try:
                        response = future.result()
                    except Exception as error:
                        response = {
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "error": {"code": -32603, "message": str(error)},
                        }
                    self.send_response(write_lock, response)

                def send_response(self, write_lock, response):
                    with write_lock:
                        try:
                            self.wfile.write((json.dumps(response) + "\n").encode())
                            self.wfile.flush()
                        except (BrokenPipeError, ConnectionResetError, ValueError):
                            return

            with socketserver.UnixStreamServer(str(args.socket), Handler) as server:
                os.chmod(args.socket, 0o600)
                (args.state / "ready.json").write_text(
                    json.dumps({"ready": True, "pid": os.getpid()})
                )
                print(
                    "FFF scan, content index, and Git metadata ready; accepting benchmark clients",
                    flush=True,
                )
                server.serve_forever()
        finally:
            backend.terminate()
            try:
                backend.wait(timeout=10)
            except subprocess.TimeoutExpired:
                backend.kill()
                backend.wait()
            args.socket.unlink(missing_ok=True)
            (args.state / "ready.json").unlink(missing_ok=True)


def rpc(backend, request):
    backend.stdin.write(json.dumps(request) + "\n")
    backend.stdin.flush()
    if "id" not in request:
        return None
    for line in backend.stdout:
        response = json.loads(line)
        if response.get("id") == request["id"]:
            return response
    raise RuntimeError("FFF closed its response stream")


class RpcBridge:
    def __init__(self, backend):
        self.backend = backend
        self.lock = threading.Lock()
        self.pending = {}
        self.next_id = 1
        self.closed = False
        threading.Thread(target=self.read_responses, daemon=True).start()

    def send(self, request):
        with self.lock:
            if self.closed:
                raise RuntimeError("FFF closed its response stream")
            future = None
            if "id" in request:
                future = Future()
                self.pending[self.next_id] = (request["id"], future)
                request = dict(request, id=self.next_id)
                self.next_id += 1
            self.backend.stdin.write(json.dumps(request) + "\n")
            self.backend.stdin.flush()
            return future

    def read_responses(self):
        try:
            for line in self.backend.stdout:
                response = json.loads(line)
                with self.lock:
                    entry = self.pending.pop(response.get("id"), None)
                if entry is not None:
                    original_id, future = entry
                    future.set_result(dict(response, id=original_id))
        finally:
            with self.lock:
                self.closed = True
                pending, self.pending = self.pending, {}
            for _, future in pending.values():
                future.set_exception(RuntimeError("FFF closed its response stream"))


def connect(path, setup=None):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.connect(str(path))
        pending = b""
        while True:
            readable, _, _ = select.select([sys.stdin.buffer, connection], [], [])
            for source in readable:
                data = os.read(source.fileno(), 65536)
                if not data:
                    return
                if source is connection:
                    if setup:
                        pending += data
                        while b"\n" in pending:
                            line, pending = pending.split(b"\n", 1)
                            response = rewrite_setup(json.loads(line), setup)
                            sys.stdout.buffer.write(
                                (json.dumps(response) + "\n").encode()
                            )
                    else:
                        sys.stdout.buffer.write(data)
                    sys.stdout.buffer.flush()
                else:
                    connection.sendall(data)


def rewrite_setup(response, setup):
    result = response.get("result", {})
    if "instructions" in result:
        result["instructions"] = setup["instructions"]
    for tool in result.get("tools", []):
        config = setup.get("tools", {}).get(tool["name"], {})
        if "description" in config:
            tool["description"] = config["description"]
        for name, description in config.get("parameters", {}).items():
            tool["inputSchema"]["properties"][name]["description"] = description
    return response


def read_new_logs(directory, offsets):
    chunks = []
    for path in directory.glob("fff+*.log"):
        with path.open() as log:
            offset = offsets.get(path, 0)
            log.seek(offset if path.stat().st_size >= offset else 0)
            chunks.append(log.read())
    return "\n".join(chunks)


if __name__ == "__main__":
    main()
