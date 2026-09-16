# Review log

Brief for later reviewers. Do not treat README or the 5-script fixture as the product.

Framework: **unity-llm-graph** `0.11.0`  
Repo: https://github.com/luchenkan/unity-llm-graph
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
| 7 | DeepSeek-V4-Pro | Reviewed **adoption**, not the fixture: read a production project's `calls.log`, token ledger, hook config, and a competing Editor MCP. Found Grok 4.6's diagnosis was still unfixed, then patched 0.7.0 to take the Editor MCP's "list hierarchy / components" query off the table. |
| 9 | GLM-5.3 | Author of 0.7.3 (pull/merge/rebase auto-refresh hooks) and 0.8.0 (stale detection, `digest`, `usage`, error self-correction). Works daily on the same production project. |
| 10 | DeepSeek-V4.1-Flash | Reviewed the production project's `calls.log` (again, not the fixture) and found the **property blind spot**: `members.kind` had no `property` at all. Shipped 0.10.0. |
| 11 | DeepSeek-V4.1-Flash | Ran a **declaration-coverage census** on the production project instead of reading the README's TODOs: found `event` / `delegate` / positional `record` missing, and — more valuable — that `x?.Foo()` (1772 call sites) was never a call edge. Shipped 0.11.0. |

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

## Current shape (`0.11.0`)

```
meta.py          guid ← .meta and/or guidmap.tsv
unity_yaml.py    guid refs + UnityEvent + GameObject name via m_GameObject
                 + fileID object graph (Transform m_Father, component m_GameObject)
csharp.py        types / methods / fields / properties / events / delegates,
                 lifecycle, string APIs, method_ref, field_call, `x?.Foo()`
visual_assets.py Animator state machines + Timeline tracks/clips
graph.py         SQLite union + call resolution + is_mono inheritance walk
                 + objects table + file_state (staleness) + atomic build
queries.py       impact / refs / components (with hierarchy tree) / deadcode /
                 find / validate / animator / timeline / digest / usage /
                 error_report
mcp_server.py    tools split into core/full/admin profiles
```

Tests: 227 assertions / 34 groups on `tests/fixtures/SampleProject`.

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

## Round 7 — adoption review, not another fixture pass

Reviewed the **same real production project** Round 5 measured (a ~37.5k-asset
project) the way Round 5 asked: read `.unity-llm/calls.log`, the token ledger, hook
config, and the installed Editor MCP. Findings, in order of severity:

1. **Grok 4.6's diagnosis was still unfixed.** `calls.log` was dominated by graph
   refresh (`unity_update`, roughly two thirds of calls) plus a few `unity_find`, with
   **zero** `unity_refs` / `unity_components` calls. The two tools grep cannot replace
   were never called.
2. **The token ledger over-credits the graph.** The running total mostly came from
   grep, read-discipline, and the Editor MCP's `execute_code`/`SerializedObject` — not
   from graph queries. `baseline - spent` is an upper bound, not the framework's
   contribution.
3. **The Editor MCP is eating the serialized-ref use case.** Models listed prefab
   hierarchy/components via `execute_code` instead of `unity_components`, because the
   graph could not answer "what does this prefab look like".

Fix shipped in `0.7.0`: parse the `fileID` object graph into an `objects` table and make
`unity_components` emit an indented GameObject hierarchy tree (Transform `m_Father` for
parenting, component `m_GameObject` for mounting, MonoBehaviour `m_Script` for script
names). The query half of the Editor MCP is now redundant; the mutation half
(create/delete/modify GameObject, save prefab, build, refresh) is explicitly out of
scope and must stay with the Editor MCP.

Rule file updated with a hard constraint: structure queries go to the graph, mutations
go to the Editor MCP. This is the "强制约束" the prior rounds never enforced — a rules
row is a hint, not a guarantee, so the tool now wins the query race on merit instead of
asking the model to remember.

Still deliberately not implemented (unchanged from Round 6):

- prefab override `propertyPath` semantics (nested prefab field overrides).
- AnimationEvent, Timeline Signal, Addressables/YooAsset semantic graphs.
- Roslyn-quality `using` alias/generic/extension-method resolution.

Review completed by **DeepSeek-V4-Pro**.

## Round 8 — pre-public hygiene pass

Reviewed the repository as the final pass before making it public, not as another
feature review:

1. **Version drift.** `pyproject.toml` still said `0.6.0` while `unity_llm`
   and `README.md` described `0.7.0`. Package metadata now reads the single
   source of truth from `unity_llm.__version__`.
