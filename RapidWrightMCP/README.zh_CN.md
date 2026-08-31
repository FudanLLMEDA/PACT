# RapidWright MCP Server

[English](README.md) | [简体中文](README.zh_CN.md)

这是一个 MCP（Model Context Protocol）服务器，为 AI 助手提供访问 [RapidWright](https://github.com/Xilinx/RapidWright) 的能力。RapidWright 是 AMD 提供的开源 FPGA 设计工具框架。

服务器允许 AI 助手使用自然语言与 FPGA 设计交互、查询器件信息、分析 Design Checkpoint，并探索 Xilinx/AMD 器件架构。

## 功能

- **器件信息**：查询支持的 FPGA 器件与系列，获取详细器件规格
- **Design Checkpoint 分析**：加载 Vivado `.dcp`、检查设计统计信息并搜索 cell
- **器件架构探索**：获取 tile/site 信息并查询器件资源
- **设计优化**：LUT 输入锥优化和高 fanout net 拆分

## 快速开始

### 前置要求

- Python 3.8 或更高版本
- Java 11 或更高版本

### 安装

推荐使用仓库根目录的 Makefile。该流程会从 Git 子模块构建 RapidWright：

```bash
cd PACT
make setup
```

该命令会安装 Python 依赖、构建 `RapidWright/` 子模块，并设置 `RAPIDWRIGHT_PATH` 与 `CLASSPATH`，使 pip 包使用本地源码。

修改 RapidWright 源码后可重新构建：

```bash
make build-rapidwright
```

### 独立手动安装

```bash
cd RapidWrightMCP
./setup.sh
python3 test_server.py
```

## 在 Cursor 中使用

在 MCP 配置文件中加入：

```json
{
  "mcpServers": {
    "rapidwright": {
      "command": "python3",
      "args": ["/absolute/path/to/RapidWrightMCP/server.py"],
      "env": {
        "RAPIDWRIGHT_PATH": "/absolute/path/to/RapidWright",
        "CLASSPATH": "/absolute/path/to/RapidWright/bin:/absolute/path/to/RapidWright/jars/*"
      }
    }
  }
}
```

保存后重启 Cursor。

## 可用工具

| 工具 | 说明 |
|---|---|
| `initialize_rapidwright` | 初始化 RapidWright，必须首先调用 |
| `get_supported_devices` | 列出支持的 FPGA 器件 |
| `get_device_info` | 获取指定器件的详细信息 |
| `read_checkpoint` | 加载 Vivado Design Checkpoint（`.dcp`） |
| `write_checkpoint` | 将设计保存为 `.dcp` |
| `get_design_info` | 获取当前设计统计信息 |
| `search_cells` | 按名称或类型搜索 cell |
| `get_tile_info` | 获取指定 tile 的信息 |
| `search_sites` | 按类型搜索器件 site |
| `optimize_lut_input_cone` | 将 LUT 链合并为单个 LUT |
| `optimize_fanout` | 复制驱动器以拆分高 fanout net |

## 使用示例

```text
用户：“初始化 RapidWright，并显示可用器件。”
AI：[调用 initialize_rapidwright 和 get_supported_devices]
    “RapidWright 支持 50 多种器件，包括 xcvu3p、xcvu9p、xcku040……”

用户：“加载 ~/my_design.dcp，并告诉我用了多少 LUT。”
AI：[调用 read_checkpoint，然后调用 get_design_info 和 search_cells]
    “该设计使用了 15,432 个 LUT6 cell……”

用户：“优化 pin top/cpu/alu/result[0] 的 LUT 输入锥。”
AI：[调用 optimize_lut_input_cone]
    “已把 3 个串联 LUT 合并为一个 LUT6。”

用户：“net clk_enable 的 fanout 很高，把它拆成 4 组。”
AI：[调用 optimize_fanout]
    “已将原 fanout 为 2,456 的 net 拆成 4 个，每个约 614 个 load。”
```

## 使用建议

- 每个会话只需初始化一次，之后可连续调用其他命令
- `.dcp` 文件使用绝对路径
- 器件名称应明确，例如使用 `xcvu9p` 而不是 `vu9p`
- 可串联请求，例如“加载设计 X 并分析 Y”
- 可以直接使用自然语言，不必记住精确工具名

## 常见 Cell 与 Site 类型

| Cell 类型 | 说明 |
|---|---|
| LUT6 | 6 输入查找表 |
| FDRE | 带 clock enable 和同步 reset 的 D flip-flop |
| FDCE | 带 clock enable 和异步 clear 的 D flip-flop |
| CARRY8 | UltraScale+ carry logic |
| DSP48E2 | DSP block |
| RAMB36E2 | 36 Kb block RAM |
| BUFGCE | 全局 clock buffer |

| Site 类型 | 说明 |
|---|---|
| SLICEL | 逻辑 slice（LUT、FF、carry） |
| SLICEM | 支持 distributed RAM 的 memory slice |
| DSP48E2 | DSP/数学 block |
| RAMB36/RAMB18 | Block RAM |
| URAM288 | 288 Kb UltraRAM |

## 设计优化指南

### LUT 输入锥优化

把串联的小 LUT 合并为单个更大的 LUT（最大为 LUT6），以降低逻辑深度。

```text
优化前：Input -> LUT2 -> LUT3 -> LUT4 -> Output  （3 级逻辑）
优化后：Input -> LUT6 -> Output                   （1 级逻辑）
```

适用场景：关键路径包含多个 LUT level，或希望执行布线后 ECO 而不重新综合完整设计。

参数：

- `hierarchical_input_pins`：需要优化的 pin 列表，例如 `["top/cpu/alu/result[0]"]`
- `output_dcp_path`：可选输出 DCP 路径

限制：输入总数最多为 6；只适用于完全由 LUT 驱动的路径；不能跨 flip-flop、DSP 或其他非 LUT 单元优化。

### Fanout 优化

复制源驱动器，把高 fanout net 的 load 分配给多个副本。

```text
优化前：Driver -> [1000 loads]
优化后：Driver_1 -> [250 loads]
        Driver_2 -> [250 loads]
        Driver_3 -> [250 loads]
        Driver_4 -> [250 loads]
```

适用于高 fanout enable/control signal、由高负载 net 引起的布线拥塞，以及高 fanout 关键路径的 timing closure。

拆分因子建议：

- `k=2`：fanout 约 500–1000
- `k=3–4`：fanout 约 1000–3000
- `k>=5`：fanout 大于 3000

更大的 `k` 会降低每条 net 的 fanout，但会增加驱动 cell、面积和功耗。该优化只适用于已布线 net，对较小 fanout 通常无益。

### 优化故障排查

| 错误 | 处理方法 |
|---|---|
| `Pin not found` | 检查层级路径，使用包含 top module 的完整名称 |
| `No optimization possible` | pin 可能不是 LUT 驱动，或已经是单 LUT 最优形式 |
| `6 maximum inputs` | 输入锥超过 6 个输入，尝试从更靠后的 stage 优化 |
| `Net not found` | 使用 physical net name，而不是 hierarchical logical name |

## 架构

```text
┌─────────────────┐
│     Cursor      │
│  AI Assistant   │
└────────┬────────┘
         │ MCP Protocol（JSON-RPC over stdio）
┌────────▼────────┐
│  server.py      │  ← MCP Server
└────────┬────────┘
┌────────▼────────────┐
│ rapidwright_tools.py│  ← Tool Wrappers
└────────┬────────────┘
┌────────▼────────┐
│  RapidWright    │  ← pip package（JPype + Java libs）
└─────────────────┘
```

`rapidwright` pip 包提供 JPype/Python 桥接，`RAPIDWRIGHT_PATH` 和 `CLASSPATH` 将其重定向到本地 `RapidWright/` Git 子模块编译出的 Java class，因此可以直接修改并重新构建 RapidWright 源码。

## 开发

### 项目结构

```text
RapidWrightMCP/
├── server.py              # 主 MCP 服务器
├── rapidwright_tools.py   # RapidWright wrapper 函数
├── requirements.txt       # Python 依赖
├── setup.sh               # 安装脚本
├── test_server.py         # 测试套件
└── rapidwright_mcp.log    # 运行时生成的日志

../RapidWright/            # Xilinx/RapidWright Git 子模块
├── src/                   # Java 源码
├── bin/                   # 编译后的 class
├── jars/                  # 第三方依赖
└── gradlew                # Gradle wrapper
```

### 添加新工具

1. 在 `rapidwright_tools.py` 中添加函数。
2. 在 `server.py` 的 `list_tools()` 中注册工具及 input schema。
3. 在 `call_tool()` 中添加对应 handler。

### 运行测试

```bash
python3 test_server.py
tail -f rapidwright_mcp.log
```

## 故障排查

| 问题 | 处理方法 |
|---|---|
| `RapidWright not initialized` | 先调用 `initialize_rapidwright`，并确认已安装 Java 11+ |
| 找不到服务器 | 配置中使用绝对路径，并完整重启 Cursor/Claude |
| 内存不足 | 增大 `jvm_max_memory`，例如 `8G` 或 `16G` |
| 本地修改未生效 | 在仓库根目录运行 `make build-rapidwright` |
| 安装问题 | 对 JPype bridge 执行 `pip3 install --force-reinstall rapidwright` |

检查 Python、Java、RapidWright pip bridge、子模块、编译输出、环境变量、绝对配置路径和运行日志。

## 资源

- [RapidWright 文档](https://www.rapidwright.io/docs/)
- [RapidWright Javadoc](https://www.rapidwright.io/javadoc/)
- [RapidWright GitHub](https://github.com/Xilinx/RapidWright)
- [Model Context Protocol](https://modelcontextprotocol.io/)
