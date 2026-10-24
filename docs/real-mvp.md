# 真实 GitHub 仓库 API Migration MVP 1

## 改造前的结构与差距

现有 `rf_factory.cli` 调用 `Pipeline.run`，顺序经过 `RepositoryPreparer`、`ApiMigrationDiscovery`、候选筛选、`ApiMigrationSpecBuilder`、`ApiMigrationGoldGenerator`、`ProblemGenerator`、`OracleBuilder`、`verifier`、`Evaluator`、`InstanceBuilder`。各阶段的 JSON、Gold 和正负控制结果保留在运行目录。Toy 路线接受本地目录和显式 migration recipe，基线与 gold/no-op 测试在宿主机执行，导出的是 `rf.instance.v3`，Dockerfile 没有真实项目的依赖安装或 Git commit 约束。

MVP 1 的真实路线由 `run-real` 进入 `RealPipeline`。它从 GitHub URL 或该 GitHub 仓库的本地克隆取得独立 checkout，固定完整 SHA；扫描顶层双参数 Python 函数及静态调用图，自动选择把第二个位置参数迁移为仅关键字参数的候选。已有 Gold 变换与题面模块继续使用；新增独立 Test Patch、Docker 正负验证及 ProMax 导出。候选不来自历史 PR。

参考 `swe-series-factory` 的固定快照、阶段产物和 Docker 执行约束，但没有复制 NL2Repo、DeepSWE 或 ProgramBench 的任务语义。官方 ProMax 的数据项包含 `instance_id`、`repo`、`base_commit`、`environment_setup_commit`、`image_name`、`working_dir`、`patch`、`test_patch`、`problem_statement`、`hints_text`、`created_at`、`language`；`eval.json` 以实例 ID 为键并提供 `eval_script`。官方 `test_run.py` 在镜像 `/testbed` 应用模型或 Gold Patch，再运行 `eval_script`；脚本必须自行应用 Test Patch 并以退出码报告结果。镜像运行时断网。

## 实施文件与顺序

1. `rf_factory/real_pipeline.py`：获取/固定 commit、自动候选、Docker 基线、Gold/Test Patch、正负验证和官方导出。
2. `rf_factory/api_migration.py`：让 Gold 输出为 `git apply` 可识别的 Git diff；支持包 `__init__.py` 路径。
3. `rf_factory/config.py`、`rf_factory/cli.py`：自动发现 recipe 的空行为用例约定和 `run-real` 入口。
4. `scripts/official_eval_windows.py`：在 Windows 上调用未修改的官方评分模块，修正其 POSIX 单引号与临时文件换行问题。
5. `tests/test_real_pipeline.py`：候选发现和 Test Patch 的 no-op/gold 控制测试。

生成阶段使用独立克隆 `acquired/` 和无 Git 的 `source/`；发布成功后清理这些临时副本。发布目录保留固定 commit、镜像 ID、`repository.json`、候选诊断、`Dockerfile`、`environment.json`、`baseline.json`、每个候选的 Gold/Test Patch 与正负报告、`swe-bench-promax.json`、`eval.json`、`instances.jsonl` 和 `summary.json`。离线评测需预先加载该固定镜像；`prepare-rollout` 从镜像导出求解者工作区。

## 可复制运行命令

在项目根目录使用一个尚不存在的输出目录。下面的本地仓库是公开 GitHub 仓库的现有克隆；省略 `--local-path` 会从 URL 获取。

```powershell
python -m rf_factory run-real --repo-url https://github.com/peng-weihan/swe-series-factory --local-path D:\class\remote\SJT_LLMSE\swe-series-factory --commit 661d5b978a30e6d3517ad8232451353bb5e81ce1 --output runs --max-candidates 8 --base-image key4127/refactor-dockerhub:confident-ai__deepeval-c_08844c8 --offline-build --test-command 'python -m unittest discover -s tests -p test_factory_shared.py'
python -m rf_factory prepare-rollout --run runs/peng-weihan__swe-series-factory-c_661d5b9-37f35876 --instance peng-weihan__swe-series-factory-c_661d5b9-37f35876 --output <separate-solver-directory>
python scripts/official_eval_windows.py --official-script D:\class\remote\SJT_LLMSE\promax-official\src\evaluation\test_run.py --pred <predictions.json> --golden runs/peng-weihan__swe-series-factory-c_661d5b9-37f35876/swe-bench-promax.json --eval runs/peng-weihan__swe-series-factory-c_661d5b9-37f35876/eval.json --output <score.json> --local-images-only
```

`preds-gold.json` 是用于复核官方评分入口的预测文件，内容为 `[{"instance_id": "...", "model_patch": "<gold patch>"}]`。Linux 上可以直接调用官方 `src/evaluation/test_run.py`；Windows 原版脚本把 POSIX 单引号传给 `cmd.exe`，且临时脚本写成 CRLF，因此本机使用上述启动器。启动器只替换进程启动和临时文件写入，补丁应用、脚本执行及评分逻辑仍取自官方源码。

## 本次结果与边界

固定 SHA 为 `661d5b978a30e6d3517ad8232451353bb5e81ce1`。自动候选 `nl2repo_factory/common.py:require_process` 有 4 个调用模块、18 处静态调用。镜像 `rf-promax:c9859530dba17680` 内的 Git HEAD 与该 SHA 一致。选定的 13 个原有回归测试和新增 2 个 API 约束测试在 Gold 下通过；no-op 的原有测试通过而新增约束失败。官方评分模块通过 Windows 启动器得到模型 Gold 预测和 Golden 各 1/1 `success`。测试命令：`python -m pytest tests/test_real_pipeline.py -q` 为 2 passed；`python -m pytest tests/test_factory.py -q` 为 9 passed。

该仓库全量 173 个测试的容器基线有 1 个 CRLF 字节比较失败，所以本次只将可移植的 `test_factory_shared.py` 纳入回归门槛，不能声称全量回归通过。离线构建使用本机已有的官方 Python 镜像作基础，镜像含其预装依赖；在另一台机器重建需先取得该基础镜像。MVP 只识别保守静态调用、顶层双参数函数和关键字参数迁移，动态调用、复杂重导出、其他语言以及任意第三方仓库的依赖求解不在当前验证范围。候选失败会记录并尝试下一个候选；中断后的阶段续跑尚未实现。
