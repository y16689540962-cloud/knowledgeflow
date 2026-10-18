# 参与开发 · Contributing

> **先说清楚这是什么。** KnowledgeFlow 目前是**单人项目** —— 全部代码、测试与文档
> 由 [@y16689540962-cloud](https://github.com/y16689540962-cloud) 一人完成（见 [CONTRIBUTORS.md](CONTRIBUTORS.md)）。
> 所以这份文档**不是在征集人手**，而是把你真要在自己 clone 上动这个项目时需要知道的东西
> 一次讲完：怎么跑起来、怎么确认没改坏、什么样的改动会被接受。
>
> 没有 CLA、没有 code owner 流程、没有行为准则 —— 一个人维护的项目硬加这些是装样子。
> 下面每一条都对应仓库里真实存在的东西。

**English summary** is at the [bottom of this file](#english-summary).

---

## 一、把环境跑起来

前置：**Python 3.11+**（开发环境是 3.13，CI 同时测 3.11 与 3.13）。

```bash
git clone https://github.com/y16689540962-cloud/knowledgeflow.git
cd knowledgeflow
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements.txt
```

Windows 上把 `.venv/bin/` 换成 `.venv\Scripts\`。

**关于 `requirements.txt` 的最后几行**：`faster-whisper` / `av` / `pytesseract` / `pillow`
是**可选能力**（ASR / OCR），不是核心链路的前置依赖。代码里全是懒加载 + 明确降级，
装不上只影响对应能力，`python -m pytest` 照样全绿。**只想跑核心链路的话，把那几行注释掉即可。**

（`av` 有个上界 `<19`，不是随手写的：faster-whisper 调用 `av.open(..., metadata_errors=...)`，
而 PyAV 19 删掉了这个关键字参数 —— faster-whisper 只声明 `av>=11`，没有上限。
测到 av 19.0.0 直接 `TypeError`。原因写在 `requirements.txt` 的注释里，别顺手放宽。）

配一份本地配置（`.env` 已在 `.gitignore` 里，**不要提交**）：

```bash
cp backend/.env.example backend/.env
$EDITOR backend/.env
```

至少填 `LLM_PROVIDER` 与对应的 API Key、`LLM_MODEL`、`OBSIDIAN_VAULT_PATH`。

---

## 二、怎么确认你没改坏

```bash
cd backend && ../.venv/bin/python -m pytest
```

**期望：全绿。** 本机（macOS Apple Silicon）实测是 **1579 passed, 20 skipped**。

> 跳过的条数**随平台与已装的可选依赖而变**，别拿一个固定数字去对。默认跳过的包括：
> 真实 ASR / OCR（需要装引擎，首次还要联网下模型）、真实浏览器端到端（需要 Playwright + Chrome）、
> 平台专属的用例（Windows 的进程/端口管理、macOS 的 `os.killpg` / `lsof`、
> 符号链接越界、要 `node` 才能跑的 JS 语法校验等）。
> 这些**都是设计如此**，不是环境坏了 —— 需要真实引擎时用上面对应的环境变量显式打开，
> 具体见 README 的「跑测试」一节。

### 三道守卫（改动碰到对应面时必须跑）

测试全绿不等于没改坏。仓库里有三个脚本，各自盯一类**不会让功能测试变红**的问题：

```bash
cd backend
../.venv/bin/python scripts/check_coverage.py          # 行覆盖率下限 90%（当前 97.0%）
../.venv/bin/python scripts/check_doc_consistency.py   # 文档里的数字口径
../.venv/bin/python scripts/check_compliance_guard.py  # 合规边界（Cookie / CORS / 扩展权限…）
```

- **`check_coverage.py`** —— 没装 `coverage` 时它会自己跳过，不会误报失败。
- **`check_doc_consistency.py`** —— 加了用例、改了 CLI 选项、动了某个「待做」的状态，
  文档里的数字就该跟着变；这个脚本把「同一事实散落在多处、只能靠人发现」变成一条可复跑的命令。
- **`check_compliance_guard.py`** —— 专拦一类**功能测试永远抓不到**的回归：
  日志脱敏清单被删、CORS 被改成放行 `*`、扩展权限被放大、README 的免责声明被删掉。
  这些改动不会让任何一条功能测试失败，但会破坏项目对外声明过的边界。

### 想跑真实引擎 / 真实浏览器

默认全是离线的（`httpx.MockTransport` + Mock LLM），需要真实链路时显式打开：

```bash
KNOWLEDGEFLOW_REAL_OCR=1 ../.venv/bin/python -m pytest tests/test_ocr_real.py -v
KNOWLEDGEFLOW_REAL_ASR=1 WHISPER_MODEL_SIZE=small ../.venv/bin/python -m pytest tests/test_asr_real.py -v
KNOWLEDGEFLOW_BROWSER_TEST=1 ../.venv/bin/python -m pytest tests/test_web_ui_browser.py -v
```

### 起服务看一眼

```bash
cd backend && ../.venv/bin/python scripts/serve.py --port 8000   # 加 --reload 可自动重载
```

`http://127.0.0.1:8000` 是界面，`/docs` 是自动生成的 API 文档。

> **不要加 `--expose`。** 服务没有鉴权，绑到 `0.0.0.0` 等于把「写你 Obsidian 库、
> 用你 LLM 额度」的接口开放给局域网。这条不是风格建议，`scripts/serve.py` 里有守卫。

---

## 三、提交纪律

### 提交信息

仓库的既有风格是 **conventional commits 前缀 + 中文正文**，正文写「为什么」而不是「改了什么」：

```
fix(packaging): 生成的 KnowledgeFlow.cmd 是 LF 行尾，cmd.exe 把注释当命令执行
feat(packaging): Windows 10+ x64 便携版，并修掉 GBK 控制台把 --check 打成 traceback
docs: 测试数 1490 → 1491 同步到全部交付物（含图片重渲染）
ci: 升级 action 到 v7、测 3.11+3.13、声明最小权限
```

常用前缀：`feat` / `fix` / `docs` / `test` / `ci`（打包相关改动用 `feat(packaging)`、
`fix(launcher)` 这类 scope）。早期提交没有前缀，是纯中文短句 —— 保持一致的读感即可，
不必为此重写历史。

### 一条硬纪律：改了实现，就同步**全部**交付物

这个项目栽过好几次：同一个数字（用例数、覆盖率、CLI 选项）散落在 README、README.en.md、
进度文档、演示图里，改了一处忘了另一处 —— **代码是对的，文档之间互相矛盾**，
而且不会报错，只有人会发现。所以：

- 改了用例数 / 覆盖率 → 跑 `check_doc_consistency.py`，按它的输出把所有出现处改齐
- 改了 CLI 选项、脚本行为 → README 里印的命令行必须跟着改
- **README.md 与 README.en.md 必须同口径**：中英两版是同一份承诺的两种语言，
  英文版此前就漏过一条（「不破解加密」），属于真出过的缺陷

### 别改这份清单里的东西

- **`.env` 与任何凭据**：`DOUYIN_COOKIE`、API Key 一律不进仓库，也不进测试夹具（用 `httpx.MockTransport`）
- **`backend/tests/` 里的断言不要为了变绿而放宽**：这个仓库的测试是刻意写「有牙」的 ——
  负向变异、枚举全覆盖、检查器不许把自己的注释当证据。测试红了先怀疑实现，再怀疑测试
- **合规边界**：不伪造签名、不破解加密、不绕过验证码、Cookie 不回显不进日志 ——
  这些在 `check_compliance_guard.py` 里有对应断言，不要为了让改动通过而删守卫

---

## 四、几处「看着奇怪但其实有原因」的地方

改这些地方之前先读注释，注释里写了为什么：

- **打包层只做「内嵌解释器 + 环境变量映射 + 启动」，业务代码一行都不动。**
  要加一个平台（Intel Mac / Linux），照 `packaging/macos/` 或 `packaging/windows/` 补一层即可，
  不需要碰 `backend/app/`。
- **`packaging/macos` 与 `packaging/windows` 各自独立、互不 import。**
  代价是 `probe_llm` / `probe_vault` 这两段纯函数要同步维护，换来的是两边都能独立发布。
- **`requirements.txt` 里的可选依赖不走「CI 专用依赖」那套**：CI 装的就是 README 里那一条命令，
  否则 CI 绿不代表用户照文档做能得到同样的结果。
- **前端纪律**（`app/web/` 与 `extension/`）：不用 `innerHTML`、不用内联事件、不引框架、
  不加载远程代码。有测试钉着。

---

## English summary

KnowledgeFlow is currently a **single-author project** — all code, tests and docs are written
by one person ([CONTRIBUTORS.md](CONTRIBUTORS.md)). This file is not a call for volunteers;
it documents what you need to know to work on your own clone.

**Getting started.** Python 3.11+ (dev env is 3.13; CI tests 3.11 and 3.13).
`python3 -m venv .venv && .venv/bin/pip install -r backend/requirements.txt`, then
`cp backend/.env.example backend/.env` and fill in your LLM provider, API key, model and
Obsidian vault path. The last lines of `requirements.txt` (`faster-whisper`, `av`,
`pytesseract`, `pillow`) are **optional** ASR/OCR capabilities — everything is lazily
loaded with explicit degradation, so a normal `pytest` run stays green without them.

**Verifying your change.** `cd backend && ../.venv/bin/python -m pytest` — 1579 passed,
20 skipped on this machine. The number of skips **varies by platform and installed
optional dependencies**; those tests are designed to skip, not broken. Tests are fully
offline (`httpx.MockTransport` + a mock LLM); real engines and the browser E2E require
explicit opt-in environment variables.

Three guards cover what feature tests cannot catch:
`scripts/check_coverage.py` (90% line-coverage floor), `scripts/check_doc_consistency.py`
(numbers printed in docs match reality), and `scripts/check_compliance_guard.py`
(the declared boundaries — log redaction, CORS not wildcarded, minimal extension
permissions, disclaimer present).

**Commit discipline.** Conventional-commit prefixes (`feat` / `fix` / `docs` / `test` / `ci`)
with Chinese bodies explaining *why*. **If you change an implementation fact that is printed
in the docs, update every place it appears** — README.md and README.en.md must state the
same thing, and the doc-consistency guard exists precisely because that drifted before.
Never commit credentials (`.env` is gitignored), never weaken the compliance guards, and
never relax a test assertion just to make it pass.
