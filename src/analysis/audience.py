"""受众画像（任务05）：从 config/audience_profile.yaml 加载，供评分引擎与选题使用。

- 画像来源是用户直接提供的偏好描述（非 AI 推断），版本号进评分结果可追溯；
- signals.high/mid 用于「受众匹配」维度与本地预筛；negative 命中 → 受众无关降权；
- 本模块纯本地（读文件/纯函数），无网络请求。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_PROFILE_PATH = Path(__file__).resolve().parents[2] / "config" / "audience_profile.yaml"


@dataclass(frozen=True)
class AudienceProfile:
    profile_id: str
    name: str
    description: str
    platforms: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=lambda: ["zh", "en"])
    interests: list[str] = field(default_factory=list)
    pain_points: list[str] = field(default_factory=list)
    high_signals: list[str] = field(default_factory=list)
    mid_signals: list[str] = field(default_factory=list)
    negative_signals: list[str] = field(default_factory=list)
    source: str = ""
    updated_at: str = ""

    # ------------------------------------------------------------------
    def summary(self) -> str:
        """给 LLM/文档用的画像摘要（一段话 + 要点）。"""
        pains = "；".join(self.pain_points[:3])
        interests = "；".join(self.interests[:5])
        return (f"{self.name}。{self.description} 关注方向：{interests}。"
                f"主要痛点：{pains}。内容平台偏好：{'/'.join(self.platforms) or '抖音/小红书'}。")


def load_profile(path: str | Path = DEFAULT_PROFILE_PATH) -> AudienceProfile:
    """从 YAML 加载受众画像；文件缺失/字段非法时抛错（不静默回退，避免拿错受众评分）。"""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"受众画像文件不存在：{p}")
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    required = ["profile_id", "name", "description"]
    for k in required:
        if not raw.get(k):
            raise ValueError(f"受众画像缺必填字段：{k}（{p}）")
    sig = raw.get("signals") or {}
    return AudienceProfile(
        profile_id=str(raw["profile_id"]),
        name=str(raw["name"]),
        description=str(raw["description"]),
        platforms=list(raw.get("platforms") or []),
        languages=list(raw.get("languages") or ["zh", "en"]),
        interests=list(raw.get("interests") or []),
        pain_points=list(raw.get("pain_points") or []),
        high_signals=[str(x).lower() for x in sig.get("high") or []],
        mid_signals=[str(x).lower() for x in sig.get("mid") or []],
        negative_signals=[str(x).lower() for x in raw.get("negative") or []],
        source=str(raw.get("source") or ""),
        updated_at=str(raw.get("updated_at") or ""),
    )


def match_audience(text: str, profile: AudienceProfile) -> tuple[int, list[str], list[str]]:
    """画像信号匹配：返回 (0-2 分, 命中理由, 负面命中词)。

    - 负面信号（受众无关，如加密货币/撸毛/外国政治）命中 → 0 分；
    - 高信号 ≥1 → 2 分；仅中信号 → 1 分；无命中 → 0 分。
    """
    text_l = (text or "").lower()
    neg = [k for k in profile.negative_signals if k in text_l]
    if neg:
        return 0, [f"命中受众无关信号「{neg[:3]}」→ 0 分"], neg
    highs = [k for k in profile.high_signals if k in text_l]
    if highs:
        return 2, [f"命中画像高信号「{h}」" for h in highs[:4]], []
    mids = [k for k in profile.mid_signals if k in text_l]
    if mids:
        return 1, [f"命中画像中信号「{m}」" for m in mids[:3]], []
    return 0, ["未命中画像信号（受众相关性存疑）"], []
