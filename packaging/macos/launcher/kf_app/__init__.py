"""KnowledgeFlow 桌面启动器（v0.4 macOS 打包层）。

这一层**只做启动**，不碰业务：

* 解析 .app 内部路径与用户数据目录
* 首次运行向导（写 settings.json）
* 启动 / 探活 / 复用 / 关闭现有 FastAPI 后端
* 单实例与孤儿进程处理
* 把异常翻译成人话（**绝不把 Python traceback 甩给用户**）

业务逻辑一行都不改 —— 后端仍是 ``backend/`` 里那份代码，原样运行。
"""
