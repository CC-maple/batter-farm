# BatterFarm

Windows 农场画面识别与 UI 自动化工具，包含截图、OCR、YOLO 图标检测、键鼠录制回放和 PySide6 桌面界面 BatterUI。

## 功能

- 实时识别：持续截图并显示状态、检测框与置信度，不执行任务路线。
- 全自动任务：根据农田、鱼塘、牧场和加工器标识选择任务，统一调度截图和键鼠动作。
- 独立任务：执行指定路线，包括手动泡温泉等任务。
- 录制与回放：记录客户区坐标、按键边沿和鼠标事件，支持标记与定时截图。
- 鱼卡结果识别：在固定 ROI 内精确 OCR 匹配 `恭喜获得鱼卡`，置信度至少为 0.90。
- 数据工具：参考图标标注草稿、YOLO 数据准备、Windows CPU 与 WSL ROCm 训练、评估和导出。

## 环境与安装

使用 Windows 10/11、Python 3.11 或 3.12，以及 PowerShell 7。建议使用 Python 3.12；本地检测网页工具依赖的 `cgi` 在 Python 3.13 中已移除。依赖版本范围见 `requirements.txt`，目前未提供经过独立复现的精确锁定文件。

在仓库根目录运行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## 启动

仅浏览界面，不连接游戏或发送输入：

```powershell
.\.venv\Scripts\python.exe -m src.ui.app --demo
```

启动界面：

```powershell
.\.venv\Scripts\python.exe -m src.ui.app
```

执行真实键鼠任务需要管理员权限。请使用管理员 PowerShell，或者双击 `scripts/start-ui.bat` 按提示提升权限。批处理只使用仓库内 `.venv`，缺失时明确报错。

命令行预览与正式运行：

```powershell
.\.venv\Scripts\python.exe main.py --dry-run --overlay
.\.venv\Scripts\python.exe main.py --overlay
```

默认读取 `configs/config.yaml`。相对的模型、模板和路线路径以仓库根目录为基准，不以配置文件目录为基准。可通过 `--config` 指定其他配置。

## 模型与运行资源

仓库已提供当前程序使用的四分类模型，类别顺序为 `watering`、`harvest`、`fishing`、`midwifery`：

```text
models/farm-icons/best.onnx
models/farm-icons/best.pt
```

`best.onnx` 用于运行检测，`best.pt` 用于继续训练或重新导出。类别表、标注工具配置、模型来源和 SHA-256 校验信息也位于 `models/farm-icons/`。**训练数据集不提供。** 模型缺失时，运行预检会明确报告错误。五分类模型尚未接入默认配置，不要直接覆盖四分类模型。

仓库包含六张实际使用的模板及遮罩，统一位于 `assets/template-matching/`。`*_mask.png` 由加载器自动关联同名模板。

固定路线 JSON 位于 `assets/recordings/`，包含当前配置及离线测试需要的 11 份文件。新的录制、截图与运行日志写入被 Git 忽略的 `logs/`，与固定路线分开。

## 热键

| 热键 | 用途 |
|---|---|
| F7 | 显示或隐藏运行遮罩 |
| F8 | 暂停或恢复任务 |
| F9 | CLI 等待态开始扫描，或空闲时请求重扫；录制工具中添加标记 |
| F10、ESC | 运行任务安全停止，并释放键鼠输入 |

录制、回放工具的热键以各自 `--help` 为准。

## 目录结构

```text
src/                 核心识别、输入、运行时、业务任务和 UI
configs/             默认运行配置
assets/              固定模板及路线
models/              当前四分类模型权重、类别表与校验信息
tools/recording/     录制、回放、拆分与摘要
tools/vision/        校准、模板匹配和标注草稿
tools/training/      数据准备、训练、检测与评估
scripts/             Windows 项目虚拟环境启动脚本
tests/               离线回归测试
docs/                使用、架构与训练说明
main.py              命令行入口
```

## 开发与验证

从仓库根目录运行：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
.\.venv\Scripts\python.exe tools/recording/run_all_recordings_once.py --list
```

离线测试不等于真实游戏验收。餐厅路线目前停用，须重新录制并实机验证；录制路线依赖画面尺寸、视角和游戏状态，默认参考客户区为 1600×900。

更多说明见 [使用指南](docs/usage.md)、[架构说明](docs/architecture.md) 和 [训练说明](docs/training.md)。

## 许可证

代码按 AGPL-3.0 发布，见 [LICENSE](LICENSE)。
