import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "benchmark", Path(__file__).with_name("benchmark-search.py")
)
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)
session_spec = importlib.util.spec_from_file_location(
    "session", Path(__file__).with_name("benchmark-mcp-session.py")
)
session = importlib.util.module_from_spec(session_spec)
session_spec.loader.exec_module(session)


class ScoreTrialTests(unittest.TestCase):
    def setUp(self):
        self.target = {"path": "src/example.rs", "line": 12, "symbol": "Example"}
        self.task = {"targets": [self.target]}

    def test_cached_and_reasoning_tokens_are_not_counted_twice(self):
        events = self.events([self.target])
        result = benchmark.score_trial(events, self.task, "native", Path("/repo"))
        self.assertTrue(result["success"])
        self.assertEqual(result["total_tokens"], 110)
        self.assertEqual(result["wall_s"], 8)

    def test_partial_answer_fails(self):
        task = {
            "targets": [
                self.target,
                {"path": "src/other.rs", "line": 4, "symbol": "Other"},
            ]
        }
        result = benchmark.score_trial(
            self.events([self.target]), task, "native", Path("/repo")
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["targets_found"], 1)

    def test_wrong_declaration_line_fails(self):
        findings = [dict(self.target, line=100)]
        result = benchmark.score_trial(
            self.events(findings), self.task, "native", Path("/repo")
        )
        self.assertFalse(result["success"])

    def test_extra_incorrect_declaration_fails(self):
        findings = [self.target, dict(self.target, symbol="Wrong")]
        result = benchmark.score_trial(
            self.events(findings), self.task, "native", Path("/repo")
        )
        self.assertFalse(result["success"])

    def test_direct_reads_are_allowed_in_both_arms(self):
        events = self.events([self.target])
        result = benchmark.score_trial(events, self.task, "fff", Path("/repo"))
        self.assertTrue(result["success"])
        self.assertEqual(result["fff_calls"], 0)
        events.insert(
            0,
            {
                "elapsed_s": 1,
                "event": {
                    "type": "item.completed",
                    "item": {
                        "type": "mcp_tool_call",
                        "server": "fff",
                        "tool": "grep",
                    },
                },
            },
        )
        result = benchmark.score_trial(events, self.task, "fff", Path("/repo"))
        self.assertTrue(result["success"])

    def test_rust_qualified_symbol_is_accepted(self):
        events = self.events([dict(self.target, symbol="crate::Example")])
        result = benchmark.score_trial(events, self.task, "native", Path("/repo"))
        self.assertTrue(result["success"])

    def test_incomplete_turn_is_not_successful(self):
        events = self.events([self.target])[:-1]
        result = benchmark.score_trial(events, self.task, "native", Path("/repo"))
        self.assertFalse(result["completed"])
        self.assertFalse(result["success"])

    def test_parallel_mcp_calls_are_detected(self):
        calls = []
        for kind, item_id in [
            ("started", "a"),
            ("started", "b"),
            ("completed", "a"),
            ("completed", "b"),
        ]:
            calls.append(
                {
                    "elapsed_s": 1,
                    "event": {
                        "type": f"item.{kind}",
                        "item": {
                            "id": item_id,
                            "type": "mcp_tool_call",
                            "server": "fff",
                        },
                    },
                }
            )
        result = benchmark.score_trial(
            calls + self.events([self.target]), self.task, "fff", Path("/repo")
        )
        self.assertEqual(result["mcp_parallel_requests"], 1)
        calls[1], calls[2] = calls[2], calls[1]
        result = benchmark.score_trial(
            calls + self.events([self.target]), self.task, "fff", Path("/repo")
        )
        self.assertEqual(result["mcp_parallel_requests"], 0)

    def events(self, findings):
        return [
            {
                "elapsed_s": 7,
                "event": {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "text": json.dumps({"findings": findings}),
                    },
                },
            },
            {
                "elapsed_s": 8,
                "event": {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 100,
                        "cached_input_tokens": 60,
                        "output_tokens": 10,
                        "reasoning_output_tokens": 5,
                    },
                },
            },
        ]


class SetupTests(unittest.TestCase):
    def test_bridge_preserves_ids_and_out_of_order_responses(self):
        script = (
            "import json, sys\n"
            "requests = [json.loads(sys.stdin.readline()) for _ in range(2)]\n"
            "for request in reversed(requests):\n"
            " print(json.dumps(dict(request, result=request['params'])), flush=True)\n"
        )
        with subprocess.Popen(
            [sys.executable, "-u", "-c", script],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        ) as backend:
            bridge = session.RpcBridge(backend)
            first = bridge.send({"id": 7, "method": "first", "params": "a"})
            second = bridge.send({"id": 7, "method": "second", "params": "b"})
            self.assertEqual(second.result(timeout=5)["result"], "b")
            self.assertEqual(first.result(timeout=5)["result"], "a")
            self.assertEqual(first.result()["id"], 7)
            self.assertEqual(second.result()["id"], 7)
            backend.wait(timeout=5)

    def test_readiness_ignores_previous_server_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fff+old.log"
            path.write_text("old index finished\n")
            offsets = {path: path.stat().st_size}
            with path.open("a") as log:
                log.write("new index started\n")
            self.assertEqual(
                session.read_new_logs(Path(directory), offsets), "new index started\n"
            )

    def test_setup_changes_metadata_without_changing_search_results(self):
        setup = {
            "instructions": "New instructions",
            "tools": {
                "grep": {
                    "description": "New description",
                    "parameters": {"query": "Scoped query"},
                }
            },
        }
        response = {
            "id": 1,
            "result": {
                "tools": [
                    {
                        "name": "grep",
                        "description": "Old description",
                        "inputSchema": {"properties": {"query": {"type": "string"}}},
                    }
                ],
            },
        }
        changed = session.rewrite_setup(response, setup)
        tool = changed["result"]["tools"][0]
        self.assertEqual(tool["description"], "New description")
        self.assertEqual(
            tool["inputSchema"]["properties"]["query"],
            {
                "type": "string",
                "description": "Scoped query",
            },
        )
        search = {"id": 2, "result": {"content": [{"text": "src/example.rs:12"}]}}
        before = json.dumps(search)
        self.assertEqual(json.dumps(session.rewrite_setup(search, setup)), before)


if __name__ == "__main__":
    unittest.main()
