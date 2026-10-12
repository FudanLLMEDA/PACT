# PACT FPT'26 论文 Artifact — v1 维护线

[English](README.md) | [简体中文](README.zh_CN.md)

本目录对应 **FPT'26 论文 v1**，不是 main 上的 FPL'26 竞赛 v2。使用 AE 归档指定的
v1 release。共有 35 个原始 DCP（27 个开发集、8 个留出集）；不包含 Vivado 及许可证。

## 1. 获取与校验

Git checkout 用户执行：

```bash
git submodule update --init --recursive
git lfs install
git lfs pull
(cd benchmarks && sha256sum -c SHA256SUMS)
```

完整 AE 归档也应执行校验。GitHub 自动生成的源码 ZIP 可能只有 LFS 指针，且不包含
子模块，不等于完整 artifact。详见 [基准说明](benchmarks/README.zh_CN.md)。

## 2. 安装

需要 Linux、Python 3.10+、Vivado **2025.1** 及许可证、Java 11+ 和构建后的 RapidWright。
在 artifact 根目录执行：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
source /path/to/Vivado/2025.1/settings64.sh
export VIVADO_EXEC="$(command -v vivado)"
make build-rapidwright
export RAPIDWRIGHT_PATH="$PWD/RapidWright"
export CLASSPATH="$RAPIDWRIGHT_PATH/bin:$RAPIDWRIGHT_PATH/jars/*"
cp .env.example .env  # 仅首次执行，不要覆盖已有配置
```

填写 `.env` 中的本地路径和密钥，或导出环境变量。AE Gateway 示例（禁止提交真实密钥）：

```bash
export OPENAI_BASE_URL="https://ai-gateway.vercel.sh/v1"
export OPENAI_API_KEY="<your-evaluation-key>"
export FDAGENTS_MODEL="openai/gpt-5.5"
```

直连 OpenAI 时使用对应 endpoint 与 `gpt-5.5`。记录 `vivado -version`、`java -version`、
`python -m pip freeze`；Codex baseline 还需记录 `codex --version`。

## 3. 无 LLM 冒烟测试

```bash
DCP_DIR="$PWD/benchmarks" PYTHON="$(command -v python)" \
  OUT_ROOT="$PWD/runs/smoke" scripts/run_functional_smoke.sh
```

测试 VexRiscv、LogicNets 的后端执行与 route replay。**这不是完整 timing/功能验证**；
生成 timing report 不代表 hold、pulse-width 或等价检查通过。

## 4. PACT 单设计与批处理

```bash
python -m FDAgents.agent benchmarks/amd_mini-isp_2025.1.dcp \
  --output runs/pact/mini-isp/output.dcp --run-dir runs/pact/mini-isp \
  --model "$FDAGENTS_MODEL" --time-limit 3600
