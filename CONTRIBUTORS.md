# 贡献者与致谢 · Contributors & Acknowledgements

## 代码、测试与文档

| 贡献者 | 范围 |
|---|---|
| [羊宗胜](https://github.com/y16689540962-cloud) | 全部代码、测试、文档与打包（v0.1 → v0.4） |

## 依赖与服务

下面这些**不是代码贡献者**（它们没有为本项目写过一行代码），但这个项目离不开它们。
单独列出来，是为了把「谁写的」和「靠什么跑的」分开说清楚：

| 名称 | 在本项目里的角色 |
|---|---|
| **DeepSeek** | 提供本项目使用的 **LLM 推理服务**：结构化分析、claim 分类、摘要生成。开发与演示全部用其模型（`deepseek-flash`） |
| [FastAPI](https://fastapi.tiangolo.com/) · [Starlette](https://www.starlette.io/) | HTTP 接口层 |
| [Pydantic](https://docs.pydantic.dev/) | Schema 校验与配置加载 |
| [SQLAlchemy](https://www.sqlalchemy.org/) + [aiosqlite](https://github.com/omnilib/aiosqlite) | 异步数据访问（SQLite） |
| [httpx](https://www.python-httpx.org/) | HTTP 客户端（含全部离线测试用的 MockTransport） |
| [faster-whisper](https://github.com/SYSTRAN/faster-whisper) · [CTranslate2](https://github.com/OpenNMT/CTranslate2) | 本地语音转写 |
| [PyAV](https://pyav.org/) | 音视频解码（**所以本项目不需要系统装 ffmpeg**） |
| [Tesseract](https://github.com/tesseract-ocr/tesseract)（经 `pytesseract`） | 图片文字识别 |
| [python-build-standalone](https://github.com/astral-sh/python-build-standalone) | macOS 安装包里内嵌的 Python 运行时 |
| [Playwright](https://playwright.dev/) + Chromium | 界面截图、端到端测试与演示录制 |
| [Obsidian](https://obsidian.md/) | 笔记最终落地的地方（本项目与官方无关联） |

## 关于「贡献」的口径

**刻意把「写代码的人」和「用到的服务/库」分开列。**

把 DeepSeek 这类**服务提供方**写进「代码贡献者」名单会是不实陈述，
也容易被读成「存在合作或背书关系」—— 本项目与任何平台都没有这种关系，
也没有得到任何一方的赞助或背书（见 README 的免责声明）。

如果你要感谢某个 LLM 服务，放在这一节里是准确的；写成 `Co-authored-by` 就不是了。
