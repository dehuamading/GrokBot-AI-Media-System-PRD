"""隐私脱敏单元测试（纯文本处理，无网络/Notion）。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.analysis.privacy import is_x_material, scrub_identifiers


def test_is_x_material_checks_source_type_and_platform_independently():
    assert is_x_material({"source_type": "x_posts", "platform": "aihot"})
    assert is_x_material({"source_type": "aihot", "platform": "x"})
    assert is_x_material({"source_type": "x"})
    assert not is_x_material({"source_type": "aihot", "platform": "github"})


def test_scrub_identifiers_only_replaces_three_chars_or_at_handle():
    text = "作者 A 和 ab 以及 alice 和 @al 都在这里"
    cleaned = scrub_identifiers(text, ["A", "ab", "alice", "@al"])

    assert "A" in cleaned
    assert "ab" in cleaned
    assert "alice" not in cleaned
    assert "@al" not in cleaned
