"""红线守卫测试：不访问 X / 不建定时任务 / 密钥不入库 / 无发布路径 / 无真实数据入库。"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

ALLOWED_HOSTS = {"aihot.news", "api.notion.com"}  # src 中唯一允许的硬编码 API 主机


def test_no_hardcoded_hosts_beyond_allowlist():
    """src/ 中出现的 https:// 字面量只允许 aihot.news 与 api.notion.com。"""
    bad = []
    for py in (ROOT / "src").rglob("*.py"):
        for m in re.finditer(r'https://([a-zA-Z0-9.\-]+)', py.read_text(encoding="utf-8")):
            host = m.group(1)
            if host in ALLOWED_HOSTS or host.startswith("www.notion.so"):
                continue
            if host.endswith((".example.com", ".example.org")):  # 保留测试假域名
                continue
            bad.append(f"{py.name}: {host}")
    assert not bad, f"发现越权主机: {bad}"


def test_no_x_access_in_source():
    """代码中不得出现访问 X/Twitter API 的调用。"""
    src = "\n".join(p.read_text(encoding="utf-8")
                    for p in (ROOT / "src").rglob("*.py"))
    assert "api.x.com" not in src and "api.twitter.com" not in src
    assert "twitter.com/i/api" not in src


def test_no_publish_path():
    """系统不得存在发布到社交媒体的代码路径。"""
    src = "\n".join(p.read_text(encoding="utf-8")
                    for p in (ROOT / "src").rglob("*.py"))
    for kw in ("def publish", "def post_to", "upload_video", "send_tweet"):
        assert kw not in src, f"发现疑似发布能力：{kw}"


def test_no_schedule_in_ci():
    """GitHub Actions 不得有 schedule/cron 触发（禁止定时任务）。"""
    for yml in (ROOT / ".github" / "workflows").glob("*.yml"):
        text = yml.read_text(encoding="utf-8")
        assert "schedule:" not in text, f"{yml.name} 含 schedule 触发（禁止定时任务）"
        assert "cron:" not in text, f"{yml.name} 含 cron 表达式（禁止定时任务）"


def test_gitignore_covers_secrets_and_local_data():
    gi = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for pattern in (".env", ".secrets/", "*token*", "*.cookie", "data/raw/",
                    "data/state/", "data/topics/", "inputs/"):
        assert pattern in gi, f".gitignore 缺少 {pattern}"


def test_examples_are_fake():
    """入库的样例必须是假数据：examples 下不得出现与真实抓取相同的文件。"""
    ex = ROOT / "data" / "examples"
    for f in ex.glob("*.json"):
        text = f.read_text(encoding="utf-8")
        assert "example.com" in text or "example.org" in text or "脱敏示例" in text or "假数据" in text, \
            f"{f.name} 未标注假数据"
        # 真实抓取文件名模式不得出现在 examples
        assert "aihot_selected" not in f.name, f"{f.name} 疑似真实抓取数据"


def test_no_secret_in_repo_files():
    """已跟踪/将提交文件中不得出现密钥样式字符串。"""
    patterns = [r"ntn_[A-Za-z0-9]{20,}", r"secret_[A-Za-z0-9]{16,}",
                r"sk-[A-Za-z0-9]{20,}", r"github_pat_[A-Za-z0-9]{20,}"]
    hits = []
    for p in ROOT.rglob("*"):
        if (".git" in p.parts or "__pycache__" in p.parts or p.suffix not in
                (".py", ".md", ".json", ".yml", ".yaml", ".sh", ".txt", ".example")):
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        for pat in patterns:
            if re.search(pat, text):
                hits.append(str(p))
    assert not hits, f"疑似密钥泄漏: {hits}"
