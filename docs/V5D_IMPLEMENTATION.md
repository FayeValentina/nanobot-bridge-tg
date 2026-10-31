# V5-D：按语境表达

2026-10-31。V5-D 已实施并完成实际模型生成验证；发布结果见本文末尾。SQLite 保持 **schema 16**，主 Social 协议升至 **6**。V5-E 的同配置前后自然度对照和长期实际反馈仍待执行，不把本批三例链路验证当成整体自然度验收。

## 生成前的单次合并选择

新增通道内 `social/selection.py` 和 `templates/telegram/social_expression_selection.md`，通过已有 `_SocialExposureHook.before_iteration` 接入实际 AgentLoop/Runner；未向核心循环另写群聊分支，也未新增学习调度器。

输入来自**本次请求真正携带的观察快照**：完整快照正文匹配现有 view，取末尾最多 8 条，包含 message_id/revision、说话人、引用、回复对象和媒体类型。首次生成时 view 尚未绑定 turn，也能选择；选择本身不标记 seen/read。SQL 粗筛只用最新 4 条非 SELF 文本及引用，实际意图、反讽、分享、求助仍由模型判断，关键词和场景提示只扩召回、不决定必须玩梗。

| 阶段 | 预算与处理 |
| --- | --- |
| SQLite 粗筛 | 当前 scope、可用状态和有效期过滤；关键词、已有 scene_tags 匹配优先，同分按 intent_category/备注前缀分散。无向量、不取全局最热条目 |
| 有界候选池 | 表达最多 10、黑话最多 20、已标注贴纸默认 40（可配 5–80），共默认最多 70。贴纸扫描当前 scope 的整库，按备注匹配排序后 LIMIT，因此第 125 张也能入选 |
| 模型精排 | 一个合并请求，直接 `provider.chat`，无重试或 fallback；工具关闭、temperature=0、reasoning_effort=none。短请求编号只存在本次内存，转换回原条目/贴纸 ID 后注入 |
| 最终参考 | 最多 **3 表达 / 5 黑话解释 / 5 贴纸**，默认合计 4000 字符；超字符预算的条目不算实际选中。可全部为空 |
| 截止时间 | `selectionTimeoutSeconds=2`，可调 0.05–2 秒；扣除粗筛耗时再等模型。用 `asyncio.wait` 截止后取消请求，即使 provider 吞掉 cancellation 也立即返回空，迟到结果无写库路径 |
| 缓存/修订 | 按 scope/turn 以及原 message_id/revision/相关值直接比较；同轮工具迭代复用。输入或条目变更返回空，不在同一自然轮再次调用。新轮才能重选；DB 调用记录也防止缓存淘汰/重启买到第二次调用。无内容哈希 |
| 降级 | 无库、关闭或 provider 不可用不调用；超时、模型错误、非法 JSON/编号、过期/停用/修订冲突均静默空候选，主模型继续 |

`learning.selection.provider/model` 可独立配置；默认沿用现有普通 preset 的模型/provider。`selection` 旧的通用 timeout/retries 不用于热路径：本批以独立的 2 秒硬上限和一次调用优先。不修改主模型、V3 模型、凭据或温度。

候选贴纸携带已看图保存的视觉/情绪/可用场景备注，主模型可以按原 ID 直接 `social_sticker(action="send")`，无须先写完整文字再搜索，也无须再看完整贴纸包。仍可选择纯文字、纯贴纸、贴纸加短句或沉默；协议与角色明确不要复述画面。推荐不自动发送，不消耗发送额度，真实工具和回执仍是发送依据。

黑话 `understand_only` 可进入“解释”参考，明确不鼓励主动使用；candidate/disabled 不入池。它不能被 `confirm_use` 记成主动自然用语。群内义优先于公开义，错误/旧版候选不会恢复。

## 真实发送与软性去重

查询最近最多 80 条**status=sent** 的 social actions 和普通 message 回执；后者按回执 message_ids 关联本 scope 的真实 SELF 观察记录。queued、failed、unknown、草稿均不入统计，即使 unknown 附带 ID 也不计。没有把候选或模型 final 当成实际已说的话。

按 turn_id 合并多气泡和同轮贴纸，默认最近 6 轮，可配 `recentUsageTurns=1–10`。保留每轮最多 240 字符的原话片段供句式参考，并统计四个既有口头禅、重复开头/结尾和贴纸；每轮每项最多计一次。提示明确“最近刚用过这些，无关话题换种自然说法”，**不删词、不设黑名单**，聊 Token/白饭、引用解释时照常接话。普通 message 的历史片段依赖仍保留的观察记录，本批不新建整份永久发言档案。

选中与使用分开：只有推荐贴纸通过真实 `sent` 回执且 ID 相同时，才确认该贴纸的使用关联。发了其他文字不会顺便把选中的表达/黑话都记成已用，也不靠字面包含词语猜测“解释”还是“主动使用”。软去重独立读取实际发送原文，不要求表达库归因成功。

## 人设源与 Dream 摘要

已同步修改仓库 `docs/harness/groupmate.md` 与实际 `telegram-social/roles/groupmate.md`：移除“动不动”“嘴上经常挂着”及含四个固定词的 OS 范例，保留肥鱼、嘴硬、摸鱼和按语境吐槽，改成极偶尔自然出现；贴纸先看本轮推荐，不足再搜。增加旧 pending 不驱动新闲聊的偏好。