2. **Missing graph could create an empty DB.** `graph.connect()` opened SQLite in
   read-write mode even for pure queries, so a CLI query on a project without
   `.unity-llm/graph.db` would silently create an empty database and return empty
   results. `connect()` now fails fast with a `build` instruction.
3. **`include_external` was half-applied.** `impact` filtered code callers but not
   asset/UnityEvent channels, and `refs` / `components` had no switch at all.
   The flag is now honored consistently across `impact`, `refs`, `components`,
   CLI, and the MCP tool schemas.
4. **CLI errors could traceback.** `cli.main()` only caught `RuntimeError`;
   SQLite/OS/value errors surfaced as raw Python tracebacks. It now prints a
   concise error and exit code by default, with `--debug` for full stack traces.
5. **Leak check.** Searched tracked files and Git history for project-specific
   names/paths from the test game, credentials, tokens, and local absolute paths;
   none were found. The only personal identifier is the intended public remote
   URL/username.

Re-ran `python tests/run_tests.py`: **142 assertions / 25 groups passing**.

## Round 9 — freshness & proactive value (0.7.3 + 0.8.0)

Reviewed by **GLM-5.3** (Tencent CodeBuddy), which is not a fly-by reviewer: it
runs this graph daily on the same ~37.5k-asset production project through both
the MCP server and the git hooks, and authored 0.7.3 earlier the same day.

### 0.7.3 — closing the pull gap

`post-commit` alone meant **pulled-in remote changes left the graph stale** —
exactly the failure mode this engine exists to prevent (someone else restructures
a prefab, you pull, every later query silently answers from the old structure).
Shipped `tools/post-merge.sample` (fires on `git pull` ff/merge and `git merge`)
and `tools/post-rewrite.sample` (`pull --rebase` / `rebase` / `commit --amend`;
the rebase path never triggers post-merge). Both diff `ORIG_HEAD..HEAD`; update
is idempotent so re-replayed local commits are harmless. Verified on the
production repo; pushed to GitHub.

### 0.8.0 — "know when you're wrong" + "speak before being asked"

The eight prior rounds made the engine *correct*. Two failure classes remained,
both invisible in fixture tests:

1. **Silent staleness is the most dangerous failure mode.** Hooks run in the
   background and fail silently by design — when they die, the graph freezes
   and every subsequent query returns confident wrong answers. Fix:
   `file_state(path, mtime, size)` written by build/update; `impact / refs /
   components / animator / timeline` now validate the target's files and attach
   a `graph_stale: {warning, files}` block when disk has moved on. Old DBs
   without the table degrade silently — no error, no false alarm. This moves
   the trustworthiness line from build-time into query-time, where it belongs.
2. **The graph knows, nobody asks.** After a pull brings in 200 files, the
   answers existed but were never queried. `digest` eats a whole file batch
   (stdin pipe from `git diff`) and aggregates top code callers / top asset
   dependents / mount-point count — a blast-radius summary for pull and code
   review, instead of N full impact reports. And `usage` productizes the
   Round 5/7 methodology: tool histogram, `unity_update` share, zero-call
   warnings for core query tools, straight from `calls.log`.

Also fixed: unknown-tool MCP errors now list the available tools for the active
profile instead of three bare characters (models retried the same call in the
wild — the log proves it).

### Measured on the production project (not the fixture)

`usage` on the real `calls.log` (172 calls, 2026-08-13 → 08-27):
`unity_components` 57 / `unity_find` 56 / `unity_impact` 25 / `unity_context`
23 / `unity_refs` 8, `update_share = 0.0` (refresh goes through git hooks, not
MCP). The Round 7 disease — refs/components at zero — is **cured**; the 0.7.0
objects table is being used as intended. `digest` on a real 17-file commit
returned a sensible caller ranking (BattleFormationMono 97 calls on top).
The three logged `未知工具` errors turned out to be **stale server processes
predating 0.7.2** (core profile gained animator/timeline then) — historical,
not a live bug, but exactly the kind of thing nobody would ever have noticed
without the report.

### Deliberately not implemented in this round

- AnimationEvent `functionName` ingestion (cheap and deadcode-relevant, but
  wants a fixture with `.anim` events first).
- Addressables/YooAsset bundle-group analysis (high value on this project,
  needs a representative fixture with real group configs).
- Roslyn backend — still the wrong trade against the zero-dep contract.

Tests: 172 → **186 assertions / 31 groups**, all green; real-project smoke
tests for stale downgrade, digest and usage documented above.

## Round 10 — the property blind spot (0.10.0)

