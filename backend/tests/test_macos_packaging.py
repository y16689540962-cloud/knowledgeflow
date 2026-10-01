"""v0.4 桌面打包层测试（定稿第二十节的 10 项验收）。

分两层：

**逻辑层**（任何平台都能跑）——路径解析、配置读写与权限、环境变量映射、
Vault 可写性、LLM 错误翻译、Info.plist 与依赖清单的约束。
这一层是**纯函数**，所以能在 CI（Linux x86）上跑，不需要 Mac。

**端到端层**（真的起一次后端）——用临时 ``HOME`` 模拟一台干净 Mac，
把启动器当**子进程**跑起来，验证：服务真的起来、Web UI 能访问、
SQLite 落在用户数据目录（不是 .app 里）、重复启动不会产生第二个实例、
SIGTERM 之后**不留孤儿进程**、再启动能复用同一个数据库。

两条纪律：

1. **测试绝不碰真实家目录**：``HOME`` 与 ``KF_DEV_RUNTIME`` 每个用例都指向临时目录。
   （踩过一次：早期版本把 ``~/.knowledgeflow-dev/bin/python3`` 写进了真实 home，
   顺带把另一个用例的断言搞错了。）
2. **能测行为就别扫文本**：例如「不许暴露 LAN」用假 Popen 抓真实 argv，
   而不是在源码里搜 ``0.0.0.0`` —— 后者会被自己的注释命中（本项目栽过好几次）。
"""

from __future__ import annotations

import json
import os
import plistlib
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent
LAUNCHER_DIR = REPO_ROOT / "packaging" / "macos" / "launcher"
PKG_DIR = REPO_ROOT / "packaging" / "macos"
BUILD_SCRIPT = REPO_ROOT / "scripts" / "build_macos_arm64.sh"
INFO_PLIST_TEMPLATE = PKG_DIR / "Info.plist"

pytestmark = pytest.mark.skipif(
    not LAUNCHER_DIR.is_dir(), reason="这个仓库里没有打包层（packaging/macos/launcher）"
)

if str(LAUNCHER_DIR) not in sys.path:
    sys.path.insert(0, str(LAUNCHER_DIR))

from kf_app import paths as kf_paths  # noqa: E402
from kf_app import service as kf_service  # noqa: E402
from kf_app import settings as kf_settings  # noqa: E402
from kf_app import wizard as kf_wizard  # noqa: E402


