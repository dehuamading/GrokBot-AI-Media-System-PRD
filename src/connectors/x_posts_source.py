"""连接器层（任务05 + A2）：X 帖子文件读取适配器（降级模式，纯本地）。

本适配器只读本地 JSON，绝不访问 X 的任何网络接口——本模块不引入任何网络库。
兼容三种输入：
1) 扁平数组：[{id,url,author_username,author_name,created_at,text,...}, ...]
2) 包装格式：{"posts":[...]}（可带顶层 "replies":[...]）
3) X API 原样：{"data":[...], "includes":{"users":[...]}}（可带顶层 "replies"）

顶层 replies 按 conversation_id / parent id 挂到对应帖子的 comments。
"""

from __future__ import annotations

import json
from pathlib import Path

from src.normalize import normalize_x_post, normalize_x_reply

REQUIRED_RAW_FIELDS = ["id", "text", "created_at"]
FLAT_REQUIRED_RAW_FIELDS = ["id", "url", "text", "created_at"]


class XPostsResult(list):
    """帖子列表 + 文件级错误（例如无法归属的孤儿回复），仍保持 list 兼容。"""

    def __init__(self, items=()):
        super().__init__(items)
        self.errors: list[str] = []


def _is_api_post(raw: dict) -> bool:
    return "author_id" in raw or "public_metrics" in raw


def _users_map(data: dict) -> dict:
    includes = data.get("includes") if isinstance(data.get("includes"), dict) else {}
    users = includes.get("users") if isinstance(includes.get("users"), list) else []
    return {str(u["id"]): u for u in users
            if isinstance(u, dict) and u.get("id") is not None}


def _parse_payload(data) -> tuple[list, list, dict, bool]:
    """返回 (posts, top_replies, users, is_api_payload)，坏结构直接报错。"""
    if isinstance(data, list):
        return data, [], {}, False
    if not isinstance(data, dict):
        raise ValueError("X 帖子文件应为数组或对象")
    if isinstance(data.get("posts"), list):
        posts = data["posts"]
        replies = data.get("replies") or []
        return posts, replies, _users_map(data), False
    # X API 原样：data 必须非空，避免把任意 {"data": []} 误当合法输入。
    if isinstance(data.get("data"), list) and data["data"]:
        replies = data.get("replies") or []
        return data["data"], replies, _users_map(data), True
    raise ValueError("X 帖子文件应为扁平数组、{\"posts\": [...]} 或 X API data[] 格式")


def _has_reply_marker(raw: dict) -> bool:
    for key in ("parent_id", "in_reply_to", "in_reply_to_id",
                "in_reply_to_status_id", "reply_to"):
        if raw.get(key):
            return True
    refs = raw.get("referenced_tweets")
    if isinstance(refs, list):
        return any(isinstance(ref, dict)
                   and ref.get("type") in ("replied_to", "reply")
                   for ref in refs)
    return False


def _is_root_post(raw: dict) -> bool:
    """只允许根帖注册 conversation 映射，避免回复条目覆盖根帖。"""
    raw_id = str(raw.get("id") or "")
    conversation = raw.get("conversation_id")
    if conversation is not None and str(conversation) == raw_id:
        return True
    return not _has_reply_marker(raw)


def _attach_top_replies(posts_raw: list[dict], items: list[dict],
                        replies: list) -> list[str]:
    """按 conversation_id / parent id 把顶层回复挂到帖子 comments。"""
    post_by_id: dict[str, dict] = {}
    post_by_conversation: dict[str, dict] = {}
    for raw, item in zip(posts_raw, items):
        raw_id = str(raw.get("id") or "")
        if raw_id:
            post_by_id[raw_id] = item
        if not _is_root_post(raw):
            continue
        conversation = raw.get("conversation_id") or raw.get("id")
        if conversation:
            post_by_conversation.setdefault(str(conversation), item)

    # 预先建立「回复 id → conversation_id」映射，支持父回复再挂到同一会话。
    reply_conversation: dict[str, str] = {}
    for reply in replies:
        if isinstance(reply, dict):
            reply_id = str(reply.get("id") or "")
            conversation = reply.get("conversation_id")
            if reply_id and conversation:
                reply_conversation[reply_id] = str(conversation)

    orphans = 0
    for i, raw in enumerate(replies):
        if not isinstance(raw, dict):
            raise ValueError(f"第 {i} 条顶层回复不是对象")
        comment = normalize_x_reply(raw)
        target = None

        conversation = raw.get("conversation_id")
        if conversation:
            target = (post_by_conversation.get(str(conversation))
                      or post_by_id.get(str(conversation)))

        parent = comment.get("parent_id")
        if target is None and parent:
            target = post_by_id.get(str(parent))
            if target is None and str(parent) in reply_conversation:
                parent_conversation = reply_conversation[str(parent)]
                target = (post_by_conversation.get(parent_conversation)
                          or post_by_id.get(parent_conversation))

        if target is None:
            orphans += 1
            continue  # 无法归属到任何帖子时不编造归属
        target["comments"].append(comment)
        target["user_problem_evidence"] = [
            c["text"] for c in target["comments"] if c.get("text")
        ][:5]

    errors: list[str] = []
    if orphans:
        errors.append(f"顶层回复无法归属到任何帖子：{orphans} 条（未记录用户名）")
    return errors


def load_x_posts(path: str | Path) -> list[dict]:
    """读取 X 帖子文件并归一化为统一素材格式（纯本地，无网络）。"""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"X 帖子文件不存在：{p}")
    data = json.loads(p.read_text(encoding="utf-8"))
    posts, top_replies, users, api_payload = _parse_payload(data)
    if not isinstance(posts, list):
        raise ValueError("X 帖子文件应为帖子数组（list）")
    if not isinstance(top_replies, list):
        raise ValueError("顶层 replies 应为回复数组（list）")

    out: list[dict] = []
    for i, raw in enumerate(posts):
        if not isinstance(raw, dict):
            raise ValueError(f"第 {i} 条不是对象")
        api_post = api_payload or _is_api_post(raw)
        required = REQUIRED_RAW_FIELDS if api_post else FLAT_REQUIRED_RAW_FIELDS
        for f in required:
            if raw.get(f) in (None, ""):
                raise ValueError(f"第 {i} 条缺必填字段 {f}")
        out.append(normalize_x_post(raw, users=users))

    result = XPostsResult(out)
    if top_replies:
        result.errors.extend(_attach_top_replies(posts, out, top_replies))
    return result
