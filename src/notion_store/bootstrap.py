"""Notion 知识库结构（PRD 7.1）。

PRD 允许第一轮合并为 5 库（素材/需求/选题/文案/经验复盘）；本实现建立 7 个数据库：
- 5 个合并库之外，另设「00_X关注账号名单」（X 降级模式下的情报源注册表，
  任务02 决定 X 仅作账号名单使用）与「08_维护与审计」（PRD 7.3 要求的操作审计）。
逻辑字段与 PRD 7.1 表格一一对应，后续可平滑拆分为 8 库。
所有结构必须建在用户授权页面「AI 自媒体系统」之下（任务02 约束）。
"""

from __future__ import annotations

from src.notion_store.client import NotionClient

PAGE_ROOT_TITLE = "AI 自媒体系统"


def _title(): return {"title": {}}
def _rt(): return {"rich_text": {}}
def _url(): return {"url": {}}
def _date(): return {"date": {}}
def _num(): return {"number": {"format": "number"}}
def _sel(*options: str):
    return {"select": {"options": [{"name": o} for o in options]}}


def _status_sel():
    return _sel("待验证", "已验证", "正在使用", "需要复核", "已过时")


DATABASE_SPECS: dict[str, tuple[str, dict]] = {
    "00_X关注账号名单": ("📇", {
        "username": _title(),
        "name": _rt(),
        "description": _rt(),
        "followers": _num(),
        "source_type": _sel("x_following"),
        "collected_at": _date(),
        "processing_status": _sel("new", "screened", "candidate", "archived"),
        "note": _rt(),
    }),
    "01_原始素材": ("📥", {
        "item_id": _title(),
        "source_type": _sel("x_following", "x_posts", "x_discovery", "aihot", "overseas_case",
                            "github_domestic", "feishu_domestic", "manual"),
        "source_url": _url(),
        "author_or_publisher": _rt(),
        "published_at": _date(),
        "collected_at": _date(),
        "title_or_summary": _rt(),
        "claims": _rt(),
        "evidence_status": _sel("verified", "self_reported", "third_party", "unverified"),
        "tool_or_method": _rt(),
        "processing_status": _sel("new", "screened", "candidate", "archived"),
        "screen_score": _num(),
        "screen_reasons": _rt(),
        "aihot_score": _num(),
        "category": _rt(),
        "aihot_url": _url(),
    }),
    "02_用户需求": ("🙋", {
        "problem_id": _title(),
        "user_type": _sel("职场提效者", "非程序员开发者", "个人项目/副业关注者", "小企业主", "其他"),
        "problem": _rt(),
        "evidence_url": _url(),
        "sample_limit": _rt(),
        "status": _sel("待验证", "已验证"),
    }),
    "03_选题决策": ("🎯", {
        "topic_id": _title(),
        "拟定标题": _rt(),
        "建议平台": _sel("抖音", "小红书", "两者"),
        "面向谁": _rt(),
        "核心价值": _rt(),
        "为什么现在": _rt(),
        "关联素材": _rt(),
        "需求证据": _rt(),
        "可信度与风险": _rt(),
        "最强反证": _rt(),
        "差异与难度": _rt(),
        "推荐级别": _sel("推荐", "备选", "观察"),
        "推荐理由": _rt(),
        "用户选择结果": _sel("待用户选择", "已选定", "未选用"),
        "用户指定平台": _sel("抖音", "小红书", "两者"),
        # 任务05 新增列（任务书 §4 选题卡必填内容；旧库经只增列升级补齐）
        "为什么适合": _rt(),
        "切入角度与钩子": _rt(),
        "国内可用性": _rt(),
        "引用帖子": _rt(),
        "综合评分": _num(),
    }),
    "04_文案与版本": ("✍️", {
        "draft_id": _title(),
        "topic_id": _rt(),
        "platform": _sel("抖音", "小红书"),
        "version": _num(),
        "status": _sel("待生成", "V1", "修订中", "已确认"),
        "feedback_summary": _rt(),
        "change_summary": _rt(),
        "updated_at": _date(),
        "生成引擎": _rt(),
    }),
    "05_经验与复盘": ("🧠", {
        "record_id": _title(),
        "类型": _sel("规则", "经验", "复盘"),
        "适用范围": _rt(),
        "内容": _rt(),
        "依据": _rt(),
        "状态": _status_sel(),
        "性质": _sel("原始事实", "当事人自述", "用户偏好", "AI推断假设", "已验证经验"),
        "version": _num(),
        "updated_at": _date(),
    }),
    "08_维护与审计": ("🧾", {
        "event_id": _title(),
        "对象": _rt(),
        "操作": _rt(),
        "结果": _sel("success", "failed"),
        "详情": _rt(),
        "occurred_at": _date(),
    }),
}


