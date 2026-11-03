# 国内内容统一导入 schema（阶段2-B3）

本接口只读取**用户本地导出的 JSON/CSV 文件**：不登录平台、不抓取、不绕过限制。
飞书仅保留接口类与字段映射，本阶段没有联网代码。

## 1. 素材字段

| 字段 | 必填 | 说明 |
|---|---|---|
| `platform` | 是 | `douyin` / `bilibili` / `xiaohongshu` / `other` |
| `original_url` | 是 | 原始链接；用于规范化去重与来源追溯 |
| `title` | 是 | 标题；与链接一起参与去重 |
| `comments` | 否 | 公开评论文本数组；CSV 用 `|` 分隔，多行同 URL 会合并 |
| `published_at` | 是 | 发布时间，ISO-8601（如 `2026-10-20T10:00:00Z`） |
| `collected_at` | 否 | 采集时间；缺省用当前 UTC 时间 |
| `keywords` | 否 | 关键词数组；CSV 用 `|` 分隔 |
| `metrics` | 否 | 互动指标对象（如 `{"likes": 120, "comments": 3}`） |
| `source` | 否 | 导入渠道：`飞书` / `Notion` / `手工` 等；缺省“国内导入” |
| `authorization_status` | 是 | `公开` / `已授权` / `待确认` |

缺必填字段或枚举非法：该行不进入素材池，错误写入
`data/state/import_errors.jsonl`（含批次 id、源文件名、行号），不中断其余行。

## 2. JSON 示例（脱敏假数据）

```json
{
  "items": [
    {
      "platform": "douyin",
      "original_url": "https://example.com/video/1001",
      "title": "零基础用 AI 做短视频（虚构样例）",
      "comments": ["安装要多久？", "国内能直接用吗？"],
      "published_at": "2026-10-20T10:00:00Z",
      "collected_at": "2026-11-02T09:00:00Z",
      "keywords": ["AI", "短视频"],
      "metrics": {"likes": 120, "comments": 2},
      "source": "手工导入",
      "authorization_status": "公开"
    }
  ]
}
```

顶层也可以直接是数组。完整样例见
`tests/fixtures/import_sample_masked.json`（全部虚构，作者写“用户A/用户B”，
链接只用 example.com）。

## 3. CSV 示例（脱敏假数据）

表头固定为：

```text
platform,original_url,title,comments,published_at,collected_at,keywords,metrics,source,authorization_status
```

- `comments` 与 `keywords` 用 `|` 分隔；
- `metrics` 写 JSON 字符串，例如 `"{""likes"": 50}"`；
- 多行相同 `original_url`（同一标题）会合并评论、关键词与指标，不产生重复素材；
- 文件用 UTF-8（可带 BOM）。

完整样例见 `tests/fixtures/import_sample_masked.csv`。

## 4. 校验、去重与追溯

- URL 规范化：去 `utm_*` 等跟踪参数、末尾斜杠，并忽略 `http/https` 差异；
- 去重键：规范化 URL + 标题哈希；重复行合并公开评论，保留首行素材；
- 每条素材记录：`import_batch`、`source_file`、`row`（及合并行 `rows`）；
- 评论需求分类钩子：`classify_comment_demand(text) -> list[str]`，当前为关键词
  stub，10 类：安装部署、注册付款、国内使用、免费替代、AI编程、AI内容创作、
  AI副业赚钱、工作效率、工具比较、故障排查；结果写入
  `comments[].demand_tags`。第3阶段会替换为正式需求引擎。
- 授权状态映射到统一 `license_status`：`公开→公开`、`已授权→授权`、
  `待确认→待确认`。

## 5. 飞书多维表格字段映射（仅预留，不联网）

| 飞书字段（建议列名） | 统一字段 | 转换说明 |
|---|---|---|
| 平台 | `platform` | 抖音/哔哩哔哩/小红书 → `douyin`/`bilibili`/`xiaohongshu`，其余 → `other` |
| 原链接 | `original_url` | 原样保留，去重时规范化 |
| 标题 | `title` | 原样 |
| 公开评论 | `comments` | 多行文本按换行拆分；只保留评论文本，不含评论者身份 |
| 发布时间 | `published_at` | 飞书日期 → ISO-8601 |
| 采集时间 | `collected_at` | 飞书日期 → ISO-8601 |
| 关键词 | `keywords` | 多选/逗号分隔 → 数组 |
| 互动指标 | `metrics` | 数字列 → `{likes, comments, shares, views}`（缺省不编造） |
| 来源 | `source` | 如“飞书多维表格” |
| 授权状态 | `authorization_status` | 公开/已授权/待确认（必填） |
| 记录 ID | `extra.import.feishu_record_id` | 仅追溯用，不进入公开素材 |

飞书接入代码位置：`src/connectors/import_source.py::FeishuAdapter`。
其方法当前全部抛 `NotImplementedError`，不会发起任何网络请求。
