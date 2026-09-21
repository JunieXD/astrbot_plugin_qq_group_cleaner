# 配置说明与交互约定

状态：设计契约，尚未实现。所有时间参数在界面中写明单位；展示中文名称，保存稳定英文键。界面不可只显示一个没有解释的 `risk_score` 或英文 JSON 框。

## 1. 表单结构

一级区域：①总开关与权限 ②群策略 ③高级条件 ④清理顺序 ⑤账号频率与额度 ⑥通知 ⑦日志与存储。

群策略区域按 `template_list` 增加群，`display_item` 使用名称；每项分为基础、容量、成员保护、活动数据、执行窗口、按群覆盖。固定保护显示说明，不提供关闭开关。

规则行也采用标准 `template_list`：`policy_id`、阶段、规则编号、字段、运算符、值、启用；数值/布尔/枚举使用不同模板，数值不是任意表达式。一个群可以有多行，不依赖无限嵌套表单。排序行有明确 `priority`，不依赖列表物理顺序。

常用预设在创建群时选择，由插件的“预设应用”命令显示变更后明确保存；选择并保存后参数展开成可编辑值。预设更新不暗中重写既有群规则。高级区必须能显示类似 `A=(未活动>=180天 且 群等级<8)；B=(未活动>=90天 且 QQ等级<32)` 的编译后解释；首版用私聊预览呈现，不承诺原生表单有自定义图形编辑器。

## 2. 继承、校验和配置生效

顶层 `defaults` 提供各分组完整默认值。每个群的 `overrides` 可按组选择 `mode=inherit/custom`；custom 提供完整 `value`，不做深层隐式混合。规则、保护白名单和排序列表都按整组替换，不追加。实际白名单为“配置白名单 ∪ 私聊命令设置的白名单”，命令不能删除配置文件仍保留的保护。

全局账号上限不可被群覆盖放宽：有效额度取较小值；有效最短间隔取较大值。间隔范围 `[min,max]` 合成后校验 `min<=max`，不允许 NaN/负数/字符串溢出。0 表示禁用只限明确注明的字段，不表示无限额度或继承；默认没有无限额度开关。

保存后编译策略并输出：成功/失败、字段路径、错误原因、生效值与来源、能力依赖、粗略请求量。新策略只有验证成功后才原子替换；一个群无效只暂停该群，全局账号/存储配置无效暂停所有执行。配置解析失败时不得继续用可能更宽松的旧配置执行；旧配置只保留供显示和恢复。

AstrBot 的配置保存和插件内存更新不是同一个动作。首版明确支持“保存后重载插件”应用；后续如支持在线重载，必须经过相同编译和版本切换。卸载/重载不清空计划、额度、暂停和未知操作。

## 3. 顶层基础配置

| 字段 | 中文名称 | 默认值 | 校验与说明 |
| --- | --- | --- | --- |
| schema_version | 配置版本 | 1 | 未知更高版本禁止执行，不擅自降级 |
| enabled | 插件总开关 | false | 关闭后不扫描、不采集、不执行；生成观测缺口 |
| operator_qq_ids | 主管理员 QQ | [] | 与 AstrBot 管理员身份联合核验；标识用字符串保存 |
| defaults | 各群默认参数 | 本文各表 | 不包含真实群号或通知对象 |
| groups | 群策略列表 | [] | 显式启用；禁止自动对所有加入的群生效 |
| rule_clauses | 高级条件行 | [] | 关联 policy_id，未知引用报错 |
| sort_rules | 排序行 | [] | 关联 policy_id，优先级不能重复 |
| account_safety | 账号调度上限 | 见第 10 节 | 同一 self_id 跨群共用 |
| logging / storage | 日志和存储 | 见第 12 节 | 仅插件专属文件 |

本表 `operator_qq_ids` 允许缩小谁能使用跨群命令；为空时使用 AstrBot 主管理员集合。不能仅在此填入普通成员就授予整个 AstrBot 管理权限。

## 4. 每个群的基础配置

