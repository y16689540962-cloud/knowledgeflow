#!/usr/bin/env python
"""合规边界守卫：`python scripts/check_compliance_guard.py`

README / COMPLIANCE.md / SECURITY.md 里向用户**承诺**的边界，绝大部分是
「代码不做某件事」—— 不伪造签名、不绕验证码、不放行任意来源、不把 Cookie
写进日志。这类承诺有个共同的坏性质：**破坏它不会让任何功能测试变红。**
把 ``allow_origins=["*"]`` 塞进 CORS，整套路用（一千五百多条）照样全绿。

所以这里把承诺翻译成可复跑的断言，做七件事：

1. **Cookie 永不泄露** —— ``with_cookie`` 只认显式传入的值，且日志脱敏清单覆盖凭据键名
2. **域名白名单有效** —— 白名单非空，且白名单外域名真的抛 ``DOMAIN_NOT_ALLOWED``
3. **CORS 不放行通配** —— 只放行 ``chrome-extension://`` 一种来源，绝不 ``*``
4. **扩展权限最小化** —— ``host_permissions`` 只含本机回环地址
5. **无验证码绕过** —— 撞上验证页时如实抛 ``REQUEST_BLOCKED``，且没有打码依赖
6. **免责声明仍在** —— 中英 README 的稳定措辞 + ``COMPLIANCE.md`` 是否存在
7. **无内部文档外泄** —— 内部进度文档不在工作区、不在 git 索引里、也没人引用它

退出码 0 = 边界完好；1 = 有边界被破坏。

``--repo-root`` 可以把「被检查的仓库根目录」指到别处（默认是脚本自己所在的仓库）。
存在的唯一理由是**负向测试**：把 README / COMPLIANCE.md / 扩展清单换成故意违规的
临时副本，断言守卫真的会报红 —— 否则这七条断言永远绿也没人知道。
``backend/tests/test_compliance_guard.py`` 就是拿它做变异测试的。

**为什么用 import 而不是正则搜源码**：要求的是「代码真的做/不做这件事」，
不是「源码里出现过某个字符串」。反例就在本文件里 —— 它自己的说明文字写着
``allow_origins=["*"]`` 这个字样，正则扫源码会**扫到自己**，于是无论 CORS 怎么改，
那条断言永远绿。所以下面每条断言要么真的调用被断言的函数，要么读**被断言的
那个文件**的源码（从不读本文件）。"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

#: 被检查的**代码**根目录：``import app.*`` 与「源码里有没有打码依赖」的扫描都用它。
#: 永远指向脚本自己所在的仓库 —— 负向测试换掉的是文档/清单，不是被测代码本身。
BACKEND_ROOT = Path(__file__).resolve().parents[1]

#: 脚本自己所在的仓库根目录（``--repo-root`` 未指定时的默认值）。
SCRIPT_REPO_ROOT = BACKEND_ROOT.parent

#: 被检查的**仓库**根目录：README / COMPLIANCE.md / 扩展清单 / 内部文档扫描都用它。
#: ``main()`` 会按 ``--repo-root`` 调 :func:`configure_repo_root` 覆盖。
REPO_ROOT = SCRIPT_REPO_ROOT

EXTENSION_MANIFEST = REPO_ROOT / "extension" / "manifest.json"
COMPLIANCE_DOC = REPO_ROOT / "COMPLIANCE.md"
README_ZH = REPO_ROOT / "README.md"
README_EN = REPO_ROOT / "README.en.md"


def configure_repo_root(repo_root: Path | None) -> None:
    """把「被检查的仓库根目录」指向 ``repo_root``；``None`` = 恢复脚本自己的仓库。

    只影响那些「读仓库文件」的断言（扩展清单 / 免责声明 / 内部文档扫描）。
    被测代码的 import 与源码扫描仍用 :data:`BACKEND_ROOT` —— 否则变异测试
    得复制整个后端才能跑。
    """
    global REPO_ROOT, EXTENSION_MANIFEST, COMPLIANCE_DOC, README_ZH, README_EN

    REPO_ROOT = SCRIPT_REPO_ROOT if repo_root is None else Path(repo_root).expanduser().resolve()
    EXTENSION_MANIFEST = REPO_ROOT / "extension" / "manifest.json"
    COMPLIANCE_DOC = REPO_ROOT / "COMPLIANCE.md"
    README_ZH = REPO_ROOT / "README.md"
    README_EN = REPO_ROOT / "README.en.md"

def _display_path(path: Path) -> str:
    """尽量用「相对仓库根」展示路径；代码根在仓库外时（负向测试）退回绝对路径。"""
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return str(path)


#: 内部文档：**不随公开仓库发布**。文件名本身就是判断依据。
INTERNAL_DOC_NAMES = ("KnowledgeFlow-进度与问题清单.md",)

#: 公开仓库里**允许**提到内部文档名的文件 —— 逐条注明理由，防止这份白名单被滥用。
#: 判据是「源码里是不是作为路径字面量用它」，不是「有没有出现这个词」。
INTERNAL_DOC_MENTION_ALLOWED = {
    "backend/scripts/check_doc_consistency.py": "内部文档的检查器，--doc 默认值就是它",
    "backend/scripts/check_compliance_guard.py": "本文件：内部文件名是这条检查的判据",
    "backend/tests/test_docs_consistency.py": "缺了该文档就 skip（它自己注释说明了原因）",
}

#: 域名白名单的规模下限。**不为 0 就够了** —— 真有人把白名单删成 3 个域名，
#: 「非空」这条照样通过。实测口径见同目录 README/COMPLIANCE.md。
MIN_ALLOWED_HOSTS = 8

#: 扩展只该请求本机回环地址。
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

#: 扩展声明里**不该出现**的权限：越权读所有标签页 / 读页面内容 / 全网请求。
#: 只列「真的会扩大攻击面」的，不追求穷举。
FORBIDDEN_EXTENSION_PERMISSIONS = frozenset(
    {"tabs", "<all_urls>", "webRequest", "webRequestBlocking", "debugger", "cookies", "proxy"}
)

#: 浏览代码时**不该出现**的解码/绕过信号（依赖名、打码平台）。
#: ``captcha`` 不在里面 —— 代码里必须能**识别**验证页（见第 5 项），
#: 这里针对的是「识别出来之后想办法过掉」。
BYPASS_MARKERS = (
    "2captcha",
    "anti-captcha",
    "anticaptcha",
    "capsolver",
    "yescaptcha",
    "ruokuai",
    "ddddocr",
    "deathbycaptcha",
    "solve_captcha",
    "captcha_solver",
    "bypass_captcha",
)

#: 免责声明里**稳定的**措辞。
#:
#: 刻意只钉这两条 —— 它们描述的是「我们不做的事」，与 MIT 许可不冲突，
#: 不会因为许可澄清或链接/锚点调整而改动（README 的其它句子会）。
#: 锚点、链接格式、段落顺序都不在这里断言。
#:
#: 这里存的是**相对仓库根的文件名**而不是 Path：``--repo-root`` 会把
#: :data:`REPO_ROOT` 换掉，import 时就把绝对 Path 钉进元组会让负向测试失效。
DISCLAIMER_MARKERS = (
    ("README.md", "不伪造签名", "中文 README 的「不伪造签名」承诺"),
    ("README.md", "不绕过验证码", "中文 README 的「不绕过验证码」承诺"),
    ("README.en.md", "does not forge signatures", "英文 README 的对应承诺（伪造签名）"),
    ("README.en.md", "bypass CAPTCHAs", "英文 README 的对应承诺（绕过验证码）"),
)

#: 日志脱敏必须覆盖的键名（第二十四节硬性约束，COMPLIANCE.md 逐字引用过）。
REQUIRED_SENSITIVE_KEYS = ("api_key", "authorization", "token", "cookie")

#: 白名单**外**的域名，逐个都要被 ``validate_url`` 挡下。
DISALLOWED_PROBE_HOSTS = (
    "https://evil.example.com/steal",
    "http://localhost:8000/api/health",
    "https://www.douyin.com.evil.example.com/video/123456",
)

URL_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)

#: 让 ``import app.*`` 生效：脚本自己把 ``backend/`` 塞进 ``sys.path``。
#: CI 在 ``backend/`` 下跑（``defaults.run.working-directory``），本地也照 README 从
#: ``backend/`` 跑，所以正常情况下 sys.path 里已经有 ``backend/``；显式兜一下底，
#: 让「从仓库根直接跑 scripts/…」也能工作。
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="合规边界守卫（不伪造签名 / 不绕验证码 / 不放行通配）")
    parser.add_argument("--verbose", action="store_true", help="逐项打印每条断言的结果")
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="被检查的仓库根目录（默认：脚本自己所在的仓库）。只给负向测试用。",
    )
    return parser.parse_args(argv)


# --------------------------------------------------------------------------- #
# 结果收集
# --------------------------------------------------------------------------- #
class Guard:
    """逐条记结果：``check(name)`` 里 yield 的每个字符串都是一条违规。

    ``notes`` 是给「能查的查了、不能查的说明白」用的：优雅降级要**说出来**，
    否则读者分不清「检查通过」和「检查压根没跑」。
    """

    def __init__(self, verbose: bool = False) -> None:
        self.verbose = verbose
        self.problems: list[str] = []
        self.sections: list[str] = []
        self.notes: list[str] = []

    def check(self, title: str, items) -> None:
        found = [item for item in items if item]
        self.problems += found
        if found:
            self.sections.append(f"[FAIL] {title}")
        else:
            self.sections.append(f"[OK] {title}")
        if self.verbose:
            for item in found:
                self.sections.append(f"       ↳ {item}")


# --------------------------------------------------------------------------- #
# 1. Cookie 永不泄露
# --------------------------------------------------------------------------- #
def check_cookie_never_leaks(guard: Guard) -> None:
    def items():
        from app.logging_config import is_sensitive_key, scrub_text

        # --- 正向：with_cookie 的**行为**，逐个边界情况真调一遍 ---
        from app.ingestion.redirects import with_cookie

        if with_cookie(None) != {}:
            yield "with_cookie(None) 不是空 dict —— 会塞一个空 Cookie 头让服务端以为有会话"
        if with_cookie("   ") != {}:
            yield "with_cookie('   ') 不是空 dict —— 空白 Cookie 也算「有会话」"
        sample = "sessionid=secret-value"
        if with_cookie(sample) != {"Cookie": sample}:
            yield f"with_cookie({sample!r}) 没有把显式传入的值原样并进请求头"

        # --- 反向：任何人都不能**凭空**造出 Cookie 头 ---
        # 传 None（= 用户没配登录态）却拿到 Cookie 头，就是窃取/伪造的信号。
        forged = with_cookie(None)
        if any(key.lower() == "cookie" for key in forged):
            yield f"用户没配登录态却拿到了 Cookie 头：{forged!r} —— 存在凭空造 Cookie 的路径"

        # --- 与请求头合并层：不显式传 cookie 时，请求头里不得凭空出现 Cookie ---
        try:
            from app.ingestion import redirects as _redirects

            for helper in ("build_headers", "request_headers"):
                merge = getattr(_redirects, helper, None)
                if merge is None:
                    continue
                merged = merge(cookie=None)
                if any(str(k).lower() == "cookie" for k in merged):
                    yield f"{helper}(cookie=None) 的请求头里凭空出现 Cookie"
        except ImportError as exc:  # pragma: no cover - 只有代码结构被大改时才会走到
            yield f"导入 app.ingestion.redirects 失败：{exc}"

        # --- 脱敏清单 + 真实脱敏行为 ---
        for key in REQUIRED_SENSITIVE_KEYS:
            if not is_sensitive_key(key):
                yield f"日志脱敏清单漏了 {key!r} —— 该键名的值会被原样写进日志"
            for variant in (key.upper(), f"X-{key}", f"my_{key}_value"):
                if not is_sensitive_key(variant):
                    yield f"日志脱敏对 {variant!r} 不生效（键名匹配必须大小写不敏感、子串匹配）"

        # --- 真实凭据形状的字符串必须被 scrub 掉（模拟有人直接拿原文去 log） ---
        leaks = (
            ("api_key=AKIAIOSFODNN7EXAMPLE", "AKIAIOSFODNN7EXAMPLE", "api_key="),
            ("Authorization: Bearer ghp_1234567890abcdef", "ghp_1234567890abcdef", "Bearer "),
            ("cookie=sessionid%3Dabcdef123456", "abcdef123456", "cookie="),
            ("token=abcdef1234567890", "abcdef1234567890", "token="),
        )
        for text, secret, _prefix in leaks:
            if secret in scrub_text(text):
                yield f"scrub_text 没脱敏 {text!r} 里的凭据"

    guard.check("Cookie 永不泄露（with_cookie 只认显式值 + 日志脱敏清单）", items())


# --------------------------------------------------------------------------- #
# 2. 域名白名单有效
# --------------------------------------------------------------------------- #
def check_domain_allowlist(guard: Guard) -> None:
    def items():
        from app.errors import ErrorType, URLValidationError
        from app.normalization.url import DEFAULT_ALLOWED_HOSTS, host_allowed, validate_url

        hosts = tuple(str(h) for h in DEFAULT_ALLOWED_HOSTS)
        if len(hosts) < MIN_ALLOWED_HOSTS:
            yield (
                f"域名白名单只剩 {len(hosts)} 条（下限 {MIN_ALLOWED_HOSTS}）："
                f"{hosts!r} —— 白名单被削到形同虚设"
            )
        for host in hosts:
            if not host or "/" in host or "*" in host or not re.fullmatch(r"[a-z0-9.\-:]+", host):
                yield f"白名单条目 {host!r} 不合法（通配符 / 空值 / 带路径都会让白名单失效）"

        # --- 正向：白名单内的域名必须放行 ---
        for url in ("https://www.douyin.com/video/123456", "https://b23.tv/abcdefg"):
            try:
                validate_url(url)
            except URLValidationError as exc:
                yield f"白名单内的 {url} 被拒绝（{exc.error_type}）—— 白名单生效过头了"

        # --- 反向：白名单外的域名必须抛 DOMAIN_NOT_ALLOWED ---
        for url in DISALLOWED_PROBE_HOSTS:
            try:
                validate_url(url)
            except URLValidationError as exc:
                if exc.error_type is not ErrorType.DOMAIN_NOT_ALLOWED:
                    yield f"{url} 被拒但 error_type={exc.error_type}，不是 DOMAIN_NOT_ALLOWED"
            else:
                yield f"{url} 通过了校验 —— 白名单失效（任意域名都能进来）"

        # --- 后缀白名单不能被后缀拼接骗过（douyin.com.evil.example.com）---
        if host_allowed("www.douyin.com.evil.example.com", hosts):
            yield "host_allowed 被 'douyin.com.evil.example.com' 骗过 —— 后缀匹配写错了"

        # --- 用户自定义白名单不能变成「关掉校验」的后门 ---
        try:
            validate_url("https://evil.example.com/x", allowed_hosts=("*",))
        except URLValidationError:
            pass
        else:
            yield "allowed_hosts=('*',) 放行了任意域名 —— 自定义白名单存在通配后门"

    guard.check("域名白名单有效（白名单外一律 DOMAIN_NOT_ALLOWED）", items())


# --------------------------------------------------------------------------- #
# 3. CORS 不放行通配
# --------------------------------------------------------------------------- #
def check_cors(guard: Guard) -> None:
    def items():
        from app.api.app import CORS_ALLOWED_HEADERS, CORS_ALLOWED_METHODS, EXTENSION_ORIGIN_PATTERN

        pattern = str(EXTENSION_ORIGIN_PATTERN)
        if not (pattern.startswith("^") and pattern.endswith("$")):
            yield f"EXTENSION_ORIGIN_PATTERN 没用 ^…$ 锚定：{pattern!r} —— 前缀匹配会放行伪造来源"
        # ``*`` 只在**正则量词**位置合法（``[a-p]{32}`` / ``.*`` 里的 ``*``），
        # 作为**匹配内容**出现则是通配放行。
        if re.search(r"\\\*|\*[^){?0-9]|\(\?[^)]*\*", pattern):
            yield f"EXTENSION_ORIGIN_PATTERN 里出现通配符：{pattern!r}"
        if "chrome-extension://" not in pattern:
            yield f"EXTENSION_ORIGIN_PATTERN 不再限定 chrome-extension 来源：{pattern!r}"

        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            yield f"EXTENSION_ORIGIN_PATTERN 不是合法正则：{exc}"
            return

        # 32 个 a-p 是 Chrome 扩展 id 的实际字符集。
        if compiled.match("chrome-extension://" + "a" * 32) is None:
            yield f"合法扩展来源 chrome-extension://{'a'*32} 不被放行：{pattern!r}"

        # --- 反向：通配 / 网页来源 / 长度不对的扩展 id 都必须被拒 ---
        for origin in (
            "*",
            "null",
            "http://evil.example.com",
            "https://www.douyin.com",
            "http://127.0.0.1:8000",
            "chrome-extension://" + "z" * 32,  # z 不在 a-p 里
            "chrome-extension://" + "a" * 31,  # 长度不对
            "chrome-extension://" + "a" * 32 + ".evil.com",
        ):
            if compiled.match(origin) is not None:
                yield f"来源 {origin!r} 被 CORS 放行 —— 只该放行 chrome-extension://<32 位 a-p id>"

        # --- 中间件配置：不能出现 allow_origins=["*"] 这条路径 ---
        from app.api.app import install_extension_cors

        captured: list[tuple] = []
        original = None
        try:
            from fastapi import FastAPI

            try:
                app = FastAPI()
            except Exception:  # pragma: no cover - fastapi 缺失时下面会报出来
                yield "起不了 FastAPI 实例，无法核对 CORS 中间件配置"
                return
            # 捕获 add_middleware 的调用参数（CORSMiddleware 本身不暴露 options）。
            original = app.add_middleware

            def spy(*args, **kwargs):  # type: ignore[no-untyped-def]
                captured.append((args, kwargs))
                return original(*args, **kwargs)

            app.add_middleware = spy  # type: ignore[method-assign]
            install_extension_cors(app)
        except ImportError as exc:
            yield f"导入 install_extension_cors 失败：{exc}"
            return

        calls = [
            kwargs
            for args, kwargs in captured
            if getattr(args[0], "__name__", "") == "CORSMiddleware"
        ]
        if not calls:
            yield "install_extension_cors 没有装 CORSMiddleware（或装在了别的对象上）"
        for kwargs in calls:
            if "allow_origins" in kwargs:
                yield f"CORS 用了 allow_origins={kwargs['allow_origins']!r} —— 通配来源就是数据泄露"
            regex = kwargs.get("allow_origin_regex")
            if regex != pattern:
                yield f"中间件实际用的 allow_origin_regex={regex!r}，与 EXTENSION_ORIGIN_PATTERN 不一致"
            if not kwargs.get("allow_methods"):
                yield "CORS 没限定 allow_methods（等于放行所有方法）"
            if kwargs.get("allow_credentials"):
                yield "CORS 开了 allow_credentials —— 放行来源不再是唯一防线"

        # --- 用真实来源打一次中间件，确认非法来源真的拿不到放行头 ---
        if not _preflight_allowed(pattern, "chrome-extension://" + "a" * 32):
            yield "真实预检失败：合法扩展来源没有拿到 Access-Control-Allow-Origin 头"
        for bad in ("http://evil.example.com", "*", "null"):
            if _preflight_allowed(pattern, bad):
                yield f"真实预检放行了非法来源 {bad!r}"

        if "GET" not in CORS_ALLOWED_METHODS or "POST" not in CORS_ALLOWED_METHODS:
            yield f"CORS_ALLOWED_METHODS={CORS_ALLOWED_METHODS!r} 缺了扩展真正要用的方法"
        if "Content-Type" not in CORS_ALLOWED_HEADERS:
            yield f"CORS_ALLOWED_HEADERS={CORS_ALLOWED_HEADERS!r} 少了 Content-Type（扩展的 POST 会失败）"

    guard.check("CORS 不放行通配（只认 chrome-extension://<32 位 id>）", items())


def _preflight_allowed(origin_pattern: str, origin: str) -> bool:
    """拿真实中间件跑一次预检，看该来源有没有拿到放行头。

    用 ``httpx.ASGITransport`` 而不是 ``starlette.testclient.TestClient``：
    后者会触发 ``StarletteDeprecationWarning``（httpx/httpx2 迁移），而守卫自己
    不该在 CI 日志里制造这种噪音 —— 用 httpx 直接驱动 ASGI 应用，行为等价且无告警。
    """
    try:
        import asyncio

        import httpx
        from fastapi import FastAPI
        from fastapi.middleware.cors import CORSMiddleware
    except ImportError:  # pragma: no cover - 依赖缺失时由调用方的其它断言兜底
        return False

    app = FastAPI()
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=origin_pattern,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type"],
        allow_credentials=False,
    )

    @app.get("/probe")
    async def probe() -> dict[str, bool]:  # pragma: no cover - 只被下面的 client 调用
        return {"ok": True}

    async def send() -> bool:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://guard.local") as client:
            response = await client.get("/probe", headers={"Origin": origin})
        return response.headers.get("access-control-allow-origin") is not None

    return asyncio.run(send())


# --------------------------------------------------------------------------- #
# 4. 扩展权限最小化
# --------------------------------------------------------------------------- #
def check_extension_permissions(guard: Guard) -> None:
    def items():
        if not EXTENSION_MANIFEST.is_file():
            yield f"找不到扩展清单：{EXTENSION_MANIFEST.relative_to(REPO_ROOT)}"
            return
        try:
            manifest = json.loads(EXTENSION_MANIFEST.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            yield f"{EXTENSION_MANIFEST.name} 不是合法 JSON：{exc}"
            return
        if not isinstance(manifest, dict):
            yield f"{EXTENSION_MANIFEST.name} 顶层不是对象"
            return

        permissions = [str(item) for item in manifest.get("permissions", [])]
        hosts = [str(item) for item in manifest.get("host_permissions", [])]

        if not hosts:
            yield "host_permissions 为空 —— 扩展请求不了本机后端（要么写错，要么被清空）"

        for permission in permissions:
            if permission in FORBIDDEN_EXTENSION_PERMISSIONS or permission == "<all_urls>":
                yield f"扩展申请了越权权限 {permission!r}（只该要 activeTab + storage）"
        for extra in ("content_scripts", "web_accessible_resources", "externally_connectable"):
            if extra in manifest:
                yield f"扩展清单里出现 {extra} —— 扩大了扩展的可见面，需要重新评估权限"

        if "activeTab" not in permissions:
            yield f"permissions 里没有 activeTab：{permissions!r}（读取当前标签页 URL 的前提）"

        for entry in hosts:
            host = entry
            if not URL_SCHEME.match(host):
                yield f"host_permissions 条目 {entry!r} 不是完整 URL，无法判断放行范围"
                continue
            parts = urlsplit(host)
            hostname = (parts.hostname or "").lower()
            if hostname == "*" or "*" in hostname:
                yield f"host_permissions 出现通配主机 {entry!r} —— 扩展可以请求任意站点"
            if hostname and hostname not in LOCAL_HOSTS:
                connection = f"{hostname}:{parts.port}" if parts.port else hostname
                yield (
                    f"host_permissions 放行了非本机地址 {entry!r}（解析为 {connection!r}）——"
                    "扩展只该访问 127.0.0.1/localhost"
                )

        # --- 反向：假装清单里加了 <all_urls>，解析器必须能认出来 ---
        injected = {"permissions": permissions, "host_permissions": hosts + ["<all_urls>"]}
        if not any("*" in str(item) for item in injected["host_permissions"]):
            yield "权限解析器对 <all_urls> 不敏感 —— 这条检查是空断言"

    guard.check("扩展权限最小化（host_permissions 只含本机回环地址）", items())


# --------------------------------------------------------------------------- #
# 5. 无验证码绕过
# --------------------------------------------------------------------------- #
def check_no_captcha_bypass(guard: Guard) -> None:
    def items():
        from app.errors import ErrorType
        from app.ingestion import douyin
        from app.ingestion.redirects import RequestBlockedError

        # --- 正向：撞上验证页时必须如实报错，而不是继续 -- 更不是想办法过掉 ---
        classify = getattr(douyin, "classify_page", None)
        if classify is None:
            yield "app/ingestion/douyin.py 里没有 classify_page —— 验证页不再被识别"
        else:
            for marker in ("验证后继续", "captcha", "slider-verify"):
                error = classify(f"<html><body>{marker}</body></html>")
                if error is None:
                    yield f"页面含验证标记 {marker!r} 时 classify_page 返回 None —— 会继续采集"
                elif not isinstance(error, RequestBlockedError):
                    yield f"页面含验证标记 {marker!r} 时返回 {type(error).__name__}，不是 REQUEST_BLOCKED"
                elif error.error_type is not ErrorType.REQUEST_BLOCKED:
                    yield f"验证页的 error_type={error.error_type}，不是 REQUEST_BLOCKED"
            if classify("<html><body>正常页面</body></html>") is not None:
                yield "正常页面被误判成验证页（误报会让这条守卫失去意义）"

        markers = [str(m) for m in getattr(douyin, "BLOCK_MARKERS", ())]
        for required in ("captcha", "验证后继续"):
            if not any(required in marker.lower() for marker in markers):
                yield f"BLOCK_MARKERS 漏了 {required!r}：{markers!r}"

        # --- 反向：不许出现打码平台 / 解算依赖 ---
        for path in sorted((BACKEND_ROOT / "app").rglob("*.py")):
            source = path.read_text(encoding="utf-8").lower()
            for marker in BYPASS_MARKERS:
                if marker in source:
                    yield f"{_display_path(path)} 出现绕过信号 {marker!r}"

        for requirements in ("requirements.txt", "requirements.lock", "pyproject.toml"):
            path = BACKEND_ROOT / requirements
            if not path.is_file():
                continue
            source = path.read_text(encoding="utf-8").lower()
            for marker in BYPASS_MARKERS + ("captcha", "ocr-captcha"):
                if marker in source:
                    yield f"{requirements} 里引入了 {marker!r} —— 打码依赖就是绕过能力"

        # --- 抖音详情接口不得伪造签名参数（README 的「不伪造签名」）---
        params = getattr(douyin, "DOUYIN_DETAIL_API_PARAMS", None)
        if isinstance(params, dict):
            for name in params:
                if re.fullmatch(r"(?i)(a_?bogus|_signature|x-?bogus|ms_?token|verify_?f?p|signature|sign)", str(name)):
                    yield f"DOUYIN_DETAIL_API_PARAMS 里有签名参数 {name!r} —— README 承诺「不伪造签名」"

    guard.check("无验证码绕过 / 不伪造签名（撞验证页就如实报错）", items())


# --------------------------------------------------------------------------- #
# 6. 免责声明仍在
# --------------------------------------------------------------------------- #
def check_disclaimer(guard: Guard) -> None:
    def items():
        if not COMPLIANCE_DOC.is_file():
            yield f"缺少 {COMPLIANCE_DOC.name}（README 指向它的链接会 404）"
        for name, marker, description in DISCLAIMER_MARKERS:
            path = REPO_ROOT / name
            if not path.is_file():
                yield f"找不到 {path.name}"
                continue
            if marker not in path.read_text(encoding="utf-8"):
                yield f"{description}丢失：{path.name} 里找不到 {marker!r}"

        # 反向：不许把承诺改成「视情况而定」这类软化措辞。
        if README_ZH.is_file():
            text = README_ZH.read_text(encoding="utf-8")
            for softened in ("在可能的情况下不绕过验证码", "尽量不绕过验证码"):
                if softened in text:
                    yield f"README.md 的承诺被软化：{softened!r}"

    guard.check("免责声明仍在（中英 README 的稳定措辞 + COMPLIANCE.md）", items())


# --------------------------------------------------------------------------- #
# 7. 无内部文档外泄
# --------------------------------------------------------------------------- #
def tracked_files() -> list[str] | None:
    """``git ls-files -z``；不在 git 仓库里（例如解压后的发布包）返回 ``None``。

    **必须用 ``-z``**。默认的 ``git ls-files`` 会对非 ASCII 路径做 C 风格引号转义
    （``"KnowledgeFlow-\\350\\277\\233..."``），而本检查要抓的内部文档名恰恰是中文的 ——
    按行解析就永远匹配不上，这条「防误提交」的断言会静默失效。
    实测：文件确实在索引里，``git ls-files | grep 进度`` 却什么都搜不到。
    """
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    # ``-z`` 用 NUL 分隔、且**不做**引号转义，二进制安全。
    raw = result.stdout.decode("utf-8", errors="surrogateescape")
    return [name for name in raw.split("\0") if name]


def is_noise(path: Path) -> bool:
    parts = set(path.parts)
    return bool(parts & {".git", "node_modules", "__pycache__", ".venv", "dist", "build"}) or path.suffix in {
        ".pyc",
        ".pyo",
    }


#: 会被扫描「有没有引用内部文档名」的源码后缀。
SCANNABLE_SUFFIXES = frozenset(
    {".py", ".md", ".yml", ".yaml", ".toml", ".cfg", ".sh", ".ps1", ".json", ".txt", ".js", ".html"}
)

#: 扫描「内部文档名作为引用出现」时**不要**进的目录。
SCAN_SKIP_DIRS = frozenset(
    {".git", "node_modules", "__pycache__", ".venv", "dist", "build", "data", ".pytest_cache"}
)


def scan_roots() -> tuple[Path, ...]:
    """要扫描「有没有引用内部文档名」的目录。

    仓库根一把扫完（``.git`` / ``__pycache__`` 等由 :func:`is_noise` 排除）：
    公开仓库里这个文件根本不存在，**任何**对它的引用都是坏引用 ——
    除了上面白名单里逐条注明了理由的那几处。
    """
    return (REPO_ROOT,)


def check_no_internal_docs(guard: Guard, tracked: list[str] | None) -> None:
    def items():
        # --- 工作区：内部文档本身不该存在 ---
        seen: set[Path] = set()
        for name in INTERNAL_DOC_NAMES:
            for path in REPO_ROOT.rglob(name):
                if is_noise(path) or path in seen:
                    continue
                seen.add(path)
                yield (
                    f"工作区里存在内部文档 {path.relative_to(REPO_ROOT)} ——"
                    "不随公开仓库发布，留在仓库根就有误提交风险"
                )

        # --- git 索引：绝不能已经提交进去 ---
        # 不在 git 工作区（发布包解压 / tarball）时无法判断，**优雅跳过**而不是报红：
        # 那种场景下上面对工作区的扫描已经把「文件散落在仓库里」这件事查掉了，
        # 剩下的「已提交」风险此时不存在（没有索引可提交）。报 FAIL 是假红，
        # 假红会训练人忽略这道守卫 —— 参考 check_coverage.py 的沙箱降级。
        if tracked is None:
            guard.notes.append(
                "跳过 git 索引检查：当前目录不是 git 工作区（发布包 / tarball 场景），"
                "已改用工作区扫描覆盖内部文档"
            )
        else:
            for name in INTERNAL_DOC_NAMES:
                for path in tracked:
                    if Path(path).name == name:
                        yield f"内部文档 {path} 已被 git 跟踪 —— 一旦推上去就是泄密"

        # --- 引用面：公开仓库里没人能引用一个不存在的文件 ---
        for root in scan_roots():
            if not root.is_dir():
                continue
            for path in root.rglob("*"):
                if not path.is_file() or is_noise(path):
                    continue
                relative = path.relative_to(REPO_ROOT).as_posix()
                if relative in INTERNAL_DOC_MENTION_ALLOWED:
                    continue
                if path.suffix not in SCANNABLE_SUFFIXES:
                    continue
                try:
                    source = path.read_text(encoding="utf-8")
                except (UnicodeDecodeError, OSError):
                    continue
                for name in INTERNAL_DOC_NAMES:
                    if f'"{name}"' in source or f"'{name}'" in source:
                        yield (
                            f"{relative} 把内部文档名当路径字面量引用 ——"
                            f"公开仓库里 {name} 不存在，这行代码必然失效"
                        )

    guard.check("无内部文档外泄（不在工作区 / 不在 git 索引 / 无引用）", items())


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    configure_repo_root(args.repo_root)

    guard = Guard(verbose=args.verbose)
    check_cookie_never_leaks(guard)
    check_domain_allowlist(guard)
    check_cors(guard)
    check_extension_permissions(guard)
    check_no_captcha_bypass(guard)
    check_disclaimer(guard)
    check_no_internal_docs(guard, tracked_files())

    for line in guard.sections:
        print(line)

    for note in guard.notes:
        print(f"[NOTE] {note}")

    if guard.problems:
        print(f"\n[FAIL] 合规守卫发现 {len(guard.problems)} 处违规：")
        for item in guard.problems:
            print(f"  - {item}")
        print(
            "\n说明：这些边界写在 README.md / COMPLIANCE.md 里，破坏它们不会让功能测试变红，"
            "所以必须由这道守卫拦住。"
        )
        return 1

    print(f"\n[OK] 合规守卫全部通过（工作区 {REPO_ROOT}）")
    return 0


if __name__ == "__main__":  # pragma: no cover - 脚本入口
    raise SystemExit(main())
