# Evaluation checkpoints

[English](README.md) | [简体中文](README.zh_CN.md)

This directory contains the 35 input Vivado design checkpoints used by the
PACT evaluation: 27 training designs and 8 held-out test designs. The files
retain their directory structure because the paths in `MANIFEST.csv` are
relative to the repository root.

The DCPs are stored with Git LFS. After cloning the repository, install Git LFS
and materialize the checkpoint files:

```bash
git lfs install
git lfs pull
```

Verify all checkpoint contents before running an experiment:

```bash
cd benchmarks
sha256sum -c SHA256SUMS
```

`MANIFEST.csv` records each design name, train/test split, repository-relative
DCP path, original Fmax, and resource counts. Do not use optimized output DCPs
as inputs; every file listed here is an original evaluation checkpoint.

The deterministic no-LLM smoke test can be run from the repository root with:

```bash
scripts/run_functional_smoke.sh vexriscv logicnets
```
