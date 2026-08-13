# Review log

Brief for later reviewers. Do not treat README or the 5-script fixture as the product.

Framework: **unity-llm-graph** `0.6.0`  
Repo: https://github.com/luchenkan/unity-llm-graph (private)  
Constraint: zero runtime deps, Python ≥ 3.9, stdlib only.

## What this is (and is not)

**Is:** a query engine over **on-disk** Unity projects. It merges a heuristic C# graph with the **serialized-reference graph** (`.meta` guid, prefab/scene/SO YAML, UnityEvent `m_PersistentCalls`). That YAML coupling is what tree-sitter / generic code graphs structurally cannot see.

**Is not:**

- Unity Editor automation (create/modify prefabs, `AssetDatabase.Refresh`). That is a separate Editor MCP.
- A Roslyn-grade C# compiler.
- A game-specific event-bus adapter. Project APIs belong in that project's rules file, not here.

Two MCP servers can and should run together: this one answers “what is coupled on disk”; the Editor MCP mutates the live session.

## Reviewers so far

| Round | Model | Role |
| ----- | ----- | ---- |
| 1 | Kimi k3 | Created the skeleton (guid map, YAML refs, C# regex, SQLite, MCP, fixture tests). |
| 2 | Claude Opus 5 | Caught that rewritten `.meta` is untrustworthy; added guidmap from `AssetDatabase`, UnityEvent extract, call confidence, `validate`. |
| 3 | Kimi k3 | Incremental `update`, shipped `UnityLlmGuidDump.cs` into the framework repo. |
| 4 | Claude Opus 5 | Fuzzy-resolve false negatives, internal vs external callers, `dead_code_exclude`, `method_ref`, field `code_used`, comment/string masking, CI, `calls.log`. |
| 5 | Cursor Grok 4.6 | Reviewed against a real ~37.5k-asset project **and** MCP call logs, not the fixture. Then patched 0.5.0. |
| 6 | GPT-5.6 Sol | Re-prioritized correctness before token claims; patched namespace resolution, dead-code false negatives, deletion tombstones, strict context budgets, MCP profiles, and atomic builds for 0.6.0. |

Round 5 is the first pass that measured **which tools models actually invoked**, and that rejected encoding one game’s bus type into the engine.

## What held up

- Two-graph merge is the right wedge. Token-saving is a side effect; **guid-linked YAML** is the exclusive value.
- `.meta` may be rewritten; `guidmap.tsv` from `AssetDatabase` is the authority.
- `low` confidence must default off. Hot names (`Refresh`) otherwise explode.
- `method_ref` (`Register(OnFoo)`, `+= OnFoo`) is real Unity, not a one-project hack.
- Fuzzy resolve must not map a type name onto a folder and report “0 dependents = safe to delete”.
- Zero-dep regex C# is an explicit trade: miss chains rather than invent edges.

## What round 1–4 got wrong or over-fit

- **UnityEvent as the headline.** On the measured project, Inspector `onClick` was almost unused; UI binds in code. The parser is still worth keeping (generic Unity), but it is not universally the main coupling.
- **Fixture-complete ≠ project-complete.** Mini `Enemy.cs` has no partial types, no custom `MonoBehaviour` base, no `Type.Field.Method()` chains, no missing GameObject names on `MonoBehaviour` docs.
- **`unity_update` as a session ritual.** Call logs were ~70% graph refresh, ~0% `unity_refs` / `unity_components`. Refresh belongs in git hooks; session tools should answer questions grep cannot.
- **Cursor ≠ Claude Code MCP paths.** Project-root `.mcp.json` does not mean Cursor loaded the server.
- Round 5 first pass over-fit a project event bus name into MCP copy. That was reverted: the engine now matches **`Type.Field.Method()`** (PascalCase type, any method), kind `field_call`. Old kind `relay` is still read for stale DBs.

## Current shape (`0.6.0`)

```
meta.py        guid ← .meta and/or guidmap.tsv
unity_yaml.py  guid refs + UnityEvent + GameObject name via m_GameObject
csharp.py      types/methods/fields, lifecycle, string APIs, method_ref, field_call
graph.py       SQLite union + call resolution + is_mono inheritance walk
queries.py     impact / refs / components / deadcode / find / validate
mcp_server.py  10 tools split into core/full/admin profiles
```

Tests: 130 assertions / 24 groups on `tests/fixtures/SampleProject`.

## Open questions for the next model

Please disagree with evidence, not with the README.

1. **YAML coverage vs C# precision.** Nested prefab variants, Timeline signals, Animation Event `functionName`, Addressables/YooAsset labels — these are still blind and are more “Unity-shaped” than a Roslyn backend. Is the next dollar of work here?
2. **Tool surface.** Addressed in 0.6.0 with profiles. Measure whether the five-tool
   default `core` should shrink further; do not decide from schema aesthetics alone.
3. **`update` cost.** Every incremental update re-resolves all call edges. Acceptable for a post-commit hook; hostile as a per-edit MCP call.
4. **False “0 dependents”.** Remaining wrappers (`RegisterX(Type.Field, callback)` with no `.Method(`) still invisible. Should the engine grow another generic pattern, or stay dumb and let project rules document wrappers?
5. **`field_call` false positives.** Any `Foo.Bar.Baz(` with PascalCase `Foo` becomes a field-call. Measure noise on a real project before widening further.
6. **Do not add named types from any one game.** If a pattern needs a whitelist, it is not generic enough for this repo.
7. **Positioning.** Keep this complementary to Editor MCP. No prefab mutation, no `refresh_unity`, no scene hierarchy writes.

## How to re-test (not optional)

- Run `python tests/run_tests.py`.
- Then query a **real** Unity project: a partial `MonoBehaviour` split across files; a `Type.Field.Method()` bus/singleton; a prefab whose MonoBehaviour YAML has empty `m_Name`.
- Read that project’s `.unity-llm/calls.log` if it exists. Tool histograms beat fixture pass rates.

## Round 6 — prioritized completion

Order used:

1. **Trustworthiness:** never promote a namespace-ambiguous short type to `high`;
   count the terminal method of `field_call` in dead-code; stop treating every YAML
   field name as a method-use whitelist.
2. **Preserve evidence:** deleted assets get minimal tombstones, so old path/guid
   queries can still explain inbound references while `validate` reports them dangling.
3. **Token control:** default MCP result limits are deliberately lower; duplicate
   transitive payloads and internal resolution fields are removed; context is
   evidence-first and obeys a CJK-aware hard budget.
4. **Tool-choice control:** default MCP profile exposes only five day-to-day queries.
   Maintenance remains available through `admin`, and `full` preserves compatibility.
5. **Operational safety:** no implicit minute-scale build from a read query; full builds
   use a temporary DB and atomic replacement; indexes are created after bulk loading.

Deliberately not implemented in this round:

- Local `fileID` object graph and prefab-override `propertyPath` interpretation.
- AnimationEvent, Timeline Signal, Addressables/YooAsset semantic graphs.
- Roslyn-quality `using` alias/generic/extension-method resolution.

Those are high-value but need representative fixtures and real-project measurement.
Adding partial parsers without evidence would expand false confidence more than capability.

Token-report wording was also corrected: `baseline - spent` is an **upper-bound estimate
of avoided full-file reads**, not measured API billing. MCP schema/input and partial reads
remain outside that estimate.

Review completed by **GPT-5.6 Sol**.