Reviewed by **DeepSeek-V4.1-Flash** (WorkBuddy), against the same real
~44k-asset / 11k-script production project rather than the fixture.

### Starting from what the log got wrong, not from what the code lacks

Rounds 5/7/9 established the method: read `calls.log`, do not trust the README.
Doing that again, `usage` reported:

```
unity_find 15 (65%)  unity_context 4  unity_components 2  unity_impact 1  unity_refs 1
```

`unity_find` is the dominant tool, and the log held two entries that look like
one-offs but were systematic:

- `unity_refs target=PlayerModel.FirstSelectIPID` → `resolved_kind: unknown`
- `unity_find pattern=<a property name>` → empty

Both trace to the same root: `members.kind` only ever held `method` / `field`.

| measure | count |
| --- | --- |
| property declarations in source (parser) | **7723** |
| … of which modifier-less interface members | 194 |
| … expression-bodied (`=>`) | 1724 |
| rows with `kind='property'` in `members` | **0** |

That is ~6.8% of all members, and effectively the whole API surface of the data
model layer: `find` could not find the name, `refs Type.Prop` degraded to
`unknown`, `context` showed half an interface.

Notably this was **not** an oversight of the parser's authors — `FIELD_RE`
already carried the comment *"表达式体属性 `public float X => _x;` 不是字段"*.
Properties had been deliberately kept out of the *field* bucket but never given
a bucket of their own. "Rather miss than invent" became "invisible".

### The one part that needed thought: over-collect vs. collect-wrong

The first cut (no line anchor, unconstrained first char of the type) collected
**8927** property matches on this project. Auditing every "suspicious" match
found 3 real false positives, all the same shape — LINQ lambdas:

```csharp
names.ToDictionary(x => x, x => x + "-hit");
//                   ^^^^^^^^^^  `> x, x =>` reads as "type `> x,` + name `x` + `=>`"
```

The three sites were `ToDictionary(x => x, x => …)`,
`ToDictionary(x => x.Key, x => …)`, `.ToLookup(o => o.Key, pair => …)` — all in
one file, all mid-line. Two **independent** constraints fixed it: `^[ \t]*`
line anchoring (a legal property declaration always starts a line) and
restricting the type's first char to `[\w<]` as a backstop. Result: 8927 → 7779,
zero type-anomalies, all three lambda sites gone.

**This is the one lossy decision worth re-auditing**: line anchoring means two
property declarations on one line would be missed. Not observed in the wild.

### Measured on the production project (full rebuild, 6m12s)

| query | before | after |
| --- | --- | --- |
| `members_by_kind.property` | **0** | **11459** |
| `find FirstSelectIPID` | empty | hits `PlayerModel`, `kind=property` |
| `refs PlayerModel.FirstSelectIPID` | `resolved_kind: unknown` | `kind=property`, signature `int FirstSelectIPID` |
| `context PlayerModel` | methods only | properties listed under `## Key signatures` |
| `dead_code` | — | properties in neither list; no spike |
| parse cost | — | **+1.5%** (1200 files, 7.92s vs 7.80s) |

The parse-cost number matters because the rebuild log showed a suspicious gap
(833→1344 scripts taking 106s, i.e. ~5 files/s instead of ~170). An A/B on 1200
real files with `PROPERTY_RE` swapped for a never-matching pattern cleared it:
the regression is **1.5%, not 20×**. The slow window was disk/IO or one of the
other parse stages, not this feature. Anyone re-measuring should A/B rather than
reason from the progress log — the log prints only on counter change, so a stall
looks like a slow feature.

One design addition that the measurement made necessary: because properties
**structurally** have no call edges, `refs` / `impact` on a property return zero
callers. Round 6 already learned that a bare "0 dependents" gets misread as
"safe to delete", so both now attach `note_property` explaining that the zero is
a language fact, not evidence.

### Deliberately not done

- **No call edges from getter/setter bodies.** `public int X { get { return Foo(); } }`
  still hides `Foo()` from the call graph, so `Foo` remains exposed to false
  dead-code reports. That problem **predates 0.10.0**; this round neither widened
  nor fixed it. It needs its own entry point.
- **No property-access edges** (`x.Prop`). Property access is not a method call in
  C#; manufacturing an edge would only create fake dependencies. What properties
  buy is *discoverability*, and that is stated as an explicit limitation in the
  README rather than papered over.
- **Properties stay out of dead-code.** They are public API surface and Unity does
  not serialize them; both `dead_code` lists select by kind whitelist, so
  properties are structurally excluded (test asserts this).

