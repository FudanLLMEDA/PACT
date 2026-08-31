# PACT FPT'26 论文 Artifact

[English](README.md) | [简体中文](README.zh_CN.md)

本仓库包含 PACT 布线后 FPGA 检查点优化智能体、智能体驱动的 Vivado 与 RapidWright MCP 服务器，以及优化知识库。35 个基准 DCP 通过 Git LFS 提供；专有 FPGA 工具不包含在仓库中。

## 目录结构

- `FDAgents/`：智能体、类型化技能、配方规划器、决策逻辑与记忆
- `baselines/codex_agent/`：自由形式 Codex Agent 基线运行器和参考结果
- `baselines/datuner/`：DATuner 检查点运行器和参考结果
- `RapidWrightMCP/`、`VivadoMCP/`：后端 MCP 服务器
- `knowledge/`：优化知识和实验记录
- `benchmarks/`：35 个评测 DCP、清单与校验和
- `journals/`：为 Artifact 准备保留的论文源码
- `tests/`：不需要基准 DCP 的单元测试
- `runs/`：生成的运行输出，已被 Git 忽略

## 环境

完整 Artifact 需要 Python 3、Vivado 2025.1、Java，以及已经构建的 RapidWright checkout。将 `.env.example` 复制为 `.env`，填写本地路径与 LLM 凭据。`.env` 和生成的 DCP 已被 Git 忽略。

## 运行

```bash
python -m pip install -r requirements.txt
python -m FDAgents.agent /path/to/design.dcp --model <model>
```

## 确定性功能冒烟测试

两个不使用 LLM 的流程分别测试两个后端，适合作为初始 Artifact 检查。把以下竞赛检查点放到同一目录：

- `vexriscv_re-place_2025.1.dcp`
- `logicnets_jscl_2025.1.dcp`

然后运行：

```bash
DCP_DIR=/path/to/checkpoints \
OUT_ROOT="$PWD/reproduction-smoke" \
PYTHON=/path/to/python \
VIVADO_EXEC=/path/to/vivado \
scripts/run_functional_smoke.sh
```

传入 `vexriscv` 或 `logicnets` 可以只运行一个设计。当 MCP 后端返回应用级错误、VexRiscv 流程没有移动 cell、Vivado 报告布线错误、输出 DCP 缺失，或独立重开的检查点未通过 route signoff 时，命令会失败。日志、校验和、退出码、signoff timing report 与输出 DCP 会写入 `OUT_ROOT`。

无需 Vivado 的轻量门控回归：

```bash
python tests/test_smoke_checks.py
```

对输入和输出 DCP 进行结构及基于仿真的比较：

```bash
python validate_dcps.py input.dcp output.dcp \
  --precheck-vectors 50 --vectors 200
```

完整的 35 设计实验和基准获取流程将在 Artifact 提交前单独记录。

## 许可证

由 FudanLLMEDA 贡献者编写的 PACT 代码采用 [MIT License](LICENSE)。保留 AMD 版权头和 `SPDX-License-Identifier: Apache-2.0` 标记的文件继续采用 [Apache License 2.0](LICENSE-APACHE-2.0.txt)，根目录 MIT 许可证不会重新许可这些文件。`RapidWright/` 子模块和其他第三方依赖遵循各自的上游许可证。Vivado 是专有软件，不包含在本仓库中。

## 快照

本 Artifact 分支起始于 2026-06-25 的实现快照。Git 历史在发布前被有意压缩并净化。为兼容已评测 DCP，必要位置仍保留历史基准文件名和 `clk_fpl26contest` 时钟标识。