| 字段 | 中文名称 | 默认值 | 说明 |
| --- | --- | --- | --- |
| policy_id | 策略标识 | 必填 | 稳定简短标识；例如 study_group，不含空格 |
| name | 策略名称 | 必填 | 给人看的名称；改名称不影响历史 |
| enabled | 启用本群 | false | 全局和本群都开启才运行 |
| platform_id | AstrBot 平台实例 | 必填 | 不能只填 aiocqhttp 类型名 |
| self_id | 执行 QQ 账号 | 必填 | 绑定真实账号，防止切号后误执行 |
| group_id | QQ 群号 | 必填 | 同一账号不得重复启用 |
| run_mode | 运行模式 | observe | observe / confirm / auto |
| admin_qq_ids | 本群操作人 | [] | 普通操作人必须同时具有当前群管理权限 |
| overrides | 本群自定义分组 | {} | 未指定按 defaults 继承 |

运行模式变化、目标群变化或规则变化都会更新 `policy_revision`，旧确认码失效。

## 5. 容量与检查频率（trigger）

| 字段 | 中文名称 | 默认值 | 说明 |
| --- | --- | --- | --- |
| kind | 人数阈值类型 | absolute | absolute / percent |
| trigger_count | 达到多少人开始 | 490 | 人数模式；需按实际群容量调整 |
| target_count | 清理到多少人停止 | 470 | 必须小于触发人数 |
| trigger_percent | 触发占用百分比 | 98 | 比例模式，0～100，取上整 |
| target_percent | 目标占用百分比 | 94 | 比例模式，低于触发比例，取下整 |
| check_interval_seconds | 定时检查间隔（秒） | 1800 | 最少 300；检查只是读人数，不每次都全量拉成员 |
| check_jitter_seconds | 检查额外随机等待（秒） | 300 | 在基础间隔上额外加 0～该值 |
| on_membership_change | 入退群后触发检查 | true | 事件合并，不为每个事件发一次请求 |
| event_debounce_seconds | 事件检查最短间隔（秒） | 300 | 与定时检查共用互斥和请求预算 |
| cycle_max_days | 一个清理周期最长天数 | 7 | 到期停止，重新达到触发条件才创建新周期 |

绝对/比例两组参数只使用当前 kind 对应一组；界面说明哪些值未生效。日历执行窗口不影响轻量活动记录，扫描可以运行但不得窗口外执行踢人。

## 6. 保护条件（protection）

| 字段 | 中文名称 | 默认值 | 说明 |
| --- | --- | --- | --- |
| whitelist | 永久保护 QQ | [] | 任何规则都不能覆盖 |
| temporary_whitelist | 临时保护 | [] | 每项 qq、expires_at、reason；时区明确 |
| new_member_days | 新人保护天数 | 30 | 允许 0 关闭；入群时间未知仍不能自动踢 |
| recent_activity_days | 最近活动保护天数 | 30 | 有近期可信活动即保护；允许 0 关闭此业务保护 |
| group_level_enabled / group_level_min | 高群等级保护 / 最低级别 | false / 10 | 启用后 >= 保护；字段未认证或未知则暂保 |
| qq_level_enabled / qq_level_min | 高 QQ 等级保护 / 最低级别 | false / 64 | 启用增加查询需求，不以列表 0 代替缺值 |
| title_mode | 头衔保护 | off | off / any_nonempty / allowlist |
| title_allowlist | 保护头衔名单 | [] | 精确字符串，避免正则误匹配 |
| protect_marked_bots | 保护平台标记的机器人 | true | 并入人工 bot 白名单 |
| protect_muted | 保护当前禁言成员 | true | 缺失状态按未知处理 |
| pause_when_all_muted | 全员禁言时暂停 | true | 防止不能发言期间被清理 |
| after_all_mute_grace_days | 全员禁言解除后保护天数 | 7 | 只能对观察到的禁言周期生效 |
| custom_rules_enabled | 启用高级保护条件 | false | rule_clauses 的 protect 阶段，规则组之间 OR |

固定管理员保护不可配置关闭。入群未满门槛也会被候选条件排除，二者即使重复也显示各自原因。

## 7. 候选与活动（eligibility / activity / data）

