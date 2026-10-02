# 点歌插件（music-request）

MaiBot 插件：搜索点歌、解析音乐链接与分享卡片，把歌曲以**音乐卡片**或**语音音频**发到聊天里。
支持网易云音乐（163）与 QQ音乐（qq）双平台。

> 参考实现：[pan-ice/maibot-music](https://github.com/pan-ice/maibot-music)（v1.4.6）。
> 本插件按自己的代码规范重写，接口协议与踩坑结论沿用，结构做了拆分与收敛。

## 功能

- **命令点歌**：`/点歌 <歌曲名>`，可用 `/点歌 163 <歌名>` / `/点歌 qq <歌名>` 指定平台。
- **交互选歌**：多首结果时列出候选，用 `/选歌 <序号>` 选一首；也可配置直接发第一首。
- **LLM 点歌**：`search_and_play_music` 工具，自然语言触发（"放首歌"、"点首晴天"），
  直接发最佳匹配；默认平台失败会自动换另一个平台并逐首尝试候选。
- **链接解析**：自动识别消息里的网易云 / QQ音乐链接，命中即发送并拦下这条消息。
- **卡片解析**：识别音乐分享卡片（`[QQ音乐] 歌名 - 歌手`、`[小程序] QQ音乐：…`、
  "分享xxx的单曲《…》"等），配置了 NapCat HTTP API 时会回溯原始消息的 `json` 段
  按歌曲 ID 精确解析，拿不到才退化成按歌名搜索。
- **两种播放形态**：`card` 音乐卡片 / `voice` 语音音频（本地缓存或远程 URL）。
- **三种发送形态**：`card` 音乐卡片 / `voice` 语音音频 / `file` 音频文件（保留原始音质）；
  LLM 点歌时可用 `send_as` 参数二选一，默认发语音。
- **状态自检**：`/点歌状态` 一眼看清配置是否生效、平台可用性、缓存用量；
  `/点歌自检` 写探针文件核对缓存目录与 NapCat 是否指向同一份文件。
- **本地歌曲库**（v1.5.0）：直接播放磁盘上的音频文件（MP3/FLAC/M4A/APE 等），
  `/本地歌 <关键词>` 搜索、`/本地库` 查看状态；LLM 可经 `play_local_music` 工具点播本地歌。

## 安装

1. 把整个 `music-request/` 目录放进 MaiBot 的 `plugins/` 下。
2. 启动 / 重启 MaiBot，观察加载日志里的一行自检：
   `点歌插件已加载 version=1.5.0 enabled=True platform=163 mode=card …`
3. 在 WebUI（`http://127.0.0.1:8001`）插件管理中确认插件出现并启用，
   或在配置里设 `[plugin] enabled = true`。

`config.toml` 由 Runner 依据 `config_model` 自动生成，**不要手工提交**，且写入时不能带 UTF-8 BOM。

## 配置

| 配置节 | 字段 | 默认值 | 说明 |
|---|---|---|---|
| `[plugin]` | `enabled` | `true` | 是否启用插件 |
| `[plugin]` | `config_version` | `1.0.0` | 配置版本，勿手改 |
| `[music]` | `default_platform` | `"163"` | 默认平台：`163` / `qq` |
| `[music]` | `command_prefix` | `"/"` | 命令前缀，可改 `#`（全角 `／＃` 也会识别） |
| `[music]` | `search_limit` | `5` | 搜索结果数量上限 |
| `[music]` | `relevance_floor` | `0.6` | 候选相关度地板（相对最佳匹配的比例）。`1.0` = 只尝试最佳匹配，`0` = 不过滤 |
| `[music]` | `auto_select_first` | `false` | 多首结果时跳过选歌，直接发第一首 |
| `[music]` | `select_timeout_seconds` | `300` | 待选列表有效期（秒） |
| `[music]` | `auto_parse_url` | `true` | 自动解析消息里的音乐链接 |
| `[music]` | `auto_parse_card` | `true` | 自动解析音乐分享卡片 |
| `[music]` | `play_mode` | `"card"` | `card` 音乐卡片 / `voice` 语音音频 / `file` 音频文件 |
| `[music]` | `tool_default_mode` | `"voice"` | LLM 调点歌工具但没指定形态时用哪种：`voice`（默认）/ `file` |
| `[music]` | `voice_source` | `"local"` | voice 模式音频来源：`local` 本地缓存 / `remote` 远程 URL |
| `[netease]` | `music_u` | `""` | `MUSIC_U` Cookie，配了才能点 VIP / 高音质 |
| `[netease]` | `csrf_token` | `""` | `__csrf` Cookie，与 `music_u` 配对 |
| `[qq]` | `uin` | `""` | QQ音乐登录账号，**搜索必需** |
| `[qq]` | `qqmusic_key` | `""` | QQ音乐登录凭证，**搜索必需** |
| `[napcat]` | `http_url` | `""` | NapCat HTTP API 地址，留空则禁用直连通道 |
| `[napcat]` | `http_token` | `""` | NapCat 访问令牌，留空表示不鉴权 |
| `[cache]` | `storage_dir` | `""` | MaiBot 写缓存的目录，留空用插件数据目录下的 `audio_cache` |
| `[cache]` | `napcat_dir` | `""` | 同一目录在 NapCat 进程内的路径，留空同上 |
| `[cache]` | `max_size_mb` / `expire_hours` / `cleanup_interval_hours` | `1024` / `24` / `24` | 容量上限、过期阈值、清理间隔 |
| `[cache]` | `max_file_size_mb` / `download_timeout_seconds` | `50` / `30` | 单文件上限、下载超时 |
| `[library]` | `enabled` | `false` | 是否启用本地歌曲库 |
| `[library]` | `music_dir` | `""` | 歌曲库目录（MaiBot 侧，递归扫描音频文件） |
| `[library]` | `napcat_dir` | `""` | 同一目录在 NapCat 进程内的路径（Docker 挂载时与上面不同），留空同上 |
| `[library]` | `send_mode` | `"file"` | 本地歌曲发送形态：`file` 音频文件（保真）/ `voice` 语音（受 QQ 转码与 60 秒限制） |
| `[library]` | `tool_prefer_local` | `true` | LLM 点歌工具优先查本地库：强匹配直接发本地文件，未命中/发送失败再走平台 |
| `[library]` | `refresh_minutes` | `10` | 索引自动刷新间隔（分钟），`0` = 只在重启/`/本地库 刷新` 时重扫 |
| `[library]` | `search_limit` | `10` | 本地库搜索结果数量上限 |

### Cookie 怎么拿

| 平台 | 步骤 |
|---|---|
| 网易云 | 浏览器登录 music.163.com → F12 → Application → Cookies → 取 `MUSIC_U` 与 `__csrf` |
| QQ音乐 | 浏览器登录 y.qq.com → F12 → Application → Cookies → 取 `uin` 与 `qqmusic_key` |

可播放范围 = 该账号的会员 / 数字专辑 / 版权权限。登录态失效时 QQ音乐搜索会返回
"登录态失效或被拒绝"，需重新获取 Cookie。

### 播放模式

| 模式 | 行为 |
|---|---|
| `card` | 网易云走平台型 `music` 段（`type=163` + `id`），NapCat 负责解析音频与卡片；QQ音乐由插件自解析标题/歌手/封面/直链，拼成自定义卡片。网易云发卡不需要登录；QQ音乐搜索仍需登录态 |
| `voice` | 取音频直链后以语音消息发送。`voice_source=local` 时先下载 MP3 到本地共享缓存，把 NapCat 可见路径交给它；`remote` 则直接给远程 URL |
| `file` | 取音频直链（**不限 MP3**，优先无损/高码率）落本地缓存，再直连 NapCat `upload_group_file` / `upload_private_file` 发送。详见下文「发送形态：卡片 / 语音 / 文件」 |

**voice + local 的目录要求**：MaiBot 与 NapCat 必须能读到同一份文件。

- 同机非 Docker：`[cache] storage_dir` 与 `napcat_dir` 填同一个绝对路径。
- NapCat 在 Docker 里：给容器挂只读 volume，并把 `napcat_dir` 设成容器内路径。
- 跨主机：把 `voice_source` 改成 `remote`，不要指望本地路径。
- 缓存只接受校验通过的 MP3（FLAC/M4A 不会被改名成 `.mp3`），下载走 `.tmp` + 原子重命名。

### 音质：为什么语音消息听起来差

**根因是通道，不是源。** `voice` 模式发的是 QQ **语音消息**（平台 `record` 段），
QQ 对语音消息强制转码为 SILK/AMR（约 6–12 kbps、单声道），并且**限制 60 秒**。
插件送进去的是 320k MP3 还是 FLAC，用户听到的都是转码后的结果——这是平台侧限制，
插件无法绕过。

| 发送形态 | 音质 | 代价 |
|---|---|---|
| `card` 音乐卡片 | **最好**：点开由 QQ音乐 / 网易云官方播放器播放，音质取决于收听者账号权益（可到无损） | 需要点一下卡片 |
| 文件发送 | 完整保留源文件（320k / 无损） | 需下载后用播放器播放；本插件暂未实现 |
| `voice` 语音消息 | **最差**：SILK ≈ 6–12 kbps，且超过 60 秒会被截断 | 点开即播，无额外操作 |

**所以想提高音质就把 `[music] play_mode` 改成 `card`；想拿到「文件本身是原始音质」就用 `file`（见下节）。**

voice 通道上「源侧」已经取到上限，没有剩余空间：

- QQ音乐直链优先 **M800（320k MP3）**，其次 M500（128k）；绝不会把 FLAC 改名成 `.mp3`
  （本地缓存会校验文件头，遇到 FLAC/M4A 直接拒绝并回退远程 URL）。
- 网易云在 `mp3_only` 时请求 `br=320000`，三通道依次尝试 eapi → 标准 Web → 直链重定向。
- 缓存命中复用同一份文件，不影响音质。

## 发送形态：卡片 / 语音 / 文件

| 形态 | 命令与链接解析（`play_mode`） | LLM 工具（`send_as`） | 音质 |
|---|---|---|---|
| `card` | ✅ 默认 | 可选 | 最好：点开走官方播放器，可到无损 |
| `voice` | ✅ | ✅ **默认** | 最差：SILK ≈ 6–12 kbps，超 60 秒截断 |
| `file` | ✅ | 可选 | 完整保留源文件（FLAC / 320k） |

三条路径的关系：

- **`play_mode`** 决定命令（`/点歌`）、链接解析、卡片解析用什么形态。
- **`send_as`** 是 LLM 工具的可选参数（`voice` / `file`，默认 `voice`）；
  没指定时用 `[music] tool_default_mode`（默认 `voice`）。
- LLM 可以在「用户说要好音质 / 发文件 / 这歌太长」时把 `send_as` 填成 `file`，
  也可以填 `card`（虽未宣传，但传了就照做，不会被静默吞掉）。
- **双向兜底**（代码层硬约束，不依赖模型自觉）：
  - 触发消息里明确出现「发文件 / 无损 / 要好音质」等意图 → 即使 LLM 没传 `send_as` 也强制 `file`；
  - 反过来，LLM 传了 `send_as=file` 但触发消息里没有任何文件意图 → **回落** `tool_default_mode`。
    真机实锤（2026-09-12）：配置 `tool_default_mode=voice`，用户只说「放一首万能处方」，
    模型却被工具描述里的「音频文件」诱导传了 `file`，导致收到文件而非语音。
  - 拿不到触发消息文本（如命令、卡片解析路径）时不介入判断，尊重调用方入参；
    纯占位符文本（`[voiceurl消息]` / `[music消息]`）同样按「拿不到文本」处理。
  - **否定语境过滤**（v1.4.6）：意图判定按标点切小句、逐句判断，句内含否定或反问词
    （不 / 别 / 勿 / 无需 / 干嘛 / 为什么 …）即跳过。否则「不要发文件，放语音」
    会被当成「要文件」，反向兜底反而把形态强制成 `file`——把用户的否定读成了肯定。
    跨行输入（`发\n文件`）刻意不判为文件意图：漏判只会退回默认形态，风险远小于误判。

### `file` 形态怎么工作

1. 取音频直链（**不限 MP3**：优先无损 / 高码率，这正是它存在的意义）。
2. 落到本地缓存，按 URL 推断真实后缀（`.flac` / `.m4a` 都会按各自后缀校验文件头）。
3. 直连 NapCat 的 `upload_group_file`（群）或 `upload_private_file`（私聊）上传，
   文件名形如 `晚安糖果罐 - 洛天依.flac`。

前置条件与失败行为：

| 条件 | 不满足时 |
|---|---|
| `[napcat] http_url` 已配置，且能拿到群号 / QQ号 | **降级为音乐卡片**（刻意不降级为语音，避免音质静默回退），并提示「暂时发不了文件」 |
| NapCat 能读到缓存文件（同 `/点歌自检` 那套目录前提） | 同上 |
| 音频下载失败 | 同上 |

> 降级会**同步反馈给 LLM**：工具返回文本会说明「原本要发文件…已改为发送音乐卡片」，
> 并要求模型如实转述，避免出现「告诉用户已发文件、实际收到卡片」的错报。
> `[napcat] http_url` 可省略 `http://` 前缀（会自动补全）；文件上传超时为 120 秒
> （搜索等其它 NapCat 调用仍为 10 秒）。

> 文件上传走的是 NapCat 的独立动作（`upload_group_file` / `upload_private_file`），
> 不是消息段——MaiBot 的 send 能力里没有「文件」。这也是它**不占** `capabilities`
> 的原因（NapCat HTTP API 不由 Host 管控），但要求 NapCat HTTP API 可用。
> 单文件大小受 `[cache] max_file_size_mb` 限制（默认 50MB），群文件的额外限额由 QQ 侧决定。

### 为什么不会放错歌

搜索结果里常常混着干扰项。真机实锤（2026-09-11）：用户说「放晚安糖果罐」，
网易云第一条正是《晚安糖果罐 - 洛天依》，但该曲目在网易云**版权受限**
（`song_code=-110`）拿不到直链。旧实现是「候选逐条尝试直到成功」，
于是它**静默降级**去试第二条，把
《嘘嘘声+羊水声+胎心音+八音盒 星星糖果罐 - 晚安宝贝》当成结果播了出去。

**最佳匹配播不了是平台问题；改播一首无关的歌是逻辑问题。** 修法两步：

1. **相关度排序**：把「整串」与「按空白/顿号拆出的 token」分别与标题比对，
   取最高分（所以 `晚安糖果罐 洛天依` 不会因为多带歌手而掉分）；
   查询里出现歌手名时再加分，让同一歌名的多个演唱版本里正确那个排前面。
   中文没有分词，字符覆盖率用「查询里有几个字出现在标题中」近似——
   对「只共享尾词」的干扰项足够有效（「晚安糖果罐」拿 1.0，
   「…星星糖果罐」只有 0.36）。
2. **相对地板过滤**：只尝试分数 ≥ `最佳分 × relevance_floor` 的候选。
   用**相对**而不是绝对阈值——查询本身很模糊时所有分数都低，
   绝对阈值会把结果全砍光；相对地板只在「存在明显更优候选」时才剔除。

效果：上例中干扰项（0.36）低于地板（1.0 × 0.6 = 0.6）被剔除，
工具如实报告「均未取到可播放音频」，而不是放一首错的。
同时**同曲不同版本**（如 `晚安糖果罐 (Live)`，得分 0.925）仍在同一档内，
版权失败时依然能正常回退——过滤不会过头。

### NapCat 直连（可选）

MaiBot 适配器对 `music` 段只按固定结构处理（`platform` + `id`），会把带
`url/audio/title/image/content` 的 QQ音乐自定义卡片降级成文本。填了 `[napcat] http_url` 后，
插件会直连 NapCat 的 `/send_group_msg`、`/send_private_msg` 发送 CQ 码形式的卡片，绕开这层改写；
直连不可用时自动回退适配器路径。

> 代价：直连发出的消息**不写入 MaiBot 聊天历史**（只记日志），且要求 `http_url`
> 指向机器人所在的 NapCat、`http_token` 与其配置一致。

## 命令

| 命令 | 说明 | 示例 |
|---|---|---|
| `{pfx}点歌 <歌曲名>` | 用默认平台搜索 | `/点歌 晴天` |
| `{pfx}点歌 163 <歌曲名>` | 指定网易云 | `/点歌 163 晴天` |
| `{pfx}点歌 qq <歌曲名>` | 指定 QQ音乐 | `/点歌 qq 稻香` |
| `{pfx}选歌 <序号>` | 从候选里选一首 | `/选歌 1` |
| `{pfx}点歌状态` | 查看运行状态与自检信息 | `/点歌状态` |
| `{pfx}点歌自检` | 核对缓存目录与 NapCat 是否指向同一份文件 | `/点歌自检` |
| `{pfx}本地歌 <关键词>` | 搜本地歌曲库并播放，多首命中时列候选 | `/本地歌 夜曲` |
| `{pfx}本地库 [刷新]` | 查看本地歌曲库状态；`刷新` 强制重建索引 | `/本地库` |

`{pfx}` 为 `[music] command_prefix`，默认 `/`。命令要求前缀匹配：配了 `/` 时 `#点歌` 不会被处理
（也不会拦截消息）。全角 `／` 会归一化成 `/`。

### 确认 MaiBot 与 NapCat 看到的是同一份文件

`voice_source=local` 唯一真正的前提是「两个目录指向同一份文件」。
**「两边都能 `ls` 到同名文件」不算确认**——那也可能是两份互不相干的数据，
表现为语音莫名发不出或 0 秒。权威判据是**内容摘要一致**。

```
/点歌自检
```

插件会在 `storage_dir` 写一个探针文件（随机内容），并回显：

```
🔍 点歌缓存目录自检

写入目录: E:\maibot\data\music_cache
NapCat 目录: /app/music_cache
探针文件: .probe_a1b2c3d4.txt
内容摘要: 5f8d2c19（MD5 前 8 位）
路径映射: ✅ 成功

在 NapCat 侧执行其一，比对摘要前 8 位：
  docker exec <NapCat容器名> md5sum /app/music_cache/.probe_a1b2c3d4.txt
  md5sum /app/music_cache/.probe_a1b2c3d4.txt    # NapCat 非 Docker（Linux）
```

| 结果 | 含义 | 处置 |
|---|---|---|
| 摘要一致 | 确实是同一份文件 | `voice_source=local` 可用 |
| 摘要不一致 / 文件不存在 | 两份数据或没挂载上 | 改 `remote`，或修正 volume/路径 |
| `路径映射: ⚠️ 失败` | 两个目录前缀对不上，NapCat 会收到宿主机路径 | 检查两处配置是否除前缀外字面一致 |

探针文件以 `.probe_` 开头，不参与 `*.mp3` 的过期与容量清理，核对完可手动删除。
`/点歌状态` 只在两目录字面相同时标注「两目录相同」，字面不同不代表有问题
（Docker 挂载就是典型的不同路径指同一份文件）——所以最终仍以摘要比对为准。

## 本地歌曲库（v1.5.0）

把磁盘上的音频文件直接当歌放：不经过音乐平台、不吃版权限制、**文件形态保留原始音质**。

```toml
[library]
enabled = true
music_dir = "D:/Music"        # MaiBot 侧目录，递归扫描
napcat_dir = ""               # NapCat 侧路径（Docker 挂载时必配）
send_mode = "file"            # file = 音频文件（推荐）/ voice = 语音
```

- **搜索口径**：按文件名匹配（不含扩展名），整串命中 > 全部关键词命中 > 部分命中；
  「歌名 歌手」与「歌手 歌名」都能命中。命中多首时与平台点歌一样列候选，用 `/选歌 <序号>` 选。
- **发送通道**：本地文件没有 URL，走 **NapCat HTTP 直连**——`file` 形态调
  `upload_group_file` / `upload_private_file`，`voice` 形态发 `CQ:record`（file:// URI）。
  因此 `[napcat] http_url` 必须配置，且群聊/私聊目标能从消息或聊天流反查到。
  LLM 工具路径的上传超时被压到 **45s**（Host `invoke_tool` RPC 预算 60s，内层必须更短，
  否则工具结果送不回模型、还会留下僵尸调用重复传文件——真机实锤 2026-09-26，
  用户收到 3 个重复文件）；命令路径保持 120s 默认。同一文件在途上传时，
  重复请求会被去重跳过（提示「正在发送中」）。
- **路径映射**：与音频缓存同一套逻辑。Docker 部署时把 `napcat_dir` 配成容器内路径，
  可用 `/点歌自检` 的思路核对两个目录是否同一份文件。
- **索引刷新**：目录 mtime 变化且超过 `refresh_minutes` 时自动重扫；
  大批量增删歌后可 `/本地库 刷新` 立即重建。
- **LLM 点播**：`play_local_music` 工具——用户明确说「放本地库的歌」时触发；
  此外普通点歌工具 `search_and_play_music` 也会**优先查本地库**（`tool_prefer_local=true`，
  默认开）：关键词强匹配（整串或全部关键词命中文件名）直接发本地文件，
  未命中或发送失败再搜音乐平台；命中本地时返回文本带「来自本地歌曲库」标记。
- **局限**：本地文件构不了音乐卡片；语音形态会受 QQ 转码（SILK）与 60 秒上限影响，
  想听完整音质请保持 `send_mode = "file"`。

## 与歌词识别插件联动（cv_lyric_context）

配合 [org.mai-mai.cv-lyric-context]（中V歌词识别）使用时，链路是**经 LLM 规划器**的松耦合，
两边都不硬依赖对方：

```text
用户在群里贴歌词
  → cv_lyric_context 识别出歌名/歌手，并向 maisaka.planner.before_request 注入
     「可点播查询：歌名 歌手」
  → 用户说「放一下」
  → 规划器调用本插件的 search_and_play_music(query="歌名 歌手")
  → 本插件把歌发到当前会话
```

本插件侧为此做了两件事：

- **工具描述覆盖这条触发路径**：明确写了「用户贴了歌词、正在聊某首歌并表示想听时也用这个工具」，
  以及「若上下文里已有『可点播查询』串，直接整串填进 query，不要把一句歌词当 query」。
  描述是规划器选工具的唯一依据——只写「用户点歌」会让规划器在歌词语境下选不中它。
- **一次只发一首、失败不重试**：避免规划器连点多次刷屏。

不装 cv_lyric_context 时本插件不受任何影响（联动发生在对方一侧的注入里）。
反过来，不想参与联动就在对方的 `integration.play_tool_enabled` 关掉。

| 现象 | 排查 |
|---|---|
| 贴了歌词、bot 也不放歌 | 先看日志有没有 `已向规划器注入歌曲信息`（对方侧）；有注入仍不放，多半是规划器没选中工具——点歌工具默认在 deferred 池，需 `tool_search` 检索，对方注入文案已提示 |
| 放了但歌不对 | 歌名之外的歌手信息很关键：`query` 带上歌手能显著降低搜到翻唱/同名曲的概率 |
| 播不出来 | 与本插件自身的平台登录态有关，见上方故障排查表（QQ音乐搜索必须配置 `[qq] uin` + `qqmusic_key`） |

## 权限与能力

manifest `capabilities`：

| 能力 | 用途 |
|---|---|
| `send.text` | 结果回显、状态、错误提示 |
| `send.custom` | 发送 `music` / `voiceurl` 自定义消息段 |
| `chat.get_all_streams` | Tool 场景下按 `stream_id` 反查群号，供 NapCat 直连发送 |

Python 依赖：`httpx >= 0.27`、`cryptography >= 42`（网易云 eapi 加密通道）。
未装 `cryptography` 时插件不会崩：会自动跳过 eapi，退到标准 Web 接口与直链重定向
（免费歌曲通常仍可用，VIP/高音质会拿不到）。NapCat 直连**不占** capabilities
（NapCat HTTP API 不在 Host 管控范围内，与适配器能力体系无关）。

改动 `capabilities` 后**必须完整重启 MaiBot**，热重载不生效。

## 故障排查

| 现象 | 原因与处置 |
|---|---|
| `[E_CAPABILITY_DENIED] 未获授权能力: xxx` | manifest 漏声明 → 补声明 + **完整重启** |
| 命令发了没反应 | 前缀不匹配（`command_prefix` 与输入不一致）；或命令未注册 → 重启 MaiBot；WebUI「Bot 配置 → 命令」里确认组件在列表 |
| QQ音乐搜索报"需要先配置 qq.uin 与 qqmusic_key" | 属预期：QQ音乐搜索必须登录态。补配置，或改用 `/点歌 163 <歌名>` |
| QQ音乐搜索报"登录态失效或被拒绝" | Cookie 过期，重新获取 `uin` / `qqmusic_key` |
| 找到歌但"未返回可用音频" | 版权 / VIP 限制，或登录态对应账号权限不足。卡片模式下 QQ音乐会退成可点击跳转的卡片 |
| 日志出现 `song_code=-110` | 网易云该曲目不可用/版权受限（不是插件错误）。插件会自动退到 Web 接口与直链，仍不行则由工具换平台重试；配好 `[netease] music_u` 能提高命中率 |
| 放的歌不对 | 先看日志有没有 `按相关度过滤掉 N 个候选`；若过滤后剩的就是错的，说明平台结果里没有更好的匹配 → 带上歌手重试（`/点歌 歌名 歌手`）。把 `relevance_floor` 调到 `1.0` 可强制只尝试最佳匹配 |
| 语音音质差 / 只播一半就断 | 语音消息通道的固有限制（SILK 转码 + 60 秒上限）→ 改 `play_mode = "card"`，见上文「音质」一节 |
| stderr 刷 `coroutine ... was never awaited` | v1.2.1 已修（网易云三通道改为惰性创建协程）。升级后若仍出现，说明真机跑的还是旧目录 |
| 音乐卡片发出来是纯文本 | 适配器不支持自定义 `music` 段 → 配置 `[napcat] http_url` 走直连 |
| 卡片能点但播不了 | 直链时效过期或版权限制；提示"受版权/登录限制无法直接播放"即为该情况 |
| voice 模式语音发不出 / 语音 0 秒 | 跑 `/点歌自检` 按提示核对摘要：一致才是同一份文件；不一致或映射失败就改 `remote` 或修正挂载路径 |
| `插件配置版本非法: 缺少 plugin.config_version` | 旧 `config.toml` 缺版本号 → WebUI 保存一次，或删除文件让 Runner 重新生成 |
| TOML `Invalid statement (at line 1, column 1)` | `config.toml` 带 UTF-8 BOM → 用无 BOM 方式重写 |
| 链接没被识别 | 确认格式在支持列表内；短链（`163cn.tv` / `c6.y.qq.com`）需要重定向解析，跳转目标不在白名单会被拒绝 |
| 卡片没被解析 | `auto_parse_card` 是否开启；精确解析需要 `[napcat] http_url`，未配置时退化成按歌名搜索 |
| 改了代码不生效 | 依赖模块命中 `sys.modules` 缓存 → **完整重启**；并用 `/点歌状态` 确认版本号是新的 |

### 支持的音乐链接格式

- 网易云：`music.163.com/song?id=`、`music.163.com/#/song?id=`、`music.163.com/m/song?id=`、
  `y.music.163.com/m/song?id=`（卡片 jumpUrl）、`163cn.tv/xxx`（短链）
- QQ音乐：`y.qq.com/n/ryqq/songDetail/`、`y.qq.com/n/m/detail/song/`、
  `i.y.qq.com/v8/playsong.html?songmid=`（卡片 jumpUrl）、`c6.y.qq.com/base/fcgi-bin/u?__=`（短链）

## 安全说明

- **短链 SSRF 闸门**：解析 `163cn.tv` / `c6.y.qq.com` 时会跟随重定向，
  目标 host 只允许 `music.163.com`、`y.music.163.com`、`y.qq.com`、`i.y.qq.com`、`c6.y.qq.com`；
  指向内网、环回、元数据地址（`169.254.169.254`）或后缀伪装域名的跳转一律拒绝并记警告。
  - v1.4.6 起：**入口 host 也走白名单**（短链域名单列），不在白名单的 URL 连请求都不发；
    client 归属按 `urlparse().hostname` 判定，不再用 `"y.qq.com" in url` 子串包含
    （`https://evil.com/?x=y.qq.com` 这类 URL 曾能骗过判定）。
  - v1.4.6 起：重定向**手动逐跳跟随**（`follow_redirects=False`），每跳重新校验 host。
    httpx 在跨主机重定向时不会剥离无 domain 限定的 cookie，一条 302 就能把凭据送出白名单。
- **凭据不外泄**：Cookie 只注入 httpx 的 cookie jar（不放 headers，否则会吞掉 jar）；
  **cookie 必须带 domain 限定**——网易云到 `.music.163.com`、QQ 音乐到 `.y.qq.com`。
  直接传 dict 会创建「无 domain」cookie，httpx 会把它附加到该 client 的所有请求上；
  上游响应写日志前会遮蔽 `authst` / `cookie` / `qqmusic_key` / `token` / `signature` 等字段。
- **落盘只在受控目录**：缓存文件名由「平台 + 歌曲 ID」映射生成，非字母数字字符统一替换为 `_`，
  不接受用户输入直接做文件名，也不会有路径穿越。
- **数据目录**：缓存默认写 `ctx.paths.data_dir/audio_cache`，不使用 `os.path.dirname` 绕出插件目录。
- **待确认项**：QQ 音乐卡片的 `url` / `audio` / `image` 字段**刻意不转义**（转义后需 NapCat
  正确解码 `&amp;` / `&#44;` 才能还原，真机曾按「转了就打不开」处理）。代价是 URL 里的
  `,` / `]` 理论上能破坏 CQ 参数结构，属低危（仅影响本次卡片参数字段，不涉及凭据）。
  改动前需先在真机确认 NapCat 的解码行为。

## 开发与测试

```bash
# 结构自检（零依赖）
python check_plugin.py --plugin .

# FakeHost 冒烟（不启 MaiBot，用离线替身挡网络）
python tests/smoke_test.py

# 单元测试（321 项：链接/卡片解析、相关度排序与过滤、组件绑定、命令正则、
#          配置模型、CQ 转义、eapi 加密、路径映射与探针、形态兜底与否定语境、
#          本地歌曲库（扫描/搜索/映射/命令/工具）、
#          凭据不外发 / 短链白名单 / 逐跳重定向校验等安全回归）
python -m pytest -q tests

# 交付门禁（三层串跑）
python run_gates.py --plugin .
```

三条测试铁律（本插件的用例都遵守）：

1. **新用例必须注册进调用列表** —— 写完不注册等于没写。
2. **断言要能反向验证** —— 把实现改回错误写法，对应用例必须失败
   （例如去掉短链优先级、放宽 SSRF 白名单、让 Tool 参数名撞 Host 注入字段）。
3. **冒烟不依赖外网** —— 音乐接口在开发机上可能被墙/被代理拦，
   冒烟里用 `StubAPI` 替身验证编排逻辑，真实接口留给真机。

### 真机部署（开发机 ≠ 运行机）

```text
本地门禁全绿 → 整目录替换真机 plugins/music-request/
            → 完整重启 MaiBot（manifest / 依赖模块缓存热重载均无效）
            → /点歌状态 确认版本与配置生效
            → 贴真机日志回传分析
```

两条实测结论值得记住：

- **热重载对 manifest 无效**：capabilities / 版本 / 依赖改了必须完整重启。
- **禁用再启用插件命中 `sys.modules` 缓存**：`from xxx import yyy` 可能拿到旧对象，
  所以要靠 `/点歌状态` 的版本号与启动日志自检确认，而不是读源码文本。

## 许可证

MIT
