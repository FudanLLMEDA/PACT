# DATuner 基线

[English](README.md) | [简体中文](README.zh_CN.md)

本目录包含 PACT 评测所用的 DATuner 风格基线。它将 Xu 等人在 FPGA 2017 论文 “A Parallel Bandit-Based Approach for Autotuning FPGA Compilation” 中描述的动态空间划分与多臂老虎机搜索改造为从 Vivado Design Checkpoint 开始运行。

运行器搜索包含 47,250 个点的空间，参数涵盖 Vivado 优化、布局、物理优化和布线 directive、fanout 上限与目标时钟比例。每次 trial 都从未修改的输入 DCP 开始。

## 要求

- Python 3.10 或更高版本
- AMD Vivado 2025.1 及有效许可证
- `/usr/bin/time`
- 已通过 Git LFS 将 DCP 实体化到 `benchmarks/`

若 `vivado` 不在 `PATH` 中，请设置 `VIVADO_BIN`。

## 单设计运行

```bash
VIVADO_BIN=/path/to/vivado \
python baselines/datuner/run_local.py \
  --dcp benchmarks/logicnets_jscl_2025.1.dcp \
  --budget 15 \
  --timeout 1200 \
  --output-dir runs/datuner/logicnets
```

原始实验针对不同设计使用 `trial_counts.csv` 中的 8、12 或 15 次 trial 预算，每次 trial 的超时为 1,200 秒。DATuner 不受 PACT 每个设计一小时 wall-clock 上限约束。

## 完整基准批处理

不启动 Vivado，仅预览 35 条命令：

```bash
python baselines/datuner/run_batch.py --dry-run
```

顺序运行：

```bash
VIVADO_BIN=/path/to/vivado \
python baselines/datuner/run_batch.py \
  --output-dir runs/datuner
```

只有机器具备足够的 Vivado 许可证、内存和 CPU 时，才应使用 `--jobs N` 并行运行。

每个设计目录包含 `global_result.txt`、`summary.json`、批处理控制台日志和各 trial 的 Vivado report。`reference_results.csv` 包含论文使用的 DATuner Fmax 与 wall-clock 数值。

重新计算论文中的 DATuner 汇总结果：

```bash
python baselines/datuner/summarize_results.py
```

与论文比较方法一致，当某种方法未超过原始输入检查点时，其归一化 Fmax 按 1.0 计。预期几何平均提升为：27 个开发设计 +18.39%，8 个留出设计 +4.80%，全部 35 个设计 +15.14%。

## 归属说明

基线搜索基于：

> C. Xu, G. Liu, R. Zhao, S. Yang, G. Luo, and Z. Zhang, “A Parallel
> Bandit-Based Approach for Autotuning FPGA Compilation,” FPGA 2017,
> pp. 157–166. DOI: 10.1145/3020078.3021747.

PACT 的检查点运行器和 Vivado 参数化是面向本 Artifact 的改造。