def ensure_kb(client: NotionClient, page_id: str) -> dict[str, str]:
    """幂等确保页面下存在全部数据库，返回 {库名: database_id}。
    顺带验证对该页面的访问权限（token 只授权了「AI 自媒体系统」页）。"""
    client.get_page(page_id)
    existing = client.child_databases(page_id)
    ids: dict[str, str] = {}
    for name, (icon, props) in DATABASE_SPECS.items():
        if name in existing:
            ids[name] = existing[name]
            continue
        db = client.create_database(page_id, name, props, icon=icon)
        ids[name] = db["id"]
    return ids


# 任务03 工作流升级：对既有库「只增列」（绝不删除/修改既有列与数据，红线）。
# - 03_选题决策 ➕ 用户指定平台：GATE-1 用户可在 Notion 里指定抖音/小红书/两者
# - 04_文案与版本 ➕ 生成引擎：记录初稿由哪个 LLM 引擎产生（透明可审计）
# 任务05 升级（同样只增，不动既有列/数据）：
# - 03_选题决策 ➕ 为什么适合/切入角度与钩子/国内可用性/引用帖子/综合评分
#   （任务书 §4 选题卡必填内容；旧卡新列为空，不受影响）
# - 01_原始素材 source_type 下拉 ➕ x_posts 选项（X 帖子文件输入，任务05）
DB_UPGRADES: dict[str, dict] = {
    "03_选题决策": {
        "用户指定平台": {"select": {"options": [{"name": o} for o in
                                             ("抖音", "小红书", "两者")]}},
        "为什么适合": {"rich_text": {}},
        "切入角度与钩子": {"rich_text": {}},
        "国内可用性": {"rich_text": {}},
        "引用帖子": {"rich_text": {}},
        "综合评分": {"number": {"format": "number"}},
    },
    "04_文案与版本": {
        "生成引擎": {"rich_text": {}},
    },
}

# 只增下拉选项升级（不删除既有选项，不改动已有行数据）
DB_SELECT_OPTION_UPGRADES: dict[str, dict[str, list[str]]] = {
    "01_原始素材": {"source_type": ["x_posts"]},
}


def ensure_kb_upgrades(client: NotionClient, db_ids: dict[str, str]) -> dict[str, list[str]]:
    """幂等执行只增列升级，返回 {库名: [新增列名...]}（未发生新增则为空列表）。"""
    added: dict[str, list[str]] = {}
    for name, props in DB_UPGRADES.items():
        db_id = db_ids.get(name)
        if db_id:
            added[name] = client.add_database_columns(db_id, props)
    for name, prop_options in DB_SELECT_OPTION_UPGRADES.items():
        db_id = db_ids.get(name)
        if not db_id:
            continue
        new_opts = []
        for prop, options in prop_options.items():
            new_opts += client.ensure_select_options(db_id, prop, options)
        if new_opts:
            added.setdefault(name, []).extend(new_opts)
    return added


def ensure_nav_page(client: NotionClient, page_id: str, db_ids: dict[str, str]) -> str:
    """幂等创建「📖 知识库导航」子页面（六模块之『导航』）。"""
    existing_pages = client.child_pages(page_id)
    if "📖 知识库导航" in existing_pages:
        return existing_pages["📖 知识库导航"]
    desc = {
        "00_X关注账号名单": "X 情报源注册表（降级模式：仅账号名单，帖子内容为已知缺口）",
        "01_原始素材": "统一格式素材池：来源/时间/证据等级/处理状态",
        "02_用户需求": "真实用户问题与证据（当前为空：需求证据采集未开始）",
        "03_选题决策": "候选选题卡 5—10 个，等用户人工选定（第一人工关口）",
        "04_文案与版本": "每题×每平台仅 1 份稿件的版本链（第二人工关口后启用）",
        "05_经验与复盘": "规则/偏好/经验/复盘，区分性质与验证状态",
        "08_维护与审计": "系统操作审计日志",
    }
    blocks = [
        {"type": "heading_2", "heading_2": {"rich_text": [{"text": {"content": "知识库导航"}}]}},
        {"type": "paragraph", "paragraph": {"rich_text": [{"text": {
            "content": "正式知识库（PRD 7.1）。状态生命周期：待验证→已验证→正在使用→需要复核→已过时；"
                       "知识与性质分离：原始事实/当事人自述/用户偏好/AI推断假设/已验证经验。"}}]}},
    ]
    for name, db_id in db_ids.items():
        blocks.append({
            "type": "bulleted_list_item",
            "bulleted_list_item": {"rich_text": [{"text": {
                "content": f"{name} — {desc[name]} — https://www.notion.so/{db_id.replace('-', '')}"}}]},
        })
    page = client.create_page(
        {"type": "page_id", "page_id": page_id},
        {"title": {"title": [{"text": {"content": "📖 知识库导航"}}]}},
        children=blocks[:100],
    )
    return page["id"]