| 分组.字段 | 中文名称 | 默认值 | 说明 |
| --- | --- | --- | --- |
| eligibility.mode | 候选规则模式 | simple | simple / rules |
| eligibility.minimum_join_days | 入群至少多少天 | 30 | 首版数值必须 >=1；防止刚进群即清理 |
| eligibility.minimum_inactive_days | 至少不活跃多少天 | 90 | >=1；是所有高级规则的共同底线 |
| eligibility.group_level_enabled / group_level_max | 限制最高群等级 | false / 3 | 启用后 <=；与保护相冲突时提示无候选范围 |
| eligibility.qq_level_enabled / qq_level_max | 限制最高 QQ 等级 | false / 32 | 启用后 <=；需要补查有效等级 |
| eligibility.unknown_policy | 候选条件数据不足 | skip_member | skip_member / pause_group；无“按0处理” |
| activity.source | 活动依据 | platform_speech | platform_speech / observed_events |
| activity.count_poke | 戳一戳算活动 | false | 仅 observed_events，有 actor 能力才能开启 |
| activity.count_reaction | 表情回应算活动 | false | 同上，能力不满足则配置报错 |
| activity.unknown_last_speech | 最后发言时间未知 | protect | protect / observe_until_eligible |
| activity.count_window_days | 本地计数窗口（天） | 30 | 活跃天数/消息条数规则使用；统计保留期必须覆盖 |
| data.member_cache_seconds | 扫描成员缓存（秒） | 1800 | 只用于筛选，不能代替执行前复查 |
| data.plan_snapshot_max_age_seconds | 计划数据最大年龄（秒） | 1800 | 过期重建，不消费旧计划 |
| data.qq_level_lookup | QQ 等级补查 | off | off / on_demand；依赖该字段时不能关闭 |
| data.qq_level_cache_hours | QQ 等级缓存（小时） | 24 | 用于筛选；作为保护/执行门槛时执行前重新确认 |
| data.qq_level_batch_limit | 每轮 QQ 等级补查人数 | 10 | 取账号全局预算更小值；额度不足等待，不无限并发 |
| data.group_level_certified | 已比对本群群等级字段 | false | 存入适配器/QQ版本指纹；启用依赖此字段的自动执行前需核验 |
| data.invalid_field_ratio_stop | 必需字段异常暂停比例 | 0.5 | 0～1；超过时暂停群，单个未知仍按保护/候选语义处理 |

`group_level_certified` 表示字段与 UI 的语义已比对，不把零值自动认证为真实 0；零值仍可能是缺失。审核证据和核验版本由诊断记录持久化，仅修改布尔值不能伪造一次通过的诊断。

只有排序用 QQ 等级也需要明确启用补查。补查按初筛候选逐步进行，进度持久保存。QQ 等级是低优先级排序时，可只补需要比较的同优先级区间；跨切分边界的同分成员不能任意截断。若高优先级 QQ 条件要求全查而预算不足，显示“等待补充数据”，不擅自降级规则。

## 8. 高级规则表（rule_clauses）

每行：`policy_id`、`phase`（protect / candidate）、`rule_id`、`field`、`op`、`value`、`enabled`。相同策略/阶段/rule_id 的行 AND，不同 rule_id OR。两个阶段完全分离。

| 可选字段 | 类型 / 单位 | 允许运算 | 边界 |
| --- | --- | --- | --- |
| joined_days | 数值 / 天 | >= > <= < == | 从本次有效入群时间计算 |
| inactive_days | 数值 / 天 | >= > <= < == | 按活动来源计算，unknown 不替代为无限 |
| group_level | 整数 | >= > <= < == | 本群字段核验且值有效 |
| qq_level | 整数 | >= > <= < == | 详情有效数据，需查询预算 |
| observed_message_count | 非负整数 / 窗口内条数 | >= > <= < == | 完整观测覆盖才能作为低活跃依据 |
| observed_active_days | 非负整数 / 窗口内日数 | >= > <= < == | 时区使用群策略时区 |
| has_title | 布尔 | == | 当前平台头衔，不推断永久 |
| currently_muted | 布尔 | == | 当前状态有效才可比较 |

首版不给自由文本名单、昵称模糊匹配或身份猜测作为踢人条件。临时豁免用明确 QQ。所有条件值有限，拒绝空 rule_id、未知字段、非法枚举、永真空组。

如果某条保护规则包含未知字段，按三值逻辑计算后仍 unknown 就保护。某条候选组 unknown，但另一条候选组 true，整体可为 true；仍需通过独立保护和排序数据健康检查。

## 9. 排序配置（ranking / sort_rules）

`ranking.preset`：conservative / low_group_level / longest_inactive / custom。

- conservative 展开为 inactive_bucket desc、group_level asc、inactive_days desc。
- low_group_level 展开为 group_level asc、inactive_days desc；用户显式启用后可再加 qq_level asc。
- longest_inactive 展开为 inactive_days desc、group_level asc。
- custom 读取该策略全部启用的 sort_rules；至少一行。

