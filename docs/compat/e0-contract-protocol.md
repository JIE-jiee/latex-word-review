# E0 上游契约实验规范

## 目的

用同一组公开、确定性的输入验证上游真实行为，形成 `adopt`、`wrap`、`contribute`、`self-build` 决策。README、示例和类型签名只作为线索，不能代替运行结果。

## 隔离与可复现性

- Python 包安装到 `build/upstream-eval/` 的独立环境，不修改全局环境；
- 外部工具记录精确版本、来源、许可证和发现路径，但公开报告不保存个人绝对路径；
- 每次命令保存参数数组、工作目录的项目相对表示、退出码、标准输出/错误摘要、开始/结束时间；
- 输入和关键输出保存 SHA-256；DOCX 比较规范化 OOXML 和语义计数，不比较 ZIP 时间戳字节；
- 固定 fixture 作者、带时区时间、locale 和可控随机种子；
- 构建目录不进入发布物，结论与最小回归测试进入 `docs/` 和 `tests/`。

## 输入集合

1. `fixture-minimal`：中英正文、多文件、公式、图、表、标签、引用和文献；
2. `fixture-revisions`：插入、删除、替换、移动、格式修订、单点/范围批注、多作者和时间；
3. `fixture-anchors`：完好、缺失、重复和篡改锚点；
4. 每个 fixture 配套人工审定的 expected JSON，不以被测工具生成的输出作为唯一预言机。

## 转换契约

对 tex2word 和 Pandoc 基线至少记录：

- 安装、探测和最小导出的成败；
- 正文审阅单元、OMML、图片、表格、书签、字段和关系计数；
- manifest/source span/source map 可用性；
- 警告、未知结构和降级是否可机器读取；
- 相同输入重复运行的规范化语义是否稳定；
- 输出能否被 Word/LibreOffice 打开且无需修复。

## 修订读取契约

使用同一 returned DOCX 比较 Pandoc `--track-changes=all`、`docx-revisions` 和最小原始 OOXML 观测器：

- `w:ins`、`w:del`、`moveFrom`、`moveTo`、格式修订和 comments 的发现数；
- author、date、before、after、comment range 和原始 part/节点证据；
- 跨 run、跨段、嵌套和缺失元数据行为；
- 是否会自动接受修订、丢弃删除或改写原件；
- 重复运行的事件 ID 和顺序稳定性。

原始 OOXML 观测器在 E0 只充当测试预言机，不得演变为未经 ADR 的生产解析器。

## 结果记录最小字段

```json
{
  "experiment_id": "string",
  "fixture_sha256": "hex",
  "tool": {"name": "string", "version": "string", "source": "string"},
  "command": ["string"],
  "exit_code": 0,
  "duration_ms": 0,
  "artifacts": [{"path": "relative/path", "sha256": "hex"}],
  "observations": {},
  "diagnostics": [],
  "verdict": "pass|partial|fail|blocked"
}
```

## E0 退出门槛

- 每个候选均有真实 fixture 结果和最小复现命令；
- 明确主转换后端、对照后端和修订解析路径；
- 明确最低/最高已测版本、平台风险、依赖/外部可执行边界和替换策略；
- 所有已知内容丢失均被定位，任何静默删除行为直接判为生产路径不合格；
- ADR-0001 明确哪些能力上游贡献、哪些由本项目实现，以及为什么不 fork 或必须 fork。
