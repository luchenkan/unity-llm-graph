"""unity-llm-graph 自动化测试(纯标准库,直接运行: python tests/run_tests.py)。

覆盖:
  1. 图谱构建(guid 映射 / C# 解析 / 序列化引用 / UnityEvent)
  2. impact  影响面分析(代码 + prefab + scene 传递闭包 + UnityEvent 绑定)
  3. impact  精度回归:同名方法不能跨类型误报(Decoy.Refresh vs Enemy.Refresh)
  4. refs    引用查找
  5. components prefab 挂了哪些脚本 / 脚本被谁挂载
  6. deadcode Unity 感知死代码(生命周期/消息/字符串调用/UnityEvent 不误报)
  7. validate 悬空 guid 体检
  8. find    符号搜索
  9. context 上下文打包
 10. MCP     stdio server 握手 + tools/list + tools/call 冒烟测试
 11. update  增量更新(改名 / prefab 新增引用 / 删文件)
 12. resolve 模糊命中必须标 fuzzy 并给候选(防「0 依赖 = 可以删」假阴性)
 13. impact  外部调用方 / 内部自调用分区
 14. deadcode 排除模式 + 目录聚合
 15. config  .unity-llm.json 项目级配置
 16. guid    非标准 guid 告警只在真有影响时出现
 17. method_ref 委托/方法组引用(Register(OnFoo) / evt += OnFoo)算被使用
 18. parser  注释/字符串同遍遮罩(URL 里的 // 不能吃掉后面的代码)
 19. build   verbose 进度写 stderr,默认安静
 20. visual  Animator/Timeline 结构解析(缩进列表 / AnyState / 负 fileID)
"""
import base64
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from unity_llm import graph, queries, context as context_mod  # noqa: E402
from unity_llm import __version__  # noqa: E402
from unity_llm.mcp_server import TOOLS, make_dispatcher, tools_for_profile  # noqa: E402
from unity_llm.tokens import estimate_tokens  # noqa: E402

FIXTURE = os.path.join(ROOT, "tests", "fixtures", "SampleProject")

ENEMY_CS_GUID = "e1000000000000000000000000000001"
PREFAB_GUID = "e1000000000000000000000000000010"
SCENE_GUID = "e1000000000000000000000000000020"
PNG_GUID = "e1000000000000000000000000000030"
CONFIG_GUID = "e1000000000000000000000000000040"

PASSED = []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        PASSED.append(name)
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        raise AssertionError(f"{name}: {detail}")


def ensure_png():
    """HealthBar.png 用 1x1 像素占位(仓库里不提交二进制)。"""
    p = os.path.join(FIXTURE, "Assets", "UI", "HealthBar.png")
    if not os.path.exists(p):
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
        with open(p, "wb") as f:
            f.write(png)


def test_build():
    print("[1] build")
    stats = graph.build(FIXTURE)
    check("脚本数", stats["scripts"] == 10, str(stats))
    check("类型数", stats["types"] == 13, str(stats))
    check("YAML 资产数", stats["yaml_assets"] == 6, str(stats))
    check("序列化引用数>=5", stats["refs"] >= 5, str(stats))
    check("资产数>=9", stats["assets"] >= 9, str(stats))
    check("UnityEvent 绑定==1", stats["events"] == 1, str(stats))
    check("guid 来源为 meta", stats["guid_source"] == "meta", str(stats))
    check("无解析错误", stats["errors"] == 0, str(stats))


def test_stats():
    print("[2] stats")
    s = queries.stats(FIXTURE)
    check("mono_behaviours==6", s["mono_behaviours"] == 6, str(s))
    check("scriptable_objects==1", s["scriptable_objects"] == 1, str(s))
    check("生命周期方法==4(Start/Update/Awake+BusListener.Start)", s["lifecycle_methods"] == 4, str(s))
    check("unity_events==1", s["unity_events"] == 1, str(s))
    check("置信度分布存在", isinstance(s["call_confidence"], dict)
          and sum(s["call_confidence"].values()) == s["calls"],
          str(s.get("call_confidence")))
    check("guid 无异常告警", "guid_warning" not in s, str(s))


def test_impact_type():
    print("[3] impact(类)")
    r = queries.impact(FIXTURE, "Enemy")
    check("解析为 type", r["resolved"].get("kind") == "type", str(r["resolved"]))
    callers = {c["caller"] for c in r["code_dependents"]}
    check("Player.Attack 调用了 Enemy 成员",
          any("Player.Attack" in c for c in callers), str(callers))
    direct = {a["asset"] for a in r["asset_dependents"] if a["depth"] == 1}
    check("Enemy.prefab 直接引用", any("Enemy.prefab" in a for a in direct),
          str(direct))
    trans = {a["asset"] for a in r["asset_dependents"]}
    check("Main.unity 传递引用", any("Main.unity" in a for a in trans), str(trans))
    check("摘要计数", r["summary"]["code_dependents"] >= 1, str(r["summary"]))
    binds = {(e["event"], e["method"]) for e in r["event_bindings"]}
    check("m_OnClick -> OnButtonClicked 被抽出",
          ("m_OnClick", "OnButtonClicked") in binds, str(binds))


def test_impact_precision():
    print("[4] impact 精度(同名方法不跨类型误报)")
    r = queries.impact(FIXTURE, "Enemy")
    callers = {c["caller"] for c in r["code_dependents"]}
    check("Decoy.Poke 不是 Enemy 的依赖(它调的是 Decoy.Refresh)",
          not any("Decoy" in c for c in callers), str(callers))
    d = queries.impact(FIXTURE, "Decoy")
    dcallers = {c["caller"] for c in d["code_dependents"]}
    check("Decoy.Poke 是 Decoy 自己的依赖",
          any("Decoy.Poke" in c for c in dcallers), str(dcallers))
    highs = [c for c in d["code_dependents"] if c["confidence"] == "high"]
    check("接收者类型已知 -> high 置信度", len(highs) >= 1,
          str(d["code_dependents"]))
    check("默认过滤 low", all(c["confidence"] != "low"
                              for c in r["code_dependents"]),
          str(r["code_dependents"]))


