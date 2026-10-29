# 佳明训练与身体状况数据 · AstrBot 插件

通过 **Garmin Connect 官方 Personal API** 只读查询佳明手表同步到云端的训练与健康数据，
并支持每日训练恢复推送与每周训练周报。

支持 **中国大陆站**（`personal.connectapi.garmin.cn`）与 **国际站**（`personal.connectapi.garmin.com`）。

> 插件只读：只申请读权限，不会修改、删除或上传任何佳明数据。

## 功能

### 9 个 LLM 工具

| 工具名 | 能力 |
|---|---|
| `garmin_activities` | 训练活动列表（日期范围 + 条数），含时长/距离/配速/心率/步频/训练效果/训练负荷 |
| `garmin_activity_detail` | 单次活动详情 + 分段摘要（按 `splitType` 分组） |
| `garmin_body_composition` | 体重、BMI、体脂率、体水分、肌肉量、骨量、内脏脂肪、代谢年龄 |
| `garmin_health` | 睡眠（评分/各阶段/睡眠心率）、心率、HRV（含状态）、压力、Body Battery、血氧、每日汇总 |
| `garmin_daily_summary` | 步数、步数目标、活动热量、静息心率、中/高强度分钟 |
| `garmin_pace_hr_analysis` | 配速分布、心率区间（Z1–Z5）时间占比、每周里程、最快/最长一次 |
| `garmin_week_report` | 最近 7 天训练 + 恢复周报（跑量、配速、负荷、睡眠、HRV、静息心率、步数） |
| `garmin_fitness_age` | 健身年龄及构成要素（静息心率、BMI、剧烈运动分钟等） |
| `garmin_profile` | 账号资料（可用于自检 Key 是否有效） |

### 定时推送

- **每日推送**：昨天训练 + 今晨睡眠/HRV/静息心率 + 今日训练建议
- **每周周报**：周跑量、平均配速、心率区间结构、睡眠与 HRV 趋势、静息心率变化

推送在插件进程内用 `asyncio` 调度，到点直接调用 `context.send_message` 发送，
**不依赖模型生成**，也不额外消耗 LLM 额度。

## 安装

### 方式一：WebUI 安装（推荐）

1. 把本仓库放到 `AstrBot/data/plugins/` 下，或通过 WebUI 的插件安装功能从 GitHub 安装
2. 重启或在 WebUI 插件页点「重载」
3. 在插件设置里填写 `api_key` 与 `region`

### 方式二：手动复制

```bash
cd AstrBot/data/plugins
git clone https://github.com/<your-name>/Garmin-date-transmission.git astrbot_plugin_garmin
```

依赖：`httpx`、`docstring-parser`（见 `requirements.txt`，AstrBot 自带 httpx 0.28+）。
插件目录名建议保持 `astrbot_plugin_garmin`，与 `metadata.yaml` 的 `name` 一致。

## 获取 API Key

1. 登录 `connect.garmin.cn`（国际站为 `connect.garmin.com`）
2. 进入 **设置 → Personal API Key**
3. 创建 Key，选择有效期（30/90/180 天或 1 年）
4. **Key 只显示一次**，请立即保存；过期需重新生成

> 大陆站的 Key 只能用于 `.cn` 端点，国际站的 Key 只能用于 `.com` 端点，混用会返回 401。

## 配置项

| 配置项 | 说明 | 默认 |
|---|---|---|
| `api_key` | 佳明 Personal API Key | 空 |
| `region` | `cn`（中国大陆站）/ `global`（国际站） | `cn` |
| `push_enabled` | 开启每日推送 | `false` |
| `push_time` | 每日推送时间 `HH:MM`（主机本地时间） | `08:00` |
| `weekly_enabled` | 开启每周周报 | `false` |
| `weekly_weekday` | 周报星期，`1`=周一 … `7`=周日 | `1` |
| `weekly_time` | 周报时间 `HH:MM` | `08:30` |
| `push_umo` | 推送目标会话 `platform_id:message_type:session_id` | 空 |

配置文件：`AstrBot/data/config/astrbot_plugin_garmin_config.json`
必须是**无 BOM 的 UTF-8**，否则 AstrBot 读取时会 JSON 解析失败。

### 如何找到 `push_umo`

`push_umo` 是 AstrBot 的 unified_msg_origin，格式为 `平台ID:消息类型:会话ID`，例如：

```
aiocqhttp:FriendMessage:123456789
```

