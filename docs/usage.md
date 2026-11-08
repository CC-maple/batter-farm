# 使用指南

以下命令均在仓库根目录执行，并使用安装好依赖的 `.venv`。

## 配置与预检

默认配置位于 `configs/config.yaml`，包含目标窗口标题、进程、截图区域、OCR、检测器、任务和热键设置。相对资源路径以仓库根目录解析。

正式任务预检会验证目标窗口、管理员权限、模型类别、OCR、模板与可回放路线。缺少资产时应补齐对应文件，不跳过预检。餐厅任务保持停用。

## 录制

```powershell
.\.venv\Scripts\python.exe tools/recording/record_farm_actions.py --screenshot-interval-ms 250
```

需要管理员权限。默认每 250 毫秒保存一次客户区截图；`--screenshot-interval-ms 0` 关闭定时截图，事件截图由 `--screenshot-mode` 控制。F9 添加标记，F10 停止。

统一键鼠录制及 F5/F6 转向说明：

```powershell
.\.venv\Scripts\python.exe tools/recording/record_input_actions.py --help
```

新录制默认输出到 `logs/recordings/`。经过校验、准备作为公共路线的 JSON 才复制到 `assets/recordings/` 并更新配置；截图不作为固定路线资源提交。

## 回放

```powershell
.\.venv\Scripts\python.exe tools/recording/replay_recording.py --help
.\.venv\Scripts\python.exe tools/recording/run_all_recordings_once.py --list
```

回放会发送真实键鼠输入，正式执行前核对窗口、参考分辨率和路线起点。仅 `--list` 不执行路线。

## 视觉工具

```powershell
.\.venv\Scripts\python.exe tools/vision/match_fishing_icon.py --help
.\.venv\Scripts\python.exe tools/vision/auto_annotate_reference_icon.py --help
.\.venv\Scripts\python.exe tools/vision/calibrate_farm_vehicle.py --help
```

自动标注工具只生成草稿。先使用 `--dry-run` 检查结果，再人工复核类别和框；不要直接将草稿当作最终训练标注。

## 模板约定

`assets/template-matching/` 只包含当前配置或调试工具使用的模板。模板与遮罩英文名保持一致，例如 `restaurant_reception.png` 和 `restaurant_reception_mask.png`。加载器自动读取遮罩；不得只重命名其中一个文件。