def test_impact_method():
    print("[5] impact(方法)")
    r = queries.impact(FIXTURE, "TakeDamage")
    check("解析为 method", r["resolved"].get("kind") == "method",
          str(r["resolved"]))
    callers = {c["caller"] for c in r["code_dependents"]}
    check("Player.Attack 是调用者", "Game.Combat.Player.Attack" in callers,
          str(callers))
    r2 = queries.impact(FIXTURE, "Enemy.TakeDamage")
    check("Owner.Method 写法可解析", r2["resolved"].get("kind") == "method",
          str(r2["resolved"]))


def test_impact_by_guid_and_path():
    print("[6] impact(guid / 路径)")
    r = queries.impact(FIXTURE, ENEMY_CS_GUID)
    check("guid 解析", r["resolved"].get("kind") == "asset", str(r["resolved"]))
    r2 = queries.impact(FIXTURE, "Assets/Scripts/Enemy.cs")
    check("路径解析", r2["resolved"].get("kind") == "asset", str(r2["resolved"]))


def test_refs():
    print("[7] refs")
    r = queries.find_refs(FIXTURE, "Assets/Scripts/Enemy.cs")
    fields = {(x["src_path"], x["field"]) for x in r["serialized_refs"]}
    check("prefab m_Script 引用",
          any("Enemy.prefab" in f[0] and f[1] == "m_Script" for f in fields),
          str(fields))
    r2 = queries.find_refs(FIXTURE, PNG_GUID)
    fields2 = {(x["src_path"], x["field"]) for x in r2["serialized_refs"]}
    check("healthBar 字段引用贴图",
          any("Enemy.prefab" in f[0] and f[1] == "healthBar" for f in fields2),
          str(fields2))
    r3 = queries.find_refs(FIXTURE, "Enemy.TakeDamage")
    calls = {c["calls"] for c in r3["code_refs"]}
    check("方法目标只返回该方法的引用(不是整个类型的)",
          calls == {"TakeDamage"}, str(r3["code_refs"]))


def test_components():
    print("[8] components")
    r = queries.components(FIXTURE, "Assets/Prefabs/Enemy.prefab")
    scripts = {s["script"] for s in r["scripts"]}
    check("prefab 上列出 Enemy.cs",
          any("Enemy.cs" in s for s in scripts), str(scripts))
    check("prefab 上列出 Player.cs(按钮那个组件)",
          any("Player.cs" in s for s in scripts), str(scripts))
    r2 = queries.components(FIXTURE, "Enemy")
    mounted = {m["asset"] for m in r2["mounted_on"]}
    check("Enemy 被 Enemy.prefab 挂载",
          any("Enemy.prefab" in m for m in mounted), str(mounted))
    on_go = {m["on"] for m in r2["mounted_on"] if "Enemy.prefab" in m["asset"]}
    check("挂载点是 GameObject 名而不是 MonoBehaviour",
          "Enemy" in on_go, str(on_go))


def test_deadcode():
    print("[9] deadcode")
    r = queries.dead_code(FIXTURE)
    methods = {m["method"] for m in r["dead_methods"]}
    check("UnusedPrivateMethod 被报",
          any("UnusedPrivateMethod" in m for m in methods), str(methods))
    check("NeverCalled 被报", any("NeverCalled" in m for m in methods),
          str(methods))
    check("Die() 不误报(被内部调用)", not any("Die(" in m for m in methods),
          str(methods))
    check("OnPlayerTouched 不误报(SendMessage 字符串调用)",
          not any("OnPlayerTouched" in m for m in methods), str(methods))
    check("OnButtonClicked 不误报(UnityEvent 绑定)",
          not any("OnButtonClicked" in m for m in methods), str(methods))
    check("序列化字段恰好同名不能隐藏死方法",
          any("UnusedPrivateMethod" in m for m in methods), str(methods))
    check("field_call 末段方法计为已调用",
          not any("EventChannel.AddListener" in m for m in methods), str(methods))
    check("Start/Update 不误报(生命周期)",
          not any(m.endswith(".Start()") or m.endswith(".Update()")
                  for m in methods), str(methods))
    check("OnTriggerEnter 不误报(消息回调)",
          not any("OnTriggerEnter" in m for m in methods), str(methods))
    check("public 方法不误报(TakeDamage/Attack)",
          not any("TakeDamage" in m or ".Attack" in m for m in methods),
          str(methods))
    check("免责声明存在", "UnityEvent" in r["disclaimer"])


def test_validate():
    print("[10] validate")
    r = queries.validate(FIXTURE)
    check("无悬空 guid", r["summary"]["distinct_dangling"] == 0, str(r))
    check("guid 来源为 meta", r["summary"]["guid_source"] == "meta", str(r))
    check("无非标准 guid 告警", "hint" not in r["summary"], str(r["summary"]))


def test_find():
    print("[11] find")
    r = queries.find_symbols(FIXTURE, "Player")
    names = {t["name"] for t in r["types"]}
    check("找到 Player 类", "Player" in names, str(names))


def test_context():
    print("[12] context")
    text = context_mod.build_context(FIXTURE, "Enemy", budget_tokens=2000)
    check("包含类型签名", "Game.Combat.Enemy" in text)
    check("包含生命周期标注", "lifecycle" in text)
    check("包含 prefab 依赖", "Enemy.prefab" in text)
    check("包含 UnityEvent 区块", "UnityEvent" in text)
    check("严格 token 预算内", estimate_tokens(text) <= 2000,
          str(estimate_tokens(text)))
    tiny = context_mod.build_context(FIXTURE, "Enemy", budget_tokens=120)
    check("小预算也严格封顶", estimate_tokens(tiny) <= 120,
          str(estimate_tokens(tiny)))


