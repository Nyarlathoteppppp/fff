#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import random
import selectors
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(
        description="Compare Codex search with FFF MCP and shell tools."
    )
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--tasks", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--fff", type=Path, default=Path("target/release/fff-mcp"))
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--mcp-socket", type=Path)
    parser.add_argument("--setup", type=Path)
    parser.add_argument("--tool-exposure", choices=["default", "direct"], default="default")
    parser.add_argument(
        "--modes", nargs="+", choices=["native", "fff"], default=["native", "fff"]
    )
    parser.add_argument("--models", nargs="+", default=["gpt-6.1-sol", "gpt-6-astra"])
    parser.add_argument("--effort", default="medium")
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--seed", type=int, default=20261015)
    args = parser.parse_args()
    if args.setup and not args.mcp_socket:
        parser.error("--setup requires a resident --mcp-socket")
    if args.repeats < 1 or args.timeout < 1:
        parser.error("--repeats and --timeout must be positive")
    args.repo = args.repo.resolve()
    args.fff = args.fff.resolve(strict=True)
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    tasks = json.loads(args.tasks.read_text())
    if len({task["id"] for task in tasks}) != len(tasks):
        parser.error("Task IDs must be unique")
    for task in tasks:
        for target in task["targets"]:
            source = (args.repo / target["path"]).read_text().splitlines()
            if target["symbol"] not in source[target["line"] - 1]:
                parser.error(f"Stale target in {task['id']}")
    schema = {
        "type": "object",
        "properties": {
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "line": {"type": "integer"},
                        "symbol": {"type": "string"},
                    },
                    "required": ["path", "line", "symbol"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["findings"],
        "additionalProperties": False,
    }
    schema_path = args.output / "answer-schema.json"
    schema_path.write_text(json.dumps(schema))
    manifest = {
        "protocol_version": 3,
        "verification": "Numbered source excerpts from search or read are sufficient; reread only for missing evidence.",
        "mcp_tool_exposure": args.tool_exposure,
        "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "bridge_sha256": hashlib.sha256(
            Path(__file__).with_name("benchmark-mcp-session.py").read_bytes()
        ).hexdigest(),
        "bridge_concurrency": resident_bridge_mode(args.mcp_socket),
        "modes": args.modes,
        "setup_sha256": hashlib.sha256(args.setup.read_bytes()).hexdigest()
        if args.setup
        else None,
        "models": args.models,
        "effort": args.effort,
        "repeats": args.repeats,
        "timeout_seconds": args.timeout,
        "seed": args.seed,
        "fff_sha256": hashlib.sha256(args.fff.read_bytes()).hexdigest(),
        "tasks_sha256": hashlib.sha256(args.tasks.read_bytes()).hexdigest(),
        "codex_version": subprocess.check_output(
            [args.codex, "--version"], text=True
        ).strip(),
        "cache_policy": (
            "Resident MCP with completed content index; OS cache is not flushed."
            if args.mcp_socket
            else "Fresh MCP process per trial; OS cache is not flushed."
        ),
        "timing": (
            "Model session launch through final answer; FFF startup and indexing excluded."
            if args.mcp_socket
            else "Process launch through final answer; includes MCP startup and indexing."
        ),
        "resident_mcp": bool(args.mcp_socket),
        "tokens": "Input plus output as reported by Codex; cached input remains included.",
        "tasks": [{"id": t["id"], "label": t["label"]} for t in tasks],
    }
    manifest_path = args.output / "manifest.json"
    if manifest_path.exists() and json.loads(manifest_path.read_text()) != manifest:
        parser.error(
            "Output contains a different benchmark; use a new output directory"
        )
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    rng = random.Random(args.seed)
    for repeat in range(args.repeats):
        order = tasks.copy()
        rng.shuffle(order)
        for index, task in enumerate(order):
            for model_index, model in enumerate(args.models):
                modes = ["native", "fff"]
                if (repeat + index + model_index) % 2:
                    modes.reverse()
                for mode in modes:
                    if mode in args.modes:
                        run_trial(args, schema_path, task, model, mode, repeat)


def resident_bridge_mode(path):
    if path is None:
        return None
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(10)
        connection.connect(str(path))
        connection.sendall(
            b'{"jsonrpc":"2.0","id":0,"method":"initialize","params":{}}\n'
        )
        with connection.makefile("r") as stream:
            response = json.loads(stream.readline())
    return response["result"].get("_meta", {}).get("fff_benchmark_bridge", "serial")


