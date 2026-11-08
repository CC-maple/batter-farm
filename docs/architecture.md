# 架构说明

## 分层

- `src/core/`：窗口截图、OCR、模板与模型识别、键鼠输入、安全释放和遮罩。
- `src/runtime/`：worker 协议、任务注册、预检、调度、截图协调与动作互斥。
- `src/tasks/`：农场业务执行与 marker sequence。
- `src/ui/`：桌面页面及控制器；UI 通过独立 worker 执行业务。
- `src/utils/`：窗口查找、激活、客户区等辅助功能。

UI 入口为 `python -m src.ui.app`，CLI 入口为 `main.py`。UI 的延迟导入顺序保持不变，避免输入栈在 Qt 设置 DPI 上下文前加载。

## 资源与配置

默认配置为 `configs/config.yaml`。核心运行资源路径统一相对于仓库根目录；模型 YAML 中的 `model_path` 则相对于该 YAML 的目录。

固定模板位于 `assets/template-matching/`，固定路线位于 `assets/recordings/`。运行日志、新录制和调试截图位于 `logs/`，个人任务保存在配置旁的 `*.user_tasks.yaml`，均由 Git 忽略。

仓库提供当前四分类模型的 `best.onnx` 和 `best.pt`，其他训练产物仍由 Git 忽略；训练数据集不提供。鱼卡为 OCR 目标，餐厅任务尚未重新验收。

## 复制整理范围

本目录由当前工作区源码复制整理，不导入原仓库 `.git` 或历史。仅重新组织模块与路径，并改用项目虚拟环境启动。没有复制历史开发文档、研究下载项目、个人网站、未使用图片、缓存或训练运行输出。