def test_mcp_smoke():
    print("[13] MCP server 冒烟")
    proc = subprocess.Popen(
        [sys.executable, "-m", "unity_llm", "serve", "--project", FIXTURE],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=ROOT, text=True, encoding="utf-8", errors="replace")

    def rpc(msg):
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()
        return json.loads(proc.stdout.readline())

    init = rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2024-11-05",
                           "capabilities": {},
                           "clientInfo": {"name": "test", "version": "0"}}})
    check("initialize 握手", init["result"]["serverInfo"]["name"] ==
          "unity-llm-graph", str(init))
    proc.stdin.write(json.dumps(
        {"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
    proc.stdin.flush()
    tools = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = {t["name"] for t in tools["result"]["tools"]}
    core = {t["name"] for t in tools_for_profile("core")}
    check("默认只暴露 core 工具", names == core and len(names) < len(TOOLS),
          str(names))
    check("core 含 Unity 独占查询",
          {"unity_impact", "unity_refs", "unity_components"} <= names, str(names))
    call = rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "unity_impact",
                           "arguments": {"target": "TakeDamage"}}})
    payload = json.loads(call["result"]["content"][0]["text"])
    check("tools/call unity_impact",
          any("Player.Attack" in c["caller"]
              for c in payload["code_dependents"]), str(payload)[:300])
    call2 = rpc({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                 "params": {"name": "unity_stats", "arguments": {}}})
    check("core 拒绝 admin 工具", call2["result"].get("isError") is True,
          str(call2))
    call3 = rpc({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                 "params": {"name": "unity_components",
                            "arguments": {"target": "Enemy"}}})
    payload3 = json.loads(call3["result"]["content"][0]["text"])
    check("tools/call unity_components",
          any("Enemy.prefab" in m["asset"] for m in payload3["mounted_on"]),
          str(payload3)[:300])
    call4 = rpc({"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                 "params": {"name": "unity_nope", "arguments": {}}})
    check("未知工具走 isError", call4["result"].get("isError") is True,
          str(call4))
    proc.kill()
    log = os.path.join(FIXTURE, ".unity-llm", "calls.log")
    lines = [json.loads(x) for x in open(log, encoding="utf-8")
             if x.strip()] if os.path.exists(log) else []
    check("calls.log 记录了工具调用", len(lines) >= 4, str(len(lines)))
    check("calls.log 带返回体大小/估算 token",
          any(r["tool"] == "unity_impact" and r["chars"] > 0
              and r["approx_tokens"] > 0 for r in lines),
          str(lines[:2]))


def test_update():
    print("[14] update(增量更新)")
    import shutil
    import tempfile
    tmp = tempfile.mkdtemp(prefix="unity_llm_test_")
    try:
        dst = os.path.join(tmp, "Proj")
        shutil.copytree(FIXTURE, dst,
                        ignore=shutil.ignore_patterns(".unity-llm"))
        graph.build(dst)
        # 1) 修改脚本:重命名一个私有死方法
        enemy = os.path.join(dst, "Assets", "Scripts", "Enemy.cs")
        text = open(enemy, encoding="utf-8").read()
        text = text.replace("UnusedPrivateMethod", "NewDeadMethod")
        open(enemy, "w", encoding="utf-8").write(text)
        # 2) 修改 prefab:新增一条指向 GameConfig.cs 的序列化引用
        prefab = os.path.join(dst, "Assets", "Prefabs", "Enemy.prefab")
        ptext = open(prefab, encoding="utf-8").read()
        ptext = ptext.replace(
            "  hp: 100",
            "  hp: 100\n  gameConfig: {fileID: 11400000, guid: "
            "e1000000000000000000000000000003, type: 2}")
        open(prefab, "w", encoding="utf-8").write(ptext)
        r = graph.update_files(dst, ["Assets/Scripts/Enemy.cs",
                                     "Assets/Prefabs/Enemy.prefab"])
        check("两个文件更新成功且无错误",
              len(r["updated"]) == 2 and not r["errors"], str(r))
        check("增量更新同步 schema 版本",
              queries.stats(dst)["version"] == __version__)
        d = queries.dead_code(dst)
        methods = {m["method"] for m in d["dead_methods"]}
        check("新方法名进入死代码", any("NewDeadMethod" in m for m in methods),
              str(methods))
        check("旧方法名已消失",
              not any("UnusedPrivateMethod" in m for m in methods), str(methods))
        rr = queries.find_refs(dst, "Assets/Scripts/GameConfig.cs")
        fields = {(x["src_path"], x["field"]) for x in rr["serialized_refs"]}
        check("prefab 新增引用可见",
              any("Enemy.prefab" in f[0] and f[1] == "gameConfig"
                  for f in fields), str(fields))
        # 3) 删除文件
        victim = "Assets/Scripts/Util/DeadHelper.cs"
        os.remove(os.path.join(dst, victim.replace("/", os.sep)))
        r2 = graph.update_files(dst, [victim])
        check("删除生效", r2["deleted"] == [victim], str(r2))
        d2 = queries.dead_code(dst)
        check("删除后 NeverCalled 从死代码里消失",
              not any("NeverCalled" in m["method"] for m in d2["dead_methods"]))
        # 4) 删除一个仍被 prefab 引用的脚本:tombstone 保留可解释性
        os.remove(enemy)
        r3 = graph.update_files(dst, ["Assets/Scripts/Enemy.cs"])
        refs = queries.find_refs(dst, ENEMY_CS_GUID)
        check("删除资产仍可按旧 guid 解释入边",
              refs["resolved"].get("deleted") is True
              and any("Enemy.prefab" in x["src_path"]
                      for x in refs["serialized_refs"]), str(refs))
        check("删除资产仍计为悬空引用",
              queries.validate(dst)["summary"]["distinct_dangling"] >= 1)
        os.remove(enemy + ".meta")
        graph.build(dst)
        rebuilt_refs = queries.find_refs(dst, ENEMY_CS_GUID)
        check("全量 build 后 tombstone 仍保留",
              rebuilt_refs["resolved"].get("deleted") is True
              and bool(rebuilt_refs["serialized_refs"]), str(rebuilt_refs))
        escaped = graph.update_files(dst, ["../outside.cs"])
        check("增量路径不能越出项目根目录",
              bool(escaped["errors"]) and not escaped["updated"], str(escaped))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_resolve_fuzzy():
    print("[15] resolve 模糊命中提示(假阴性防护)")
    from unity_llm.graph import connect
    from unity_llm.queries import resolve_target
    conn = connect(FIXTURE)
    # Assets/Scripts/Util 是目录(有 folderAsset 的 .meta),不是类型
    n = resolve_target(conn, "Util")
    check("目录被标记 fuzzy", n.get("fuzzy") is True, str(n))
    check("目录命中带 hint", "hint" in n and "目录" in n["hint"], str(n))
    check("目录命中不吞掉其它同名候选",
          n["path"].endswith("Scripts/Util"), str(n))
    # 精确类型仍然优先于同名文件路径
    n2 = resolve_target(conn, "Enemy")
    check("Enemy 仍解析为 type", n2["kind"] == "type" and not n2.get("fuzzy"),
          str(n2))
    # 前缀同名类型:子串命中路径时也要把类型候选带上,别让模型只看到贴图
    n3 = resolve_target(conn, "Enem")
    check("子串命中带类型候选",
          any("Enemy" in c["full_name"] for c in n3.get("type_candidates", [])),
          str(n3))
    # 路径完全不匹配、只有同前缀类型 -> 直接给候选,不返回空结果
    conn.execute("INSERT INTO types(name, full_name, kind, namespace, bases,"
                 " modifiers, attributes, file, guid, line) VALUES"
                 " ('ZetaThing','Game.ZetaThing','class','Game','','public','',"
                 "'Assets/Scripts/Enemy.cs','x',1)")
    n4 = resolve_target(conn, "Zeta")
    check("无路径匹配但有同前缀类型 -> 给候选",
          n4["kind"] == "ambiguous"
          and any("ZetaThing" in c for c in n4["candidates"]), str(n4))
    conn.rollback()
    conn.close()
    r = queries.impact(FIXTURE, "Util")
    check("impact 把 hint 透出", "hint" in r, str(r)[:200])
    r2 = queries.impact(FIXTURE, "NoSuchThingAtAll")
    check("未知目标也给 hint", "hint" in r2, str(r2))
    txt = context_mod.build_context(FIXTURE, "NoSuchThingAtAll")
    check("context 未解析不崩且给提示", "解析提示" in txt, txt[:200])


def test_caller_scope():
    print("[16] 外部/内部调用方分区")
    r = queries.impact(FIXTURE, "Enemy")
    scopes = {c["caller"]: c["scope"] for c in r["code_dependents"]}
    check("Player.Attack 是外部调用方",
          any(v == "external" for k, v in scopes.items() if "Player.Attack" in k),
          str(scopes))
    internal = [c for c in queries.impact(FIXTURE, "Enemy")["code_dependents"]
                if c["scope"] == "internal"]
    check("Enemy 自己调 Die 算内部自调用",
          any("Die" in c["calls"] for c in internal), str(internal))
    s = r["summary"]
    check("摘要拆出 external/internal",
          s["external_callers"] + s["internal_calls"] == s["code_dependents"],
          str(s))
    text = context_mod.build_context(FIXTURE, "Enemy")
    check("上下文包分两段", "外部调用方" in text and "内部调用" in text,
          text[-800:])


def test_deadcode_exclude():
    print("[17] deadcode 排除 + 目录聚合")
    base = queries.dead_code(FIXTURE)
    check("by_directory 聚合存在",
          isinstance(base["by_directory"]["dead_methods"], dict)
          and base["by_directory"]["dead_methods"], str(base["by_directory"]))
    r = queries.dead_code(FIXTURE, exclude=["Util"])
    check("排除 Util 目录后 NeverCalled 消失",
          not any("NeverCalled" in m["method"] for m in r["dead_methods"]),
          str(r["dead_methods"]))
    check("排除模式回显", r["summary"]["excluded_patterns"] == ["Util"],
          str(r["summary"]))
    check("排除命中计数 >=1", r["summary"]["excluded_hits"] >= 1, str(r["summary"]))
    check("未排除时仍能报出", any("NeverCalled" in m["method"]
                                  for m in base["dead_methods"]))


def test_project_config():
    print("[18] .unity-llm.json 项目配置")
    import shutil
    import tempfile
    from unity_llm import meta
    tmp = tempfile.mkdtemp(prefix="unity_llm_cfg_")
    try:
        dst = os.path.join(tmp, "Proj")
        shutil.copytree(FIXTURE, dst, ignore=shutil.ignore_patterns(".unity-llm"))
        with open(os.path.join(dst, meta.CONFIG_FILE), "w", encoding="utf-8") as f:
            json.dump({"dead_code_exclude": ["Util"],
                       "external_segments": ["UI"]}, f)
        meta._CONFIG_CACHE.pop(os.path.abspath(dst), None)
        graph.build(dst)
        r = queries.dead_code(dst)
        check("配置里的 dead_code_exclude 生效",
              not any("NeverCalled" in m["method"] for m in r["dead_methods"]),
              str(r["dead_methods"]))
        check("external_segments 影响 external 标记",
              meta.is_external("Assets/UI/HealthBar.png", dst), "UI 应被判为外部")
        check("未配置的项目不受影响",
              not meta.is_external("Assets/UI/HealthBar.png", FIXTURE))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_guid_warning_precision():
    print("[19] guid 告警不误报")
    from unity_llm.graph import connect
    from unity_llm.queries import _guid_health
    conn = connect(FIXTURE)
    h = _guid_health(conn)
    check("fixture 无非标准 guid", h["nonstandard"] == 0, str(h))
    # 造一个包目录下、无入边的非标准 guid 资产 -> 应判为可忽略
    conn.execute("INSERT INTO assets(guid, path, ext, external) VALUES"
                 " ('BywbvXz+BiogUSgqOIts','LocalPackages/x/package.json',"
                 "'.json',1)")
    h2 = _guid_health(conn)
    check("包内无入边 -> 不算风险", h2["nonstandard"] == 1 and h2["risky"] == 0,
          str(h2))
    check("文案是可忽略而非告警", "可忽略" in h2["message"], h2["message"])
    # Assets/ 下的同类资产 -> 必须报
    conn.execute("INSERT INTO assets(guid, path, ext, external) VALUES"
                 " ('BywbvXz+BiogUSgqOItt','Assets/Art/Thing.png','.png',0)")
    h3 = _guid_health(conn)
    check("Assets 下 -> 算风险", h3["risky"] == 1, str(h3))
    check("风险文案要求导出 guidmap", "Dump GUID Map" in h3["message"],
          h3["message"])
    conn.rollback()
    conn.close()
    s = queries.stats(FIXTURE)
    check("stats 无 guid 告警", "guid_warning" not in s, str(s))


def test_method_ref():
    print("[20] 委托/方法组引用 + 死字段精度")
    r = queries.dead_code(FIXTURE)
    methods = {m["method"] for m in r["dead_methods"]}
    check("OnHpChanged 不误报(作为委托传参)",
          not any("OnHpChanged" in m for m in methods), str(methods))
    conn = graph.connect(FIXTURE)
    rows = list(conn.execute(
        "SELECT src_owner, name, kind, confidence FROM calls "
        "WHERE kind='method_ref' AND name='OnHpChanged'"))
    check("method_ref 边落库", len(rows) == 1, str(rows))
    check("同类方法组 -> high confidence",
          rows and rows[0]["confidence"] == "high", str(rows))
    imp = queries.impact(FIXTURE, "Player.OnHpChanged")
    kinds = {d.get("via") for d in imp.get("code_dependents", [])}
    check("impact 能看到 method_ref 依赖", "method_ref" in kinds, str(kinds))
    flds = {f["field"] for f in r["maybe_unused_fields"]}
    check("neverTouched 被报(prefab/代码都没用)",
          any("neverTouched" in f for f in flds), str(flds))
    check("hp 不误报(代码里读写过)", not any(f.endswith(".hp") for f in flds),
          str(flds))
    conn.close()


def test_parser_masking():
    print("[21] 注释/字符串遮罩 + 字符串耦合边")
    from unity_llm.csharp import parse_csharp
    src = ('class A {\n'
           '    const string U = "https://x.com/a";  // 带 // 的 URL\n'
           '    void Go() { SendMessage("Ping"); Helper(); }\n'
           '    void Helper() { }\n'
           '    public int keep = 1;\n'
           '}\n')
    f = parse_csharp(src)
    t = f.types[0]
    names = {m.name for m in t.methods}
    check("URL 里的 // 不吃掉后面的代码", {"Go", "Helper"} <= names, str(names))
    check("URL 之后的字段仍能解析", "keep" in {x.name for x in t.fields},
          str([x.name for x in t.fields]))
    go = next(m for m in t.methods if m.name == "Go")
    strs = [c.arg for c in go.calls if c.kind == "api_string"]
    check("SendMessage 字符串参数抓到", strs == ["Ping"], str(strs))
    check("字段被代码读写 -> code_used",
          next(x for x in t.fields if x.name == "keep").code_used is False,
          "keep 只声明一次,不该算 used")
    src2 = ('class B {\n'
            '    public string VerifyServer;\n'
            '    public string Dump() => $"v: {VerifyServer} end";\n'
            '}\n')
    b = parse_csharp(src2).types[0]
    check("内插字符串里的字段算 code_used",
          next(x for x in b.fields if x.name == "VerifyServer").code_used,
          "$\"{VerifyServer}\" 里的洞是真代码")


def test_partial_and_relay():
    print("[22] partial 合并 + Type.Field.Method 链式调用 + is_mono 基类链")
    from unity_llm.graph import connect
    from unity_llm.queries import resolve_target
    conn = connect(FIXTURE)
    n = resolve_target(conn, "BattleCore")
    check("短名 BattleCore 不因 partial 变成 ambiguous",
          n.get("kind") == "type", str(n))
    check("partial 标记", n.get("partial") is True, str(n))
    check("guid 并集包含主文件和 Net 文件",
          len(n.get("guids") or []) >= 2, str(n.get("guids")))
    shop = resolve_target(conn, "ShopPage")
    check("ShopPage 沿 BasePage 链标成 MonoBehaviour",
          shop.get("is_mono") == 1, str(shop))
    conn.close()

    imp = queries.impact(FIXTURE, "BattleCore.OnGiveup")
    assets = {a["asset"] for a in imp["asset_dependents"]}
    check("方法级 impact 能看到主文件挂的 prefab(不是 Net.cs 的 guid)",
          any("BattleCore.prefab" in a for a in assets), str(assets))
    mounted = queries.components(FIXTURE, "BattleCore")
    on_go = {m["on"] for m in mounted.get("mounted_on") or []}
    check("BattleCore 挂在 BattleCoreGO 上",
          "BattleCoreGO" in on_go, str(mounted))

    rly = queries.impact(FIXTURE, "EventHub")
    vias = {c["via"] for c in rly["code_dependents"]}
    check("EventHub 能看到 field_call 边", "field_call" in vias, str(rly["code_dependents"]))
    field = queries.impact(FIXTURE, "EventHub.ScoreChanged")
    check("Owner.Field 解析为 field",
          field["resolved"].get("kind") == "field", str(field["resolved"]))
    callers = {c["caller"] for c in field["code_dependents"]}
    check("BusListener.Start 经静态字段调用了 ScoreChanged",
          any("BusListener.Start" in c for c in callers), str(callers))
    unused = queries.impact(FIXTURE, "EventHub.UnusedChannel")
    check("没人用的静态字段没有外部调用方",
          unused["summary"].get("code_dependents", 0) == 0, str(unused["summary"]))


def test_correctness_hardening():
    print("[23] namespace 消歧 + MCP 不隐式建图")
    conn = graph.connect(FIXTURE)
    cur = conn.cursor()
    for full in ("N1.Widget", "N2.Widget", "N2.Caller", "N3.Caller"):
        name, ns = full.rsplit(".", 1)[1], full.rsplit(".", 1)[0]
        cur.execute(
            "INSERT INTO types(name,full_name,kind,namespace,bases,modifiers,"
            "attributes,file,guid,line) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (name, full, "class", ns, "", "", "", "synthetic.cs", "", 1))
    for owner, name in (("N1.Widget", "Foo"), ("N2.Widget", "Foo"),
                        ("N1.Widget", "Unique")):
        cur.execute(
            "INSERT INTO members(owner,kind,name,signature,modifiers,attributes,"
            "extra,line,file,guid) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (owner, "method", name, name + "()", "", "", "void", 1,
             "synthetic.cs", ""))
    for src, name in (("N2.Caller", "Foo"), ("N3.Caller", "Unique")):
        cur.execute(
            "INSERT INTO calls(src_owner,src_member,kind,target,name,recv,"
            "recv_type,arg,line,file) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (src, "Run", "dotcall", "Widget." + name, name, "Widget",
             "Widget", "", 1, "synthetic.cs"))
    graph._resolve_calls(cur)
    local = cur.execute(
        "SELECT resolved_owner,confidence FROM calls"
        " WHERE file='synthetic.cs' AND name='Foo'").fetchone()
    ambiguous = cur.execute(
        "SELECT resolved_owner,confidence FROM calls"
        " WHERE file='synthetic.cs' AND name='Unique'").fetchone()
    check("同 namespace 唯一候选可 high",
          tuple(local) == ("N2.Widget", "high"), str(dict(local)))
    check("无 namespace 证据的重名类型不误标 high",
          ambiguous["confidence"] == "medium"
          and ambiguous["resolved_owner"] == "N1.Widget", str(dict(ambiguous)))
    conn.rollback()
    conn.close()

    import tempfile
    import shutil
    root = tempfile.mkdtemp(prefix="unity_llm_no_auto_")
    try:
        os.makedirs(os.path.join(root, "Assets"))
        dispatch = make_dispatcher(root)
        try:
            dispatch("unity_impact", {"target": "X"})
            raised = False
        except RuntimeError:
            raised = True
        check("MCP 查询不隐式触发昂贵全量 build",
              raised and not graph.has_graph(root))
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_atomic_build():
    print("[24] 原子 build + 延后索引")
    import shutil
    import tempfile
    tmp = tempfile.mkdtemp(prefix="unity_llm_atomic_")
    try:
        dst = os.path.join(tmp, "Proj")
        shutil.copytree(FIXTURE, dst, ignore=shutil.ignore_patterns(".unity-llm"))
        graph.build(dst)
        before = queries.stats(dst)["scripts"]
        old = graph._resolve_calls

        def fail_after_parse(cur, progress=None):
            raise RuntimeError("synthetic build failure")

        graph._resolve_calls = fail_after_parse
        try:
            graph.build(dst)
            raised = False
        except RuntimeError:
            raised = True
        finally:
            graph._resolve_calls = old
        check("失败 build 不覆盖旧 graph.db",
              raised and queries.stats(dst)["scripts"] == before)
        conn = graph.connect(dst)
        idx = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        conn.close()
        check("calls(kind,arg) 复合索引存在",
              "idx_calls_kind_arg" in idx, str(idx))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_hierarchy():
    print("[25] 层级树(fileID 对象图)")
    # 多层 Transform:UIRoot 下挂 Panel 和 Button 两个子节点
    r = queries.components(FIXTURE, "Assets/Prefabs/UiPanel.prefab")
    hier = r.get("hierarchy", "")
    check("层级树存在", bool(hier), str(r))
    lines = [l for l in hier.split("\n") if l.strip()]
    check("根节点是 UIRoot", lines[0].strip() == "UIRoot", str(lines))
    check("Panel 缩进为子节点", "  Panel" in lines, str(lines))
    check("Button 缩进为子节点", "  Button" in lines, str(lines))
    check("Panel 挂 BattleCore.cs", "BattleCore.cs" in hier, str(hier))
    check("Button 挂 Player.cs", "Player.cs" in hier, str(hier))
    check("game_objects 计数为 3", r["summary"]["game_objects"] == 3,
          str(r["summary"]))
    check("RectTransform 是变换节点、不当组件泄漏", "- RectTransform" not in hier,
          str(hier))
    # 单层 Transform:Enemy 挂两个 MonoBehaviour
    r2 = queries.components(FIXTURE, "Assets/Prefabs/Enemy.prefab")
    h2 = r2.get("hierarchy", "")
    check("Enemy 层级树根是 Enemy", h2.split("\n")[0].strip() == "Enemy", str(h2))
    check("Enemy 挂 Enemy.cs 和 Player.cs",
          "Enemy.cs" in h2 and "Player.cs" in h2, str(h2))
    # 无 Transform 的 GameObject(BattleCoreGO)也要能显示
    r3 = queries.components(FIXTURE, "Assets/Prefabs/BattleCore.prefab")
    h3 = r3.get("hierarchy", "")
    check("无 Transform 的 GO 也进层级树",
          h3.split("\n")[0].strip() == "BattleCoreGO", str(h3))
    check("BattleCoreGO 挂 BattleCore.cs", "BattleCore.cs" in h3, str(h3))


def test_build_progress():
    print("[26] build 进度输出")
    import io
    from contextlib import redirect_stderr
    quiet = io.StringIO()
    with redirect_stderr(quiet):
        graph.build(FIXTURE, verbose=False)
    check("默认不打进度", "build:" not in quiet.getvalue(), quiet.getvalue()[:200])
    buf = io.StringIO()
    with redirect_stderr(buf):
        graph.build(FIXTURE, verbose=True)
    text = buf.getvalue()
    check("进度写到 stderr", "build:" in text, text[:300])
    check("有 guid 扫描阶段", "[1/6]" in text, text[:500])
    check("有 C# 阶段", "[3/6]" in text, text[:800])
    check("有完成行", "完成" in text, text[-400:])


def test_visual_assets():
    print("[27] 视觉资产结构解析")
    from unity_llm import visual_assets as va
    # 真实 Unity 输出:文档体内的 key 带 2 空格缩进,列表项内联;fileID 可以是负数
    controller = """%YAML 1.1
--- !u!91 &9100000
AnimatorController:
  m_Name: Demo
  m_AnimatorLayers:
  - serializedVersion: 5
    m_Name: Base Layer
    m_StateMachine: {fileID: -1001}
    m_Mask: {fileID: 0}
--- !u!1107 &-1001
AnimatorStateMachine:
  m_Name: Base Layer
  m_ChildStates:
  - serializedVersion: 1
    m_State: {fileID: -1101}
    m_Position: {x: 0, y: 0, z: 0}
  - serializedVersion: 1
    m_State: {fileID: -1102}
    m_Position: {x: 0, y: 0, z: 0}
  m_ChildStateMachines:
  - serializedVersion: 1
    m_StateMachine: {fileID: -1108}
    m_Position: {x: 0, y: 0, z: 0}
  m_AnyStateTransitions:
  - {fileID: -3002}
  m_DefaultState: {fileID: -1101}
--- !u!1107 &-1108
AnimatorStateMachine:
  m_Name: "\\u5B50\\u673A"
  m_ChildStates:
  - serializedVersion: 1
    m_State: {fileID: -1103}
    m_Position: {x: 0, y: 0, z: 0}
  m_ChildStateMachines: []
  m_AnyStateTransitions: []
  m_DefaultState: {fileID: -1103}
--- !u!1102 &-1101
AnimatorState:
  m_Name: Idle
  m_Motion: {fileID: 7400000, guid: aa000000000000000000000000000001, type: 2}
  m_Transitions:
  - {fileID: -3001}
--- !u!1102 &-1102
AnimatorState:
  m_Name: Run
  m_Transitions: []
--- !u!1102 &-1103
AnimatorState:
  m_Name: "\\u9AA8\\u67B6|Open"
  m_Transitions: []
--- !u!1101 &-3001
AnimatorStateTransition:
  m_Conditions:
  - m_ConditionMode: 6
    m_ConditionEvent: Speed
    m_EventTreshold: 1.5
  m_DstState: {fileID: -1102}
--- !u!1101 &-3002
AnimatorStateTransition:
  m_Conditions: []
  m_DstState: {fileID: -1101}
"""
    a = va.parse_animator(controller)
    names = {s["name"] for s in a["states"]}
    by_name = {s["name"]: s for s in a["states"]}
    check("controller 名", a["name"] == "Demo", str(a["name"]))
    check("状态齐全", names == {"Idle", "Run", "骨架|Open"}, str(names))
    check("非 ASCII 名字解掉 \\uXXXX 转义", "骨架|Open" in names, str(names))
    check("层名抽到", [l["name"] for l in a["layers"]] == ["Base Layer"],
          str(a["layers"]))
    check("state 归到所在层", by_name["Idle"]["layer"] == "Base Layer",
          str(by_name["Idle"]))
    check("子状态机的 state 层路径带子机名",
          by_name["骨架|Open"]["layer"] == "Base Layer/子机",
          str(by_name["骨架|Open"]))
    check("默认状态标出来", by_name["Idle"]["is_default"] is True
          and by_name["Run"]["is_default"] is False, str(a["states"]))
    check("负 fileID 的 motion guid 抽到",
          [s["motion_guid"] for s in a["states"] if s["name"] == "Idle"]
          == ["aa000000000000000000000000000001"], str(a["states"]))
    by_from = {t["from"]: t for t in a["transitions"]}
    check("缩进的 m_Transitions 能解析(不能是 0 条)", len(a["transitions"]) == 2,
          str(a["transitions"]))
    check("state 转移指向目标 state 的 fileID",
          by_from["-1101"]["to"] == "-1102", str(by_from))
    check("转移带层信息", by_from["-1101"]["layer"] == "Base Layer", str(by_from))
    check("条件抽出参数名/模式/阈值",
          by_from["-1101"]["conditions"] == [
              {"event": "Speed", "mode": "6", "threshold": "1.5"}],
          str(by_from["-1101"]))
    check("AnyState 转移不丢(from 记 AnyState)",
          "AnyState" in by_from and by_from["AnyState"]["to"] == "-1101",
          str(by_from))

    # Timeline:track 是 MonoBehaviour(114),clip 是内联对象,m_Asset 指向同文件内的 playable asset
    playable = """%YAML 1.1
--- !u!114 &-5001
MonoBehaviour:
  m_Script: {fileID: 11500000, guid: bb000000000000000000000000000001, type: 3}
  m_Name: MyTrack
  m_Clips:
  - m_Version: 1
    m_Start: 1.5
    m_Duration: 2.25
    m_DisplayName: hit
    m_Asset: {fileID: -5002}
  m_Markers:
    m_Objects: []
--- !u!114 &-5002
MonoBehaviour:
  m_Script: {fileID: 11500000, guid: bb000000000000000000000000000002, type: 3}
  m_Clip: {fileID: 7400000, guid: cc000000000000000000000000000003, type: 2}
--- !u!114 &-5003
MonoBehaviour:
  m_Script: {fileID: 11500000, guid: bb000000000000000000000000000009, type: 3}
  m_Name: GroupTrack
  m_Children:
  - {fileID: -5001}
  m_Clips: []
--- !u!114 &-5004
MonoBehaviour:
  m_Script: {fileID: 11500000, guid: bb000000000000000000000000000001, type: 3}
  m_Name: Recorded
  m_Clips:
  - m_Version: 1
    m_Start: 0
    m_Duration: 1
    m_DisplayName: rec
    m_Asset: {fileID: -5005}
--- !u!114 &-5005
MonoBehaviour:
  m_Script: {fileID: 11500000, guid: bb00000000000000000000000000000a, type: 3}
  m_AnimationClip: {fileID: 7400001}
"""
    t = va.parse_timeline(playable)
    check("轨道解析出 3 条(含 GroupTrack)", len(t["tracks"]) == 3, str(len(t["tracks"])))
    by_fid = {tk["fileid"]: tk for tk in t["tracks"]}
    tr = by_fid["-5001"]
    check("轨道名/脚本 guid", tr["display_name"] == "MyTrack"
          and tr["script_guid"] == "bb000000000000000000000000000001", str(tr))
    check("clip 时序", [(c["start"], c["duration"], c["display_name"])
                      for c in tr["clips"]] == [(1.5, 2.25, "hit")], str(tr["clips"]))
    check("clip 外部资产 guid 跳过 m_Script 只取内容引用",
          tr["clips"][0]["asset_guid"] == "cc000000000000000000000000000003",
          str(tr["clips"][0]))
    check("GroupTrack 子轨道记到 parent_fileid",
          tr["parent_fileid"] == "-5003" and by_fid["-5003"]["parent_fileid"] == "",
          str([(x["fileid"], x["parent_fileid"]) for x in t["tracks"]]))
    rec = by_fid["-5004"]["clips"][0]
    check("内联(录制)clip 没外部 guid 也能靠 asset_kind 说明类型",
          rec["asset_guid"] == ""
          and rec["asset_kind"] == "bb00000000000000000000000000000a", str(rec))


def test_visual_query_end_to_end():
    print("[28] animator 查询 + 增量更新不留旧结构")
    import shutil
    import tempfile
    rel = "Assets/Anim/Demo.controller"
    r = queries.animator(FIXTURE, rel)
    states = {s["state"]: s for s in r.get("states", [])}
    check("建图后 animator 查得到状态", set(states) == {"Idle", "Run"}, str(r))
    check("状态带层名", states.get("Idle", {}).get("layer") == "Base Layer",
          str(states))
    check("默认状态标记", states.get("Idle", {}).get("default") is True, str(states))
    check("转移带条件",
          r["transitions"] and r["transitions"][0]["conditions"]
          == [{"event": "Speed", "mode": "3", "threshold": "0.1"}],
          str(r.get("transitions")))

    tmp = tempfile.mkdtemp(prefix="unity_llm_vis_")
    try:
        dst = os.path.join(tmp, "Proj")
        shutil.copytree(FIXTURE, dst, ignore=shutil.ignore_patterns(".unity-llm"))
        graph.build(dst)
        p = os.path.join(dst, rel.replace("/", os.sep))
        text = open(p, encoding="utf-8").read().replace("m_Name: Run",
                                                        "m_Name: Walk")
        open(p, "w", encoding="utf-8").write(text)
        u = graph.update_files(dst, [rel])
        check("增量更新 controller 无错误",
              u["updated"] == [rel] and not u["errors"], str(u))
        after = {s["state"] for s in queries.animator(dst, rel).get("states", [])}
        check("增量更新后状态机是新的、没留旧状态",
              after == {"Idle", "Walk"}, str(after))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_stale_detection():
    print("[29] stale 检测(图谱落后于磁盘时查询必须告警)")
    import shutil
    import tempfile
    tmp = tempfile.mkdtemp(prefix="unity_llm_test_")
    try:
        dst = os.path.join(tmp, "Proj")
        shutil.copytree(FIXTURE, dst, ignore=shutil.ignore_patterns(".unity-llm"))
        graph.build(dst)
        r = queries.impact(dst, "Enemy")
        check("新鲜图谱不带 stale 告警", "graph_stale" not in r, str(r.get("graph_stale")))
        # 改磁盘不改图:模拟 hook 挂掉 / pull 后还没刷
        enemy = os.path.join(dst, "Assets", "Scripts", "Enemy.cs")
        open(enemy, "a", encoding="utf-8").write("\n// touched\n")
        r2 = queries.impact(dst, "Enemy")
        check("磁盘变过 -> impact 带 graph_stale",
              "graph_stale" in r2
              and any(x["path"].endswith("Enemy.cs")
                      for x in r2["graph_stale"]["files"]), str(r2.get("graph_stale")))
        rr = queries.find_refs(dst, "Enemy")
        check("refs 同样带 graph_stale", "graph_stale" in rr)
        # update 之后告警消失
        graph.update_files(dst, ["Assets/Scripts/Enemy.cs"])
        r3 = queries.impact(dst, "Enemy")
        check("update 后告警消失", "graph_stale" not in r3, str(r3.get("graph_stale")))
        # 老库(没有 file_state 表)必须静默降级,不报错也不误报
        conn = graph.connect(dst)
        conn.execute("DROP TABLE file_state")
        conn.commit()
        conn.close()
        open(enemy, "a", encoding="utf-8").write("\n// touched again\n")
        r4 = queries.impact(dst, "Enemy")
        check("老库无 file_state -> 不告警也不报错",
              "graph_stale" not in r4 and "错误" not in str(r4.get("hint", "")),
              str(r4.get("graph_stale")))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_digest():
    print("[30] digest(一批变更文件的影响面摘要)")
    import shutil
    import tempfile
    tmp = tempfile.mkdtemp(prefix="unity_llm_test_")
    try:
        dst = os.path.join(tmp, "Proj")
        shutil.copytree(FIXTURE, dst, ignore=shutil.ignore_patterns(".unity-llm"))
        graph.build(dst)
        d = queries.digest(dst, ["Assets/Scripts/Enemy.cs",
                                 "Assets/Prefabs/Enemy.prefab"])
        check("文件分类正确",
              d["files_total"] == 2 and d["scripts"] == 1
              and d["yaml_assets"] == 1, str(d))
        check("调用方聚合含 Player",
              any("Player" in c["caller"] for c in d["top_code_callers"]),
              str(d["top_code_callers"]))
        check("资产引用方聚合含 Main.unity",
              any("Main.unity" in a["asset"] for a in d["top_asset_dependents"]),
              str(d["top_asset_dependents"]))
        check("挂载点统计到 Enemy.prefab", d["mounted_by"] >= 1, str(d))
        d2 = queries.digest(dst, ["Assets/NoSuchFile.cs"])
        check("未知文件进 unknown 不报错",
              d2["unknown"] == ["Assets/NoSuchFile.cs"], str(d2))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_usage():
    print("[31] usage(MCP 工具采用率报告)")
    import shutil
    import tempfile
    tmp = tempfile.mkdtemp(prefix="unity_llm_test_")
    try:
        dst = os.path.join(tmp, "Proj")
        os.makedirs(os.path.join(dst, ".unity-llm"))
        with open(os.path.join(dst, ".unity-llm", "calls.log"),
                  "w", encoding="utf-8") as f:
            f.write(json.dumps({"t": 1700000000, "tool": "unity_update",
                                "chars": 10, "approx_tokens": 5}) + "\n")
            f.write(json.dumps({"t": 1700000001, "tool": "unity_update",
                                "chars": 10, "approx_tokens": 5}) + "\n")
            f.write(json.dumps({"t": 1700000002, "tool": "unity_impact",
                                "chars": 400, "approx_tokens": 100}) + "\n")
        u = queries.usage_report(dst)
        check("工具计数正确",
              u["total"]["calls"] == 3
              and u["by_tool"][0]["tool"] == "unity_update", str(u))
        check("刷新占比 2/3", abs(u["update_share"] - 0.667) < 0.01, str(u))
        check("零调用核心工具被点名",
              "unity_refs" in u["never_called"]
              and "unity_components" in u["never_called"], str(u))
        u2 = queries.usage_report(os.path.join(tmp, "Empty"))
        check("无日志时给人话提示", "hint" in u2, str(u2))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    try:  # Windows 控制台默认 GBK,测试输出里有中文
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    ensure_png()
    tests = [test_build, test_stats, test_impact_type, test_impact_precision,
             test_impact_method, test_impact_by_guid_and_path, test_refs,
             test_components, test_deadcode, test_validate, test_find,
             test_context, test_mcp_smoke, test_update, test_resolve_fuzzy,
             test_caller_scope, test_deadcode_exclude, test_project_config,
             test_guid_warning_precision, test_method_ref,
             test_parser_masking, test_partial_and_relay,
             test_correctness_hardening, test_atomic_build, test_hierarchy,
             test_build_progress, test_visual_assets,
             test_visual_query_end_to_end,
             test_stale_detection, test_digest, test_usage]
    failed = 0
    for t in tests:
        try:
            t()
        except Exception as e:
            failed += 1
            print(f"  ERROR in {t.__name__}: {e}")
    print(f"\n{'='*50}")
    print(f"通过 {len(PASSED)} 项断言, {len(tests)-failed}/{len(tests)} 个测试组")
    if failed:
        print("有失败!", file=sys.stderr)
        return 1
    print("全部测试通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
