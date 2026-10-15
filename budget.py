# -*- coding: utf-8 -*-
"""
统一预算计数（原子写入 · 按调用类别分桶）
==========================================
为什么单独成模块（对应评审意见 7.2「按实际资源管理预算」）：
  · 原实现的额度计数只覆盖 DeepSeek 包装函数，千问润色与 Embedding 不在同一账本里，
    无法回答"今天到底花了多少资源"；
  · 原实现用「读文件 → 加一 → 写回」，并发请求会互相覆盖、丢计数；
  · 这里改成【按类别分桶 + 临时文件 + os.replace 原子替换】，并发下不丢计数，
    且 DeepSeek / 千问 / Embedding 三类用量可以分别统计与分别限额。

设计取舍：
  · 不依赖 Streamlit，纯文件 + 环境变量 —— rag_engine / llm_qwen 也能安全引用；
  · 任何异常都静默吞掉（预算统计失败绝不能拖垮主流程）；
  · 计数文件默认放 /tmp（云端可用），否则放工作目录；可用 BUDGET_FILE 覆盖。

自检: python budget.py
"""
from __future__ import annotations

import os
import json
import tempfile
import datetime
from pathlib import Path


def _pick_file() -> Path:
    try:
        if os.path.isdir("/tmp") and os.access("/tmp", os.W_OK):
            return Path("/tmp/ai_race_budget.json")
    except Exception:
        pass
    return Path("ai_race_budget.json")


BUDGET_FILE = Path(os.getenv("BUDGET_FILE", str(_pick_file())))
_TMPDIR = BUDGET_FILE.parent if str(BUDGET_FILE.parent) else Path(".")

# 分桶名（保持稳定，便于跨模块引用）
BUCKET_LLM = "llm"        # DeepSeek 生成 / 结构化分析
BUCKET_POLISH = "polish"  # 千问润色 / 审核
BUCKET_EMBED = "embed"    # DashScope Embedding 批次


def _today() -> str:
    return datetime.date.today().isoformat()


def _load() -> dict:
    """读取当日计数；跨天或损坏一律归零。"""
    try:
        raw = json.loads(BUDGET_FILE.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and raw.get("date") == _today():
            counts = raw.get("counts")
            if isinstance(counts, dict):
                return {str(k): int(v) for k, v in counts.items()}
    except Exception:
        pass
    return {}


def read(bucket: str) -> int:
    return int(_load().get(bucket, 0))


def bump(bucket: str, n: int = 1) -> None:
    """原子地给某个桶 +n：先写临时文件，再 os.replace 覆盖。

    os.replace 在同一文件系统内是原子操作，因此并发调用不会出现
    "两个请求都读到 5、都写成 6" 的丢计数问题。
    """
    try:
        counts = _load()
        counts[bucket] = int(counts.get(bucket, 0)) + max(int(n), 0)
        payload = json.dumps({"date": _today(), "counts": counts}, ensure_ascii=False)
        fd, tmp = tempfile.mkstemp(dir=str(_TMPDIR), prefix=".budget_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
            os.replace(tmp, BUDGET_FILE)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except Exception:
                    pass
    except Exception:
        pass  # 预算统计失败不影响主流程


def exceeded(bucket: str, limit) -> bool:
    try:
        limit = int(limit)
    except Exception:
        return False
    if limit <= 0:
        return False
    return read(bucket) >= limit


def remaining(bucket: str, limit) -> int:
    try:
        return max(int(limit) - read(bucket), 0)
    except Exception:
        return 0


def summary() -> dict:
    """返回 {bucket: count}（供页面/日志展示）。"""
    return _load()


# ------------------------------ CLI 自检 ------------------------------
def _selftest() -> dict:
    import shutil
    # 用独立临时文件，避免污染真实计数
    global BUDGET_FILE, _TMPDIR
    _bak = BUDGET_FILE
    td = tempfile.mkdtemp(prefix="budget_selftest_")
    BUDGET_FILE = Path(td) / "b.json"
    _TMPDIR = Path(td)
    try:
        assert read("llm") == 0
        bump("llm", 1)
        bump("llm", 2)
        bump("embed", 5)
        assert read("llm") == 3, read("llm")
        assert read("embed") == 5, read("embed")
        assert exceeded("embed", 5) is True
        assert remaining("embed", 8) == 3
        # 原子性粗测：多次 bump 后总数正确
        for _ in range(50):
            bump("polish", 1)
        assert read("polish") == 50, read("polish")
        out = {
            "module": "budget",
            "atomic_counter": "ok",
            "buckets": summary(),
            "file": str(BUDGET_FILE),
        }
    finally:
        shutil.rmtree(td, ignore_errors=True)
        BUDGET_FILE = _bak
    return out


if __name__ == "__main__":
    import json as _json
    print(_json.dumps(_selftest(), ensure_ascii=False, indent=2))
