# MySekaiStoryteller AstrBot 插件

[MySekaiStoryteller-API](https://github.com/yonglanws/MySekaiStoryteller-API)（无头纯 API 的
Project SEKAI 风格 Live2D 视频渲染框架）的官方配套插件：接收 QQ/Telegram 用户消息 →
调用 LLM 生成符合规范的 JSON 剧本 → 交给
[MySekaiStoryteller-API 渲染宿主](https://github.com/yonglanws/MySekaiStoryteller-API)
渲染成 Live2D 视频 → 自动回传到群里。与渲染宿主通过 HTTP API 交互，零配置兼容。

## 功能特性

- **两种创作模式**
  - `/视频对话`：AI 根据消息内容自动选择角色出演，生成短视频回复（带 30 分钟持久化聊天历史）
  - `/剧本生成`：根据场景描述生成多角色完整剧本，渲染为完整视频
- **角色人格自定义**：在插件配置中为每个角色绑定整段人设提示词，聊天与剧本模式共用；
  未配置的角色不注入任何内置人设，由 AI 依据角色名合理演绎（详见下文「角色人格配置」）
- **资源目录动态感知**：从渲染宿主 `GET /api/v1/resources` 拉取角色/动作/表情/背景清单，
  提示词中的角色对照表与校验白名单**自动跟随宿主资源变化**（构建 prompt 前自动刷新，
  5 分钟 TTL）——新增模型只需在宿主 `resources/models/models.yaml` 登记；新增背景在
  `resources/images/images.yaml` 写画面描述，由 AI 按剧情自行选图，无需改插件
- **结构化输出保障**：优先走 OpenAI 兼容 `json_object` 响应格式；JSON 提取失败或校验失败时，
  把错误信息拼回 prompt 让 AI 自我修正（最多重试 2 次）
- **资源白名单校验**：LLM 输出的模型路径、动作（motion）、表情（facial）、背景图逐项对照
  资源目录校验，非法值自动回退该角色默认值，杜绝渲染报错
- **队列与公平调度**：任务入队排队（容量 20），聊天任务优先于剧本任务；同一用户每轮只执行
  一个任务，防止刷屏；超时自动取消，失败自动重试（最多 2 次）。并发入队、
  重试与满队列拒绝均有测试覆盖
- **错误反馈**：群内只回简短可读原因（超时 / 排队已满 / 剧本解析失败 / 渲染服务不可用），
  堆栈和本地路径不会发给用户
- **健壮的视频回传**：本地文件直发 → 多变体 URL → AstrBot 文件服务 token 等五重降级策略，
  兼容 Docker/NAT 等复杂网络环境
- **运维能力**：导出统计、队列查看、任务取消、资源查看、临时文件自动清理、维护模式开关（`/mssadmin` 仅管理员可用）

## 系统要求

- AstrBot v4.10.4+（人格列表配置使用 `template_list` 类型）
- Python 3.10+
- 已在 AstrBot WebUI 配置好的 LLM 对话模型（无需单独的 API Key）
- 可访问的 MySekaiStoryteller-API 渲染宿主（部署方式见
  [主仓库 README](https://github.com/yonglanws/MySekaiStoryteller-API#快速开始) 与
  [docs/host-deployment.md](https://github.com/yonglanws/MySekaiStoryteller-API/blob/main/docs/host-deployment.md)）

## 安装步骤

```bash
# 1. 克隆插件到 AstrBot 插件目录（目录名保持 astrbot_plugin_msst）
git clone https://github.com/yonglanws/astrbot_plugin_msst AstrBot/data/plugins/astrbot_plugin_msst

# 2. 安装依赖（仅 httpx）
cd AstrBot/data/plugins/astrbot_plugin_msst
pip install -r requirements.txt

# 3. 在 AstrBot WebUI 的插件管理中点击"重载插件"
```

## 指令一览

### 创作指令

| 指令              | 别名                                     | 说明                              | 示例                       |
| --------------- | -------------------------------------- | ------------------------------- | ------------------------ |
| `/视频对话 <消息>`     | `/视频生成` `/视频聊天`                        | AI 选角生成短视频回复                   | `/视频对话 你好`                |
| `/剧本生成 <场景>`     | `/剧本对话` `/故事生成` `/生成剧本` `/生成故事` `/story` | 生成完整剧本视频                       | `/剧本生成 深夜在Nightcord`      |
| `/测试视频对话 <消息>`   | `/测试视频生成` `/测试视频聊天`                     | 同视频对话（不受维护模式限制）                 | `/测试视频对话 你好`              |
| `/测试剧本生成 <场景>`   | `/测试故事生成` `/测试生成剧本` `/测试story`          | 同剧本生成（不受维护模式限制）                 | `/测试剧本生成 放学后的教室`          |
| `/统计`           | `/stats` `/统计信息` `/导出统计`                | 查看导出统计                          | `/统计`                     |

### 管理指令（`/mssadmin` 组，仅管理员）

| 指令                          | 别名              | 说明          | 示例                            |
| --------------------------- | --------------- | ----------- | ----------------------------- |
| `/mssadmin status`          | `/状态` `/系统状态`    | 插件与 API 连接状态 | `/mssadmin status`            |
| `/mssadmin queue`           | `/队列` `/排队`      | 查看队列详情       | `/mssadmin queue`             |
| `/mssadmin cancel <任务ID>`   | `/取消` `/终止` `/停止` | 取消排队中的任务     | `/mssadmin cancel 123`        |
| `/mssadmin resources`       | `/资源列表` `/模型列表`  | 查看宿主可用角色/背景/BGM | `/mssadmin resources`         |
| `/mssadmin cleanup`         | `/清理` `/清理文件`    | 清理临时文件       | `/mssadmin cleanup`           |
| `/mssadmin setapi <URL>`    | -               | 设置渲染宿主 API 地址 | `/mssadmin setapi http://...` |

## 配置项

在 AstrBot WebUI 的插件配置页修改：

| 配置项                    | 说明                                      | 默认值                   |
| ---------------------- | --------------------------------------- | --------------------- |
| `llm_provider_id`      | 用于生成剧本的 LLM 提供商（留空用当前默认提供商）             | 空                     |
| `mss_api_url`          | 渲染宿主 API 地址（同机 `127.0.0.1:9881`，跨机填对方 IP） | `http://127.0.0.1:9881` |
| `export_timeout`       | 视频导出超时（秒）                               | `600`                 |
| `max_concurrent_exports` | 最大并发导出数                                | `1`                   |
| `temp_dir`             | 临时文件目录（视频/剧本产物，留空用插件数据目录）                | 空                     |
| `callback_api_base`    | AstrBot 文件服务的外部可达地址（`http://<外部IP>:<端口>`），用于视频回传；留空自动探测 | 空 |
| `test_mode`            | 维护模式：正式指令提示维护中，仅 `/测试*` 指令可用              | `false`               |
| `personas`             | 角色人格列表：每条绑定一个角色并配置整段人设提示词（详见下文「角色人格配置」） | `[]`                  |

> **提示词**：剧本生成与对话的 prompt 骨架内置于 `main.py`（模型对照表、开场/退场序列规范、
> 可用动作/表情清单）。对话多少、每句长短由角色人设自行决定，不额外规定条数或字数；角色人设部分来自
> 「角色人格配置」，未配置的角色由 AI 合理演绎，无需外部模板文件。

## 角色人格配置

在插件配置页的「角色人格列表」中添加条目，为角色绑定自定义人设：

| 字段              | 说明                                                    |
| ---------------- | ------------------------------------------------------ |
| `character_name` | 绑定的角色名，与渲染宿主 `resources/models/models.yaml` 的 `name`（如 `晓山瑞希`）或 `shortName`（如 `瑞希`）一致 |
| `prompt`         | 该角色的整段人设提示词：外貌、性格、说话风格、口头禅、禁忌、表情倾向等，原文注入生成模板 |

绑定规则：

- **聊天与剧本模式共用**同一份人格配置；`/视频对话` 与 `/剧本生成` 都由 AI 根据用户内容从人格池中选择角色（强互动场景可选 2 个角色）。
- 未配置人格的角色**不注入任何内置人设**，由 AI 依据角色名合理演绎——插件开箱即可用，配置人格是可选增强。
- 绑定的角色名必须能在宿主资源目录中找到（全名或短名均可），否则该条配置不生效并在插件日志中告警。
- TTS 音色与人格配置无关，仍在渲染宿主 `config.yaml` 的 `tts.characters` 中按角色名配置。
- 修改配置后在 WebUI 重载插件生效；角色名可用 `/mssadmin resources` 核对。

## 工作流程

```
用户发送 /剧本生成 <场景>
    ↓
前置检查（LLM 提供商 + 渲染宿主健康检查）
    ↓
任务入队（聊天优先于剧本，同用户公平调度）→ 立即回复排队位置与预计等待
    ↓
后台 worker：LLM 生成 JSON 剧本 → 提取/校验/自动修复 → 补译 TTS 文本
    ↓
POST /api/v1/export → 渲染宿主渲染 Live2D 并导出 MP4
    ↓
	下载视频后直接发送原片（不再二次压缩）
    ↓
五重降级发送到群（本地文件 → URL → 文件服务 token → …）
    ↓
回执：耗时 / 时长 / 大小；统计入库；临时文件定期清理
```

## 测试（可选）

提示词构建、人格绑定、队列并发与错误反馈测试无需 AstrBot 运行时：

```bash
# 离线模式（内置模拟目录，无需宿主）
python scripts/test-prompt-build.py --offline

# 对接真实渲染宿主
python scripts/test-prompt-build.py [宿主地址，默认 http://127.0.0.1:9881]
```

## 故障排除

### LLM 调用失败

1. 确认 AstrBot WebUI 已配置对话模型提供商
2. `/mssadmin status` 查看 LLM 提供商状态
3. 检查 AstrBot 日志中的详细错误

### 视频导出失败

1. 确认渲染宿主正在运行：`curl http://<宿主IP>:9881/api/v1/health`
2. `/mssadmin status` 确认 MSS API 连接状态
3. 跨设备时检查防火墙放行 9881 端口、`mss_api_url` 填写正确 IP
4. 超时类失败可调大 `export_timeout`，或检查渲染宿主 GPU 状态（health 的 `renderPool`）
5. 群里若提示「渲染服务正忙」，说明宿主达到并发上限，稍后重试或增大宿主 `render.workers`

### 视频发送失败

1. 配置 `callback_api_base` 为外部可达地址（Docker 部署 AstrBot 时常见）
2. 确认 AstrBot WebUI 的文件服务端口从机器人侧可达
3. 查看宿主日志中五种发送策略的逐级尝试记录

## 项目结构

```
astrbot_plugin_msst/
├── main.py              # 插件主文件（指令、LLM 编排、发送策略、统计与清理）
├── persona.py           # 人格配置解析与角色绑定（自定义人设提示词，纯逻辑可独立测试）
├── resource_catalog.py  # 资源目录客户端（角色/动作/表情/背景动态感知，纯逻辑可独立测试）
├── queue_manager.py     # 队列管理（并发控制、公平调度、超时重试）
├── metadata.yaml        # 插件元数据
├── _conf_schema.json    # 配置模式定义
├── requirements.txt     # Python 依赖（httpx）
├── scripts/             # test-prompt-build.py 提示词构建端到端测试
└── README.md            # 本文档
```

## 新增角色 / 资源

角色的模型路径、动作、表情、可用背景全部由渲染宿主的资源目录动态提供——**新增模型只需在宿主
`resources/models/models.yaml` 登记，插件自动感知，无需改插件代码**。完整步骤见
[主仓库 README 的「资源导入指南」](https://github.com/yonglanws/MySekaiStoryteller-API#资源导入指南)，
或直接发 `/mssadmin resources` 查看当前宿主已识别的资源。

## 许可证

本项目以 [GNU GPL v3](LICENSE) 许可证开源，与其配合的
[MySekaiStoryteller-API](https://github.com/yonglanws/MySekaiStoryteller-API) 同许可证开源。