每行 `policy_id`、`priority`（从1开始）、`field`、`direction`（asc/desc）、`enabled`。允许字段：inactive_bucket、inactive_days、group_level、qq_level、joined_days、observed_message_count、observed_active_days。排序字段最多 6 个，不重复；同序最终使用稳定 user_id。

`ranking.inactive_buckets_days` 默认 `[90,180,365]`，严格递增；`ranking.unknown_sort_value` 默认 defer，可选 last；不是把未知填成 0。`ranking` 分组和 sort_rules 自定义之间必须无歧义，preset 非 custom 时不用自定义行并在校验中提示未使用项。

## 10. 账号频率和额度（account_safety）

以下默认数值只是运维起点，不是 QQ 公布的安全阈值。每个 QQ 账号独立计算，所有群和重载共享。

| 字段 | 中文名称 | 默认值 | 说明 |
| --- | --- | --- | --- |
| shared_guard | 与其他 QQ 插件共用调度 | auto | auto / required；检测到不兼容不能悄悄另建假共享锁 |
| write_gap_min_seconds / write_gap_max_seconds | 账号写操作间隔 | 30 / 90 | 适用于本插件踢人和通知；均 >0 |
| read_gap_min_seconds / read_gap_max_seconds | 网络读取间隔 | 2 / 5 | 同账号串行；本地状态缓存不增加 QQ 网络调用 |
| max_kicks_rolling_24h | 账号24小时移出尝试上限 | 30 | 已提交、结果未知均占额度；不是仅成功才计数 |
| max_reads_hour / max_reads_rolling_24h | OneBot读取额度 | 120 / 600 | 自身调用上限，不等价于 QQ 内部请求次数 |
| max_detail_reads_rolling_24h | 单人成员详情额度 | 80 | 包含补查、执行前后核验，不能全花在排序补查上 |
| max_qq_enrich_rolling_24h | QQ等级增强额度 | 20 | 是详情额度的子预算，给操作核验预留空间 |
| max_notifications_rolling_24h | 账号24小时通知上限 | 30 | 所有群合计，普通通知用尽前预留一条暂停摘要 |
| recovery_min_seconds / recovery_max_seconds | 掉线恢复后等待 | 300 / 900 | 随机等候后重建计划，不集中补执行 |
| failure_threshold | 连续异常暂停阈值 | 3 | 权限、踢人结果未知、明确踢下线等不等累计，立即暂停 |
| failure_cooldown_seconds | 一般异常冷却 | 3600 | 冷却后先只读探测，成功才恢复；严重情况需人工恢复 |
| read_retry_limit | 一次读取最多重试数 | 2 | 指首次之外最多两次；指数退避+抖动；都计入读取额度 |

普通重连/心跳是本地桥接，不为保持“真人活跃”主动发聊天。不会自动重新登录、循环扫码、修改 GUID、开启 NapCat bypass 或伪造输入状态。

## 11. 分群执行与通知（execution / notifications）

| 分组.字段 | 中文名称 | 默认值 | 说明 |
| --- | --- | --- | --- |
| execution.timezone | 时区 | Asia/Shanghai | IANA 时区，持久时间统一 UTC |
| execution.allowed_weekdays | 可执行星期 | [1,2,3,4,5,6,7] | ISO 星期，1是周一 |
| execution.windows | 可执行时间段 | [09:00–21:00] | 支持多个区间；跨午夜需分两段，配置错误不能解释成全天 |
| execution.batch_size | 每批最多人数 | 5 | >0，受人数需求与全局额度限制 |
| execution.max_kicks_rolling_24h | 本群24小时尝试上限 | 20 | 与账号上限取较小剩余额度 |
| execution.batch_gap_seconds | 两批最短间隔（秒） | 3600 | 跨周期、重启保持 |
| execution.candidate_delay_min_seconds / candidate_delay_max_seconds | 每名成员执行前等待 | 30 / 90 | 在共享账号间隔之上取最晚就绪时间，不叠加无限队列 |
| execution.plan_ttl_seconds | 计划有效期（秒） | 1800 | 确认超时需重新预览 |
| execution.verify_wait_seconds | 首次核对等待（秒） | 10 | 优先等成员事件，超时再只读核对 |
| execution.verify_attempts | 最多只读核对次数 | 2 | 核对也受预算限制；未证实则 unknown，绝不重复踢 |
| notifications.enabled | 启用管理员通知 | true | 默认向配置的授权私聊对象发送 |
| notifications.recipient_qq_ids | 通知接收 QQ | [] | 留空使用本群操作人/主管理员，明确显示解析结果 |
| notifications.on_plan / on_batch / on_pause | 计划/批结果/暂停通知 | true / true / true | 批汇总，禁止每名成员刷一条 |
| notifications.dedup_seconds | 相同事件通知去重 | 3600 | 规则版本或状态变化才算新事件 |
| notifications.max_messages_rolling_24h | 本群通知上限 | 10 | 另受账号消息预算约束，预留暂停摘要 |
| notifications.mask_member_ids | 摘要隐藏部分QQ | true | 单人详情需授权；不公开到群 |

