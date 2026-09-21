# AstrBot / NapCat 能力核验

核验日期：2026-09-21。范围：本机安装源码、官方固定版本源码、已登录 NapCat 的只读 OneBot 调用。结论不自动推广到全部 QQ / NapCat / AstrBot 版本。

这是最初方案阶段保留的能力证据。当前版本的实际功能与限制以 [工程说明](engineering.md) 为准；下文的待验收项不是已完成联调声明。

## 1. 核验基线和证据等级

- AstrBot：本机安装版本 4.26.2，`aiocqhttp` 适配器；官方标签提交 `a619988d2d181c884f7bf04e24f30c0ea0928ff6`。
- NapCat：运行接口返回 4.18.19；官方标签提交 `af07479351c5b974e72ae1c7183f2272e79ffc1c`。
- QQ：既有本机安装 Windows x64 9.9.26-44498。
- R（只读实测）：确认本次样本的返回值；不等于长期准确性。
- S（源码确认）：确认字段映射和调用路径；不等于所有账号都有权限或字段都准确。
- P（待验收）：必须在后续实现和受控测试群中验证。

本次只读取一个既有授权管理群的基本信息、一次缓存成员列表、一个普通成员的详情。没有调用踢人、禁言、设置群信息、发送消息等写入接口。临时调试适配器已关闭。下面仅保存汇总，不保存真实群号、QQ、昵称或原始响应。

## 2. 能力矩阵

| 能力 | 证据 | 实现边界 / 产品处理 |
| --- | --- | --- |
| 群人数、容量 | R/S | `get_group_info` 返回 `member_count/max_member_count`，可能来自缓存；执行前优先 `get_group_detail_info`，但仍无原子化人数快照 |
| 群成员列表 | R/S | `get_group_member_list`；没有客户端分页参数；一次拉全表后本地筛选，不能全员逐个补查 |
| 群主/管理员角色 | R/S | `role=owner/admin/member`；每次执行前单人成员详情复核；未知角色保护 |
| 入群时间 | R/S | `join_time` 映射 QQ `joinTime`；无效/未来时间、重新入群代次必须处理 |
| 最后发言时间 | R/S | `last_sent_time` 映射 `lastSpeakTime`，不是阅读时间；零值/缺失不能推断从未发言 |
| 群内成员等级 | R/S/P | `level` 来自 `memberRealLevel`，缺失回退字符串 `"0"`；需要与该群 QQ 界面等级抽样对照，不能认定所有 0 都是真的低等级 |
| QQ 等级 | R/S | 列表字段为 `qq_level`；本次列表全部 0，详情可补到非零；陌生人接口字段名是 `qqLevel`，需统一映射 |
| 专属头衔 | R/S | `title` 可读取，但源码把 `title_expire_time` 固定为 0，不能据其判断有效期 |
| 当前禁言/全员禁言 | R/S/P | 有 `shut_up_timestamp/group_all_shut`，具体时间语义需适配测试；不能还原完整禁言历史 |
| 是否机器人 | R/S | `is_robot` 只是平台标记；未知/false 不等于普通真人，人工 bot 白名单仍有用 |
| 移出成员 | S/P | `set_group_kick` 存在；需要 bot 具有实际权限，普通成员不能操作；本次没有真实执行 |
| 允许再次申请 | S/P | `reject_add_request=false` 参数存在；效果受 QQ 群策略影响，无法保证立即可重入 |
| 群消息活动记录 | S/P | AstrBot 接收消息并保留 raw event，可记录；掉线和框架过滤会漏事件，无可靠历史重放保证 |
| 进退群、管理员变更 | S/P | OneBot notice 可进入 AstrBot；必须测试插件监听能收到、未被过滤，事件可能丢失或乱序 |
| 戳一戳/表情回应活动 | S/P | 相关 NapCat 事件存在，但发起人、目标、时间字段需逐类验证；不能直接承诺与发言同等完整 |
| 群荣誉保护 | S/P | 有荣誉查询接口，但不是完整贡献历史；首版不作为自动清理必需数据，后续独立验收后开放 |
| 私聊管理与通知 | S/P | AstrBot 命令/平台发送可用；陌生人私聊可能失败，离线通知不能靠无限重发弥补 |
| 历史发言条数/活跃天数 | 仅本地观测可实现 | 从部署后事件累计，必须记录覆盖缺口；默认不抓取全群历史聊天来补齐 |
| 确认“从未发言” | 无可靠通用能力 | 只能说在有效观测窗口内未观察到消息；平台 0 值不可作为证明 |
| 阅读群消息、其他群活跃、真实贡献、真人身份 | 无本方案可用的可靠接口 | 不提供对应自动规则 |
| QQ 风控原因/安全频率阈值 | 无可靠接口 | 只展示原始错误类别和暂停原因；不宣称能检测所有风控或保证不掉线 |
| 跨多个 bot 原子判断并踢人、自动恢复被移出成员 | 不可保证 | 配置单执行账号，复查降低竞态；不提供虚假撤销能力 |

