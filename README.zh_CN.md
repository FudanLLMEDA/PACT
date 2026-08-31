# PACT Artifact

[English](README.md) | [简体中文](README.zh_CN.md)

PACT（Post-route Agentic Checkpoint Tuning，布线后智能体检查点调优）通过带验证门控的 Vivado 与 RapidWright 操作，优化已经实现的 AMD FPGA 设计检查点。本仓库包含 PACT 智能体、两个 MCP 后端服务器、优化知识库、验证工具以及论文 Artifact 材料，也是我们 FPL'26 决赛 Top 5 方案的净化开源版本。

## Artifact 范围

快速流程使用的 FPL'26 公开检查点由 `make setup` 下载；仓库同时保留可再分发评测集的 Git LFS 指针。仓库不分发 Vivado、Java、私有评测输出、凭据、优化后检查点和生成的运行目录。当前内容支持源码审阅、轻量源码检查，以及在公开检查点上执行端到端优化。

本版本集成以下源码快照：

- `FudanLLMEDA/FDAgents`：`7691f3a99a304667b68f81d67bd6b41cb59d64ad`
- 竞赛集成仓库：`1d6bef8d909e0ea751c398ea2a45248b37c1c679`

私有开发分支、凭据、部署基础设施、隐藏基准 Artifact、原始运行数据和内部结果均有意排除。

本快照不包含论文完整的 35 个设计结果包，尤其不包含全部 35 个输入 DCP、所有逐设计原始结果，以及完整的聚合和绘图脚本。因此，仅凭本仓库目前无法重新计算论文中的全部汇总结果。请勿对本快照申报 Results Replicated 徽章；应先补齐“复现论文结果”一节列出的完整结果包和工作流。

## 仓库结构

- `FDAgents/`：PACT 智能体、类型化技能、配方规划器、决策逻辑与单次运行内案例记忆
- `VivadoMCP/`：Vivado MCP 后端
- `RapidWrightMCP/`：RapidWright MCP 后端
- `knowledge/`：优化知识与实验记录
- `docs/`：补充优化示例
- `baselines/`：论文基线运行器与参考表
- `benchmarks/`：公开/可复现检查点清单与 Git LFS 指针
- `journals/`：为 Artifact 评测保留的论文源文件
- `validate_dcps.py`：两个 DCP 的结构与随机仿真比较工具
- `runs/`：生成的运行输出，已被 Git 忽略

## 硬件与软件要求

参考 Artifact 机器于 2026-07-30 测得，配置如下：

- Microsoft Azure x86-64 虚拟机，8 个逻辑 CPU（4 核、每核 2 线程），宿主为 AMD EPYC 9V45 96-Core Processor
- 31 GiB 内存，无交换分区
- Ubuntu 24.04.4 LTS，Linux 内核 `6.8.0-1059-azure`
- 495 GiB 根文件系统；开始新运行前，PACT 与竞赛工作区约占 15 GiB
- AMD Vivado 2025.1 64 位，SW build 6140274、IP build 6138677、SharedData build 6139179，并具有 `xcvu3p-ffvc1517-2-e` 的有效许可证
- Python 3.13.14，由固定版本的 `uv` 环境安装
- RapidWright Python 包 2026.1.0；仓库内源码固定在 `f63afef5d34ad71e2544f0c1565c5286d16139fa`
- OpenJDK 11.0.16.1（Vivado 内置 Temurin 运行时）
- MCP 必须严格为 1.28.1；其余 Python 依赖锁定在 `uv.lock`

运行单个设计至少建议分配 8 个 CPU 线程、32 GiB 内存和 25 GiB 可用磁盘。完整重跑 35 个设计时，建议使用参考机器的 495 GiB 文件系统或容量相当的外部工作区，因为并行 Vivado 运行、检查点和日志会快速占用空间。公开检查点压缩包本身约为 525 MB。

不需要实体 FPGA 板卡、加速器或 GPU。Vivado 是专有软件，不包含在本仓库中。安装过程需要网络；使用 LLM 引导流程时，还需要访问 OpenAI-compatible Responses API。单次优化最长可能运行一小时，并临时占用数 GiB 磁盘。

## 安装

克隆 Artifact 及固定版本的 RapidWright 子模块：

```bash
git clone --recurse-submodules \
  https://github.com/FudanLLMEDA/PACT.git
cd PACT
```

也可以解压从 Artifact DOI 下载的归档并进入顶层目录。若归档源码包不包含 Git 子模块元数据，`make setup` 会直接从上游仓库获取同一个固定版本的 RapidWright。

执行自包含安装目标：

```bash
make setup VIVADO_EXEC=/path/to/Vivado/2025.1/bin/vivado
```

