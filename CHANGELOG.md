# 更新日志

`music-request`（点歌插件）的版本变更记录。

- 版本号以 `_manifest.json` 的 `version` 字段为准，与代码里的
  `SUPPORTED_CONFIG_VERSION` 同步（有守卫测试强制一致）。
- 遵循语义化版本：`MAJOR.MINOR.PATCH`——新能力升 MINOR，行为修复升 PATCH，
  影响已部署实例行为的改动一律升 MINOR 并在条目标题里点明。
- 新条目加在文件顶部。日期为提交日期（Asia/Shanghai）。
- **本文件自 v1.8.0（2026-11-14）起建立**。此前版本（v1.0.0 ~ v1.7.7）无
  独立 CHANGELOG，行为变更按版本散记在 README 各节（如「原曲优先 v1.6.4」
  「时长闸 v1.7.1」「并发让路 v1.7.2」「卡片作用域隔离 v1.7.7」），回溯以
  README 为准。

---

## [1.8.0] - 2026-11-14

### 新增（点歌联动加固，与 cv_lyric_context v2.19.0 配套）

- **跨插件契约单一事实源（P0-A，治 G1/G4）**：`get_song_bilibili` 的键集、
  类型、`schema_version`、match 枚举固化到 devkit
  `tools/maibot-devkit/contracts/cv_music_v1.json`；本仓库
  `tests/contracts/cv_music_v1.json` 为镜像，`tests/contract_cv_music.py`
  提供加载器与 `sample_result` / `validate_result`。门禁新增 contracts 步骤
  校验镜像与事实源**逐字节一致**（漂移即 FAIL，已反向验证）。
  `tests/test_vcpedia_lookup.py` 的手写假字典全部改为契约构造 + 全量形状
  校验（故意保留两处契约外畸形输入测降级路径）；清理 1 处整段重复定义
  （「直连时长闸」5 个用例后定义覆盖前定义的死代码）。
- **契约版本协商（治 G4）**：`_vcpedia_lookup` 校验返回的 `schema_version`——
  缺失按 v1 宽容处理（兼容未升级提供方）；显式非 1 = 不兼容未来契约，
  归类 `schema_mismatch` 回退搜索，绝不按 v1 语义误读。
- **真实对端端到端测试**：`tests/test_e2e_cv_music.py`（6 项）经 fakehost
  包式加载**真实 cv_lyric_context 插件模块**，覆盖 `_search_ranked` 与
  `search_and_play_music` 工具路径（prefix 原曲歌手采纳、点名翻唱回退等）。
  对端目录不存在时 skip（单仓库可跑）。
- **失败七分类 + 内存计数 + `/点歌联动自检` 命令（P1-C，治 G3）**：
  `hit` / `not_found_or_no_bvid` / `timeout` / `not_installed` /
  `no_api_proxy`（老 Host）/ `schema_mismatch`（含缺 `found` 键的占位结构）/
  `error`。计数存内存、重启清零。自检命令三段输出：① `api.list` 可见性
  （含提供方声明的 `timeout_ms`）；② 真实探针（与点歌同链路，不计数）；
  ③ 七类运行计数。新增 `tests/test_vcpedia_diagnostics.py`（23 项）。

### 变更

- **跨插件调用 3 秒硬超时（P0-B，治 G2）**：`asyncio.wait_for(..., 3.0)`
  包住 `ctx.api.call`（提供方 `@API` 同步声明 `timeout_ms=3000`，双闸）。
  此前宿主默认等 60s，一次慢查询能拖满整条点歌工具链。超时静默回退平台搜索。
- `_manifest.json` capabilities 增加 `api.list`（自检可见性探测用）。
  **改动 capabilities 需完整重启 MaiBot**。
- **工具描述修复（SDK 2.22+ 生产回归）**：SDK 2.22 起
  `normalize_tool_description` 在 `description` 非空时**静默丢弃**
  `detailed_description`——两个 `@Tool` 的参数指导（含「可点播查询」联动
  提示）在新 SDK 下真的丢失。已把 `detailed_description` 内容并入
  `description`；测试改用 `getattr` 兼容新旧 SDK 字段。
- `SUPPORTED_CONFIG_VERSION` 同步 bump 至 `1.8.0`（守卫测试强制与 manifest 一致）。

### 修复

- 组件计数断言 9 → 10（新增「点歌联动自检」命令），smoke 同步。
- 新命令方法名对齐既有中文风格（`cmd_点歌联动自检`），消除门禁
  「装饰器-函数名无关」WARN，music 静态检查达到 **WARN 0**。
