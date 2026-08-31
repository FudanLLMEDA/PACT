# Codex Agent 基线

[English](README.md) | [简体中文](README.zh_CN.md)

本目录包含 PACT 评测中使用的自由形式 Codex Agent 基线运行器。运行器向 Codex 提供与 FDAgents 相同的动作清单和决策策略文本，但由 Codex 自行决定如何调用原生 Vivado、RapidWright MCP 服务器，以及如何管理检查点试验。该基线禁止把任务委托回 PACT，也禁止读取 FDAgents 源码。

这些脚本从 Git 提交 `b5fb2e3` 对应的实验快照中恢复。打包修改仅涉及仓库相对导入，以及为可选的 OpenAI-compatible API endpoint 使用与供应商无关的名称。

## 内容

- `run_codex_dcp_harness.py`：运行一个或多个清单条目的 Codex 基线
- `verify_harness_outputs.py`：在 Vivado 中独立重新打开输出，检查布线、由 setup 时序计算的 Fmax、hold 和 pulse-width 时序
- `usage_accounting.py`：从 Codex JSONL 中提取 token 与成本记录
- `build_harness_manifests.py`：把源清单拆分为开发集、留出集和全集
- `merge_harness_results.py`：在验证表头后合并结果分片
- `compare_harness_results.py`：比较验证后的 Codex 与 PACT 结果表
- `manifest.csv`：35 个基准输入及开发/留出划分
- `inventory.md`：提供给自由形式智能体的动作清单
- `reference_results.csv`：论文中报告的、通过验证的参考结果

## 要求

- Python 3.10 或更高版本
- 支持 `codex exec --json` 的 Codex CLI
- Codex 配置所接受的 API 凭据
- AMD Vivado 2025.1 及有效许可证
- 在 Codex 中将本仓库的 Vivado 和 RapidWright 原生 MCP 服务器分别配置为 `vivado` 与 `rapidwright`
- 已通过 Git LFS 将 35 个 DCP 实体化到 `benchmarks/`

论文使用模型 `gpt-5.5`、`xhigh` reasoning effort，并为每个设计设置一小时 wall-clock 上限。复现报告实验前，应在 Codex 配置中使用相同 reasoning effort。

## Dry run

以下命令生成全部 35 个 prompt 和结果表，但不会启动 Codex 或 Vivado：

```bash
python baselines/codex_agent/run_codex_dcp_harness.py \
  --manifest baselines/codex_agent/manifest.csv \
  --inventory baselines/codex_agent/inventory.md \
  --run-root runs/codex_agent-dry \
  --dry-run
```

## 完整运行

首先确认 `codex mcp list` 能看到两个必需服务器，然后执行：

```bash
python baselines/codex_agent/run_codex_dcp_harness.py \
  --manifest baselines/codex_agent/manifest.csv \
  --inventory baselines/codex_agent/inventory.md \
  --run-root runs/codex_agent \
  --codex-model gpt-5.5 \
  --time-limit 3600 \
  --timeout-grace 900 \
  --require-native-mcp \
  --require-usage
```

仅在使用不同于 Codex 已配置供应商的 OpenAI-compatible endpoint 时设置 `OPENAI_BASE_URL` 和 `CODEX_MODEL_PROVIDER`。只有机器具备足够的 Vivado 许可证、内存和 CPU 时，才应使用 `--jobs N` 并行运行。

运行器会以 `danger-full-access` 启动 Codex，因为 Vivado、RapidWright 和输出检查点必须可访问。请在一次性的 Artifact 虚拟机中运行，不要向该虚拟机暴露无关文件或凭据。

## 独立验证

```bash
python baselines/codex_agent/verify_harness_outputs.py \
  --results runs/codex_agent/results.csv \
  --output runs/codex_agent/verified.csv \
  --vivado /path/to/vivado
```

验证器不信任智能体的最终消息。它会重新打开每个输出 DCP，生成新的 route 与 min/max timing report，并区分缺失、未布线、hold 失败、pulse-width 失败和验证通过的输出。

## 参考结果

```bash
python baselines/codex_agent/summarize_results.py
```

与论文一致，当方法没有超过未修改输入时，归一化 Fmax 按 1.0 计。预期几何平均提升为：27 个开发设计 +12.70%，8 个留出设计 +0.47%，全部 35 个设计 +9.78%。报告的平均运行时间为 1,524 秒，每个 DCP 平均 token 成本为 3.81 美元。

原始双机启动器因嵌入实验主机路径和网络拓扑而有意排除。`run_codex_dcp_harness.py` 是可移植的执行核心，已经支持顺序或本地并行运行。
