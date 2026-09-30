#!/usr/bin/env python
"""导出 OpenAPI schema：`python scripts/export_openapi.py --out ../frontend/openapi.json`

定稿第二十八节：前端**不手写** API 类型，用

```bash
npx openapi-typescript openapi.json -o src/api/types.ts
```

从这份文件生成。所以这个脚本的意义是：让「契约」成为一个**可交付的文件**，
而不是「跑起服务再去 /openapi.json 抓」。

用 MockProvider 只为构造应用（**不发任何请求**）—— 契约与用哪个 LLM 无关。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:  # pragma: no cover - 直接执行脚本时用
    sys.path.insert(0, str(BACKEND_ROOT))

from app.api import create_app  # noqa: E402
from app.config import Settings  # noqa: E402
from app.providers import MockProvider  # noqa: E402
from app.testing import load_fixture_json  # noqa: E402

EXIT_OK = 0
EXIT_FAIL = 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="导出 KnowledgeFlow OpenAPI schema")
    parser.add_argument(
        "--out",
        type=Path,
        default=BACKEND_ROOT.parent / "openapi.json",
        help="输出路径（默认写到仓库根 openapi.json）",
    )
    parser.add_argument("--indent", type=int, default=2, help="JSON 缩进")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    app = create_app(
        settings=Settings(_env_file=None),  # type: ignore[call-arg]
        provider=MockProvider(payload=load_fixture_json("llm_analysis_valid.json")),
        configure_logging=False,
        run_startup_recovery=False,
    )
    schema = app.openapi()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(schema, ensure_ascii=False, indent=args.indent) + "\n", encoding="utf-8"
    )

    print(f"已导出 {args.out}")
    print(f"  {len(schema['paths'])} 个路径，{len(schema['components']['schemas'])} 个 schema")
    print("\n前端生成类型：")
    print(f"  npx openapi-typescript {args.out.name} -o src/api/types.ts")
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover - 脚本入口
    raise SystemExit(main())
