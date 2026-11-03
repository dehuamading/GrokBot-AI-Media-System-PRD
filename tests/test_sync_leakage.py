"""GitHub 同步泄漏检查（只读 git 已跟踪文件，不联网）。

红线：
- 已跟踪文件不得包含 token 样式字符串；
- inputs/ 与 data/state/ 运行数据不得被跟踪；
- 不得出现真实 x.com/<user>/status 链接。测试/fixtures 的假链接只允许
  使用本仓库约定的 example/mock/real 示例用户名白名单。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]

TOKEN_PATTERNS = [
    r"ntn_[A-Za-z0-9]{20,}",
    r"secret_[A-Za-z0-9]{16,}",
    r"sk-[A-Za-z0-9]{20,}",
    r"github_pat_[A-Za-z0-9_]{20,}",
    r"ghp_[A-Za-z0-9]{20,}",
]

# 仅用于测试/fixtures 的假 X 用户名白名单（统一 example 前缀/示例名）。
FAKE_X_USERS = {
    "example", "example_a", "example_ai", "example_ai_teacher",
    "example_api_user", "example_aud", "example_coiner", "example_dev",
    "example_mock_side", "example_mock_user", "example_news",
    "example_real_user", "example_short", "example_tool_fan",
    "example_agent", "example_alice",
}


def _tracked_files() -> list[str]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT,
                         check=True, capture_output=True)
    return [p for p in out.stdout.decode("utf-8").split("\0") if p]


def test_tracked_files_never_include_local_inputs_or_state():
    tracked = _tracked_files()
    bad = [p for p in tracked
           if p.startswith("inputs/") or p.startswith("data/state/")]
    assert bad == [], f"运行数据被跟踪：{bad}"


def test_tracked_files_have_no_token_patterns():
    hits = []
    for rel in _tracked_files():
        path = ROOT / rel
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for pattern in TOKEN_PATTERNS:
            if re.search(pattern, text):
                hits.append(f"{rel}: {pattern}")
    assert hits == [], f"疑似 token 样式字符串：{hits}"


def test_tracked_files_have_only_example_x_status_links():
    pattern = re.compile(r"https?://x\.com/([A-Za-z0-9_]+)/status/")
    bad = []
    for rel in _tracked_files():
        path = ROOT / rel
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for user in pattern.findall(text):
            if user not in FAKE_X_USERS:
                bad.append(f"{rel}: x.com/{user}/status")
    assert bad == [], f"疑似真实 X 链接/未登记用户名：{bad}"