### Checklist for the next reviewer

1. Run `stats` on a real project: is `members_by_kind.property` the same order of
   magnitude as the source count? (7700+ here. An order-of-magnitude gap means a
   whole syntax form is being missed.)
2. Spot-check `context <a data-model class>` — properties should appear under
   `## Key signatures`, ordered lifecycle > serialized > public > rest.
3. Re-audit the lossy part of line anchoring: grep for two declarations on one line.
4. Exercise the old-DB upgrade path: does the `update` hint fire, and does it go
   away after a full `build`?
5. Syntax forms still not covered: **indexers** (`this[int]`, deliberately out) and
   **explicit interface implementations** (`int IFoo.Hp => …`, currently collected
   as an ordinary property).

## Round 11 — declaration-coverage audit (0.11.0)

Reviewed by **DeepSeek-V4.1-Flash** (WorkBuddy). Round 10 fixed one blind spot;
this round asks the general question: **what other C# declaration forms never make
it into the graph?** The method is the point — enumerate the forms that actually
exist in the project with independent regexes, count them, then check the graph
for each.

### The audit (source count → graph rows, before this round)

| form | source | graph | verdict |
| --- | --- | --- | --- |
| `event` | 245 in `Assets/`, **353** project-wide | 0 | fix |
| `delegate` | 58 / **141** | 0 (no such `kind` in `types`) | fix |
| positional `record` | 0 | 0 | fix (type-level loss) |
| `required` field | 0 | 0 | collect defensively (C# 11) |
| indexer `this[]` | 244 | 0 | leave out (not a named member) |
| `operator` overload | 24 | 0 | leave out (no cross-type dependency) |

**The audit also surfaced something worth more than `event` itself: `x?.Foo()`.**
It is not a *declaration* form, so it never appeared in the table above — I found
it while picking examples for `event`: `OnDead?.Invoke()` has a `?` before the dot,
which makes `DOTCALL_RE` miss the whole thing. Measured: **1772 call sites across
555 files, 53% of them pointing at non-`Invoke` business methods**
(`Release` / `ResolveRef` / `Refresh` / `Cancel`). Worse, it *cascades into
dead-code false positives*, because a method only ever called via `?.` has no
caller on the graph. After the fix, dead-code candidates went **326 → 309** and
call edges **255095 → 257860**.

**Lesson worth carrying forward:** a coverage audit must cover **call syntax**,
not just member/type declarations. One piece of language sugar outranked every
new member kind in this round.

### Three implementation traps (for the next reviewer)

1. **`delegate` must be parsed in the type region.** It has no `{}` body, so
   `body_start` stays `None`; parsing it there also lets the immediately following
   `field_scopes` cover it — otherwise member parsing hits
   `field_scopes[id(owner)]` and raises `KeyError`.
2. **Positional `record` has a hidden trap.** `CLASS_RE` previously required a
   trailing `{`; I added a `;` branch. But `body_start = m.end() - 1` then points
   at the semicolon, and `_enclosing_class`'s interval test would assign **every
   following member** to that type. It must be explicitly left empty. That change
   also pushed the `bases` capture group from 5 to 6.
3. **Both `event` forms matter.** For the accessor form the regex stops at
   `{ add`, so `group(0)` does *not* contain the closing `}` — "is this a custom
   accessor" must be decided by "does not end with `;`", not "ends with `}`".
   (The first implementation got this wrong; the fixture caught it.)

### Deliberately not done

- **Indexers** (`this[int]`): 244 occurrences, but they have no named-member
  identity — collecting them would only blur the property bucket.
- **Operator overloads**: 24, no cross-type dependency.
- **Other modern C# features**: first established the ceiling — **Unity 2022.3 and
  Tuanjie 2022.3.62t4 both stop at C# 9**, so C# 10 file-scoped namespaces,
  C# 11 `required` and C# 12 primary constructors *cannot occur* in this project.
  I only added `required` (one word, leaves the door open for newer engines).
  The rule used here, worth reusing: **add a feature when it affects dependency-graph
  correctness, not when it merely looks modern.**

### Checklist for the next reviewer

1. Re-run the declaration census above; event/delegate counts should track source.
2. Spot-check a `?.` edge: find a method called *only* via `?.` and confirm it is
   no longer in the dead-code list.
3. Verify positional-record attribution: members declared *after* `record Foo(int A);`
   must not end up owned by `Foo`.
4. Confirm the "collect but ignore" boundaries (`required`, indexers) stay quiet —
   no new false positives.
