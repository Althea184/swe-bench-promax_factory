# 参考 factory 工程分析

参考目录仅阅读，未导入、运行或修改。依据三个 pipeline、CLI、config、共享 snapshots/reports/roles、demo 和测试的实际代码；不以参考任务算法作为 RF 方法依据。

| 方面 | NL2Repo | DeepSWE | ProgramBench |
| --- | --- | --- | --- |
| 包结构 | source、ast_scan、specification、agents、packaging、docker、oracle、repair、verifier | pipeline、packaging、grader、demo、prompts | pipeline、probe、quality、delivery、actions、demo、prompts |
| 入口 | __main__ → cli.main → Pipeline(Config) | __main__ → cli → swe_factory.cli | 同共享 CLI，以 family 分流 |
| stages | acquisition → A1 → AST → A2/coverage → packaging/leak → oracle → repair → empty control | ablation/query/specialize 三路 → matrix → emit → selfcheck | acquire → generate → build → probe → QC → independent rebuild |
| config | dataclass Config、TOML、配置相对路径、类型/预算检查 | 共享 config：runtime/agent/pipeline/model | 共享 config，另有 rebuild/min_kept |
| 数据传递 | dict、Path、Agent 响应契约 | candidate、role response、plan、reports | candidate、manifest、observations、quality |
| 中间结果 | stage JSON、事件 JSONL、checkpoint、repair transaction | source、roles、patches、matrix/gate、stages、summary | build/probe/test mapping/quality/source cleaning、summary |
| 验证 | Oracle、空答案、独立 verifier、受限修复 | 原生 JUnit、红绿矩阵、P2P/F2P、导出 grader | 重复字节 probe、dummy/gold QC、独立 Docker 重建 |
| fixture | 包内 TagKit 仓库、人工题面/manifest | demo 代码内 Python fixture + synthetic agent | demo 代码内 C CLI + synthetic agent |

共同工程模式：Python 3.11+、标准库优先、pathlib、argparse 子命令、模块与 console script 双入口；独立源码快照、新输出目录、哈希 provenance、原始日志和失败证据。没有统一 stage 基类，pipeline 调度具体模块、runner 和 agent。

schema 主要为 dict + 运行时验证，部分 config/process 使用 dataclass；不存在三个 factory 共用的完整 instance schema。JSON 用 common.write_json 临时文件替换；JSONL 主要用于输入候选和 NL2Repo 事件。交付主要是目录包，RF 需自行定义版本化 instance JSONL 契约。

prompts 按角色放 markdown，Roles/CommandAgent 保存完整请求、响应、命令与日志。测试使用 unittest、TemporaryDirectory、fixture、模拟外部适配器及真实子进程 smoke。错误按 stage 保留摘要，CLI 输出机器可读 JSON；构建、内部验证、正式接受分开。NL2Repo 有额外重试和事务，另外两个以 FactoryError + summary 为主。

具体任务逻辑不直接复用：NL2Repo 的 API coverage/空答案、DeepSWE 的消融/F2P、ProgramBench 的字节探测/源码清理都不等于 RF 验收。RF 原始代码应已通过测试，更需要前后行为保持和结构改进检查。

RF 落地：独立 stage 对象、构造函数注入、dataclass 契约，保留快照和阶段产物；AST heuristic、模板 synthesis、unittest baseline 为可替换 MVP 策略。不复制 Docker/Agent/repair 体系，不引入复杂基类、自动续跑或多控制器协议。
