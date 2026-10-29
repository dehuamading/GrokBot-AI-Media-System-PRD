"""评分引擎（PRD §5.3）：七维可解释评分 → 推荐级别。

设计约束（红线级）：
- 每个维度只有 高(2)/中(1)/低(0) 三档，附可解释理由 —— 不伪装成精确模型；
- 「传播潜力」维度显式标注为假设，不得用于宣称预测传播；
- 评分版本号 SCORING_VERSION 随结果一起入库，未来调权重必须换版本并验证（PRD 5.3）。

本引擎为启发式（AI 推断假设 HYP-002），用途仅限排序辅助。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

SCORING_VERSION = "v1-heuristic"

DIMS = ["真实用户价值", "信息可信度", "受众匹配", "新颖与时效",
        "可讲清与可验证", "传播潜力假设", "制作成本"]

# 普通人可上手信号（PRD 1.1 目标用户：职场提效者/非程序员/个人项目副业）
HANDS_ON_KEYWORDS = ["教程", "如何", "指南", "免费", "开源", "本地", "上手", "步骤",
                     "实测", "模板", "提示词", "prompt", "无需编程"]
AUDIENCE_KEYWORDS = ["办公", "文档", "表格", "pdf", "ppt", "演示", "笔记", "工作流",
                     "副业", "自动化", "效率", "小企业", "团队", "写作", "翻译"]
TENSION_KEYWORDS = ["翻倍", "首个", "开源", "免费", "暴跌", "裁员", "警告", "停服",
                    "大涨", "首度", "首次", "突破", "泄露"]
EVIDENCE_POINTS = {"verified": 2, "self_reported": 1, "third_party": 1, "unverified": 0}


def _grade(points: int) -> str:
    return {2: "高", 1: "中", 0: "低"}[points]


def _dim(name: str, points: int, reason: str) -> dict:
    return {"name": name, "grade": _grade(points), "points": points, "reason": reason}


def score_item(item: dict, now: datetime | None = None) -> dict:
    """对单条素材评分，返回 {total, level, version, dimensions[], notes}。"""
    now = now or datetime.now(timezone.utc)
    text = ((item.get("title_or_summary") or "") + " " +
            " ".join(item.get("claims") or [])).lower()

    # 1) 真实用户价值：动手型内容 + 类目偏向
    hands = [k for k in HANDS_ON_KEYWORDS if k in text]
    cat = (item.get("extra") or {}).get("category") or ""
    value_pts = min(2, len(hands) + (1 if cat == "tip" else 0))
    r1 = f"命中可上手信号 {len(hands)} 个" + ("，类目 tip 加分" if cat == "tip" else "")

    # 2) 信息可信度：证据等级映射
    ev = item.get("evidence_status") or "unverified"
    r2 = f"证据等级 {ev}（{'官方渠道' if ev == 'verified' else '具名第三方' if ev == 'third_party' else '来源可核' if ev == 'self_reported' else '无交叉核验'}）"

    # 3) 受众匹配：目标人群场景词
    aud = [k for k in AUDIENCE_KEYWORDS if k in text]
    aud_pts = 2 if len(aud) >= 2 else (1 if len(aud) == 1 else 0)
    r3 = f"命中目标人群场景词 {aud[:3]}" if aud else "未命中显式人群场景词（待验证）"

    # 4) 新颖与时效：发布时间距 now
    pub = item.get("published_at")
    fresh_pts, r4 = 0, "无发布时间（按最低档计）"
    if pub:
        try:
            dt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
            if dt.tzinfo is None:  # 无时区标记（如 aihot 归一化后的截断串）→ 视为 UTC
                dt = dt.replace(tzinfo=timezone.utc)
            age = now - dt
            if age <= timedelta(days=2):
                fresh_pts, r4 = 2, f"发布于 {age.days} 天内（新鲜）"
            elif age <= timedelta(days=7):
                fresh_pts, r4 = 1, f"发布于 {age.days} 天内（一周内）"
            else:
                r4 = f"发布已超过 7 天（{age.days} 天）"
        except ValueError:
            r4 = f"发布时间无法解析：{pub}"

    # 5) 可讲清与可验证：有可打开出处 + 可复现要素
    url = item.get("source_url") or ""
    steps = [k for k in ("步骤", "指南", "教程", "实测", "如何") if k in text]
    ver_pts = (1 if url else 0) + (1 if steps else 0)
    r5 = ("有原始出处链接" if url else "缺原始出处链接") + ("；含可复现要素" if steps else "；无可复现要素")

    # 6) 传播潜力（假设维度）
    tension = [k for k in TENSION_KEYWORDS if k in text]
    spread_pts = min(2, len(tension))
    r6 = f"标题张力词 {tension[:3]}（仅假设，未经数据验证）"

    # 7) 制作成本：反向计分（低门槛→高分）
    costly = [k for k in ("融资", "上市", "s-1", "股价", "招股", "市值", "间谍") if k in text]
    cost_pts = 2 if not costly and hands else (0 if costly else 1)
    r7 = ("无需实测/无需授权素材，普通人可自行复现" if cost_pts == 2 else
          "需自行核验部分事实" if cost_pts == 1 else
          f"涉及 {costly[:2]}，事实核验/素材成本高")

    dims = [
        _dim(DIMS[0], value_pts, r1), _dim(DIMS[1], EVIDENCE_POINTS[ev], r2),
        _dim(DIMS[2], aud_pts, r3), _dim(DIMS[3], fresh_pts, r4),
        _dim(DIMS[4], min(2, ver_pts), r5), _dim(DIMS[5], spread_pts, r6),
        _dim(DIMS[6], cost_pts, r7),
    ]
    total = sum(d["points"] for d in dims)  # 0–14
    level = "推荐" if total >= 10 else ("备选" if total >= 7 else "观察")
    return {
        "total": total,
        "level": level,
        "version": SCORING_VERSION,
        "dimensions": dims,
        "notes": "七维启发式评分（HYP-002，未验证），仅用于排序辅助，不承诺传播效果",
    }