通知另受 `account_safety.max_notifications_rolling_24h` 的账号总上限控制。通知失败只记本地待查看事件，不阻塞 pause；confirm 计划未送达也绝不自动执行。恢复后最多一条合并摘要，不补发历史逐人通知。

不提供踢人无限重试数、无视管理员、关闭持久审计、自动永久禁止入群等参数；这些不是普通可调业务条件。

## 12. 日志与存储（全局）

| 字段 | 中文名称 | 默认值 | 说明 |
| --- | --- | --- | --- |
| logging.level | 运行日志级别 | INFO | DEBUG 临时开启；等级变化不关闭审计 |
| logging.max_file_mb | 单文件大小（MiB） | 10 | 正整数，UTF-8；按字节检查 |
| logging.backup_count | 轮转文件数 | 7 | 正整数，不允许 0 变成无限文件 |
| logging.retention_days | 轮转文件最长保留天数 | 14 | 同时限制大小/数量/年龄，谁先达到谁生效 |
| logging.error_dedup_seconds | 相同错误汇总窗口 | 300 | 首次堆栈，后续累计次数；恢复后给摘要 |
| logging.debug_retention_days | 临时调试日志保留 | 3 | 不允许记录密钥和完整 API 原始载荷 |
| storage.audit_retention_days | 已结束审计保留 | 180 | 未解决操作不按日龄删除 |
| storage.member_snapshot_retention_days | 非必要成员快照保留 | 30 | 当前成员最小状态、有效豁免另行保留 |
| storage.activity_retention_days | 每日活动汇总保留 | 400 | 不保存正文；必须覆盖配置的统计窗口 |
| storage.export_retention_hours | 本地导出保留 | 24 | 授权导出，防 CSV 公式注入 |
| storage.max_database_mb | 数据库容量软上限（MiB） | 512 | 先清理可过期历史，仍超限则暂停写操作，不删未决记录 |
| storage.minimum_free_mb | 最低磁盘空闲（MiB） | 256 | 低于阈值暂停执行；只保留故障摘要 |

日志轮转和数据库清理不是同一种操作；日志备份不是用户数据备份。运行数据不进 Git，也不上传到公开仓库。

## 13. 示例及界面必须提示的冲突

[policies.json](../examples/policies.json) 用虚构标识演示两个群：学习群偏保守，公开交流群使用复合条件和 QQ 等级辅助。示例中开关均关闭、模式均为观察；自定义分组为完整值，未出现的分组继承本文默认值。

必须定位到具体群并报错/警告的情况：

- 目标人数高于触发人数；群容量已缩小；百分比取整后两者相等。
- 群等级保护 `>=3`，候选又要求 `>=5`，导致永远无候选；至少显示逻辑冲突，不暗中忽略保护。
- 排序用 QQ 等级但增强查询关闭；群等级用于自动执行却未核验；统计窗口长于保留期。
- 临时白名单已过期、时区非法、窗口重叠、全部星期被禁用。
- 高级规则引用不存在群、空候选组、非法字段、重复优先级。
- 配置每天最多20人但账号每天剩余3次，预览必须显示本批最多3次。
- 依赖所有候选的 QQ 等级排序却只有10次补查预算，显示预估轮次，不宣称排序已经完成。

## 14. 配置帮助的文案标准

每个字段必须提供中文标题、单位、默认值、允许范围、是否可关闭、作用范围、修改后的生效方式。页面说明“有值”和“可信可用”不同。涉及更多查询的条件标记“会增加资料查询”，涉及人工复查的条件标记“数据不足时保留成员”。

不要向用户显示内部锁键、SQL 状态或散列值作为主要状态；显示“等待执行窗口”“账号冷却至…”“待补充QQ等级”“无法确认成员已离群”等可采取行动的原因，诊断详情再提供技术字段。