def run_trial(args, schema_path, task, model, mode, repeat):
    trial = args.output / f"{model}-{task['id']}-{repeat + 1}-{mode}"
    trial.mkdir(exist_ok=True)
    result_path = trial / "result.json"
    if result_path.exists():
        return
    command = [
        args.codex,
        "--no-daemon",
        "exec",
        "--ignore-user-config",
        "--ephemeral",
        "--json",
        "-s",
        "read-only",
        "-m",
        model,
        "-C",
        str(args.repo),
        "--output-schema",
        str(schema_path),
        "-c",
        f"model_reasoning_effort={json.dumps(args.effort)}",
        "-c",
        'web_search="disabled"',
        "-c",
        "project_doc_max_bytes=0",
        "-c",
        "suppress_unstable_features_warning=true",
    ]
    if mode == "fff":
        mcp_args = [
            str(args.repo),
            "--no-update-check",
            "--log-file",
            str(trial / "fff.log"),
            "--frecency-db",
            str(trial / "frecency"),
            "--history-db",
            str(trial / "history"),
        ]
        mcp_command = str(args.fff)
        if args.mcp_socket:
            mcp_command = sys.executable
            mcp_args = [
                str(Path(__file__).with_name("benchmark-mcp-session.py").resolve()),
                "--connect",
                "--socket",
                str(args.mcp_socket),
            ]
            if args.setup:
                mcp_args += ["--setup", str(args.setup.resolve())]
        command += [
            "-c",
            f"mcp_servers.fff.command={json.dumps(mcp_command)}",
            "-c",
            f"mcp_servers.fff.args={json.dumps(mcp_args)}",
            "-c",
            "mcp_servers.fff.startup_timeout_sec=180",
            "-c",
            "mcp_servers.fff.tool_timeout_sec=120",
        ]
        if args.tool_exposure == "direct":
            command += ["-c", 'mcp_servers.fff.omit_tools_from=["deferred"]']
        tools = (
            "Use FFF MCP when file discovery or content search is needed. "
            "Do not use shell search as a fallback."
        )
    else:
        tools = "Use normal shell tools such as rg, find, ls, and sed for discovery and reading."
    prompt = (
        f"{task['prompt']}\n\n{tools}\n"
        "Use shell commands for direct file reads when needed. "
        "If you already know a file path, you may read it directly without searching first. "
        "Numbered source excerpts returned by search tools are sufficient source verification. "
        "Read additional source only when required evidence is missing; do not reread identical lines. "
        "Search only this checkout. Do not edit files, use network services, or delegate. "
        "Return only the requested declaration locations as JSON findings with repository-relative "
        "path, one-based declaration line, and symbol. Verify each location from source. "
        "If you cannot find a target, omit it rather than guess."
    )
    command.append(prompt)
    events = []
    started = time.monotonic()
    timed_out = False
    env = os.environ.copy()
    env["RUST_LOG"] = "error"
    print(f"START {model} {task['id']} repeat={repeat + 1} {mode}", flush=True)
    with (
        (trial / "stderr.log").open("w") as stderr,
        (trial / "events.jsonl").open("w") as log,
    ):
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=stderr,
            env=env,
            start_new_session=True,
        )
        os.set_blocking(process.stdout.fileno(), False)
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        pending = b""
        try:
            while selector.get_map():
                if time.monotonic() - started > args.timeout:
                    timed_out = True
                    os.killpg(process.pid, signal.SIGTERM)
                    break
                for key, _ in selector.select(timeout=1):
                    try:
                        chunk = os.read(key.fd, 65536)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(key.fileobj)
                        break
                    pending += chunk
                    while b"\n" in pending:
                        line, pending = pending.split(b"\n", 1)
                        event = json.loads(line)
                        entry = {
                            "elapsed_s": time.monotonic() - started,
                            "event": event,
                        }
                        events.append(entry)
                        log.write(json.dumps(entry) + "\n")
                        log.flush()
        finally:
            selector.close()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
    result = score_trial(events, task, mode, args.repo)
    result.update(
        {
            "model": model,
            "task": task["id"],
            "label": task["label"],
            "mode": mode,
            "repeat": repeat + 1,
            "timed_out": timed_out,
            "exit_code": process.returncode,
            "process_s": time.monotonic() - started,
        }
    )
    result["success"] = result["success"] and not timed_out and process.returncode == 0
    if not result["completed"]:
        result["wall_s"] = result["process_s"]
    result_path.write_text(json.dumps(result, indent=2) + "\n")
    print(
        f"DONE {model} {task['id']} {mode}: {result['wall_s']:.1f}s "
        f"tokens={result['total_tokens']} success={result['success']}",
        flush=True,
    )


def score_trial(events, task, mode, repo):
    usage = {}
    answer = ""
    completed = False
    wall_s = 0
    calls = []
    active_mcp = set()
    parallel_mcp = 0
    for entry in events:
        event = entry["event"]
        if event["type"] == "turn.completed":
            usage = event["usage"]
            completed = True
            wall_s = entry["elapsed_s"]
        item = event.get("item", {})
        if item.get("type") == "mcp_tool_call":
            if event["type"] == "item.started":
                parallel_mcp += bool(active_mcp)
                active_mcp.add(item.get("id"))
            elif event["type"] == "item.completed":
                active_mcp.discard(item.get("id"))
        if event["type"] == "item.completed":
            if item.get("type") == "agent_message":
                answer = item["text"]
            elif item.get("type") in ("command_execution", "mcp_tool_call"):
                calls.append(item)
    try:
        findings = json.loads(answer)["findings"]
    except (ValueError, KeyError, TypeError):
        findings = []
    matched = []
    for target in task["targets"]:
        matched.append(
            any(
                str(f.get("path", "")).removeprefix(str(repo) + "/").removeprefix("./")
                == target["path"]
                and f.get("symbol", "").replace("::", ".").split(".")[-1]
                == target["symbol"]
                and abs(f.get("line", -100) - target["line"]) <= 2
                for f in findings
            )
        )
    fff_calls = sum(c.get("server") == "fff" for c in calls)
    return {
        "success": all(matched) and len(findings) == len(matched) and completed,
        "completed": completed,
        "wall_s": wall_s,
        "targets_found": sum(matched),
        "targets_total": len(matched),
        "usage": usage,
        "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
        "tool_calls": len(calls),
        "fff_calls": fff_calls,
        "mcp_parallel_requests": parallel_mcp,
        "answer": answer,
    }


if __name__ == "__main__":
    main()