可以在 AstrBot 日志里看到（形如 `[default] [aiocqhttp] 昵称/QQ号: 消息内容` 的来源会话），
也可以在 `data/data_v4.db` 的 `conversations` 表 `user_id` 列里找到。

## 推送样例

每日推送：

```
佳明每日推送 · 6月18日
昨天：running 10.02 km@5'12"/km 心率152
今晨：睡眠 82 分 / 7小时41分 / HRV 68（平衡） / 静息心率 48
建议：恢复状态不错，可以安排质量课（节奏跑或间歇）。
```

每周周报：

```
2026-06-12 ~ 2026-06-18 佳明周报
训练：4 次（跑步 4 次），共 42.6 km，3小时58分，训练负荷合计 612
跑步：4 次 / 42.6 km，平均配速 5'35"/km
最长一次：2026-06-15 21.10 km
平均有氧训练效果：3.2（最高 4.1）
睡眠：平均评分 81，平均时长 7小时36分（7 天有数据）
HRV：夜间均值 66（平衡 6 天，不平衡 1 天）
静息心率：均值 49 bpm（最低 47，最高 52）
步数：日均 12480 步（7 天有数据）
强度结构：高强度课 1 次 / 共 4 次
```

## 使用示例

在人格能力中勾选本插件的工具后，可以直接用自然语言提问：

- 「我最近一周跑了哪些？配速多少？」
- 「分析下我 10 月的配速和心率区间分布」
- 「生成上周训练周报」
- 「看看我昨天的睡眠和 HRV」
- 「我这个月静息心率趋势怎么样？」

## 实测发现（2026-10-29，中国大陆站真实账号）

这些是官方文档没写、但对接时一定会踩到的点：

1. **token 端点返回 `text/plain` 的 JWT 字符串**，不是 JSON；
   并且请求头**不能**带 `Accept: application/json`，否则返回 `HTTP 406 NotAcceptableException`。
2. **`/activity-service/activity/{id}/splits` 返回 404**，逐圈数据不可用，
   详情里只有 `splitSummaries`（按分型汇总，且常重复，本插件已做去重）。
3. **Personal API 不提供**：训练状态（Training Status）、训练准备度、VO2max 时间序列、比赛预测——均 404。
4. 字段结构：睡眠是**扁平**结构（不是 `dailySleepDTO` 嵌套）；压力在 `values.overallStressLevel`；
   血氧是 `averageSpO2`/`lowestSpO2`；每日汇总在 `data[0].movement.steps.value` 等多层嵌套里；
   心率区间 `hrTimeInZone_1..5` 单位是**秒**。
5. **AstrBot 自带 cron（`future_task`）不适合做推送**：它通过「唤醒 Agent」执行，
   实测在工具调用完成后链路中断（数据拿到了但回复没发出）。故本插件自建 `asyncio` 调度器。

其他限制：

- **身体成分**需要佳明 Index 体重秤或手动录入体重；手表本身不测体重
- **血氧**需要设备支持（Forerunner 255 全系不支持，接口返回空值）
- token 有效期 24 小时，插件自动缓存并续期，不需要密码

## 故障排查

| 现象 | 原因与处理 |
|---|---|
| `HTTP 401` 换 token 失败 | Key 无效/过期，或 Key 与 `region` 不匹配 |
| `HTTP 406 NotAcceptableException` | 请求带了 `Accept: application/json`，token 端点不接受 |
| `HTTP 526` | 大陆 Personal API 端点短暂不可用，稍后重试 |
| 返回「未配置佳明 Personal API Key」 | 配置未保存，或配置文件带 BOM |
| 模型不调用工具 | 人格能力里未勾选插件，或模型不支持 function calling |
| 推送没收到 | 检查 `push_enabled`、`push_umo`；看 AstrBot 日志里的 `[garmin]` 记录 |

## 自检脚本

不启动 AstrBot 也能验证 Key：

```bash
pip install httpx
export GARMIN_API_KEY="你的Key"      # Windows: $env:GARMIN_API_KEY="..."
export GARMIN_REGION="cn"
python selftest.py
```

依次检查换 token、账号资料、活动、身体成分、睡眠、心率、HRV、压力、Body Battery、血氧、每日汇总、健身年龄共 12 项。

## 开发

```bash
python -m pip install -r requirements.txt
python -m ruff check .
```

## 免责声明

个人项目，与 Garmin 官方无关。Personal API 的行为与字段可能随时变化。
请妥善保管 API Key，不要提交到仓库或分享给他人。

## License

MIT