实际 SOUL 原本没有上述强制口头禅句子，但有较旧的贴纸策略。通过**专门的确定性 Dream 更正任务**调用 `MemoryStore.build_dream_tools()` 的 read/edit 工具更新派生摘要，不放宽普通/social 工具的 Dream 文件写入归属。Dream 审计提交 **2cd3d01d**，记录来源角色文件、日期和更正；追加 permanent/correction 历史 cursor **64**，后续 Dream 不应从旧摘要恢复高频口头禅要求。没有替普通 Dream 推进未处理历史游标。

修改前角色与 SOUL 备份：实际部署根目录 `backups/v5d-persona-source-before/`。更正验证记录 `/tmp/v5d-verification/persona-correction.json`。SOUL 更正不修改 USER/MEMORY 或运行凭据。

## 事实、待办与参考分区

主模型动态上下文保留既有实时记忆入口，分为 `memories`（当前事实/偏好及证据状态）、`pending_matters`（未完成事项）、`optional_expression_references`、`recent_confirmed_usage`，并附用途说明。候选选择不读旧 pending 来替代眼前话题。

普通回合的 pending/active followup 不再获得“同一个成员又说话 +8”“active +12”的召回优势，只凭当前话题字面相关性进入待办区，且明确不是本轮任务。新闲聊、分享和斗图不能仅因旧事项没完成而催进度。V4 的显式 memory opportunity 按 source memory_id 取对应事项，原主动周期、概率、额度、两次回执契约及等待机制保持；压缩后仍能取到该机会的实时版本，完成/放弃后撤回。

动态上下文每轮迭代重新校验，模型调用后/失败/finally 恢复原始 user/tool 内容；不会把候选说明和旧索引永久写入会话或 Dream 范本。V3 广泛放行、共享普通工具权限和所有发送限额保持。

## Ring Buffer 与状态

复用 V5-A `SocialLearning.record_selection`，每 scope 最近 **25 轮**内存记录候选 ID、是否选中、原因、输入 message_id/revision、模型、调用数、Token、耗时和降级状态，输出结构化日志。`social_get_state.learning.recent_selections` 实时可查，另显示 stage=contextual_expression、selector_enabled/model/timeout。修订导致缓存撤回也在内存标注 reuse_status。

SQLite `expression_turns` 只存调用/Token/耗时/候选数/状态，calls 可为 0；`expression_selections` 只存实际选中 ID/修订和严格确认的使用回执。未选详情不入 DB，重启后内存说明清空，不引入分布式锁、租约或输入摘要。

## 验证证据与边界

V5-D 专项 **20 项通过**，覆盖 125 张贴纸中的关键词召回、scope、空库/严肃空选、单次缓存、直接值/修订撤回、伪造编号/超量/非法 JSON、吞取消的硬超时及迟到丢弃、三路径 sent 统计和多气泡合轮、pending 隔离、完整 AgentLoop 贴纸直发/确认使用/上下文不持久化、选择报错后正常文字回答、Token 非禁词、缓存清空不重复调用与字符预算。相关模块严格类型检查零错误/警告。V4/共享工具/普通 social 路径组合 **115 项通过**。

`scripts/evaluate_social_selection.py` 在临时库复制实际最大 scope 的 **120 张已标注贴纸**，加入明确标为 synthetic 的表达和旧行程，使用实际 **deepseek-flash** 跑既有 post-V3→主 AgentLoop→选择→主生成→工具→sent 回执链路。发送器全部为本地替身，无 Telegram receiver，无真实群探针，合成条目不写线上库。

| 实际模型场景 | 选择耗时/结果 | 主回复与回执 |
| --- | --- | --- |
| “我啥也没干，怎么又赢了哈哈” | **1247 ms**；1 条表达、1 张贴纸 | 直接按推荐 ID 发贴纸，再配一句短话；未搜索；两次发送均 sent，未提旧海边行程 |
| Python 空列表 IndexError 严肃求助 | **1023 ms**；空候选 | 解释空列表索引并给切题代码；两气泡 sent，没有硬塞梗或催旧事 |
| 明确 Token/实际账单话题 | **595 ms**；空候选 | 允许正常谈 Token，说明看不到账单；两气泡 sent，没有歪到行程 |

最终三例选择合计 **3 请求，7050 输入 / 80 输出 Token**；主生成 **118211 输入 / 1325 输出 Token**（多轮累计，含缓存，不是单次独占上下文），报告 `/tmp/v5d-verification/actual-generation-compact.json`，日志 `/tmp/v5d-model-compact.log`。更早的未压缩调用验证中出现 2 秒 timeout，主回复照常继续；因此缩短请求编号/理由并显式关闭选择推理，没有放宽截止时间。

这些是链路验证，**不证明总体口头禅比例已下降**：账单例仍出现了本大爷/白饭，严肃例仍带颜文字。软提示不会保证模型绝不复读，词本身也不是禁用项。粗筛仍可能漏召回，没有字面交集时的分散候选不保证语义最优；模型/网络慢时仍会空降级。V5-E 继续负责同配置 baseline/仅人设/学习库对照、重复频率和实际使用反馈。

## 本机发布

完整既有 `pipeline.sh release` 的测试、构建、自动备份、部署和健康检查结果在发布完成后补记。实际配置不必重写即可使用选择器默认值；线上表达/黑话目前为空，现有 240 张 scope 贴纸可直接进入选择流。