## 3. 本机只读实测结果

群信息成员数与列表长度均为 202，容量返回 500。仅一个群、一次缓存读取，不能作为平台完整性保证。

| 字段 | 存在条数 / 202 | 零值或空值条数 | 对设计的影响 |
| --- | --- | --- | --- |
| role | 202 | 0 | 可以做基础权限保护；执行时仍需刷新 |
| join_time | 202 | 0 | 本次有值，仍需验证范围和重入 |
| last_sent_time | 202 | 0 | 本次没有大于本机时间 5 分钟的值，历史含义仍需观察 |
| level | 202 | 111 | 不能默认把 111 人全部视为已证实的低群等级 |
| qq_level | 202 | 202 | 列表不能直接用于 QQ 等级排序 |
| title | 202 | 202 | 此样本未覆盖非空头衔 |
| is_robot | 202 | 201 | 存在 true 标记，但 false 不能用于真人认证 |
| shut_up_timestamp | 202 | 202 | 样本未覆盖被禁言成员 |

随后对一名普通成员执行 `get_group_member_info(no_cache=true)`，QQ 等级取得非零值，群等级仍为 0。证明“列表可缺失、详情可补充”的路径存在；不能证明对任意成员都能补到。

QQ 等级设计为按需且有额度的增强数据。规则不使用 QQ 等级时不补查；使用时显示查询成本、进度和缺失率。不能把 `0` 当成“账号很新”。

## 4. 源码确认的隐患

### 4.1 `no_cache=true` 不构成强一致性保证

4.18.19 的列表实现发起 `refreshGroupMemberCache()` 后使用 `cache.get(groupId) || await refreshPromise`。已有缓存时可能直接返回旧值，刷新还在后台。刷新函数内部还可能捕获异常后保留旧缓存。

因此不能用“一次 `no_cache=true` 列表没看到某人”直接认定踢人成功，也不能把一次列表认为是完整最新名单。后续实现应使用列表时间、人数对照、成员事件和单人详情多源核对；需要刷新时串行发起、等待并再次读取，证据仍冲突就暂停。没有暴露缓存版本号时，应如实标记“新鲜度未确认”。

### 4.2 `set_group_kick` 的返回值不足以确认业务成功

源码调用 `GroupApi.kickMember()` 后返回 `null`，没有在这一层显式检查底层 `result`；原生调用是否抛错也依赖具体情况。OneBot `status=ok/retcode=0` 只能记录“API 已返回”，不能单独变成“清理成功”。

匹配到自己执行的 `group_decrease/kick` 且成员代次一致，才是强确认。若只有刷新名单确认缺席，记录“观察到已离群”，不能伪称一定由本插件踢出。接口错误“成员不存在”也可能是缓存/UID 解析异常，不能当成证明。

### 4.3 单人详情也有额外成本和缓存依赖

成员详情同时查询群成员和用户详情，并读取共享成员缓存；一次 OneBot 调用可能触发多个 QQ 内部请求及内部重试。插件只能限制自身调用，不能把 OneBot 计数等同于腾讯后台请求数。

已有 QCE 插件曾污染共享 API 对象。新插件不修改 NapCat 内存对象、不注入 native hook；接口失败停止任务，通过状态和本地日志供管理员查询，不主动通知。

## 5. AstrBot 的能力和边界

- `_conf_schema.json` 支持 `object/template_list/list` 等配置结构，已有审核插件也是按群列表。首版使用每群模板、一个更多条件对象和共用节奏，不提供条件表达式编辑器。
- `AstrBotConfig` 自动补默认值，不提供我们需要的全部跨字段约束；插件必须自己生成带定位路径的校验错误，并编译不可变策略快照。
- `initialize/terminate` 支持生命周期管理；持久化队列、计划、额度、恢复流程由新插件实现，不假设框架替插件做了。
- `StarTools.get_data_dir()` 提供 `data/plugin_data/<插件名>`。数据库、日志、锁在数据目录，不放代码目录，避免更新插件时丢失。
- `event_message_type(ALL)` 可覆盖消息与通知，raw event 保留 OneBot 信息。高优先级处理器应只记轻量活动，不阻断其他插件。
- 但框架的会话停用、平台白名单、内容检查、限流和其他处理器 `stop_event()` 可能阻止事件到达。收到心跳也不能证明完整收到全群消息。依赖本地零发言统计的规则必须验证目标会话开启且监听路径覆盖；不满足则只观察，不能自动清理。
- AstrBot 系统日志支持按大小轮转，但插件不能接管宿主全局日志配置。新插件独立文件日志、审计数据库各自有容量与保留政策。
- 现有审核、GitHub 订阅插件有私有的 `_qq_automation_guard_v1` 共享锁协议；它不是 AstrBot 官方 API，不跨进程，不覆盖其他插件。新插件需兼容接入并做能力检查，不能声称已有全平台限速。
- AstrBot OpenAPI 是否能管理插件取决于 key scope；新插件运行直接用框架上下文，不需要保存用户提供的 WebUI/OpenAPI 密钥。

