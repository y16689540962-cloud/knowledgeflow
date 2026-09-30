"""匹配用文本折叠。

用于实体 / 数字的地面化比对（**不是**文件名清洗，也**不是**哈希归一化）：

* NFKC —— 让全角 ``ＡＩ`` 与 ``AI``、``１６００`` 与 ``1600``、``％`` 与 ``%`` 等价
* casefold —— 让 ``AI`` 与 ``ai`` 等价

刻意**不压缩空白**：压缩会把 ``中国\\n人口`` 变成 ``中国 人口``，反而切断匹配。
"""

from __future__ import annotations

import unicodedata


def fold_for_matching(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "").casefold()


__all__ = ["fold_for_matching"]
