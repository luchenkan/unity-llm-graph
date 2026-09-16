# 让模型真的用上它

> 从 README 拆出。一句话：**规则文件管会话内，git hook 管提交/拉取后兜底，
> 查询期自愈管编辑器里的手改**。三档可以叠加，实测组合是「档 1 + 档 2」。


装好 MCP 只是让工具**可用**,不等于会被调用。新开一个对话时,进模型上下文的
只有当前 profile 的工具名 + 描述,图谱内容一条都不读。模型什么时候调,取决于
你的问法能不能撞上工具描述 —— 问「改 X 会影响谁」大概率会调,问
「这个按钮点了没反应」它可能先去 grep。

同理,**改完文件默认交给 hook**;查询若发现目标文件已漂,会按文件增量补齐,
不必在会话里例行调 `unity_update`。主动扫全库漂移用 `update --stale`。

所以要把「可用」变成「默认动作」,以下三档任选:

### 档 1:项目规则文件(最低成本,先做这个)

在 Unity 项目根写 `CLAUDE.md`(Claude Code 每次会话必读;Cursor 用
`.cursor/rules/`,Cline 用 `.clinerules`,内容一样):

```markdown
## 改代码前
动 C# / prefab / scene 前先查影响面,别靠 grep 猜:
- 改方法/脚本:unity_impact target=类名 或 Owner.Method(partial 短名即可)
- 改 prefab/scene/SO:unity_refs / unity_components(grep 看不到)
- 改静态字段/事件总线:unity_impact target=Type.Field
- 只要签名 + 依赖:unity_context(比读整个文件省 token)

## 改完代码后
不要在会话里例行调 unity_update。交给 git hooks(post-commit / post-merge / post-rewrite)。
只有接下来还要查刚改过的文件时才调一次。
```

命中率高但不是 100%:规则是提示,不是强制。

### 档 2:git hooks(推荐,便宜且覆盖手改 + pull)

提交后批量增量更新一次。也覆盖你在 Unity 编辑器里手改的 prefab/scene ——
那些改动模型根本不知道。**以及 `git pull` 拉进来的远端提交**:别人改了
prefab 结构,你 pull 完图谱如果还是旧的,查询结论就是错的。四个模板按需装:

```bash
cp tools/post-commit.sample    <repo>/.git/hooks/post-commit    # 本地提交后
cp tools/post-merge.sample     <repo>/.git/hooks/post-merge     # git pull / merge 后(fast-forward 也触发)
cp tools/post-rewrite.sample   <repo>/.git/hooks/post-rewrite   # pull --rebase / rebase 后
cp tools/post-checkout.sample  <repo>/.git/hooks/post-checkout  # 同一 worktree 里切分支后
chmod +x <repo>/.git/hooks/post-*
# 编辑每个文件首行的 UNITY_LLM_DIR,指向本框架仓库
```

post-commit 的内容:

```bash
#!/bin/sh
# unity-llm-graph: 提交后增量刷新依赖图谱
UNITY_LLM_DIR="/path/to/unity-llm-graph"   # 框架仓库路径
ROOT=$(git rev-parse --show-toplevel)
[ -f "$ROOT/.unity-llm/graph.db" ] || exit 0   # 没建过图就不管
FILES=$(git diff-tree --no-commit-id --name-only -r HEAD \
        | grep -E '\.(cs|prefab|unity|asset|controller|anim|playable|mask|preset)$' || true)
cd "$UNITY_LLM_DIR" || exit 0
if [ -n "$FILES" ]; then
  python -m unity_llm update --project "$ROOT" --stale --files $FILES \
      >> "$ROOT/.unity-llm/hook.log" 2>&1 &
else
  python -m unity_llm update --project "$ROOT" --stale \
      >> "$ROOT/.unity-llm/hook.log" 2>&1 &
fi
exit 0
```

post-merge / post-rewrite 与之唯一的区别是差异文件的计算方式:pull / merge /
rebase 前 git 都会把 `ORIG_HEAD` 设为更新前的 HEAD,所以用
`git diff --name-only ORIG_HEAD HEAD` 拿「本次拉入的净变化」。习惯
`pull --rebase`(历史一条线)的必须装 post-rewrite —— rebase 路径不触发
post-merge;它重放的本地提交虽已被 post-commit 刷过,但 update 是幂等增量,
多刷无害,宁可多刷不漏刷。

后台跑、失败静默、永不阻塞提交/pull/rebase。git worktree 下 hook 是
**多个 worktree 共享**的,上面用 `git rev-parse --show-toplevel` 动态取项目根,
所以每个 worktree 各更新自己的图。

**post-checkout 是给「同一个 worktree 里切分支」准备的**。前三者都不覆盖
`git checkout` —— 在 release 和 dev 之间来回切时,图谱还是上一个分支的,
而错误的答案长得和正确的一样,这比"图旧了"更危险。它只在 `$3=1`(分支切换,
而非单文件检出)时跑,靠 `--stale` 扫切分支后被改写的 mtime。

**但更推荐每个分支开一个独立 worktree** —— 每个 worktree 有自己的
`.unity-llm/graph.db`,天然隔离,不需要这个 hook,也不用担心几百上千文件的
差异要重扫多久。这个 hook 是给「必须在同一目录切分支」的场景兜底的。

### 档 3:Claude Code PostToolUse hook(实时,但吵)

每次 Edit/Write 之后立刻更新。现成脚本 `tools/hook_post_edit.py`(自己从被编辑
文件向上找项目根,非 Unity 后缀直接跳过,异常一律静默)。写 `<项目>/.claude/settings.json`:

```json
{
  "hooks": {
    "PostToolUse": [
      { "matcher": "Edit|Write",
        "hooks": [{ "type": "command",
                    "command": "python /path/to/unity-llm-graph/tools/hook_post_edit.py" }] }
    ]
  }
}
```

代价:调用边解析是全局的,`update` 每次都会整体重跑一次该步骤 —— 一次重构改十几个
文件就是十几次重复付费。**除非你确实需要会话中途图谱永远最新,否则用档 2。**

三档不冲突。实测组合是 **档 1 + 档 2**:规则管会话内,hook 管提交后兜底。

### 典型 LLM 工作流

```
你:帮我改 Enemy.TakeDamage,加个护盾逻辑
LLM:(先调 unity_impact target=TakeDamage)
    → 发现 Player.Attack 调用它;Enemy.prefab 挂了脚本;Main.unity 引用了 prefab
    → 改之前就知道爆炸半径,而不是改完等 QA 报 bug
LLM:(改完文件后调 unity_update paths=[...])
    → 图谱秒级刷新,下一次查询基于最新代码
```