## 6. 不把反检测开关作为工程依赖

保持用户现有 NapCat 配置，不自动开启 bypass。维护者对这些开关有明确的不建议使用反馈：

- [Module 反检测有严重副作用（2026-04-26）](https://github.com/NapNeko/NapCatQQ/issues/1775#issuecomment-4321716146)。
- [不要使用 O3 拦截外的反检测（2026-05-22）](https://github.com/NapNeko/NapCatQQ/issues/1813#issuecomment-4519412623)。
- [4.18.6 PacketBackend 失效问题中建议不要开启反检测（2026-06-19）](https://github.com/NapNeko/NapCatQQ/issues/1918#issuecomment-4750060315)。

这些历史意见不替代未来版本测试。新插件能做到的是减少请求、避免重复和失败连发、在异常时停止；无法通过随机延迟证明“像真人”或保证账号不被限制。

## 7. 官方源码索引（固定版本）

NapCat 4.18.19：

- [成员字段映射 data.ts](https://github.com/NapNeko/NapCatQQ/blob/af07479351c5b974e72ae1c7183f2272e79ffc1c/packages/napcat-onebot/helper/data.ts#L73)
- [GetGroupMemberList.ts](https://github.com/NapNeko/NapCatQQ/blob/af07479351c5b974e72ae1c7183f2272e79ffc1c/packages/napcat-onebot/action/group/GetGroupMemberList.ts)
- [GetGroupMemberInfo.ts](https://github.com/NapNeko/NapCatQQ/blob/af07479351c5b974e72ae1c7183f2272e79ffc1c/packages/napcat-onebot/action/group/GetGroupMemberInfo.ts)
- [GetGroupInfo.ts](https://github.com/NapNeko/NapCatQQ/blob/af07479351c5b974e72ae1c7183f2272e79ffc1c/packages/napcat-onebot/action/group/GetGroupInfo.ts)
- [GetGroupDetailInfo.ts](https://github.com/NapNeko/NapCatQQ/blob/af07479351c5b974e72ae1c7183f2272e79ffc1c/packages/napcat-onebot/action/group/GetGroupDetailInfo.ts)
- [GetStrangerInfo.ts](https://github.com/NapNeko/NapCatQQ/blob/af07479351c5b974e72ae1c7183f2272e79ffc1c/packages/napcat-onebot/action/go-cqhttp/GetStrangerInfo.ts)
- [SetGroupKick.ts](https://github.com/NapNeko/NapCatQQ/blob/af07479351c5b974e72ae1c7183f2272e79ffc1c/packages/napcat-onebot/action/group/SetGroupKick.ts)
- [GroupApi 的刷新、成员详情与 kickMember](https://github.com/NapNeko/NapCatQQ/blob/af07479351c5b974e72ae1c7183f2272e79ffc1c/packages/napcat-core/apis/group.ts)

AstrBot 4.26.2：

- [aiocqhttp 适配器](https://github.com/AstrBotDevs/AstrBot/blob/a619988d2d181c884f7bf04e24f30c0ea0928ff6/astrbot/core/platform/sources/aiocqhttp/aiocqhttp_platform_adapter.py)
- [配置解析](https://github.com/AstrBotDevs/AstrBot/blob/a619988d2d181c884f7bf04e24f30c0ea0928ff6/astrbot/core/config/astrbot_config.py)
- [日志管理](https://github.com/AstrBotDevs/AstrBot/blob/a619988d2d181c884f7bf04e24f30c0ea0928ff6/astrbot/core/log.py)
- [事件唤醒与插件过滤](https://github.com/AstrBotDevs/AstrBot/blob/a619988d2d181c884f7bf04e24f30c0ea0928ff6/astrbot/core/pipeline/waking_check/stage.py)
- [数据目录工具](https://github.com/AstrBotDevs/AstrBot/blob/a619988d2d181c884f7bf04e24f30c0ea0928ff6/astrbot/core/star/star_tools.py)

## 8. 上线前仍需验证

1. 每个目标群至少抽样比对已知高/低等级成员和实际发言记录；字段异常时只允许不依赖该字段的明确策略。
2. 用受控测试账号覆盖管理员、非管理员、重入、禁言、头衔、超时和重复事件。
3. 验证消息不用 @bot 也能记录；框架过滤/停用可能漏事件，首版不依赖本地沉默统计，也不宣称能自动识别全部覆盖缺口。
4. 授权测试群中执行一次移出，验证事件、返回码、名单延迟与审计，不在正式群用真实候选试错。
5. 验证共享限速与现有两个插件同时运行、卸载重载后的持久状态。
6. 安装版本或 QQ 版本变化后重新诊断。首版检查接口形状和实际数据，不做版本认证或自动为未知版本背书。
