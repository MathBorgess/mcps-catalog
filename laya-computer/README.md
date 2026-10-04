# Laya Computer MCP

Execute multi-step desktop plans using **local Laya typed decisions** and **Cua Driver**. The calling model supplies the plan, success conditions and any minimal rescue repair. The server does not call a remote planner or require a remote API key.

## Requirements and installation

Native execution requires macOS on Apple Silicon, Python 3.12+, `uv`, and macOS Accessibility/Screen Recording permissions for the process that hosts the driver (see below). The Cua Driver SDK (`cua-driver`, pinned) is a Python dependency and bundles its native runtime; no separate Cua install is needed in embedded mode. Portable unit tests use synthetic observations and do not require Cua, Photos or model weights.

```bash
claude plugin marketplace update mathborgess-mcps
claude plugin install laya-computer@mathborgess-mcps
```

Installation from the default marketplace requires this change to have reached `main`. For local development:

```bash
uv run --project laya-computer laya-computer-mcp
```

The plugin launches `uvx --from ${CLAUDE_PLUGIN_ROOT} laya-computer-mcp`. The server talks to Cua through the typed `cua_driver` SDK (`CuaDriver.call_tool`), not through Cua's MCP endpoint: by default the driver runs **embedded in this process**; with `CUA_DRIVER_SOCKET` set it connects to an already running `cua-driver serve` daemon instead. Embedded mode means macOS attributes Accessibility/Screen Recording to the host process chain, not to `CuaDriver.app`; if grants are missing (`permissions_pending`), either grant them to that process or use the daemon, which keeps the identity that already holds them. No permission bypass, cloud provisioning or automatic driver upgrade is performed. Cua sends content-free product telemetry by default; run `cua-driver telemetry disable` to stop it.

| Environment | Default | Purpose |
|---|---|---|
| `CUA_DRIVER_SOCKET` | unset (embedded) | Unix socket of a running `cua-driver serve` daemon; unset runs the SDK embedded |
| `LAYA_MODEL` | `aac6fef/laya-typed-decisions-mlx` | Concrete local MLX checkpoint |

The checkpoint loads lazily and stays in memory. The first load may download weights and compile Metal kernels. Model work is serialized on one worker thread. Local tokens do not imply total task cost is zero.

## Tools

| Tool | Purpose |
|---|---|
| `inspect(app_bundle_id, window_title?, query?, include_schema=false)` | Initial compact accessibility observation; optional label projection and plan schema |
| `preflight_plan(plan)` | Assess a candidate plan statically without observing or acting on the desktop |
| `run_plan(plan)` | Start a bounded local run and return its ID |
| `get_run(run_id, wait_seconds=30)` | Wait for completion/state without reobserving the UI |
| `resume_plan(run_id, expected_version, plan)` | Resume `rescue_needed` with a versioned minimal repair |
| `stop_run(run_id)` | Cooperative stop; an in-flight action may finish |

The app identifier is the native bundle ID, such as `com.apple.Photos`. Plans must stay within that app. The server conservatively permits one active operation per app. Sessions and execution state are in memory; server shutdown does not persist or replay actions. Up to 32 retained runs are supported; finished runs can be evicted.

## Plan contract

Always inspect the app first. A plan contains `version`, `goal`, `app_bundle_id`, and `steps`. `start_step` defaults to the first step. Each step specifies `id`, `instruction`, `action`, a nonempty `success` list and optionally `preconditions`, `next_step`, `on_failure` and `window_title`. **No `next_step` means finish**, not advance implicitly in array order. References must name existing steps.

Actions: `click`, `double_click`, `set_value` (explicit `text`), `press_key` (explicit `key`, e.g. `cmd+home`), `scroll` (explicit `direction`), `wait` (`duration_seconds`, at most 10), and `verify`. Click, double-click, set-value and scroll require `target`. Selectors use observed `role`, `label_contains`, `description_contains`, and optional `index` (`first`, `last`, or zero-based position among matches). Indices are positions in observed candidates, never persistent Cua IDs. Observed native menu candidates are dispatched through Cua `invoke_menu` with their exact ancestor path, because menu elements are not ordinary window targets. Exact single candidates bypass inference; ambiguous matches use Laya with at most 20 alternatives. Narrow larger candidate sets in the plan.

Predicates: `exists`, `value_contains`, `selected`, `window_title_contains`. Element predicates use `role` and/or `label_contains`; value/title predicates require `value`. Predicates are combined with AND. Use conditions that actually demonstrate the intended result, not an unrelated visible button. Empty observations cannot prove success. Partial observations are refused by default; `allow_partial_observation: true` explicitly allows only positive predicates about witnessed controls. Missing controls remain unknown, and ordinal selectors (first/last/index) are forbidden in this mode. This cannot prove a globally oldest item. A final `completed` status proves the supplied predicates, not a stronger unstated goal.

Synthetic example (not a real app selector):

```json
{
  "version": 1,
  "goal": "Select the yearly view",
  "app_bundle_id": "com.example.fixture",
  "steps": [{
    "id": "yearly-view",
    "instruction": "Choose the Years view",
    "action": "click",
    "target": {"role": "AXRadioButton"},
    "success": [{"kind": "selected", "role": "AXRadioButton", "label_contains": "Years"}]
  }]
}
```

Default hard ceilings: 60 actions, 300 seconds of active execution and one rescue. A plan may lower `max_actions`, `max_seconds` or `max_rescues`. Rescue pauses do not consume active execution time; resume does not reset consumed budgets. Each action is followed by fresh observation, and Cua's own action receipt (`effect`: `confirmed`, `partial`, `unverifiable`, `suspected_noop`; `refused` is a certain, pre-dispatch failure) is kept in the history and in `metrics.effects`. A receipt never replaces the predicates: `unverifiable`/`partial` still need verification, and `suspected_noop` with unmet predicates is a known non-effect that may follow `on_failure`. Obsolete snapshots allow bounded reobservation; uncertain mutations are never blindly retried.

## Rescue and evaluation

Results distinguish `running`, `completed`, `rescue_needed`, `blocked` and `stopped`; verification is separate. On rescue, the caller gets the failed step, relevant observation, verified progress and recent effects. It should resolve the blocker and change only the unfinished part of the plan, preserving goal, app and completed steps. Increment the plan version by one. A subagent can assist the caller; no second local model is required.

The server reports actions, decisions, stale retries, rescue requests and active time. **Strong-model token use is unavailable to this MCP** (`remote_tokens: null`). Measure it in the calling client, including planning, inspection, polling, rescue and subagents. Do not claim savings from local action counts or JSON size. Jev benchmarks are references, not measurements of this desktop implementation.

The Photos pilot aims to open the oldest photo in the verified library scope and read its date. Accessibility may omit the photo grid or expose insufficient metadata. Navigating view buttons alone is not proof of finding the oldest photo. No visual model is included; unsupported surfaces pause for the strong caller.

## Validation

```bash
uv sync --project laya-computer
uv run --project laya-computer pytest -q -rs laya-computer/tests
uv run --project laya-computer ruff check --config laya-computer/pyproject.toml laya-computer
```

Tests use synthetic fixtures for stale tokens, uncertainty, plan validity, resume budgets and MCP lifecycle. They are not Photos success evidence. A live two-step navigation example is in [`examples/photos-navigation.json`](examples/photos-navigation.json). It selects Months then Years; it does not find the oldest photo. Live findings are recorded separately in the implementation report under `docs/`.
