<div align="center">

# astrbot_plugin_msst

[MySekaiStoryteller-API](https://github.com/yonglanws/MySekaiStoryteller-API) 官方 AstrBot 插件——
AI 生成符合规范的剧本，交给渲染宿主导出 Project SEKAI 风格 Live2D 视频，自动回传到 QQ / Telegram

<a href="#简介">简介</a> ·
<a href="#快速开始">快速开始</a> ·
<a href="#指令">指令</a> ·
<a href="#配置">配置</a> ·
<a href="#角色人格配置">角色人格配置</a> ·
<a href="#故障排除">故障排除</a>

</div>

## 简介

接收 `/视频对话`、`/剧本生成` 等指令后，插件调用 AstrBot 已配置的 LLM 生成 JSON 剧本，
经资源白名单校验与自动修复后提交渲染宿主渲染导出，成品视频回传到会话。
与渲染宿主通过 HTTP API 交互，宿主资源变化零配置跟随。

- **两种创作模式**：`/视频对话` 由 AI 选角生成短视频回复（带会话级聊天历史，超时自动清理）；`/剧本生成` 按场景生成约 3 分钟的多角色完整短剧
- **角色人格自定义**：按角色绑定整段人设提示词，聊天与剧本模式共用；未配置的角色不注入任何内置人设，由 AI 依角色名合理演绎
- **资源目录动态感知**：角色 / 动作 / 表情 / 背景清单来自宿主 `GET /api/v1/resources`（5 分钟 TTL），提示词对照表与校验白名单自动跟随宿主资源变化
- **结构化输出保障**：LLM 调用带异常分类与退避重试；JSON 提取 / 校验失败时带上轮原文定向修正（最多 2 轮）；动作名写错按资源目录自动纠正，修不了只丢弃该处表演、保留台词，不整场重试；剧本阶段整体受 `script_timeout` 墙钟约束，到点中止并提示
- **时序表演与听者反应**：`Talk.data.actions` 在台词内编排说话者 / 听者的动作与表情，句间用 `Motion.data.actions` 续演静默反应；入场退场为画外滑入滑出，自动避开站姿
- **流水线与队列**：剧本（LLM）与导出分开限流，剧本不占导出队列位置；导出任务排队（容量 20），默认 2 路并发，同用户公平调度；超时只从实际渲染起计，到点进入收尾宽限——渲染 / 下载仍在推进就继续等，彻底无进展才取消
- **失败重试**：瞬时故障（服务正忙、HTTP 5xx、连接类）自动重试最多 2 次；任务自身超时与下载终败不重试，直接反馈原因
- **健壮回传**：视频流式下载并按字节上报进度；本地文件直发 → 多变体 URL → 文件服务 token 五重降级，兼容 Docker / NAT 网络
- **错误反馈**：群内只回简短可读原因，堆栈与本地路径不出机器人；重复投递的同一消息事件自动去重
- **运维能力**：导出统计、队列查看、任务取消、资源查看、过期文件清理、维护模式开关（`/mssadmin` 仅管理员）

## 快速开始

> [!IMPORTANT]
> 需要一台已运行 [MySekaiStoryteller-API](https://github.com/yonglanws/MySekaiStoryteller-API) 渲染宿主。
> 部署与资源配置见[主仓库 README](https://github.com/yonglanws/MySekaiStoryteller-API#快速开始)。

- AstrBot v4.10.4+（人格列表配置使用 `template_list` 类型）
- Python 3.10+
- 已在 AstrBot WebUI 配置好的 LLM 对话模型（无需单独 API Key）

```bash
# 在 AstrBot 根目录执行；目录名必须保持 astrbot_plugin_msst
git clone https://github.com/yonglanws/astrbot_plugin_msst data/plugins/astrbot_plugin_msst
pip install -r data/plugins/astrbot_plugin_msst/requirements.txt
# 在 AstrBot WebUI 的插件管理中点击「重载插件」
```

## 指令

### 创作指令

| 指令 | 别名 | 说明 | 示例 |
| --- | --- | --- | --- |
| `/视频对话 <消息>` | `/视频生成` `/视频聊天` | AI 选角生成短视频回复 | `/视频对话 你好` |
| `/剧本生成 <场景>` | `/剧本对话` `/故事生成` `/story` | 生成完整剧本视频 | `/剧本生成 深夜在Nightcord` |
| `/测试视频对话 <消息>` | `/测试视频生成` `/测试视频聊天` | 同视频对话（不受维护模式限制） | `/测试视频对话 你好` |
| `/测试剧本生成 <场景>` | `/测试故事生成` `/测试story` | 同剧本生成（不受维护模式限制） | `/测试剧本生成 放学后的教室` |
| `/统计` | `/stats` `/统计信息` | 查看导出统计 | `/统计` |

### 管理指令（`/mssadmin` 组，仅管理员）

| 指令 | 别名 | 说明 |
| --- | --- | --- |
| `/mssadmin status` | `/状态` `/系统状态` | 插件与渲染宿主连接状态 |
| `/mssadmin queue` | `/队列` `/排队` | 查看队列详情 |
| `/mssadmin cancel <任务ID>` | `/取消` `/终止` `/停止` | 取消指定任务 |
| `/mssadmin resources` | `/资源列表` `/模型列表` | 查看宿主可用角色 / 背景 / BGM |
| `/mssadmin cleanup` | `/清理` `/清理文件` | 清理超过 2 小时的产物文件 |
| `/mssadmin setapi <URL>` | - | 设置渲染宿主 API 地址 |

## 配置

在 AstrBot WebUI 的插件配置页修改：

| 配置项 | 说明 | 默认值 |
| --- | --- | --- |
| `llm_provider_id` | 用于生成剧本的 LLM 提供商（留空用当前默认提供商） | 空 |
| `mss_api_url` | 渲染宿主 API 地址（同机 `127.0.0.1:9881`，跨机填对方 IP） | `http://127.0.0.1:9881` |
| `export_timeout` | 视频导出超时（秒），从任务实际开始渲染起计 | `600` |
| `max_concurrent_exports` | 最大并发导出数（需与宿主 `render.workers` 对齐） | `2` |
| `script_max_concurrent` | 最大并发剧本生成数（LLM 阶段限流，与导出互不占位） | `2` |
| `script_timeout` | 剧本阶段整体墙钟上限（LLM 调用、校验重试、台词翻译），到点中止 | `1200` |
| `temp_dir` | 临时文件目录（视频 / 剧本产物，留空用插件数据目录） | 空 |
| `callback_api_base` | AstrBot 文件服务的外部可达地址，用于视频回传；留空自动探测 | 空 |
| `test_mode` | 维护模式：正式指令提示维护中，仅 `/测试*` 指令可用 | `false` |
| `personas` | 角色人格列表，每条绑定一个角色（见下文） | `[]` |

> [!NOTE]
> 提示词骨架内置于 `main.py`（角色对照表、登场退场规范、台词排版硬规则、动作表情清单）。
> `/剧本生成` 按约 3 分钟短戏收束，对话多少由角色人设把握；台词排版另有程序化兜底
> （折叠连续换行、按 26 字宽硬折行、超 3 行的 Talk 自动拆条），只排版不删减台词。

### 表演序列与舞台约定

- `Talk.data.actions` / `Motion.data.actions` 至多 8 项，每项为
  `{"at": 0.4, "modelId": 角色整数ID, "motion": "完整动作名"}`，motion / facial 至少一项；
  `at` 是该片段时长的比例（0..1），**不是秒**。动作 / 表情名必须属于该角色资源目录。
- 单句配额由校验器硬执行：说话者每句至多 5 处变化、听者至多 1 处、同角色相邻变化间隔 ≥ 0.2；
  超出直接裁剪，不触发整场重试。名称非法时先按资源目录自动纠正（大小写 / 空白 / 尾数 / 唯一前缀），
  修不了丢弃该字段并保留兄弟字段。
- 入场退场：`LayoutAppear` / `LayoutClear` 由校验器改写为同侧画外滑入 / 滑出（offset ±100），
  入场退场动作缺省或为站姿时自动改选非站姿动作；单人用 Center，双人用 Left / Right 且必须占用不同槽位。
- Talk 实际时长由宿主结合语音解析；旁白（`modelId: -1`）不播动作。独立 `Motion` 只用于句间静默续演，
  带 actions 时 `0 < duration <= 120` 秒且 `wait: true`。
- 超过 3 行的 Talk 自动拆成同角色连续多条：台词一字不删，actions 按时长比例重映射到各条。

## 角色人格配置

在插件配置页的「角色人格列表」中添加条目：

| 字段 | 说明 |
| --- | --- |
| `character_name` | 绑定的角色名，与宿主 `resources/models/models.yaml` 的 `name`（如 `晓山瑞希`）或 `shortName`（如 `瑞希`）一致 |
| `prompt` | 该角色的整段人设提示词：外貌、性格、说话风格、口头禅、禁忌、表情倾向等，原文注入生成模板 |

- **聊天与剧本模式共用**同一份人格配置，由 AI 根据用户内容从人格池中选角。
- 未配置人格的角色不注入任何内置人设，开箱即可用，配置人格是可选增强。
- 绑定的角色名必须能在宿主资源目录中找到（全名或短名均可），否则该条不生效并在日志告警。
- TTS 音色与人格配置无关，仍在宿主 `config.yaml` 的 `tts.characters` 中按角色名配置。
- 修改配置后在 WebUI 重载插件生效；可用角色名发 `/mssadmin resources` 核对。

## 工作流程

```
用户发送 /剧本生成 <场景>
    ↓
前置检查（LLM 提供商 + 渲染宿主健康 + 渲染队列容量）→ 立即回复「视频生成中」
    ↓
后台流水线（同会话串行 + 全局并发闸 + script_timeout 墙钟）：
    LLM 生成 JSON 剧本 → 提取 / 校验 / 自动修复 → 补译 TTS 文本
    ↓
入导出队列（聊天优先于剧本）→ POST /api/v1/export 渲染导出 MP4
    排队不计时；渲染 / 下载有进度则宽限顺延；瞬时失败自动重试
    ↓
流式下载视频 → 五重降级发送到群（本地文件 → URL 变体 → 文件服务 token）
    ↓
回执耗时 / 大小；统计入库；临时文件定期清理
```

## 故障排除

### 剧本生成失败

1. 确认 AstrBot WebUI 已配置对话模型提供商，`/mssadmin status` 查看状态
2. 群内提示带 `[模型]` 归类原因时，按提示检查对应模型通道 / 中转配置
3. 反复失败可查看 AstrBot 日志中的详细堆栈

### 视频导出失败

1. 确认渲染宿主在运行：`curl http://<宿主IP>:9881/api/v1/health`
2. 跨设备时检查防火墙放行 9881 端口、`mss_api_url` 填写正确 IP
3. 提示「渲染服务正忙」说明宿主并发已满，稍后自动重试或增大宿主 `render.workers`
4. 长剧本频繁超时可调大 `export_timeout`（渲染）与 `script_timeout`（生成）

### 视频发送失败

1. Docker 部署 AstrBot 时配置 `callback_api_base` 为外部可达地址
2. 确认 AstrBot 文件服务端口从机器人侧可达
3. 查看插件日志中五种发送策略的逐级尝试记录

## 项目结构

```
astrbot_plugin_msst/
├── main.py              # 插件主文件（指令、LLM 编排、校验修复、发送策略、统计与清理）
├── persona.py           # 人格配置解析与角色绑定
├── resource_catalog.py  # 资源目录客户端（动态感知、名称自动纠正）
├── queue_manager.py     # 导出队列（并发控制、公平调度、宽限与重试）
├── metadata.yaml        # 插件元数据
├── _conf_schema.json    # 配置模式定义
└── requirements.txt     # Python 依赖（httpx）
```

## 相关内容

- [MySekaiStoryteller-API](https://github.com/yonglanws/MySekaiStoryteller-API) —— 渲染宿主：无头纯 API 的 Live2D 视频渲染框架
- [故事文件格式](https://github.com/yonglanws/MySekaiStoryteller-API/blob/main/doc/story-format.md) —— 插件产出剧本的目标格式
- [资源与音频配置](https://github.com/yonglanws/MySekaiStoryteller-API/blob/main/doc/resources.md) —— 新增角色 / 背景 / TTS / BGM

## 许可证

本项目以 [GNU GPL v3](LICENSE) 许可证开源，与其配合的
[MySekaiStoryteller-API](https://github.com/yonglanws/MySekaiStoryteller-API) 同许可证开源。
