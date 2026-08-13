"""无依赖的保守 token 估算。

英文/代码按约 4 字符/token；CJK 字符按 1 字符/token。它不是 tokenizer，
但比统一 chars/4 更适合中英混合的 MCP 输出，也不会把中文成本低估约四倍。
"""
from __future__ import annotations


def _is_cjk(ch: str) -> bool:
    n = ord(ch)
    return (
        0x3400 <= n <= 0x4DBF
        or 0x4E00 <= n <= 0x9FFF
        or 0xF900 <= n <= 0xFAFF
        or 0x3040 <= n <= 0x30FF
        or 0xAC00 <= n <= 0xD7AF
    )


def estimate_tokens(text: str) -> int:
    cjk = sum(1 for ch in text if _is_cjk(ch))
    return cjk + (len(text) - cjk + 3) // 4


def truncate_tokens(text: str, budget: int,
                    marker: str = "\n...(budget truncated)") -> str:
    """截到估算 token 硬预算内；marker 也计入预算。"""
    budget = max(0, int(budget))
    if estimate_tokens(text) <= budget:
        return text
    marker_cost = estimate_tokens(marker)
    if marker_cost >= budget:
        marker = ""
        marker_cost = 0
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if estimate_tokens(text[:mid]) + marker_cost <= budget:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo].rstrip() + marker
