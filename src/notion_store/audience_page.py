"""任务05：把受众画像写入 Notion「AI 自媒体系统」页下的「用户画像」子页面。

设计取舍（任务书 §1 二选一）：
- 选「新建用户画像页面」而非写入 02_用户需求 库：02_用户需求 是「逐条问题+证据链接」
  的行式结构（problem/evidence_url/状态），而画像是一份多节点的活文档（人群/兴趣/痛点/
  评分信号/负面清单），拆成行会丢结构、且画像本身是「用户提供的偏好」而非逐条已验证需求；
  未来采集到的具体用户问题仍写 02_用户需求，两者互补不冲突。
- 幂等：页面已存在则直接返回（不重复写入、不修改既有内容，红线「不删除既有资料」）。
"""

from __future__ import annotations

from src.analysis.audience import AudienceProfile
from src.notion_store.client import NotionClient

PAGE_TITLE = "用户画像"


def _para(text: str) -> dict:
    return {"type": "paragraph",
            "paragraph": {"rich_text": [{"text": {"content": (text or "")[:1900]}}]}}


def _h2(text: str) -> dict:
    return {"type": "heading_2",
            "heading_2": {"rich_text": [{"text": {"content": text}}]}}


def _bullets(items: list[str]) -> list[dict]:
    return [{"type": "bulleted_list_item",
             "bulleted_list_item": {"rich_text": [{"text": {"content": x[:1900]}}]}}
            for x in items]


def profile_blocks(profile: AudienceProfile) -> list[dict]:
    blocks = [
        _h2(f"{profile.name}（{profile.profile_id}）"),
        _para(profile.description),
        _para(f"来源：{profile.source or '用户提供'}｜更新：{profile.updated_at or '待核实'}｜"
              f"性质：用户偏好（非 AI 推断假设）｜平台偏好：{'/'.join(profile.platforms) or '抖音/小红书'}"),
        _h2("关注方向"),
        *_bullets(profile.interests or ["待核实"]),
        _h2("主要痛点"),
        *_bullets(profile.pain_points or ["待核实"]),
        _h2("选题评分怎么用它"),
        _para("仓库 config/audience_profile.yaml 是本页面的机器可读版；"
              "评分引擎（src/analysis/scoring.py + audience.py）按「受众匹配」维度读取："
              "高/中信号加分，负面信号（加密货币/撸毛/外国政治等受众无关内容）硬降权。"
              "修改信号词须升 profile_id 版本号，保证评分结果可追溯。"),
        _para("⚠️ 后续每轮真实运行（collect）若画像有更新，需人工同步本页面与 YAML；"
              "本页为 v1 快照。"),
    ]
    return blocks


def ensure_audience_page(client: NotionClient, page_id: str,
                         profile: AudienceProfile) -> tuple[str, bool]:
    """幂等创建「用户画像」子页面。返回 (page_id, 是否新建)。已存在则不改动。"""
    existing = client.child_pages(page_id)
    if PAGE_TITLE in existing:
        return existing[PAGE_TITLE], False
    page = client.create_page(
        {"type": "page_id", "page_id": page_id},
        {"title": {"title": [{"text": {"content": PAGE_TITLE}}]}},
        children=profile_blocks(profile))
    return page["id"], True
