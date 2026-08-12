// unity-llm-graph — GUID 权威映射导出器
//
// 什么时候需要它:
//   大多数项目不需要 —— 框架默认从 .meta 文本读 guid,直接 build 即可。
//   但如果你的 .meta 被资产保护/打包流水线重写过(比如 guid 变成了 56 位
//   Base64 长串,而 prefab YAML 里存的仍是 Unity 真正在用的 hex guid),
//   磁盘上的 .meta 就不可信了。Unity 编辑器通过 AssetDatabase 拿到的才是
//   真 guid,这个脚本把它导出成映射表,build 时自动优先使用。
//
// 用法:
//   1. 把本文件复制到项目的任意 Editor 目录,如 Assets/Editor/
//   2. Unity 菜单:Tools → unity-llm → Dump GUID Map
//   3. 生成 <项目>/.unity-llm/guidmap.tsv(请勿提交 git,每个克隆各自生成)
//   4. 重新运行: python -m unity_llm build --project <项目>
//
// 无第三方依赖,兼容 Unity 2019+。
using System.IO;
using System.Text;
using UnityEditor;
using UnityEngine;

namespace UnityLlmGraph
{
    public static class UnityLlmGuidDump
    {
        const string OutputDir = ".unity-llm";
        const string OutputFile = "guidmap.tsv";

        [MenuItem("Tools/unity-llm/Dump GUID Map")]
        public static void Dump()
        {
            var sb = new StringBuilder(1 << 20);
            int count = 0;
            foreach (var path in AssetDatabase.GetAllAssetPaths())
            {
                // 只要 Assets 和内嵌 Packages;Library/PackageCache 的缓存包
                // 由框架自己扫 .meta 补齐(路径形态不同,导出了也对不上)
                if (!path.StartsWith("Assets/") && !path.StartsWith("Packages/"))
                    continue;
                var guid = AssetDatabase.AssetPathToGUID(path);
                if (string.IsNullOrEmpty(guid))
                    continue;
                sb.Append(guid).Append('\t').Append(path).Append('\n');
                count++;
            }

            var dir = Path.Combine(Directory.GetCurrentDirectory(), OutputDir);
            Directory.CreateDirectory(dir);
            var file = Path.Combine(dir, OutputFile);
            File.WriteAllText(file, sb.ToString(), new UTF8Encoding(false));
            Debug.Log($"[unity-llm] GUID map dumped: {count} entries -> {file}\n" +
                      "现在可以重新运行 python -m unity_llm build");
        }
    }
}