# --------------------------------------------------------------------------- #
# 环境隔离
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def sandbox(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """把 HOME 与「开发模式 runtime」都指到临时目录。

    **没有这个夹具，测试会往真实家目录里写东西** —— 早期版本就这么干过。
    """
    root = tmp_path_factory.mktemp("kf-sandbox")
    monkeypatch.setenv("HOME", str(root))
    monkeypatch.setenv("KF_DEV_RUNTIME", str(root / "dev-runtime"))
    monkeypatch.delenv("KF_SETUP_NONINTERACTIVE", raising=False)
    monkeypatch.delenv("KF_VAULT_PATH", raising=False)
    monkeypatch.delenv("KF_LLM_API_KEY", raising=False)
    return root


def make_paths(home: Path) -> kf_paths.AppPaths:
    return kf_paths.find_app_paths(home=home)


def strip_shell_comments(source: str) -> str:
    """剥掉整行注释再匹配 —— 否则断言会被自己写的说明命中。"""
    return "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    )


# --------------------------------------------------------------------------- #
# ① 逻辑层：路径
# --------------------------------------------------------------------------- #
def test_platform_is_apple_silicon() -> None:
    """定稿第二十节 Test 1。非 Apple Silicon 上跳过 —— 这是**打包目标**的约束。"""
    import platform

    if platform.system() != "Darwin":
        pytest.skip("不是 macOS，跳过架构断言")
    assert platform.machine() == "arm64", "v0.4 的安装包只面向 Apple Silicon"


def test_user_data_never_lives_inside_the_app_bundle(tmp_path: Path) -> None:
    """定稿第七 / Test 10：用户数据与程序本体**必须**分离。

    这里搭一个**合成的 .app 目录**来测：真实打包后常见路径是
    ``/Applications/KnowledgeFlow.app/Contents/Resources/...``，
    而数据必须在 ``~/Library/Application Support/`` 下。
    写在 Resources 里的后果：.app 被替换/移动/签名校验时数据就没了。
    """
    app = tmp_path / "KnowledgeFlow.app"
    resources = app / "Contents" / "Resources"
    (resources / "launcher" / "kf_app").mkdir(parents=True)
    (app / "Contents" / "Info.plist").write_text("<plist/>", encoding="utf-8")
    launcher_file = resources / "launcher" / "kf_app" / "paths.py"
    launcher_file.write_text("", encoding="utf-8")

    home = tmp_path / "fake-home"
    paths = kf_paths.find_app_paths(launcher_file, home=home)

    assert paths.resources == resources, "应当把 .app 内的 Resources 识别出来"
    assert "Application Support" in str(paths.home)
    for directory in (paths.config, paths.data, paths.logs, paths.cache, paths.run):
        assert resources not in directory.resolve().parents, f"数据目录落在 .app 里了：{directory}"


def test_data_home_follows_HOME(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """把 HOME 指到别处 = 模拟一台全新的 Mac（端到端测试就靠这个）。"""
    assert kf_paths.data_home(tmp_path) == (
        tmp_path / "Library" / "Application Support" / "KnowledgeFlow"
    )


def test_ensure_layout_creates_only_the_five_subdirs(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    kf_paths.ensure_layout(paths)
    created = sorted(p.name for p in paths.home.iterdir() if p.is_dir())
    assert created == sorted(kf_paths.SUBDIRS)
    assert set(created) == {"config", "data", "logs", "cache", "runtime"}


# --------------------------------------------------------------------------- #
# ① 逻辑层：配置
# --------------------------------------------------------------------------- #
def test_settings_roundtrip_and_file_permissions(tmp_path: Path) -> None:
    """定稿 Test 7：配置能持久化。顺带守住 0600 —— 里面有 API Key。"""
    paths = make_paths(tmp_path)
    kf_paths.ensure_layout(paths)
    original = kf_settings.Settings(
        obsidian_vault_path=str(tmp_path / "vault"),
        llm_provider="deepseek",
        llm_api_key="sk-not-a-real-key",
        llm_model="deepseek-flash",
    )
    kf_settings.save(paths, original)

    mode = paths.settings_file.stat().st_mode & 0o777
    assert mode == 0o600, f"settings.json 权限应是 0600（当前 {oct(mode)}）—— 里面有 API Key"

    loaded = kf_settings.load(paths)
    assert loaded is not None
    assert loaded.llm_api_key == original.llm_api_key
    assert loaded.obsidian_vault_path == original.obsidian_vault_path
    assert loaded.created_at, "保存时应自动写上 created_at"


def test_broken_settings_file_does_not_crash(tmp_path: Path) -> None:
    """配置损坏 → 当作「没配过」，而不是抛 traceback 给用户。"""
    paths = make_paths(tmp_path)
    kf_paths.ensure_layout(paths)
    paths.settings_file.write_text("{ 这不是 json", encoding="utf-8")
    assert kf_settings.load(paths) is None


def test_missing_fields_are_reported_in_plain_language() -> None:
    gaps = kf_settings.Settings().missing()
    assert any("Vault" in g for g in gaps)
    assert any("API Key" in g for g in gaps)
    assert not any(g.islower() and "_" in g for g in gaps), "不该把字段名直接甩给用户"


def test_env_uses_absolute_paths_for_database_and_media(tmp_path: Path) -> None:
    """**这是打包最容易翻车的地方**。

    后端默认 ``DATABASE_URL=sqlite:///./data/...`` 依赖 cwd；Finder 启动的 .app
    cwd 是 ``/``，相对路径会落到 ``/data/``。所以打包层必须喂绝对路径。
    """
    paths = make_paths(tmp_path)
    env = kf_settings.build_env(paths, kf_settings.Settings())

    assert env["DATABASE_URL"].startswith("sqlite:////"), env["DATABASE_URL"]
    db_path = env["DATABASE_URL"].replace("sqlite:///", "")
    assert str(paths.database_file) in db_path
    assert Path(db_path).is_absolute()

    assert Path(env["DOWNLOAD_DIR"]).is_absolute()
    assert str(paths.data) in env["DOWNLOAD_DIR"]


def test_env_maps_provider_specific_keys(tmp_path: Path) -> None:
    """后端是 ``Literal["openai","deepseek"]`` + 两套 Key 名，映射不能错位。"""
    paths = make_paths(tmp_path)

    deepseek = kf_settings.build_env(
        paths, kf_settings.Settings(llm_provider="deepseek", llm_api_key="k1", llm_model="m")
    )
    assert deepseek["LLM_PROVIDER"] == "deepseek"
    assert deepseek["DEEPSEEK_API_KEY"] == "k1"
    assert deepseek["DEEPSEEK_BASE_URL"].startswith("https://api.deepseek.com")

    openai = kf_settings.build_env(
        paths,
        kf_settings.Settings(
            llm_provider="openai", llm_api_key="k2", llm_model="m", llm_base_url="http://x/v1"
        ),
    )
    assert openai["OPENAI_API_KEY"] == "k2"
    assert openai["OPENAI_BASE_URL"] == "http://x/v1"


def test_env_keeps_model_cache_inside_app_data(tmp_path: Path) -> None:
    """Whisper 模型别落到 ~/.cache —— 卸载时要能一起清掉。"""
    paths = make_paths(tmp_path)
    env = kf_settings.build_env(paths, kf_settings.Settings())
    assert env["HF_HOME"] == str(paths.cache / "huggingface")


# --------------------------------------------------------------------------- #
# ① 逻辑层：向导的判定
# --------------------------------------------------------------------------- #
def test_probe_vault_accepts_writable_and_leaves_nothing_behind(tmp_path: Path) -> None:
    vault = tmp_path / "MyVault"
    vault.mkdir()
    ok, message = kf_settings.probe_vault(str(vault))
    assert ok, message
    assert list(vault.iterdir()) == [], "测试文件必须删掉"


def test_probe_vault_rejects_missing_path(tmp_path: Path) -> None:
    ok, message = kf_settings.probe_vault(str(tmp_path / "nope"))
    assert not ok
    assert "不存在" in message


def test_probe_vault_rejects_a_file(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("x", encoding="utf-8")
    ok, message = kf_settings.probe_vault(str(target))
    assert not ok
    assert "文件" in message


@pytest.mark.parametrize(
    ("status", "expected"),
    [(401, "API Key"), (404, "Base URL"), (429, "限流")],
)
def test_probe_llm_translates_http_errors(
    monkeypatch: pytest.MonkeyPatch, status: int, expected: str
) -> None:
    """定稿第十节：失败要给出「检查什么」，不是把 HTTP 状态码原样丢出来。"""

    def fake_urlopen(request: object, timeout: float = 0) -> object:  # noqa: ARG001
        raise urllib.error.HTTPError("https://api.example.com/v1/chat/completions",
                                     status, "err", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    ok, message = kf_settings.probe_llm("https://api.example.com/v1", "k", "m")
    assert not ok
    assert expected in message, message


def test_probe_llm_reports_network_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(request: object, timeout: float = 0) -> object:  # noqa: ARG001
        raise urllib.error.URLError("nodename nor servname provided")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    ok, message = kf_settings.probe_llm("https://nope.invalid/v1", "k", "m")
    assert not ok
    assert "连不上" in message


def test_optional_capabilities_never_block_startup(tmp_path: Path) -> None:
    """定稿第十五节：ASR / OCR 缺了**不能**成为启动的硬阻塞。

    构造「内置 runtime 与后端都在」的情形，确认 ``blocking_problems`` 为空 ——
    也就是 tesseract / whisper 的缺失都不算阻塞项。
    """
    paths = make_paths(tmp_path)
    kf_paths.ensure_layout(paths)
    paths.runtime_python.parent.mkdir(parents=True, exist_ok=True)
    paths.runtime_python.write_text("#!/bin/sh\n", encoding="utf-8")
    assert paths.backend.is_dir(), "仓库布局下 backend 应存在"

    assert kf_wizard.blocking_problems(paths) == []

    checks = {name: ok for name, ok, _ in kf_wizard.check_environment(paths)}
    assert any("OCR" in name for name in checks), "环境检查应列出 OCR 状态"
    assert checks.get("音视频解码") is True, "本项目用 PyAV，不该要求系统装 ffmpeg"
    # 汇总文字里不该出现异常类型
    summary = kf_wizard.environment_summary(paths)
    assert "Traceback" not in summary and "ModuleNotFound" not in summary


def test_missing_runtime_is_blocking(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    kf_paths.ensure_layout(paths)
    problems = kf_wizard.blocking_problems(paths)  # runtime 故意不创建
    assert any("运行环境" in p for p in problems), problems


# --------------------------------------------------------------------------- #
# ① 逻辑层：启动行为（测行为，不扫文本）
# --------------------------------------------------------------------------- #
class _FakeProcess:
    def __init__(self, argv: list[str], kwargs: dict) -> None:
        self.argv = argv
        self.kwargs = kwargs
        self.pid = 4242
        self._returncode: int | None = None

    def poll(self) -> int | None:
        return self._returncode

    def wait(self, timeout: float | None = None) -> int:
        self._returncode = 0
        return 0

    def send_signal(self, sig: int) -> None:  # pragma: no cover - 由 terminate 调用
        pass


def test_service_start_never_exposes_lan(tmp_path: Path) -> None:
    """定稿第十一节：只监听 127.0.0.1，**绝不允许** 0.0.0.0 / --expose。

    用注入的假进程工厂抓真实 argv —— 比在源码里搜字符串可靠：源码里
    「我们不用 0.0.0.0」这句注释会把文本断言骗过去。

    **刻意不 patch ``subprocess.Popen``**：那是全局打补丁，会把同一进程里
    所有用它的人一起换掉（实测把运行环境的 Python 垫片搞崩过）。
    """
    captured: dict[str, list[str]] = {}

    def fake_spawn(argv: list[str], **kwargs: object) -> _FakeProcess:
        captured["argv"] = list(argv)
        return _FakeProcess(list(argv), dict(kwargs))

    paths = make_paths(tmp_path)
    kf_paths.ensure_layout(paths)
    kf_service.start(paths, {"PATH": "/usr/bin"}, 8123, log_path=paths.app_log, spawn=fake_spawn)

    argv = captured["argv"]
    joined = " ".join(argv)
    assert "--expose" not in argv, "打包层不许把服务暴露到局域网"
    assert "--host" not in argv, "打包层不许改监听地址（默认就是回环）"
    assert "0.0.0.0" not in joined
    assert argv[0].endswith("python3"), f"应当用内嵌解释器启动：{argv[0]}"
    assert argv[1].endswith("scripts/serve.py"), f"应当复用现有后端入口：{argv[1]}"


def test_service_start_writes_state_and_uses_absolute_paths(tmp_path: Path) -> None:
    def fake_spawn(argv: list[str], **kwargs: object) -> _FakeProcess:
        return _FakeProcess(list(argv), dict(kwargs))

    paths = make_paths(tmp_path)
    kf_paths.ensure_layout(paths)
    handle = kf_service.start(
        paths, {"PATH": "/usr/bin"}, 8123, log_path=paths.app_log, spawn=fake_spawn
    )

    assert handle.owned is True
    state = kf_service.read_state(paths)
    assert state is not None and state["port"] == 8123
    kf_service.clear_state(paths)
    assert kf_service.read_state(paths) is None


def test_health_probe_retries_before_concluding_nothing_runs(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """一次探活失败**不能**就断定「服务没在运行」。

    机器忙时（比如同时在跑整个测试套件）单次 1.5 秒探活可能超时。早期实现只探一次，
    于是会把自己的服务误判成「端口被别的程序占用」而拒绝启动 ——
    端到端测试时过时不过就是这个原因。这里把「失败一次再成功」造出来。
    """
    calls = {"n": 0}
    payload = {"version": "0.4.0", "llm_provider": "deepseek"}

    def flaky(port: int, *, timeout: float = 0) -> dict | None:  # noqa: ARG001
        calls["n"] += 1
        return payload if calls["n"] >= 2 else None

    monkeypatch.setattr(kf_service, "health", flaky)
    monkeypatch.setattr(kf_service.time, "sleep", lambda _s: None)  # 测试里不用真等

    assert kf_service.health_with_retry(12345) == payload
    assert calls["n"] == 2, "应当重试一次后才拿到结果"


def test_health_probe_gives_up_after_all_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def always_none(port: int, *, timeout: float = 0) -> None:  # noqa: ARG001
        calls["n"] += 1
        return None

    monkeypatch.setattr(kf_service, "health", always_none)
    monkeypatch.setattr(kf_service.time, "sleep", lambda _s: None)
    assert kf_service.health_with_retry(12345) is None
    assert calls["n"] == kf_service.PROBE_ATTEMPTS


def test_health_probe_retries_before_concluding_nothing_runs(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """一次探活失败**不能**就断定「服务没在运行」。

    机器忙时（比如同时在跑整个测试套件）单次 1.5 秒探活可能超时。早期实现只探一次，
    于是会把自己的服务误判成「端口被别的程序占用」而拒绝启动 ——
    端到端测试时过时不过就是这个原因。这里把「失败一次再成功」造出来。
    """
    calls = {"n": 0}
    payload = {"version": "0.4.0", "llm_provider": "deepseek"}

    def flaky(port: int, *, timeout: float = 0) -> dict | None:  # noqa: ARG001
        calls["n"] += 1
        return payload if calls["n"] >= 2 else None

    monkeypatch.setattr(kf_service, "health", flaky)
    monkeypatch.setattr(kf_service.time, "sleep", lambda _s: None)  # 测试里不用真等

    assert kf_service.health_with_retry(12345) == payload
    assert calls["n"] == 2, "应当重试一次后才拿到结果"


def test_health_probe_gives_up_after_all_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def always_none(port: int, *, timeout: float = 0) -> None:  # noqa: ARG001
        calls["n"] += 1
        return None

    monkeypatch.setattr(kf_service, "health", always_none)
    monkeypatch.setattr(kf_service.time, "sleep", lambda _s: None)
    assert kf_service.health_with_retry(12345) is None
    assert calls["n"] == kf_service.PROBE_ATTEMPTS


def test_stop_does_not_claim_success_without_stopping_anything(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """**修过的真 bug**：状态文件不在时，旧实现直接返回「已停止」。

    现在的契约：没停掉任何东西就返回 ``False``，让调用方如实报告。
    """
    paths = make_paths(tmp_path)
    kf_paths.ensure_layout(paths)
    monkeypatch.setattr(kf_service, "_listener_pid", lambda port: None)
    assert kf_service.stop_by_state(paths, 1) is False


# --------------------------------------------------------------------------- #
# ① 逻辑层：产物约束
# --------------------------------------------------------------------------- #
def test_info_plist_template_is_valid() -> None:
    """Info.plist 是构建产物的一部分，坏了 .app 就打不开。"""
    raw = INFO_PLIST_TEMPLATE.read_text(encoding="utf-8")
    assert "__VERSION__" in raw, "构建脚本靠这个占位符注入版本号"
    data = plistlib.loads(raw.encode("utf-8"))

    assert data["CFBundleExecutable"] == "KnowledgeFlow"
    assert data["CFBundleIdentifier"] == "com.knowledgeflow.desktop"
    # Dock 里要能看到、能右键退出（退出发 SIGTERM，启动器据此收子进程）
    assert data["LSUIElement"] is False
    assert data["NSHighResolutionCapable"] is True


def test_min_macos_version_is_consistent() -> None:
    """两处写的「最低系统版本」必须一致，否则会出现装着打不开。"""
    data = plistlib.loads(INFO_PLIST_TEMPLATE.read_bytes())
    declared = tuple(int(x) for x in data["LSMinimumSystemVersion"].split("."))
    assert declared == kf_paths.MIN_MACOS_VERSION


def test_build_script_only_targets_apple_silicon() -> None:
    source = strip_shell_comments(BUILD_SCRIPT.read_text(encoding="utf-8"))
    assert "uname -s" in source and "uname -m" in source
    assert "arm64" in source


def test_app_requirements_exclude_developer_tools() -> None:
    """装进用户机器的东西里不该有测试框架和浏览器驱动（playwright 135MB）。

    先剥注释 —— 依赖文件里正好写了「去掉的：playwright / pytest / coverage」，
    不剥的话断言会被这句说明命中。
    """
    core = strip_shell_comments(
        (PKG_DIR / "requirements-app-core.txt").read_text(encoding="utf-8")
    )
    for banned in ("playwright", "pytest", "coverage"):
        assert banned not in core, f"打包依赖里不该有 {banned}"


def test_av_is_pinned_below_19() -> None:
    """PyAV 19 删了 metadata_errors，faster-whisper 会直接 TypeError。

    这个坑之前踩过（av 19.0.0 必炸、18.1.0 正常），打包依赖里必须钉住。
    """
    media = (PKG_DIR / "requirements-app-media.txt").read_text(encoding="utf-8")
    assert re.search(r"^av>=11,<19\s*$", media, re.MULTILINE), "av 必须 <19"


def test_generated_app_launcher_uses_braced_variables() -> None:
    """``$VAR`` 后面紧跟中文时 bash 会把中文字节并进变量名（本项目踩过一次）。

    构建脚本里的入口模板必须写 ``${VAR}``；先剥注释再匹配。
    """
    source = strip_shell_comments(BUILD_SCRIPT.read_text(encoding="utf-8"))
    offenders = [
        line.strip()
        for line in source.splitlines()
        if re.search(r"\$[A-Za-z_][A-Za-z0-9_]*[^\x00-\x7F]", line)
    ]
    assert offenders == [], f"变量后面紧跟非 ASCII，请写 ${{VAR}}：{offenders}"


# --------------------------------------------------------------------------- #
# ② 端到端层：真的起一次后端
# --------------------------------------------------------------------------- #
def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _http_status(url: str, *, timeout: float = 3.0) -> int | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return int(response.status)
    except (urllib.error.URLError, OSError, TimeoutError):
        return None


def _wait_status(url: str, *, want: int, timeout: float = 60.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _http_status(url) == want:
            return True
        time.sleep(0.3)
    return False


def _launcher_env(fake_home: Path, vault: Path) -> dict[str, str]:
    """构造一个「干净 Mac」的运行环境。"""
    env = dict(os.environ)
    env.update(
        {
            "HOME": str(fake_home),
            "KF_SETUP_NONINTERACTIVE": "1",
            "KF_VAULT_PATH": str(vault),
            # 假 Key：非交互向导只校验 Vault，不发起真实模型调用
            "KF_LLM_API_KEY": "sk-not-a-real-key-for-tests",
            "KF_LLM_PROVIDER": "deepseek",
            "KF_LLM_MODEL": "deepseek-flash",
            # 「内嵌 runtime」在测试里就是当前解释器所在的 venv。
            # **不能用 Path(sys.executable).resolve()** —— venv 的 bin/python 是软链，
            # resolve() 会解析成基础解释器，而基础解释器里没有 fastapi（实测踩过）。
            "KF_DEV_RUNTIME": str(Path(sys.prefix)),
        }
    )
    for stale in ("OBSIDIAN_VAULT_PATH", "DATABASE_URL", "DOWNLOAD_DIR"):
        env.pop(stale, None)
    return env


def _spawn_launcher(env: dict[str, str], *args: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-m", "kf_app.main", *args],
        cwd=str(LAUNCHER_DIR),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def _app_home(fake_home: Path) -> Path:
    return fake_home / "Library" / "Application Support" / "KnowledgeFlow"


def _diagnostics(env: dict[str, str], fake_home: Path) -> str:
    """失败时把能看的都打出来 —— 不然只有一个 assert False 什么都查不出。"""
    parts = []
    for name in ("launcher.log", "app.log"):
        path = _app_home(fake_home) / "logs" / name
        if path.is_file():
            parts.append(f"--- {name} ---\n{path.read_text(encoding='utf-8', errors='replace')[-3000:]}")
    return "\n".join(parts) or "(没有任何日志 —— 启动器可能根本没跑起来)"


@pytest.fixture
def clean_machine(tmp_path: Path) -> tuple[Path, Path]:
    """一台「干净的 Mac」：空的 HOME + 一个空 Vault。"""
    home = tmp_path / "fake-home"
    vault = tmp_path / "ObsidianVault"
    home.mkdir()
    vault.mkdir()
    return home, vault


def _snapshot_repo_data() -> dict[str, int]:
    """记录仓库 ``backend/data/`` 里的文件与大小，用来断言「没有新增」。

    为什么不直接断言「文件不存在」：这个目录可能有更早测试留下的空库，
    那样断言会因为无关的历史文件而失败（实测踩过一次）。
    """
    data_dir = BACKEND_ROOT / "data"
    if not data_dir.is_dir():
        return {}
    return {p.name: p.stat().st_size for p in data_dir.iterdir() if p.is_file()}


def _terminate(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


def test_end_to_end_start_serve_ui_and_stop_cleanly(
    clean_machine: tuple[Path, Path]
) -> None:
    """定稿 Test 2/3/4/5/8/9/10 一起验：

    启动器（真实子进程）→ 向导（非交互）→ 起后端 → Web UI 可访问 →
    SQLite 落在临时 HOME 的用户数据目录 → SIGTERM 后**不留孤儿** →
    再启动能复用同一个数据库。
    """
    home, vault = clean_machine
    port = _free_port()
    env = _launcher_env(home, vault)
    base = f"http://127.0.0.1:{port}"

    # 前置守卫：这个端口必须是干净的。否则启动器会「复用已有服务」直接退出，
    # 于是下面的断言会以一个完全看不懂的方式失败（实测被上一轮测试的孤儿进程坑过）。
    assert _http_status(f"{base}/api/health") is None, (
        f"端口 {port} 上已经有服务在跑了 —— 先清掉残留进程再跑测试"
    )

    # 前置守卫：这个端口必须是干净的。否则启动器会「复用已有服务」直接退出，
    # 于是下面的断言会以一个完全看不懂的方式失败（实测被上一轮测试的孤儿进程坑过）。
    assert _http_status(f"{base}/api/health") is None, (
        f"端口 {port} 上已经有服务在跑了 —— 先清掉残留进程再跑测试"
    )

    repo_data_before = _snapshot_repo_data()
    proc = _spawn_launcher(env, "--port", str(port), "--no-browser")
    try:
        started = _wait_status(f"{base}/api/health", want=200)
        assert started, f"后端没能起来\n{_diagnostics(env, home)}"

        # ① Web UI 可访问（Test 4）
        assert _http_status(f"{base}/") == 200, "Web 界面打不开"

        # ② 配置与数据库落在临时 HOME（Test 5/7/10）
        app_home = _app_home(home)
        assert (app_home / "config" / "settings.json").is_file(), "配置没写进用户数据目录"
        assert (app_home / "data" / "knowledgeflow.db").is_file(), "SQLite 没建在用户数据目录"

        # ③ 数据没有跑到仓库里去（Test 10）。
        # 注意不能断言「仓库里没有 knowledgeflow.db」—— 仓库里可能早就有一个
        # 更早的测试留下的空库。要断言的是**本次运行没有新增**文件。
        assert _snapshot_repo_data() == repo_data_before, (
            "本次运行往仓库的 backend/data/ 里写了文件 —— 用户数据位置错了"
        )

        # ④ 单实例（Test 9）：再起一次应当复用，而不是撞端口
        second = _spawn_launcher(env, "--port", str(port), "--no-browser")
        out, _ = second.communicate(timeout=60)
        text = out.decode(errors="replace")
        assert second.returncode == 0, text
        assert "已经在运行" in text, text
        assert _http_status(f"{base}/api/health") == 200, "第二次启动把服务搞挂了"

        # ⑤ 状态查询
        status = _spawn_launcher(env, "--port", str(port), "--status")
        status_out, _ = status.communicate(timeout=60)
        assert status.returncode == 0, status_out.decode(errors="replace")
        assert "运行中" in status_out.decode(errors="replace")
    finally:
        _terminate(proc)

    # ⑥ 收干净：端口应当被释放（Test 8/13 —— 不留孤儿进程）
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline and _http_status(f"{base}/api/health") is not None:
        time.sleep(0.4)
    assert _http_status(f"{base}/api/health") is None, (
        f"退出后后端仍在跑 —— 留下孤儿进程了\n{_diagnostics(env, home)}"
    )

    # ⑦ 再次启动能恢复，并复用同一个数据库（Test 8）
    again = _spawn_launcher(env, "--port", str(port), "--no-browser")
    try:
        assert _wait_status(f"{base}/api/health", want=200), (
            f"重启失败\n{_diagnostics(env, home)}"
        )
        assert (_app_home(home) / "data" / "knowledgeflow.db").is_file(), "重启后数据库不见了"
    finally:
        _terminate(again)


def test_stop_command_reports_when_nothing_runs(
    clean_machine: tuple[Path, Path]
) -> None:
    home, vault = clean_machine
    env = _launcher_env(home, vault)
    proc = _spawn_launcher(env, "--stop")
    out, _ = proc.communicate(timeout=60)
    assert proc.returncode == 0
    assert "没有在运行" in out.decode(errors="replace"), out.decode(errors="replace")


def test_check_never_prints_a_traceback(clean_machine: tuple[Path, Path]) -> None:
    """定稿第八节：给用户看的输出里**不许**出现 traceback / ModuleNotFoundError。"""
    home, vault = clean_machine
    env = _launcher_env(home, vault)
    proc = _spawn_launcher(env, "--check")
    out, _ = proc.communicate(timeout=60)
    text = out.decode(errors="replace")
    assert "Traceback" not in text
    assert "ModuleNotFoundError" not in text
    assert "后端程序" in text
