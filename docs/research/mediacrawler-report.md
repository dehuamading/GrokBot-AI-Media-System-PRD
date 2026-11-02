# MediaCrawler 与 抖音/B站/小红书 数据获取 调研报告（2026-11-02，美国东部时间）

## 结论先说
- **能做的（已实测）**：在美国云电脑上，**不登录**就能拿到 **B站** 的热门视频榜、B站热搜（带热度值）、关键词“AI”搜索结果，以及**一条AI视频的少量真实评论**（只有3条）。
- **不能做的**：抖音、小红书**不登录拿不到**内容（抖音返回反爬脚本，小红书直接跳转登录页）。
- **MediaCrawler 许可证明确禁止商业用途**，不适合你的商业自媒体，除非取得作者书面同意。
- **建议**：选题研究只用 B站公开接口（小规模、低频）+ 手机上人工浏览抖音/小红书热榜；不要用爬虫登录账号批量抓取。

## 1. 美国云电脑能否访问三个平台？（实测，证据：reachability.txt、raw/）
| 平台 | 结果 |
|---|---|
| 抖音 douyin.com | HTTP 200，但返回的是反爬 JS 校验页，无内容；热榜接口 /aweme/v1/web/hot/search/list/ 返回 200 但内容为空 |
| B站 bilibili.com | 可访问（302 跳到 /zh-Hans）；公开 API 部分可用 |
| 小红书 xiaohongshu.com | /explore 302 跳转到 **登录页** |

## 2. 能否获取真实评论？
- B站：**能，但很少**。不登录只返回了第1页的 3 条评论（证据：bilibili_test.json → comments_page1）。
- 抖音、小红书：不登录**不能**（未尝试绕过）。MediaCrawler 需扫码/手机号/Cookie 登录才能抓评论 → 「未测试，理论支持」。

## 3. 能否获取热点指数？
- B站热搜 + heat_score：**能**（实测，hot_search）。B站全站排行榜 ranking/v2：返回 **-352 风控**，失败。
- 抖音热榜 / 抖音指数 / 巨量算数：本机不登录拿不到（热榜接口空、index.oceanengine.com 连接失败）。巨量算数网页需登录 → 「未完成」。
- 小红书热榜：需登录，「未完成」。

## 4. 哪些实际测试成功？（证据 bilibili_test.json、bilibili_requests_log.json）
- ✅ B站热门榜前20（popular，code 0）
- ✅ B站热搜10条带热度（search/square）
- ✅ B站搜索“AI”（search/all/v2，20条视频）
- ✅ AI视频《【漫士】AI时代，数学该何去何从？》前3条评论（只保存文字+点赞数，不存用户ID）
- ❌ B站视频详情 view 接口：HTTP 412 风控
- ❌ B站排行榜 ranking/v2：-352 风控；search/type 接口：412
- ❌ 抖音、小红书：全部被反爬/登录墙挡住

## 5. 哪些只是理论支持？
- MediaCrawler 抓抖音/小红书/B站视频和评论：需登录（LOGIN_TYPE = qrcode/phone/cookie），**未运行**。
- README 声称"完整 Linux 环境支持"，默认 HEADLESS=False 且默认 CDP 模式连接本地 Chrome；云服务器上扫码登录不便。依赖安装和 --help 启动 → 「未完成」。
- 近期 issue 显示：抖音搜索出现九宫格拖拽验证码(#996)、小红书创作者抓取失败(#985/#990)——说明经常被风控，需要人工过验证。

## 6. MediaCrawler 许可证限制
- 名称：**NON-COMMERCIAL LEARNING LICENSE 1.1（非商业学习使用许可证 1.1）**
- 原文要点："The Software is limited to learning and research purposes only, and may not be used for large-scale crawling…"；"Without the written consent of the copyright owner, the Software may not be used for any commercial purposes"。
- README 免责声明："仅供学习和参考之用，禁止用于商业用途"。
- **结论：不适合商业自媒体**。
- 项目信息：约 69,875 星，最近提交 2026-10-26（美东时间，commit 68d9ef1），219 个开放 issue/PR。

## 7. 能否免费、长期、合规运行？
- B站公开接口：免费，小规模可用，但**不稳定**（部分接口已风控，没有官方保证，随时可能改）。B站 robots.txt 只禁止极少路径。
- 抖音：robots.txt 对通用爬虫禁止 /search/ 等路径；不登录无法获取 → 不能合规免费长期。
- 小红书：robots.txt 只对指定搜索引擎开放 /explore 等，其余 Disallow → 不能。
- 法律风险（一般性说明，非法律意见）：中国有判例把绕过反爬、大量抓取平台数据认定为不正当竞争（《反不正当竞争法》第2条/第12条）；评论里的用户昵称/ID属个人信息，受《个人信息保护法》约束。**用账号登录+批量抓取+商用 = 风险最高**。
- **结论：只有"B站公开接口 + 低频 + 只存评论文字"勉强可行；三平台全覆盖无法做到免费长期合规。**

## 8. 美国云端还是国内本地电脑？
- **推荐：国内本地电脑（或直接用手机人工看）做抖音/小红书；B站轻量读取可在美国云端做。**
- 理由：美国IP本身就被抖音反爬拦、B站部分接口风控；国内本地网络更接近正常用户，但**即使在国内，登录抓取仍违反平台协议和 MediaCrawler 的非商用许可**，所以国内本地也只建议人工浏览+少量记录。

## 9. 官方/授权途径（理论，未申请，未登录）
- 抖音开放平台 https://open.douyin.com/（可访问，200）：接口主要针对开发者/企业应用、需用户授权，通常只能读**授权账号自己**的数据，不提供全站热榜评论批量读取。资质细节「未核实完」。
- B站开放平台 https://openhome.bilibili.com/（200）：面向 UP主/企业的授权数据，范围限自身账号，「未核实完」。
- 小红书开放平台 https://open.xiaohongshu.com/（200）、蒲公英/聚光：面向商家/品牌广告投放，需企业资质，非免费数据接口。
- 巨量算数/抖音指数 https://trendinsight.oceanengine.com/：网页免费但需登录，本机未登录未测试。
- RSSHub（https://docs.rsshub.app/）有 bilibili 热搜/排行等路由；公共实例 rsshub.app 从本机返回 403，需自建；抖音/小红书路由通常需 Cookie，不稳定。

## 证据文件（/workspace/research/mediacrawler/）
- repo/（MediaCrawler 浅克隆）、issues.txt
- reachability.txt、raw/1.out…9.out、official_reachability.txt
- btest.py、bilibili_test.json、bilibili_requests_log.json
- 未做：「未完成」——MediaCrawler 依赖安装与启动测试、任何需要登录的抓取、官方平台资质的逐条核实。
