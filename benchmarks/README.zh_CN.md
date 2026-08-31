# 评测检查点

[English](README.md) | [简体中文](README.zh_CN.md)

本目录包含 PACT 评测使用的 35 个输入 Vivado Design Checkpoint：27 个训练/开发设计和 8 个留出测试设计。文件保留原有目录结构，因为 `MANIFEST.csv` 中的路径相对于仓库根目录。

DCP 使用 Git LFS 存储。克隆仓库后，安装 Git LFS 并实体化检查点文件：

```bash
git lfs install
git lfs pull
```

运行实验前验证全部检查点内容：

```bash
cd benchmarks
sha256sum -c SHA256SUMS
```

`MANIFEST.csv` 记录每个设计的名称、训练/测试划分、相对仓库根目录的 DCP 路径、原始 Fmax 和资源数量。不要把优化后的输出 DCP 用作输入；此处列出的文件均为原始评测检查点。

可从仓库根目录运行不使用 LLM 的确定性流程：

```bash
make run_test DCP=benchmarks/vexriscv_re-place_2025.1.dcp
```