python scripts/run_pact_batch.py --model "$FDAGENTS_MODEL" --dry-run
python scripts/run_pact_batch.py --model "$FDAGENTS_MODEL" --run-root runs/pact-full
```

每次使用新的 run/output 路径。可重复传 `--design "AMD Mini-ISP"`、`--design "BOOM SoC"`
选择子集，名称来自 `benchmarks/MANIFEST.csv`。单设计 `--no-llm` 是诊断模式，不等于论文
LLM 实验；已知基准名称回放仍需显式启用。

### 结果与验证边界

- `summary.json`：版本化机器可读结果，包含输入 hash、配置、trial 决策、指标、验证状态、
  耗时、token 和成本。增量写入，异常/中断时保留；强制杀进程可能留下 `running`，不能算完成。
- `memory.json`：内部决策历史，不是稳定的汇总接口。
- `constraints/`：原始 timing XDC 与时钟名称/周期/波形。
- `validation/round_*/`、`validation/final/`：候选与最终输出的重开、route/timing、功能证据。
- `current_best.dcp`、skill DCP、`final_candidate.dcp`：内部候选，不是已通过验证的输出。

接受有提升的候选前，公共 gate 恢复输入 timing 约束（时钟及 timing exceptions），写出并重开
DCP，检查完整布线、setup 测量、非负 hold/pulse-width，再以恢复后的 WNS 判断提升。
Fmax 是 `1000 / (原始周期 - WNS)` 的 setup 推导值，不保证所有时钟域均能在该频率运行。
允许负 setup slack，因为目标是提升存在 timing violation 的输入的 Fmax，不承诺原频率下闭合。

最终导出重复上述 gate，并默认进行独立结构检查和 200 向量功能仿真（50 向量预检查）。
仿真跳过、报告缺失、超时均不算通过。**这不是形式等价证明，也不在每个中间候选上运行仿真。**
最终验证失败不写用户指定的输出路径；内部 DCP 留作诊断。显式 `--skip-functional-check`
允许输出但标记为未做功能验证，汇总时排除。

3,600 秒是优化预算；动作之间检查 deadline，运行中的工具可能超时完成，候选 gate 计入优化时间。
最终 signoff 是额外工作；`--validation-timeout` 默认 1,800 秒，约束最终结构/仿真子进程，
不包含独立的 Vivado report 调用。结果分别记录优化与总耗时。加强后的 gate 可能改变候选选择
与运行时间，**没有因此重跑或重新认证历史论文数字**。见 [AE 修改说明](docs/AE_CHANGES.md)。

### 评审机器上的运行时间

CPU 性能、资源分配及共享机器负载可能导致需要增加优化或验证预算。本次 AE 诊断中，
含最终验证的总耗时约为：**AMD Mini-ISP 9 分钟、Double-Clock FFT-L 15 分钟、
Stress FFT-CORDIC-M 65 分钟**。BOOM 原运行超时失败；随后恢复的 **89.654 MHz** 候选
通过了独立检查，其中 **64 GiB 内存下的结构／采样功能验证约 40 分钟**。这不等于
在一小时／32 GB 条件下完成端到端运行。
硬件配置、分阶段实测时间、单配方超时限制和延长预算的诊断用法见
[AE 运行时间参考](docs/AE_RUNTIME.md)。延长预算的运行须与论文的一小时结果分开报告，
不能通过关闭验证检查来获得“通过”。

## 5. 汇总本次 PACT 结果

```bash
python scripts/summarize_pact_results.py runs/pact-full --output-dir runs/pact-full-aggregate
```

输出逐设计 `results.csv` 和分组 `aggregate.json`。以 manifest 原始 Fmax 计算
`max(1, final_Fmax / original_Fmax)` 的几何平均，同时输出有样本数标记的时间/成本均值；CSV 保留本次测量的 baseline。
失败、缺失、未验证的运行列出但不计入均值；覆盖数量和 `complete` 显式标记是否完整。
重复运行报错，不自动挑选最佳结果。这不是历史论文数据的重建。

## 6. DATuner

详见 [DATuner 文档](baselines/datuner/README.zh_CN.md)。按 `trial_counts.csv` 指定 trial
数量，**每个 Vivado trial 超时 3,600 秒**，不是 1,200 秒，也不是 DATuner 整个设计仅一小时。

```bash
python baselines/datuner/run_batch.py --dry-run
python baselines/datuner/run_batch.py --output-dir runs/datuner
python baselines/datuner/summarize_results.py
```

最后一条汇总的是历史 reference CSV，不是上面新运行的结果。

## 7. Codex Agent

先完成 [安装、MCP 注册、凭据、模型与 reasoning 配置](baselines/codex_agent/README.zh_CN.md)，
再按其中命令 dry-run、正式运行和独立验证。`codex mcp list` 有记录不等于服务器启动成功。
使用与 PACT、DATuner 相同的输入及子集。

## 8. 单元测试与独立功能比较

```bash
python -m pip install pytest
python -m pytest -q tests
python validate_dcps.py input.dcp output.dcp \
  --precheck-vectors 50 --vectors 200 --report-json runs/comparison.json
```

单元测试不需要 Vivado，实际 DCP 验证需要。采样仿真不是形式等价认证。

## 许可证与版本

PACT 自有代码使用 [MIT](LICENSE)，保留 AMD Apache 标识的文件继续使用
[Apache 2.0](LICENSE-APACHE-2.0.txt)。第三方依赖遵循各自许可证。
v1 起始于净化后的 2026-06-25 实现快照，维护提交不重写已有 v1/v2 标签。
历史文件名与 `clk_fpl26contest` 保留用于兼容。
