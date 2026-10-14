# BlogNotes 技术说明

> 维护与发布参考。本项目不收集任何个人信息；**不要在 Issue、日志或提交中粘贴平台凭证或私人正文**。

## 技术栈

| 技术 | 用途 |
|---|---|
| Python 3.10+ / FastAPI + Uvicorn + Pydantic | 本地单进程 Web 服务与 REST API，绑定 `127.0.0.1` |
| Playwright (CDP) + 独立 Chrome Profile | 小红书页面登录、读写与数据采集 |
| HTTPX + HMAC-SHA256 签名 | 知乎官方 Publisher OpenAPI 与数据开放平台 Bearer GET |
| markdown-it-py / Markdown + BeautifulSoup | 知乎 Markdown → 文章 HTML / 本地预览渲染（源自 WorkKnacks 引擎） |
| SQLite (sqlite3) | 发布状态、统计快照（`runtime/` 下独立库） |
| Pillow + pillow-heif | 图片格式转换与 HEIC 读取 |
| 原生 HTML + JS + CSS | 单页前端，无构建步骤、无外部 CDN |

启动链路：`run.bat` → `src/launch.ps1`（依赖指纹、端口占用检查）→ `src.launch` → Uvicorn。

## 架构边界

- 内容权威源是本地目录：`xhs/<笔记名>/文案.txt + 编号图片`、`zhihu/<笔记名>/文案.md + 编号图片`；平台发布状态、密钥、登录 Profile、日志全部放 `runtime/`（已 gitignore）。
- 发布是**不可逆的外部写操作**：只在用户明确确认后执行；超时或结果不明必须保持 `pending`，绝不自动重试或重发。
- 状态机：`draft → pending → published`；`pending` 必须人工核对。
- 服务只绑定本机，跨站 mutation 校验 Origin + Fetch Metadata，静态资源完全本地，CSP 禁止外部脚本。

## 经验证的边界与问题（2026-10 核验）

- 小红书统计接口 `406` 是签名/风控拒绝，**不是登录失效**，不得据此要求重新扫码。
- 网页 HTTP 200 不等于登录成功：必须解析授权数据结构；传输错误/SPA 未就绪只能判定 `unknown`，绝不判为未登录。
- 话题标签联想缺失时必须降级：已键入标签保留在正文中，**绝不重复键入**（会重复）也不丢弃。
- 已发布原帖的**远程改写接口未实现**；界面「更新数据」只同步互动统计，不覆盖远端原文。
- 知乎发布成功判定：仅 HTTP 200 不足为证，需 `status==0` 且 `content_token` / 正式 URL 匹配。
- 知乎图片限制：非 GIF ≤ 30MB、GIF ≤ 15MB、最长边 ≤ 16384px、像素总数 ≤ 2 亿。
- 缺失采样以 NULL 表示，0 仅代表平台明确返回 0；趋势只基于真实采样点，不做推算。
- 测试（`tests/`）：不接平台的回归测试全部离线可跑；真实扫码登录、发布、统计曲线需本人账号授权后联机验收，不能以离线测试替代。

## 隐私与安全

- 凭证只存本机 `runtime/`（`zhihu-credentials.json`、浏览器 Profile）；`config/` 只放示例配置，禁止存实际密钥。
- 日志不得打印 Authorization、Cookie、App Secret、签名或完整响应凭证。
- 独立浏览器 Profile 含平台身份，备份时应按敏感信息处理。
- 已 gitignore：`runtime/`、`xhs/`、`zhihu/`、`config/config.json`、`__pycache__/`、`*.log`、`.pytest_cache/`。
- 参考外部项目仅借鉴设计思路，未整段拷贝未明确授权的代码；许可见根目录 `LICENSE`（MIT）。
