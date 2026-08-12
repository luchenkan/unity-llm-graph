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
"""
import base64
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from unity_llm import graph, queries, context as context_mod  # noqa: E402
from unity_llm.mcp_server import TOOLS  # noqa: E402

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
    check("脚本数", stats["scripts"] == 5, str(stats))
    check("类型数", stats["types"] == 5, str(stats))
    check("YAML 资产数", stats["yaml_assets"] == 3, str(stats))
    check("序列化引用数>=5", stats["refs"] >= 5, str(stats))
    check("资产数>=9", stats["assets"] >= 9, str(stats))
    check("UnityEvent 绑定==1", stats["events"] == 1, str(stats))
    check("guid 来源为 meta", stats["guid_source"] == "meta", str(stats))
    check("无解析错误", stats["errors"] == 0, str(stats))


def test_stats():
    print("[2] stats")
    s = queries.stats(FIXTURE)
    check("mono_behaviours==2", s["mono_behaviours"] == 2, str(s))
    check("scriptable_objects==1", s["scriptable_objects"] == 1, str(s))
    check("生命周期方法==3(Start/Update/Awake)", s["lifecycle_methods"] == 3, str(s))
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
    check("预算内", len(text) <= 2000 * 4 * 2, str(len(text)))


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
    check(f"tools/list {len(TOOLS)} 个工具", len(names) == len(TOOLS), str(names))
    check("含 unity_components / unity_validate",
          {"unity_components", "unity_validate"} <= names, str(names))
    call = rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "unity_impact",
                           "arguments": {"target": "TakeDamage"}}})
    payload = json.loads(call["result"]["content"][0]["text"])
    check("tools/call unity_impact",
          any("Player.Attack" in c["caller"]
              for c in payload["code_dependents"]), str(payload)[:300])
    call2 = rpc({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                 "params": {"name": "unity_stats", "arguments": {}}})
    payload2 = json.loads(call2["result"]["content"][0]["text"])
    check("tools/call unity_stats", payload2["scripts"] == 5, str(payload2))
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
    check("上下文包分两段", "外部调用方" in text and "内部自调用" in text,
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
             test_parser_masking]
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
