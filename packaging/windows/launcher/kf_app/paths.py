r"""路径解析：程序目录 与 用户数据目录，**两者严格分离**。

便携版布局（解压到哪就在哪）：

```text
<安装目录>/
├── KnowledgeFlow.exe     双击入口（无控制台窗口）
├── KnowledgeFlow.cmd     命令行入口（--status / --stop / --check）
├── runtime/              内嵌 Python（python-build-standalone · windows-msvc）
├── backend/              原样的后端代码，与仓库里那份逐字节一致
└── launcher/             kf_app（本目录）
```

用户数据（可写、独立，**绝不写进安装目录**）：

```text
%LOCALAPPDATA%\KnowledgeFlow\
├── config\     settings.json（含 API Key）
├── data\       knowledgeflow.db + media\
├── logs\       app.log / launcher.log
├── cache\      HuggingFace 模型缓存
└── runtime\    service.json（辅助状态；判定「在不在跑」以端口为准，不看它）
```

**为什么数据不能放安装目录**：便携包会被解压到 U 盘、被挪到别处、被整个删掉重下。
数据库写在里面的话，用户「重新解压一份」就等于丢数据；而且放在
``Program Files`` 之类的位置，普通用户根本没有写权限。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

#: 用户数据目录名（Windows 惯例：%LOCALAPPDATA%\<产品名>）
DATA_HOME_DIRNAME: Final[str] = "KnowledgeFlow"

#: 支持的最低 Windows 版本：10（build 10240）。Windows 11 的 major/minor 也是 10.0，
#: 靠 build 号区分 —— 所以这里只卡到「Windows 10 及以上」这个粒度。
MIN_WINDOWS_VERSION: Final[tuple[int, int]] = (10, 0)


@dataclass(frozen=True)
class AppPaths:
    """一次解析出来的所有路径。"""

    #: 安装目录（只读）
    root: Path
    #: ``<root>/runtime``（内嵌 Python）
    runtime: Path
    #: ``<root>/backend``（原样的后端代码）
    backend: Path

    #: ``%LOCALAPPDATA%\KnowledgeFlow``
    home: Path
    config: Path
    data: Path
    logs: Path
    cache: Path
    run: Path

    @property
    def runtime_python(self) -> Path:
        """内嵌解释器。用 ``python.exe`` 而不是 ``pythonw.exe``：

        后端是子进程，输出直接重定向进日志文件，不需要控制台；
        用 ``pythonw.exe`` 反而会让 ``sys.stdout`` 为 ``None``，
        某些库（含 uvicorn 的日志初始化）会因此出意外。

        两种布局都认：

        * ``runtime/python.exe`` —— 打包产物（python-build-standalone 解包后就是这样）
        * ``runtime/Scripts/python.exe`` —— 开发模式下 ``KF_DEV_RUNTIME`` 常指向一个 venv

        不认第二种的话，开发时为了跑一次 ``--check`` 就得手工摆一套目录结构。
        """
        direct = self.runtime / "python.exe"
        if direct.is_file():
            return direct
        venv_style = self.runtime / "Scripts" / "python.exe"
        if venv_style.is_file():
            return venv_style
        return direct

    @property
    def launcher_entry(self) -> Path:
        """启动器入口脚本（``.cmd`` 与开发模式都用它）。"""
        return self.root / "launcher" / "run.py"

    @property
    def settings_file(self) -> Path:
        return self.config / "settings.json"

    @property
    def database_file(self) -> Path:
        return self.data / "knowledgeflow.db"

    @property
    def media_dir(self) -> Path:
        return self.data / "media"

    @property
    def app_log(self) -> Path:
        return self.logs / "app.log"

    @property
    def pid_file(self) -> Path:
        return self.run / "service.json"


def data_home(home: Path | None = None) -> Path:
    """用户数据根目录。

    三级回退，顺序有讲究：

    1. 显式传 ``home`` → ``<home>/AppData/Local/KnowledgeFlow``。
       测试靠它模拟一台干净机器。
    2. 否则用 ``LOCALAPPDATA``。**这是 Windows 的正统答案** ——
       它可能被组策略指到 D 盘，也可能因为漫游配置而不同，
       自己拼 ``Path.home()/AppData/Local`` 是猜。
    3. 最后才退回 ``Path.home()/AppData/Local``（``LOCALAPPDATA`` 被清掉时）。
    """
    if home is not None:
        return home / "AppData" / "Local" / DATA_HOME_DIRNAME
    local = os.environ.get("LOCALAPPDATA", "").strip()
    base = Path(local) if local else Path.home() / "AppData" / "Local"
    return base / DATA_HOME_DIRNAME


def find_app_paths(launcher_file: Path | None = None, *, home: Path | None = None) -> AppPaths:
    """从「程序在哪」出发拼出全部路径。

    找不到安装目录（例如直接从源码目录跑启动器做开发）时退化成「仓库布局」：
    ``root`` 指向仓库根、``runtime`` 指向 ``KF_DEV_RUNTIME``（默认
    ``~/.knowledgeflow-dev``）。这样开发期与打包后走**同一条代码路径**。
    """
    anchor = _anchor(launcher_file)
    root = _find_install_root(anchor)

    if root is not None:
        runtime = root / "runtime"
        backend = root / "backend"
    else:
        repo_root = _find_repo_root(anchor) or anchor.parent
        root = repo_root
        runtime = Path(
            os.environ.get("KF_DEV_RUNTIME", str(Path.home() / ".knowledgeflow-dev"))
        )
        backend = repo_root / "backend"

    user_home = data_home(home)
    return AppPaths(
        root=root,
        runtime=runtime,
        backend=backend,
        home=user_home,
        config=user_home / "config",
        data=user_home / "data",
        logs=user_home / "logs",
        cache=user_home / "cache",
        run=user_home / "runtime",
    )


def _anchor(launcher_file: Path | None) -> Path:
    """定位「程序本体在哪」的锚点。

    **PyInstaller 冻结后 ``__file__`` 不可用**：它指向临时解包目录
    （``sys._MEIPASS``，形如 ``C:\\Users\\x\\AppData\\Local\\Temp\\_MEI12345\\``），
    从那里往上找永远找不到安装目录，于是会静默退化成「开发模式」——
    表现为「打包好的 exe 找不到自己的 backend」。

    所以冻结时必须用 ``sys.executable``（exe 自己的完整路径）。
    """
    if launcher_file is not None:
        return launcher_file.resolve()
    if getattr(sys, "frozen", False):  # pragma: no cover - 只在打包产物里成立
        return Path(sys.executable).resolve()
    return Path(__file__).resolve()


def _find_install_root(start: Path) -> Path | None:
    """向上最多 6 层找安装目录（同时有 ``runtime/python.exe`` 与 ``backend/scripts/serve.py``）。

    两个标记**都要**：只看 ``runtime`` 会误判用户自己建的目录；
    只看 ``backend`` 会误判源码仓库（仓库里有 backend 但没有 runtime）。
    """
    candidates = [start.parent, *list(start.parents)[:6]]
    for parent in candidates:
        if (parent / "runtime" / "python.exe").is_file() and (
            parent / "backend" / "scripts" / "serve.py"
        ).is_file():
            return parent
    return None


def _find_repo_root(start: Path) -> Path | None:
    for parent in list(start.parents)[:8]:
        if (parent / "backend" / "scripts" / "serve.py").is_file():
            return parent
    return None


#: 需要预创建的子目录（与 macOS 版一致的五项）。
SUBDIRS: Final[tuple[str, ...]] = ("config", "data", "logs", "cache", "runtime")


def ensure_layout(paths: AppPaths) -> None:
    """创建用户数据目录。**只创建，不删除任何东西。**"""
    for name in SUBDIRS:
        (paths.home / name).mkdir(parents=True, exist_ok=True)


def is_windows_x64() -> bool:
    """v1.0 的 Windows 包只面向 x64（这是**打包目标**的约束，不是运行时代码的约束）。"""
    import platform

    return platform.system() == "Windows" and platform.machine().lower() in {
        "amd64",
        "x86_64",
    }


def windows_version() -> tuple[int, int, int] | None:
    """取 ``(major, minor, build)``；非 Windows 返回 ``None``。

    用 ``sys.getwindowsversion()`` 而不是 ``platform.win32_ver()``：后者在
    Windows 11 上仍然报 ``('10', '10.0.22000', ...)`` —— major 是 10，
    真正区分 10 与 11 的是 build 号，而且它需要跑一次 WMI，慢且可能失败。
    """
    if sys.platform != "win32":
        return None
    try:
        info = sys.getwindowsversion()
    except AttributeError:  # pragma: no cover - 非 Windows 上不会走到
        return None
    return int(info.major), int(info.minor), int(info.build)


__all__ = [
    "DATA_HOME_DIRNAME",
    "MIN_WINDOWS_VERSION",
    "SUBDIRS",
    "AppPaths",
    "data_home",
    "ensure_layout",
    "find_app_paths",
    "is_windows_x64",
    "windows_version",
]
