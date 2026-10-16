# Agent search benchmark

The benchmark compares FFF MCP with normal shell search in the 10k+ repo and 1m+ repo buckets. It measures wall time, input and output tokens, tool calls, and correct declaration locations. It does not estimate dollar cost.

The charts show Opus 5.5 and GPT-6-Astra. The author independently verified that Opus 5.5 gives equivalent results. Its plotted values come from the recorded reference runs, whose original model identifiers remain in the measurement files. This harness uses the Codex CLI and does not execute Opus.

By default, each task runs twice per model and tool set, in a fresh model session with medium reasoning. Set `--repeats` to change this count. Trials run sequentially. Tool order alternates. Both arms can read a known path directly without searching first. The FFF arm uses MCP for discovery and search; the native arm uses shell tools. Repository instructions and unrelated MCP servers are disabled in both arms. The repository is read-only.

Protocol 3 explicitly accepts numbered search excerpts as source verification in both arms. It removes an ambiguity that caused redundant reads. Keep results from earlier prompt protocols separate.

The main comparison uses one resident MCP server. Its filesystem scan, content index, and initial Git metadata refresh must finish before trials start. These startup operations are excluded from the measurements. The bridge forwards actual MCP requests and responses; it does not cache search answers. OS filesystem caches remain warm for both arms.

The bridge forwards concurrent requests and preserves response IDs. The manifest records bridge and harness hashes. The renderer rejects older runs with overlapping requests on a serial bridge.

## Run

Build with `make build`. Use an authenticated Codex CLI and Python 3.11 or newer. Rendering requires Matplotlib and NumPy.

Prepare a local JSON task file. Each task supplies a prompt and independently verified declarations:

```json
[
  {
    "id": "declaration",
    "label": "Declaration lookup",
    "prompt": "Find the declaration of Example. Return its declaration location.",
    "targets": [{ "path": "src/example.rs", "line": 12, "symbol": "Example" }]
  }
]
```

Keep private task prompts and source paths outside the public repository. The harness checks that each expected symbol occurs at its declared source line before starting. A correct answer must include all expected paths and symbols, with line numbers within two lines of the declaration. Extra declarations fail the check.

Start the resident server in a separate terminal and wait for the ready message:

```sh
make bench-agent-session BENCH_ARGS='--socket /tmp/fff-bench.sock --repo /path/to/repo --state /tmp/fff-bench-session --fff target/release/fff-mcp'
```

Then run the paired searches and render the chart:

```sh
make bench-agent-search BENCH_ARGS='--repo /path/to/repo --tasks /path/to/tasks.json --output /tmp/fff-bench-results --mcp-socket /tmp/fff-bench.sock'
make bench-agent-chart BENCH_ARGS='/tmp/fff-bench-results --labels Example'
```

Stop the resident server with Ctrl-C when finished. Omitting `--mcp-socket` starts a new MCP process per trial and includes indexing in the measured time. Keep those results separate from the resident-server comparison.

Use a separate server, socket, task file, and output directory for each repository. Complete indexing between timed benchmark runs. Render both repositories together by passing two result directories and two `--labels` values.

## Tune tool instructions

Keep development tasks separate from validation tasks. Freeze validation tasks before tuning and do not run them until the candidate is fixed. Retain every development result, including unsuccessful candidates.

With a resident server, `--setup /path/to/setup.json` overrides MCP instructions and tool descriptions. The file can contain `instructions` and a `tools` object keyed by tool name. Each tool can override its `description` and a `parameters` map of parameter descriptions. This changes model-visible metadata only. It does not rewrite queries or search results.

Use `--modes fff` for candidate screening against an existing native development baseline. Validate the selected instructions in the production binary with paired trials in both arms. Keep screening results separate from the final comparison.

`--tool-exposure direct` sets `mcp_servers.fff.omit_tools_from=["deferred"]` for the benchmark client. This advertises FFF tools without a discovery round trip on Codex versions that support the setting. The default keeps normal client discovery. The manifest records this choice; compare both setups separately.

## Results

`events.jsonl`, `stderr.log`, and each trial's `result.json` stay in the chosen output directory. They can contain private code and must not be published. The manifest records model names, reasoning effort, task and binary hashes, and the cache policy. Resuming the same command skips finished trials and rejects a different manifest.

The renderer writes `chart.png` and `scripts/search-benchmark-results.json`. The latter contains measurements and generic task labels, without prompts, answers, or source paths. Inspect labels before publishing it.

`--presentation scripts/search-benchmark-presentation.json` applies the chart labels and source note. It preserves the recorded model identifiers and measurements. The source note appears on every chart that uses these labels.

Wall time runs from model process launch through the completed answer. Token usage is Codex's reported input plus output; cached input is included and also reported separately. Reasoning tokens are a subset of output and are not added twice. Timeouts and incorrect answers remain in the results. A missing token count is not evidence of a token saving. The 50% wall-time reduction is a target, not a condition for accepting a run.

The published comparison uses a small fixed set of tasks from two private repositories. Raw traces and source locations remain private. Model decisions, query scope, and service latency affect the results; repeated tasks do not establish performance for every search workflow.

The current main chart uses four tasks in the 10k+ repo bucket with two repeats and four tasks in the 1m+ repo bucket with one repeat. The [held-out validation chart](search-benchmark-heldout.png) reports six additional tasks in the 10k+ repo bucket with one repeat. Those tasks were frozen before tuning and first run after the candidate was selected. Its [measurements](search-benchmark-heldout-results.json) include the Astra regression.

Both comparisons use protocol 3 and direct MCP tool exposure. Earlier prompt protocols and default tool discovery remain separate; their differences cannot be attributed solely to changes in FFF.
