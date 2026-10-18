# 安全政策 · Security Policy

> **这个项目的安全面比较特殊，先说清楚它是什么。**
>
> KnowledgeFlow 是一个**本机运行**的工具，没有服务端、没有云端账户、没有遥测。
> 但它在磁盘上保存两类**高价值凭据**：你自己的 **LLM API Key** 和（可选的）
> **抖音登录态 Cookie**。所以它最现实的风险不是「被远程攻破」，而是
> **凭据被本机的其他进程、其他用户账号、或日志文件泄露出去**。
> 下面这份文档按这个威胁模型写。

**English summary** is at the [bottom of this file](#english-summary).

---

## 一、报告漏洞

**请用 GitHub 的私密报告渠道，不要开公开 issue：**

→ **Security → Advisories → [Report a vulnerability](https://github.com/y16689540962-cloud/knowledgeflow/security/advisories/new)**

（仓库地址：<https://github.com/y16689540962-cloud/knowledgeflow>）

公开 issue 里贴复现步骤，等于在修补方案出来之前先把利用方式公开了。
涉及**凭据泄露**的尤其如此 —— 报告正文里**不要粘贴真实的 API Key 或 Cookie**，
用占位符说明它的形态即可。

请一并说明：受影响的版本、是源码运行还是桌面安装包、以及复现步骤。
这是一个单人维护的项目，**没有 SLA**，我会尽力尽快回，但不要假定 48 小时内有答复。

### 哪些问题属于安全范围

| 属于 | 说明 |
|---|---|
| 凭据泄露 | API Key / 抖音 Cookie 出现在日志、`/api/health` 响应、错误消息、异常堆栈里 |
| 权限过宽 | 配置文件/数据目录对同机其他用户可读；扩展申请了超出必要范围的权限 |
| 本地接口被越权调用 | CORS 放行了非扩展来源；服务被绑到 `0.0.0.0` 而没有防线 |
| 路径越界 | 采集到的标题被用来拼路径时逃出 vault / 覆盖别人的文件 |
| 供应链 | 依赖或构建脚本里的可疑代码 |

### 哪些不属于

- **「没配 Cookie 就采不到抖音」**、**抖音撞验证页** —— 这是平台行为，是设计如此，不是漏洞
- **服务无鉴权** —— 已知设计，服务默认只绑 `127.0.0.1`，见下文
- **桌面安装包未签名/未公证** —— 已知且已如实写明，见 [COMPLIANCE.md](COMPLIANCE.md)
- **扩展未上架 Chrome 应用商店** —— 已知，需手动加载已解压的扩展

---

## 二、支持范围

| 版本 | 状态 |
|---|---|
| **0.4.x**（当前 `0.4.0`） | 支持 —— 安全修复会进这个分支 |
| < 0.4 | 不再支持。请先升到最新版再报告；旧版本的采集链路已随平台改版失效 |

版本号在 `backend/app/__init__.py`、`backend/pyproject.toml` 与 `extension/manifest.json`
三处声明；`/api/health` 也会返回当前版本。

---

## 三、本项目实际做了什么（都可自行验证）

下面每一条都不是声明，而是仓库里**有代码、而且有测试钉着**的实现。
验证方式都写出来了，欢迎自己复跑。

### 3.1 配置文件的权限：macOS `0600` / Windows 断继承 ACL

配置文件（`settings.json`）里装着 API Key 和抖音 Cookie，所以写入时强制收紧权限：

- **macOS**：`packaging/macos/launcher/kf_app/settings.py` 的 `save()` —— 先写临时文件、
  `fsync`、`chmod 0o600`，再 `os.replace` 原子替换。原子写是必须的：
  直接覆写的话，中途崩溃会留下一个截断的配置，向导只能重问一遍。
- **Windows**：`packaging/windows/launcher/kf_app/settings.py` 的 `restrict_to_current_user()`
  （由 `save()` 在写完临时文件后调用）—— 调
  `icacls <file> /inheritance:r /grant:r <当前用户>:(F)`。
  它**返回布尔值而不是抛异常**：收紧失败时不假装成功，由调用方决定怎么办。

> **为什么 Windows 上不能只用 `os.chmod(0o600)`**：Windows 上它只切「只读」位，
> **碰不到 DACL** —— 文件仍然是「本机所有用户可读」，而里面装着 API Key。
> 代码里有 `chmod` 就以为安全了，是这里最容易骗过自己的地方。
> `/inheritance:r` 同样关键：不摘掉从父目录继承来的 `Users` / `Everyone` 权限，
> 同一台机器上的**别的账号照样能读到你的 Key**。

### 3.2 日志脱敏：一个键名黑名单 + 一条正则兜底

`backend/app/logging_config.py` 是统一日志入口（`setup_logging` / `log_event`），
它强制三件事：

1. **键名命中即脱敏**。`SENSITIVE_KEY_PATTERNS` 是子串匹配（大小写不敏感），包含
   `api_key` / `apikey` / `api-key` / `authorization` / `token` / `cookie` /
   `secret` / `password` / `passwd` / `credential` —— 命中就替换成 `***REDACTED***`。
2. **正文默认完全抑制**。`raw_text` / `transcript` / `ocr_text` / `prompt` / `response` 等
   键名默认输出 `***REDACTED_CONTENT***`；只有显式设 `LOG_VERBOSE_CONTENT=1`
   **且** `LOG_LEVEL=DEBUG` 时才会输出，且截断 + 再过一遍脱敏。
3. **正则兜底**。即使凭据是**写在自由文本的 message 里**（而不是结构字段），
   也会被 `scrub_text()` 抓到：`sk-` 开头的串、`Bearer xxx`、
   以及 `api_key=...` / `cookie: ...` / `signature=...` 这类 `key=value` 形态。

另外 `setup_logging()` 会把 `httpx` / `httpcore` / `urllib3` 的日志级别压到 `WARNING`，
避免第三方库把完整请求体（含 `Authorization` 头）打进日志。

> 键名匹配用的是**子串**而不是精确相等 —— 所以 `content_hash` / `content_id`
> 这类正常字段不会被误伤（`CONTENT_KEYS` 用的是精确匹配或 `_<名字>` 结尾）。这个区分是有意的。

**你可以自己验证**：

```bash
cd backend && grep -n "SENSITIVE_KEY_PATTERNS" -A 12 app/logging_config.py
../.venv/bin/python -m pytest tests/test_logging.py -v
```

### 3.3 `/api/health` 不回显密钥与本机路径

健康检查只回答「配了没有」，不回答「配的是什么」。`backend/app/api/routes.py` 的
`health()` 只返回：`status` / `version` / `llm_provider` / `llm_model` /
`vault_configured`（布尔）/ 启动恢复报告。

**不回显** API Key、**不回显** vault 绝对路径、**不回显**数据库路径 ——
有人会拿 `/api/health` 当「本机信息泄露接口」用，这条边界就是防这个的。

两条测试直接钉死了它：

```
tests/test_api_app.py::test_health_never_leaks_secrets_or_paths
    —— 断言响应体里没有 "sk-"、没有 "api_key"、没有 vault 真实路径
tests/test_douyin_cookie_setting.py::test_health_never_leaks_the_cookie
    —— 断言响应体里没有 Cookie 值、没有 "sessionid"、连 "cookie" 字样都没有
```

### 3.4 抖音 Cookie 的处理方式

`DOUYIN_COOKIE` 是**账号级凭据，比 API Key 还敏感**（它能代表你登录）。所以：

- **只从 `.env` / 配置文件读**，刻意**不做**「贴到对话里」，也**不做**前端输入框
- 运行期**只判断有没有**，值不回显、不进日志
- 采集被挡时如实抛错（`COOKIE_REQUIRED` / `REQUEST_BLOCKED`），**不做绕过重试**

### 3.5 本地 HTTP 服务：只绑回环 + CORS 只认扩展

- `scripts/serve.py` 默认 `--host 127.0.0.1`；`--expose` 绑定非回环地址需要显式加，
  且有守卫（README 明确写了「不要加」）。
- CORS 用 `allow_origin_regex=r"^chrome-extension://[a-p]{32}$"`
  （`backend/app/api/app.py` 的 `EXTENSION_ORIGIN_PATTERN`），**不是 `allow_origins=["*"]`**。
  理由很直接：这个接口能**写你的 Obsidian 库、花你的 LLM 额度**，
  放行任意网页等于把本机随便一个页面都变成调用方。`allow_credentials=False`。

> ⚠️ **这个防线的已知局限（如实说明）**：正则能匹配**任何**已安装的 Chrome 扩展
> —— 未打包扩展的 id 每次加载都变，所以不能写死，只能按前缀 + 字符集匹配。
> 也就是说，**它拦不住「你自己装的另一个扩展」**。
> 真正的收敛手段是服务只绑 `127.0.0.1`，把暴露面缩到「本机上能跑的东西」。
> 这是有意的取舍，不是没考虑到。

### 3.6 Chrome 扩展的权限最小化

`extension/manifest.json`：

| 项 | 值 |
|---|---|
| `permissions` | `activeTab`, `storage` —— 只在你点图标时访问当前标签页 |
| `host_permissions` | `http://127.0.0.1:8000/*` —— **只连本机**，无公网地址，无 `<all_urls>` |

扩展不抓页面内容、不注入脚本、不加载远程代码，只用 `activeTab` 读当前标签页的 URL
交给本地后端。详细说明见 [`extension/README.md`](extension/README.md) 的「边界（要清楚）」一节。

### 3.7 域名白名单

后端解析链接时**只认白名单内的域名**（`backend/app/normalization/url.py` 的
`DEFAULT_ALLOWED_HOSTS`，12 个），白名单外一律抛 `DOMAIN_NOT_ALLOWED`。
完整清单见 [COMPLIANCE.md](COMPLIANCE.md)。

### 3.8 出网面

**全项目的出网请求只有两处**：你配置的 LLM 服务商，以及目标平台。
**没有遥测、没有回传、作者无法访问你的任何数据。** 可以自己 grep 验证。

---

## 四、你需要自己承担的部分

说清楚边界，比假装没有边界更有用：

- **服务无鉴权，只应绑 `127.0.0.1`。** 要让局域网/公网访问，请自己加反向代理 + 鉴权。
  上游一旦暴露，任何能访问它的人都能驱动你的采集链路、并消耗你的 LLM 额度。
- **你的机器若已被入侵，本项目保护不了这些凭据。** `0600` / ACL 防的是
  「同机其他用户和其他进程」，防不了以你的身份运行的恶意软件。
- **不要提交 `.env`**（已 gitignore），**不要把 Cookie / Key 贴进 issue 或截图**。
- **抖音采集：使用你自己的登录态，不改变采集方式本身要遵守的平台规则。**
  「身份合法」与「方式合规」是两件事。见
  [抖音用户服务协议](https://www.douyin.com/agreements/?id=6773901168964798477)。
- **你的内容会离开本机的唯一时机，是调用你配置的 LLM 时。** 不希望内容出本机就别配 LLM。

---

## English summary

**Scope.** KnowledgeFlow runs entirely on your machine — no server, no cloud account,
no telemetry. But it stores two high-value credentials locally: **your LLM API key** and
(optionally) **your Douyin session cookie**. The realistic threat is therefore *credential
exposure* — via logs, other local processes, other user accounts on the same machine, or
the local HTTP surface — not remote compromise.

**Reporting.** Use [GitHub Security Advisories](https://github.com/y16689540962-cloud/knowledgeflow/security/advisories/new),
**not a public issue**. Do not paste real keys or cookies into the report. This is a
single-maintainer project with **no SLA**.

**Supported versions.** `0.4.x` only (current: `0.4.0`).

**What is actually implemented (all verifiable in-repo):**

- **Config file permissions** — macOS writes `settings.json` atomically with `chmod 0600`;
  Windows uses `icacls /inheritance:r /grant:r <current user>:(F)`. Plain `os.chmod(0o600)`
  is deliberately *not* used on Windows, where it only toggles the read-only bit and never
  touches the DACL.
- **Log redaction** — `backend/app/logging_config.py` redacts any field whose key contains
  `api_key`, `authorization`, `token`, `cookie`, `secret`, `password`, `credential`;
  suppresses content fields by default; and runs a regex over free-form messages to catch
  `sk-…`, `Bearer …`, and `key=value` credential shapes. `httpx`/`httpcore`/`urllib3`
  loggers are pushed to `WARNING` so request bodies with `Authorization` headers never land in logs.
- **`/api/health` leaks nothing** — it returns provider name, model name, a boolean
  "vault configured", and the recovery report. No keys, no vault path, no DB path.
  Pinned by `test_health_never_leaks_secrets_or_paths` and `test_health_never_leaks_the_cookie`.
- **Local HTTP surface** — binds `127.0.0.1` by default; CORS uses
  `allow_origin_regex=^chrome-extension://[a-p]{32}$` with `allow_credentials=False`,
  never `*`. **Known limitation, stated plainly:** any installed Chrome extension matches
  that pattern, so this does not stop "another extension you installed" — the real
  containment is the loopback binding.
- **Extension permissions** — `activeTab` + `storage`, and `host_permissions` limited to
  `http://127.0.0.1:8000/*`. No `<all_urls>`, no public hosts.
- **Host allowlist** — URL resolution is restricted to 12 hard-coded domains.

**Your responsibility.** The service has no authentication — keep it on loopback or put a
reverse proxy with auth in front of it. Backups and compromise of your own machine are
outside what this project can protect. Never commit `.env` or paste credentials into
issues. Using your own session does not change the platform rules that apply to automated
collection.
