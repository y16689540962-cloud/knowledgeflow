"""便携包的入口脚本：把 launcher 目录加进 ``sys.path`` 后交给 ``kf_app.main``。

两个入口都指向这里：

* ``KnowledgeFlow.exe`` —— PyInstaller 冻结后的双击入口（无控制台窗口）
* ``KnowledgeFlow.cmd`` —— 命令行入口，用内嵌 ``runtime\\python.exe`` 跑本文件

之所以要有 ``.cmd`` 这一份：冻结成 ``--noconsole`` 之后 ``print()`` 没有出口，
``--status`` / ``--stop`` / ``--check`` 这些**本来就是给人看输出**的子命令
在 exe 里等于哑巴。命令行用户走 ``.cmd``，输出正常。
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from kf_app.main import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
