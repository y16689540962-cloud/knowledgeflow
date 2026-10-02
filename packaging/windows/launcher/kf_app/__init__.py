"""KnowledgeFlow 桌面启动器（Windows 打包层）。

这一层**只做启动**，不碰业务：

* 解析安装目录与用户数据目录（``%LOCALAPPDATA%\\KnowledgeFlow``）
* 首次运行向导（tkinter，写 ``settings.json``）
* 启动 / 探活 / 复用 / 关闭现有 FastAPI 后端
* 单实例与孤儿进程处理
* 控制窗口（Windows 上用户唯一看得见、点得着的东西）
* 把异常翻译成人话（**绝不把 Python traceback 甩给用户**）

业务逻辑一行都不改 —— 后端仍是 ``backend/`` 里那份代码，原样运行。
"""
