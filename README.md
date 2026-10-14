<p align="center">
  <img src="https://img.shields.io/badge/Windows-支持-blue?logo=windows" alt="Windows">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="MIT">
  <img src="https://img.shields.io/badge/PR-welcome-brightgreen" alt="PR welcome">
</p>

<h1 align="center">📝 BlogNotes</h1>
<p align="center"><b>本地管理小红书与知乎笔记</b></p>
<p align="center">编辑 · 图片处理 · 发布 · 批量队列 · 数据回导，全部在本地网页完成</p>

<p align="center">
  <a href="#-功能">功能</a> ·
  <a href="#-快速上手">快速上手</a> ·
  <a href="#-技术说明">技术说明</a>
</p>

---

## ✨ 功能

| 功能 | 说明 |
|------|------|
| 📝 **本地编辑** | 小红书（文案 + 编号图片）与知乎（Markdown + 图片）统一管理，网页内编辑、实时预览 |
| 🖼️ **图片处理** | 单图转换 PNG、批量上传、排序；知乎图片走官方上传组件 |
| 🚀 **发布队列** | 双平台串行发布：默认滚动 24 小时 6 篇、间隔 ≥60 分钟；结果不明保持待确认，**绝不自动重发** |
| 📥 **数据回导** | 已发布作品可导回本地（正文 + 平铺图片），已发布旧笔记显示「未导入」，一键导入 |
| 📊 **互动统计** | 阅读 / 点赞 / 收藏等指标按采样入库，单篇 SVG 趋势曲线；评论现场只读抓取 |

---

## 🚀 快速上手

**Windows：**

```bash
git clone <你的仓库地址>
cd BlogNotes
run.bat          # 直接双击启动！
```

首次启动自动创建 `runtime/.venv` 并安装依赖，随后自动打开网页 `http://127.0.0.1:8767/`；再次双击会先停掉旧服务再启动新代码。

- **小红书**：点「账号 → 登录」，在独立 Chrome/Edge 窗口扫码；登录后自动同步资料、作品与互动数据。
- **知乎**：在「账号」页填入 App Key / App Secret / Access Secret（申请入口见页面「帮助」），保存后点「刷新」验证。

内容规范：

- `xhs/<笔记名>/文案.txt`：正文 + 末尾 hashtag 行（发布时自动拆成可点击话题标签）
- `zhihu/<笔记名>/文案.md`：Markdown 正文 + 编号图片（`![说明](01.png)`）

首次启动需要 **Python 3.10+**（推荐 3.11/3.12），知乎图片上传还需 **Git**。发布、同步、诊断等命令行统一在 `src/cli.py`，详见页面与源码。

---

## 🛠️ 技术说明

- **Python 3.10+ / FastAPI + Uvicorn**：本地单进程 Web 服务，无构建步骤、无云端依赖
- **小红书**：Playwright + CDP 操作独立浏览器 Profile；**知乎**：官方 Publisher OpenAPI（HMAC-SHA256 签名）+ 数据开放平台
- **数据只在本机**：SQLite 存发布状态与统计快照，`runtime/` 存登录 Profile、密钥与日志，全部已 gitignore
- **前端**：原生 HTML/CSS/JS 单页，无外部 CDN

技术栈明细、经验证的边界与隐私注意见 [技术说明文档](config/docs/TECHNICAL.md)。

---

## 🤝 贡献

欢迎提 Issue 和 PR！如果你有想法，欢迎来聊：

- 小红书 / 知乎接口变化导致的兼容问题？
- 批量发布策略的改进想法？

---

<p align="center">
  <b>如果这个项目对你有帮助，欢迎 ⭐ Star ⭐ 让更多人看到</b>
</p>