`make setup` 会安装固定版本的 `uv`、Python 3.13.14 和锁定依赖，构建固定版本的 RapidWright，并下载 FPL'26 竞赛检查点压缩包 v1.2.0。若自动下载不可用，可手动从 [FPL'26 optimization contest v1.2.0 release](https://github.com/Xilinx/fpl26_optimization_contest/releases/tag/v1.2.0) 下载 `fpl26_contest_benchmarks_v1.2.0.tar.gz`，放在仓库根目录后重新执行 `make setup`。

复制环境变量模板并按本机环境填写：

```bash
cp .env.example .env
```

至少需要设置 `VIVADO_EXEC` 和 `RAPIDWRIGHT_PATH`。与竞赛提交兼容的运行使用 `OPENROUTER_API_KEY`；非提交模式也可使用 `OPENAI_API_KEY` 和可选的 `OPENAI_BASE_URL`。不要提交 `.env`。

## 快速检查

以下检查不需要 Vivado、RapidWright、DCP 或 API key，只会编译并导入公开 Python 源码：

```bash
make python-env
make check
```

这是源码/包完整性检查，不等价于复现论文中的 FPGA 时序结果。

## 端到端功能流程

### 不使用 LLM 的确定性运行

在一个公开检查点上运行 PACT 的规则/回退路径：

```bash
make run_test \
  DCP=fpl26_contest_benchmarks/vexriscv_re-place_2025.1.dcp
```

该命令等价于：

```bash
python -m FDAgents.agent INPUT.dcp --no-llm --time-limit 3600
```

### LLM 引导的 PACT 运行

对于与竞赛提交兼容的流程，设置 `OPENROUTER_API_KEY` 后执行：

```bash
make run_optimizer \
  DCP=fpl26_contest_benchmarks/logicnets_jscl_2025.1.dcp
```

若需要使用其他 OpenAI-compatible Responses endpoint，或显式指定输出位置，可直接调用底层 CLI：

```bash
python -m FDAgents.agent INPUT.dcp \
  --model MODEL_NAME \
  --time-limit 3600 \
  --run-dir runs/example \
  --output runs/example/optimized.dcp
```

运行结束时，PACT 会输出基线与最终 WNS/Fmax、耗时、token 使用量和估算成本（LLM 模式）、输出 DCP 路径及运行目录。运行目录还包含当前最佳检查点、`memory.json` 和后端日志。时序增益依赖设计与工具运行；成功表示命令完成并生成可重新打开、已布线的输出 DCP，而不是保证每次运行都能提升时序。

## 比较两个检查点

优化后可运行独立比较工具：

```bash
make validate \
  GOLDEN=/path/to/original.dcp \
  REVISED=/path/to/optimized.dcp \
  VECTORS=1000
```

该工具检查结构兼容性，并在支持仿真时使用随机 XSim 向量。它会在临时工作目录下写入 `validation_report.json`，并报告 `PASSED`、`FAILED` 或 `INFRASTRUCTURE FAILURE`。随机比较不等同于形式等价证明。

可用未修改的公开 DCP 自检验证基础设施：

```bash
make validate_demo
```

## 复现论文结果

论文在 35 个 UltraScale+ DCP 上报告以下主要结果：

- 图 1：各设计通过验证的 Fmax，相对原始 DCP 归一化
- 图 2：质量/运行时间与质量/token 成本的权衡
- 表 II：PACT、Scripted、Vivado `phys_opt`、DATuner 和 Codex Agent 在 27 个开发设计、8 个留出设计及全部 35 个设计上的 Fmax 几何平均提升
- 汇总结论：PACT Fmax +22.30%，DATuner +15.14%，Codex Agent +9.78%；相对 DATuner 的配对运行速度提升 6.4 倍；每个 DCP 平均 token 成本为 0.16 美元

完整的 Results Replicated 包还应包括：

1. 35 行基准清单，包含来源、数据划分、目标器件、DCP 文件名、SHA-256、目标时钟和基线 Fmax。
2. 所有可再分发 DCP，以及每个缺失 DCP 的获取或确定性生成说明。
3. PACT、Scripted、`phys_opt`、DATuner 和 Codex 的精确命令与配置。
4. 原始日志，以及包含每种方法逐设计验证状态、原始/最终 Fmax、运行时间、工具调用/动作、token 数与成本的机器可读表。
5. 统一 signoff 脚本，检查检查点重开、完整布线、零布线错误、setup/hold/pulse-width/min-period 时序以及主 I/O 和时钟定义保持不变。
6. 能从原始表重新生成两幅论文图和表 II 的聚合与绘图脚本。

在补齐这些文件之前，评测者可以审阅实现并执行单设计功能流程，但不能独立重新计算论文中的全部汇总数字。

## 可复现性说明

- 比较 Fmax 时使用原始目标时钟和时序例外，不要放宽约束。
- PACT 的 LLM 输出具有非确定性。每次运行应记录模型标识、API endpoint、日期、完整运行目录与 token 统计。
- 生成的检查点、日志、`.env`、`runs/` 和下载的 DCP 均被 Git 忽略。创建 DOI 前，应显式加入归档所需的原始结果。

## 许可证

由 FudanLLMEDA 贡献者编写的 PACT 代码采用 [MIT License](LICENSE)。保留 AMD 版权头和 `SPDX-License-Identifier: Apache-2.0` 标记的文件继续采用 [Apache License 2.0](LICENSE-APACHE-2.0.txt)，根目录 MIT 许可证不会重新许可这些文件。`RapidWright/` 子模块和其他第三方依赖遵循各自的上游许可证。Vivado 是专有软件，不包含在本仓库中。

## 快照说明

原始 Artifact 根提交是经过净化的 2026-06-25 快照；本版本将其更新至 2026-08-11 的最终提交实现，但没有导入私有开发仓库的分支、日志、部署文件、运行输出或凭据。为兼容评测 DCP，必要位置仍保留历史基准文件名和 `clk_fpl26contest` 时钟标识。