- README：新增「联动加固（v1.8.0）」节（契约位置互链、自检输出解读表）、
  capabilities 表补 `api.call` / `api.list`、故障排查行补分类提示。

### WebUI 显示优化（纯显示层，不改配置键与校验语义）

- **配置页分组页签（layout=tabs）**：8 个配置节由平铺改为三页签——
  「点歌行为」（plugin/music/qq）、「平台与登录」（netease/napcat/bilibili）、
  「本地与缓存」（cache/library）。未归类的新增节自动追加到最后一个页签，
  不会整块消失（前端 tabs 布局只渲染被引用的节）。
- **修复 7 处自动推导的半句话 label**：`uin——QQ音乐登录账` →「登录账号 uin」、
  `qqmusic_key—` →「登录凭证 qqmusic_key」、两处 `同一目录在` →
  「缓存目录（NapCat 侧）」/「歌曲库目录（NapCat 侧）」（按节区分，此前
  两节 label 完全相同）、`LLM 调用点歌工具但没` →「工具默认发送形态」、
  `超过这么久未使用的缓存会` →「缓存过期时间（小时）」、
  `LLM 点歌工具` →「工具优先查本地库」等；label 覆盖表改为 (节名, 字段名)
  键，手工 label 无条件优先。
- **13 个凭据/路径/格式敏感字段补 placeholder**（MUSIC_U、__csrf、uin、
  qqmusic_key、NapCat 地址/令牌、缓存与歌曲库双侧目录、SESSDATA、buvid3、
  命令前缀）；`relevance_floor` 补 step=0.05（不碰 ge/le 校验边界）。
- **可选节默认收起**：cache / library / bilibili 加入收起集合（netease /
  napcat 原已收起）；qq 保持展开——uin + qqmusic_key 是搜索必需项。
- **节内字段分组**：music 节「搜索与选歌 / 链接与卡片解析 / 发送形态」、
  cache 节「容量与清理」、library 节「目录 / 发送与检索」，主开关留在
  无标题首块。
- 回归：`test_webui_display.py` 6 → 13 项（label 断句、页签全覆盖、
  收起策略、placeholder、napcat_dir 区分、分组、step 与校验边界不变量）。
- **删除插件内过时的 `check_plugin.py` 副本**（规则基线还停在 Host 1.2.3 /
  SDK 2.8.0）：门禁改走 devkit 统一检查器（基线 1.4.3），静态检查
  PASS 45 → **PASS 47 / WARN 0**，消除「旧副本基线」WARN。

### 验证

- devkit venv（SDK 2.22.1）全量 pytest：**575 passed / 2 skipped**
  （联动加固基线 568 + WebUI 显示回归 7 项；2 项 skip 为 devkit venv
  缺 cryptography 的 eapi 用例）；FakeHost 冒烟 **smoke: OK**。
- `gate.sh` **PASS 4 / SKIP 0 / FAIL 0**：check_plugin **PASS 47 / WARN 0 /
  FAIL 0**（devkit 统一检查器）；contracts PASS；smoke OK；
  pytest 575 passed / 2 skipped。
- **官方最新实测（2026-11-14，交付准入依据）**：
  - 官方渠道核对：MaiBot **v1.4.4**（2026-11-14 发布）、SDK **2.23.0**。
    v1.4.4 的 `plugin_runtime` 与 1.4.3 仅 9 文件实质差异，
    manifest_validator / plugin_loader 零变化。
  - 官方 v1.4.4 源码 PluginLoader 加载探针（`force_plugin_compatibility=False`）：
    **加载成功，10 组件 / 5 能力**；manifest 收紧后严格区间
    host 1.0.0~1.4.4 / sdk 2.5.0~2.23.0 无警告通过。
  - SDK 2.23.0 隔离 venv（含 cryptography 50.0.2）：
    pytest **577 passed / 0 skipped**（eapi 2 项转绿 + WebUI 回归 7 项）、
    smoke OK。
  - 据此放宽 `_manifest.json`：host max 1.3.99→**1.4.4**、
    sdk max 2.16.99→**2.23.0**（实测后才 bump，未写占位大数）。
  - WebUI 变更后复跑官方 v1.4.4 loader 探针：加载成功，10 组件 / 5 能力。
- **未真机验证**：超时收紧与自检命令需真机回归（部署指南随交付给出）。
