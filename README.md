# RF Instance Factory

MVP2 pilot: [multi-file API migration and precheck](docs/pilot.md).

这是用于研究 **SWE-Bench ProMax Refactoring instance synthesis 的初始实验框架**，不是 SWE-Bench ProMax 官方实现，也不是论文方法的完整复现。

从可信本地 Python 仓库发现重构候选、生成任务、执行基本验证并导出 JSON/JSONL。无运行时第三方依赖，无需模型或 Docker。实际参考分析见 [docs/reference-analysis.md](docs/reference-analysis.md)。

## 安装和运行

Python 3.11+，在项目根目录用 Windows PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m rf_factory demo --output runs/demo-001
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

也可以不安装，直接在根目录用 `python -m rf_factory`。安装后的命令为 `rf-factory`。

```powershell
python -m rf_factory validate-config --config examples/local.toml
python -m rf_factory run --config examples/local.toml --output runs/local-001
```

TOML 的 repository.source 相对配置文件解析。每次使用不存在的新输出目录，必须位于 source 外部。退出码：0 有 instance，1 无有效 instance，2 配置/执行错误；validate-config 仅检查配置。

测试在宿主进程运行，只适用于可信本地代码，没有安全隔离或自动依赖安装。当前适配器支持 unittest；pytest/其他语言需替换 oracle。超时终止直接测试进程，不保证清理其所有后代。

## 目录与数据流

```text
rf_factory/
  cli.py / __main__.py          CLI
  config.py / models.py         TOML 与显式数据契约
  contracts.py                 可替换阶段的 Protocol 接口
  common.py                    JSON/JSONL、哈希、路径
  pipeline.py                  调度与阶段事件
  repository.py                源码快照
  discovery.py                 AST 候选发现
  synthesis.py / prompts/      任务生成与模板
  oracle.py / test_runner.py   基线测试计划与执行
  validation.py                基本门禁
  packaging.py                 instance 封装
  fixtures/tiny_repo/           示例仓库及四项测试
examples/local.toml
tests/test_factory.py
docs/reference-analysis.md
```

数据流：source → Repository → Candidate[] → Task[] → OraclePlan → Validation[] → Instance[] → JSONL。

| Stage | 当前实现 |
| --- | --- |
| preparation | 复制工作树，排除 Git/环境/cache；拒绝链接；逐文件和整体 SHA256；检查复制期间变化 |
| discovery | AST 静态解析，按行数和语句数筛选模块级同步无装饰器函数；排除测试，记录解析失败 |
| synthesis | 生成提取同模块 private helper 的任务，明确位置、行为保持与修改范围 |
| oracle preparation | 固定 unittest 目录、pattern、timeout，保存计划 |
| validation | 独立副本跑真实测试，保存日志/数量/失败/skip/超时；核对快照、候选哈希、AST 位置和字段 |
| packaging | 只封装通过门禁的任务；版本化 schema、稳定内容 ID、明确验证限制 |
| export | UTF-8 JSON 和逐行 instance JSONL，保存事件和失败摘要 |

默认要求非空且全部成功的测试；失败、skip、expected failure、收集错误和超时均拒绝。require_tests=false 仅允许测试目录不存在时降为结构验证，存在的测试失败仍拒绝。

## 输出

最终文件为 `runs/demo-001/instances.jsonl`。保留整个 run 目录，repository.snapshot 相对 run 根解析。

```text
<run>/
  config.json, runtime.json, events.jsonl, summary.json
  source/, repository.json
  candidates.json, discovery_diagnostics.json, tasks.json
  oracle_plan.json, validation.json
  baseline/{workspace/,tests.json,result.json,stdout.txt,stderr.txt}
  instances.json, instances.jsonl
```

instance 的结构（嵌套内容省略，完整定义见 models.py，完整样本由 demo 生成）：

```json
{
  "schema_version": "rf.instance.v1",
  "instance_id": "rf-<content-id>",
  "task_type": "refactoring",
  "status": "mvp_validated",
  "repository": {"snapshot": "source", "content_sha256": "...", "files": {}},
  "task": {"candidate": {}, "problem_statement": "...", "requirements": []},
  "oracle": {"mode": "baseline-unittest"},
  "validation": {"passed": true, "behavioral_equivalence": "not_proven", "limitations": []}
}
```

源工作树按内容标识，包含未提交文件，不冒充固定 Git commit。ID 包含源码、任务、模板和测试计划；运行日志含本机绝对路径。快照可脱离原输入目录使用，Python 环境和依赖需自行准备。

## MVP 边界与研究接口

临时实现是“函数长度启发式 + 固定 helper extraction 模板 + 原代码 unittest 基线”。未生成 gold patch、未执行重构后测试、未证明提取可行性或行为等价，不提供 solver evaluator、结构改进评分、难度评估或官方格式兼容。行数包含空行和注释，候选需审阅。

预留接口是 Pipeline 构造函数的 preparer/discovery/synthesizer/oracle/validator/packager 注入及 models.py 契约。每个阶段可直接调用，方法和返回值见 contracts.py 的 Protocol 和默认实现。尚无自动续跑、多仓库批调度、并发写入协议。

优先改进：候选结构质量与可重构性 → synthesis 的精确约束和参考重构 → oracle 的前后差分/属性测试与结构验收 → 独立 evaluator、容器环境、跨仓库复现。算法改变语义时升级 schema/strategy 版本，继续保留验证边界和失败证据。
