# 短信识别台账机器人

上传手机短信截图到 Telegram 机器人，自动用 PaddleOCR 识别并抽取关键字段（发件号码、运营商、正文号码、信息内容、短信时间、网址），存入 SQLite，并在本地网页上以可搜索、可筛选、可导出的表格呈现。

## 功能

- **Bot**：收到短信截图 → OCR 识别 → 抽取字段 → 入库 → 回复摘要。
- **网页**（`http://localhost:8000`）：表格浏览 + 关键词搜索 + 运营商筛选 + 时间范围筛选 + 分页。
- **导出**：CSV（带 BOM，Excel 可直接打开）与 Excel（.xlsx）。
- **多人使用**：默认允许所有人；设置 `ALLOWED_USER_IDS` 白名单后仅白名单可用。

## 快速开始（Docker）

1. 准备 Bot Token：在 Telegram 找 [@BotFather](https://t.me/BotFather) 创建机器人，拿到 Token。
2. 配置环境变量：
   ```bash
   cp .env.example .env
   # 编辑 .env，填入 BOT_TOKEN；可选填 ALLOWED_USER_IDS（逗号分隔）
   ```
3. 启动：
   ```bash
   docker compose up --build
   ```
4. 给机器人发一张短信截图，收到识别摘要。
5. 浏览器打开 `http://localhost:8000` 查看台账、筛选、导出。

> 首次构建镜像约 2~4GB（PaddlePaddle 体积大），首次识别会下载模型（已挂载到 `./paddleocr_models` 缓存，只需一次）。

## 本地开发（不用 Docker）

```bash
pip install -r requirements.txt
python parser.py          # 跑字段抽取自检（无需模型）
python main.py            # 启动 bot + web
```

## 字段抽取说明

字段抽取是启发式规则（`parser.py`），依赖短信截图的版式：

| 字段 | 规则 |
|------|------|
| 发件号码 | 顶部第一条短文本（去掉行尾时间戳） |
| 运营商 | 短号映射（10086/10010/10000）+ 关键词（移动/联通/电信），否则"其他" |
| 短信时间 | 多组正则按序尝试，解析失败留空（网页回退显示上传时间） |
| 正文号码 | 正则 `1[3-9]\d{9}`，多个去重 |
| 网址 | http/www/裸域名，独立一列，正文保留全文 |

**建议用真实截图校准**：`raw_ocr` 列保存了 OCR 原始全文，便于核对与调整规则。运营商号段精确归属暂未接入（短号 + 关键词足够时不必加）。

## 目录结构

```
main.py        入口（bot + web 并行）
config.py      配置（.env）
bot.py         aiogram 机器人
ocr.py         PaddleOCR 封装
parser.py      字段抽取
db.py          SQLite 存储
web.py         FastAPI 网页 + 导出
templates/     网页模板
data/          运行时生成：bot.db + images/
```

## 环境变量

| 变量 | 说明 | 默认 |
|------|------|------|
| `BOT_TOKEN` | Telegram 机器人 Token | 空（不启动 bot） |
| `ALLOWED_USER_IDS` | 白名单用户 ID，逗号分隔 | 空 = 允许所有人 |
| `WEB_PORT` | 网页端口 | 8000 |
