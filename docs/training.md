# 模型训练与评估

## 当前模型

默认程序使用 YOLO11n 四分类检测模型，输入尺寸为 960，类别为：

```text
0 watering
1 harvest
2 fishing
3 midwifery
```

保持类别表、数据 YAML、导出模型元数据一致。五分类实验在首位增加 `restaurant_restock`，类别编号因此不同；它没有作为当前程序的默认模型。

## 数据与标注

完整训练图片和标注已单独备份，不随源码仓库分发。数据准备入口：

```powershell
.\.venv\Scripts\python.exe tools/training/prepare_farm_icon_dataset.py --help
```

数据整理脚本的默认数据集名称仍保留 `farm_icons_v0.4`，不影响源码模型目录使用 `models/farm-icons/`。

## Windows CPU 工具

```powershell
.\.venv\Scripts\python.exe tools/training/train_farm_icon_detector.py --help
.\.venv\Scripts\python.exe tools/training/eval_farm_icon_detector.py --help
.\.venv\Scripts\python.exe tools/training/detect_farm_icons.py --help
```

训练和评估必须自行准备数据集及初始化权重。根据实际训练记录选择参数，保存类别、数据切分、运行参数、验证结果和权重 SHA-256。

## WSL ROCm

GPU 训练入口为 `tools/training/train_farm_icon_detector_wsl.py`。在 WSL 中直接使用配置好 ROCm PyTorch 的虚拟环境解释器，训练数据与运行输出放在 Linux 原生文件系统。该脚本拒绝将训练输出放在 `/mnt/c` 等 Windows 挂载目录。

先确认 `/dev/dxg`、PyTorch HIP 和目标 GPU 可用，再开始训练；不要将 GPU 不可见时的 CPU 运行当作等价替代。示例路径使用 `/usr/yolo/batter-farm` 等通用项目目录，不包含个人用户名。

本源码副本没有复制 WSL 中的后续五分类实验、审核数据和完整训练产物，需要从独立备份取得这些内容。

## 模型导出

运行使用 ONNX，继续训练使用 `.pt`。权重需单独分发并附类别表、来源、评估限制和 SHA-256；默认路径为 `models/farm-icons/best.onnx`。
