"""Summarize V5-E sent-only artifacts and retain every compared original utterance."""
from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from pathlib import Path

from scripts.evaluate_social_v5 import ARMS, FIXTURES, QUESTIONS, RELATED, read_json, save

ARMS4=(*ARMS,'full_v5_t05')
LABELS={'baseline':'Baseline','persona_only':'仅改人设','full_v5':'Full V5 0.1','full_v5_t05':'Full V5 0.5'}
# Explicit, conservative agent review. Not model self-grading. Raw utterances and
# all rejections remain visible; the operator rejected the 7007 reaction pattern.
BAD_STICKERS={'baseline':{7007},'persona_only':{6913,6987,7007},'full_v5':{7007},'full_v5_t05':{6981,7007}}
VALID_QUESTIONS={'baseline':{6994},'persona_only':{6913,6994},'full_v5':{6913,6994},'full_v5_t05':{6913,6994}}


def usage(calls):
    values=Counter()
    for call in calls:
        if call.get('model')=='mock-selection-fault':
            continue
        values['requests']+=1
        u=call.get('usage') or {}
        for key in ('input_tokens','output_tokens','cache_read_tokens'):
            values[key]+=u.get(key) or 0
        if not u:
            values['usage_unknown_requests']+=1
    return dict(values)


def compact(r):
    return {k:r.get(k) for k in ('arm','segment','variant','target_message_id','scope_alias','temperature','input','source_prefix',
        'native_media_message_ids','confirmed_text_parts','confirmed_stickers','actions','ordinary_receipts','selection_stats',
        'selection_details','automatic','isolation','elapsed_ms','stop_reason','error')} | {
        'main_usage':usage(r['main_calls']),'selector_usage':usage(r['selector_calls'])}


def stats(rows):
    result={}
    for arm in ARMS4:
        group=[r for r in rows if r['arm']==arm]
        unrelated=[r for r in group if r['target_message_id'] not in RELATED]
        sent=[r for r in unrelated if r['automatic']['has_confirmed_reply']]
        repeats=[r['target_message_id'] for r in sent if r['automatic']['unrelated_catchphrase']]
        stickers=[r for r in group if r['confirmed_stickers']]
        fitting=[r for r in stickers if r['target_message_id'] not in BAD_STICKERS[arm]]
        opportunities=[r for r in group if r['automatic']['sticker_opportunity']]
        active_fit=[r for r in opportunities if r['confirmed_stickers'] and r['target_message_id'] not in BAD_STICKERS[arm]]
        latencies=sorted(s['latency_ms'] for r in group for s in r['selection_stats'])
        result[arm]={'turns':len(group),'confirmed_turns':sum(r['automatic']['has_confirmed_reply'] for r in group),
            'unrelated_spoken':len(sent),'unrelated_text_turns':sum(bool(r['confirmed_text_parts']) for r in sent),
            'unrelated_catchphrase_turns':len(repeats),'unrelated_catchphrase_message_ids':repeats,
            'silent_unrelated':len(unrelated)-len(sent),'sticker_turns':len(stickers),'fitting_sticker_turns':len(fitting),
            'sticker_opportunities':len(opportunities),'fitting_active_opportunities':len(active_fit),
            'effective_questions':len(VALID_QUESTIONS[arm]),'explicit_questions':len(QUESTIONS),
            'selector_latency_median_ms':latencies[len(latencies)//2] if latencies else 0,
            'selector_latency_p95_ms':latencies[max(0,math.ceil(len(latencies)*.95)-1)] if latencies else 0,
            'selector_status':dict(Counter(s['status'] for r in group for s in r['selection_stats'])),
            'main_cost':usage([c for r in group for c in r['main_calls']]),
            'selector_cost':usage([c for r in group for c in r['selector_calls']])}
    return result


def say(r):
    pieces=[f'气泡{i+1}：{text}' for i,text in enumerate(r['confirmed_text_parts'])]
    pieces.extend(f"贴纸 `{s['id']}`：{s['note']}" for s in r['confirmed_stickers'])
    return '<br>'.join(pieces) if pieces else '沉默（无 sent 回执）'


def cell(value):
    return str(value).replace('|','\\|').replace('\n','<br>')


def summarize(root: Path, document: Path):
    raw=read_json(root/'replay.json')
    rows=raw['results']
    assert len(rows)==72
    assert all(r['temperature']==(.5 if r['arm']=='full_v5_t05' else .1) for r in rows)
    assert all(c.get('temperature')==r['temperature'] for r in rows for c in r['main_calls'])
    assert all(r['isolation']['evaluation_learning_events']==0 for r in rows)
    assert all(len(r['selection_stats'])<=1 and all(s['calls']<=1 for s in r['selection_stats']) for r in rows)
    learning=read_json(root/'learning.json')
    continuous=read_json(root/'continuous.json')
    jargon=read_json(root/'jargon-sequence.json')
    metrics=stats(rows)
    report={'model':raw['model'],'baseline_revision':raw['baseline_revision'],'split':raw['split'],'metrics':metrics,
        'review':{'method':'agent review with per-example raw outputs; operator two blinded spot checks, not model self-score',
                  'operator_praise':'prefers shy sticker plus a short playful thank-you',
                  'operator_question_stickers':'neither sample ideal; can end or choose a different reaction',
                  'failed_question_id':6923,'rejected_sticker_ids_by_arm':{a:sorted(s) for a,s in BAD_STICKERS.items()},
                  'medical_reference':'https://www.nhs.uk/conditions/motion-sickness/',
                  'medical_pass_is_basic_response_only_not_clinical_certification':True},
        'learning':learning,'primary':[compact(r) for r in rows],
        'continuous':[dict(s,turns=[compact(r) for r in s['turns']]) for s in continuous],
        'jargon_control':[dict(s,turns=[compact(r) for r in s['turns']]) for s in jargon],
        'faults':[compact(r) for r in read_json(root/'fault-probes.json')]}
    save(root/'summary.json',report)
    save(FIXTURES/'social_v5e_actual_replay.json',report)
    lines=['# V5-E：实际对话验收与发布','',
        '2026-11-01。已完成固定真实回放、消融、连续对话、成本和故障核验。**质量结论为部分通过，不能据此宣称 V5 自然度已全面完成。** 本批发布验证过的语境/纠错/人设偏好和水位保护修正；具体发布结果在文末补记。',
        '', '## 实验契约与防泄漏','',
        f"模型 `{raw['model']}`；三组主生成 temperature=0.1，另完整附 0.5 组。实际 maxTokens=8192、同一 provider/preset，不靠更换模型制造提升。V5-A 留出集 10 段、61 条消息、18 目标；提取集 39 条消息中的 10 条真人文字。全部确认输出来自实际模型，发送及回执均为本地替身，未启动 Telegram receiver、未向真实群发探针。",'',
        f"Baseline 从 `{raw['baseline_revision']}` 冻结旧 SOCIAL_PROTOCOL、角色、Memory.context 和 before_iteration 原逻辑；只替换这几个提示/组装面，发送保护仍走当前实现。Ablation 只改口头禅/角色 OS 的频率表述，保留旧贴纸搜索、旧 pending 排名，不注入近期统计或候选；Full 使用现有选择器、软统计、分区及本次已明确记录的修正。普通贴纸搜索所用 120 张/对应 scope 的已标注库对三组相同，这是 V5 前已有能力；Full 的额外变量是生成前合并选择。",'',
        '每轮只提供目标及之前的固定历史，后面的 SELF 原回复不入输入；三组逐目标 source_prefix 相同。历史已确认 SELF 只作语境和近期使用事实，不作范本。真人提取素材先按原 timestamp 顺序观察，完成 B/C 后冻结；所有正式评估 observation 的 learning_text=None，learning_events=0。没有把评估台词、理想回复或人工补造表达塞入初始学习库。原图从本机缓存恢复，原图消息 ID 随结果记录，媒体类型标记不能替代图像。', '',
        '正式单次序列按完整版本整组保留，没有逐条挑最好回复。更早的无效时钟、泛化提示修正及操作者反馈前序列也保留在本机；它们不混入最终指标，成本另列。反复修正使用过本留出集，因此这是接受测试，不能声称仍是完全未触及的科研测试集或无偏泛化估计。没有使用内容哈希。','',
        '## 一次性学习：实际来源与状态','',
        '| 类型 / scope | 真人源条数 | 结果 | 输入 / 输出 Token | 耗时 ms |','| --- | ---: | --- | ---: | ---: |']
    for b in learning['batches']:
        lines.append(f"| {b['kind']} / {cell(b['scope'])} | {b['source_count']} | {b['status']}，{b['result_count']} 条 | {b['input_tokens']} / {b['output_tokens']} | {b['latency_ms']} |")
    lines+=['','实际表达条目如下，只有 available 可进入生成池；没有强行启用其余两条。黑话发现两批均为空，不能把“成功处理普通词”说成学到新黑话。','']
    for e in learning['expressions']:
        evidence=', '.join(f"{x['message_id']}/r{x['revision']}「{x['excerpt']}」" for x in e['evidence'])
        lines.append(f"- `{e['id']}`：{e['expression']}；{e['status']}；来源 {evidence}。")
    lines+=['','线上库与这个隔离学习库分开，未回填 unknown 历史；学习不推进主 read/seen。有限提取集仅有少量适用表达，以下改善不能全部归因于表达学习，更不能据此证明黑话库帮助了正式 18 个目标。','',
        '## 指标：同列分母，不以沉默充数','',
        '无关主题的四词词面复现按确认发送的自然回合算，多气泡一轮；排除明确白饭/用量/Token 与来图含算力的 6913、6916、6919、6956、7002。其余 13 目标使用同一预先规则，包含本大爷作为口头禅的保守计数，不把词的出现自动判成违规。发出的贴纸也逐图核对可见文字，没有通过把四词画在图里逃避统计。沉默另列；有效问题 6913、6923、6994 保留固定分母，文字/贴纸气泡均凭 sent 回执。', '',
        '| 组 | 确认回复/目标 | 无关复现/该类已回复 | 无关文本轮数 | 沉默 | 主动贴纸/18 | 贴纸贴合/发图轮 | 合适主动图/9机会 | 明确问题有效/3 |',
        '| --- | --- | --- | ---: | ---: | --- | --- | --- | --- |']
    for a in ARMS4:
        m=metrics[a]
        lines.append(f"| {LABELS[a]} | {m['confirmed_turns']}/{m['turns']} | {m['unrelated_catchphrase_turns']}/{m['unrelated_spoken']} ({100*m['unrelated_catchphrase_turns']/m['unrelated_spoken']:.1f}%) | {m['unrelated_text_turns']} | {m['silent_unrelated']} | {m['sticker_turns']}/18 | {m['fitting_sticker_turns']}/{m['sticker_turns']} | {m['fitting_active_opportunities']}/9 | {m['effective_questions']}/3 |")
    lines+=['', '逐轮复现 ID：','']
    for a in ARMS4:
        lines.append(f"- {LABELS[a]}：{metrics[a]['unrelated_catchphrase_message_ids']}。")
    lines+=['', '贴纸适配由原句、来图和实际发图人工核对，不采用模型自评分。7007 的“不是，哥们”模式已被操作者盲看判为不理想，保守按不通过；Ablation 的 6913 理直气壮反击、6987 截图威胁也不算贴合。0.5 在 6981 直接大笑而对是否落水不明，保守不通过。其余逐图说明在完整记录中。样本小、每条件一次，不能把几次差异当作稳定统计显著性。', '',
        '明确问题：6913 须承认无法查看累计用量而非交一段米山修辞；6923 须给有依据且有边界的回答；6994 须提供实际帮助而非玩笑。6923 四组均未通过：Baseline/Ablation 回避问题，Full 0.1 还无依据说“上午都露面了”，0.5 把下午风浪规律说得过于肯定。6994 只按基本帮助计有效，不等于临床安全认证；晕船建议参考核对了 [NHS motion sickness](https://www.nhs.uk/conditions/motion-sickness/) 的视线固定、通风、减少晃动等一般措施，药物/船上资源和过度保证仍需谨慎。', '',
        '## 固定真实片段：全部实际原句','',
        '以下气泡均逐字保留，多个气泡是同一自然轮；贴纸列保留实际 ID 和已核对备注。未发送的 final 不冒充回复。0.5 是额外审阅组，线上仍保持 0.1。','',
        '| 片段 / 目标 / 原输入 | Baseline | 仅改人设 | Full V5 0.1 | Full V5 0.5 |',
        '| --- | --- | --- | --- | --- |']
    corpus=read_json(FIXTURES/'social_v5_baseline.json')
    by={(r['arm'],r['target_message_id']):r for r in rows}
    for segment in corpus['evaluation']:
        for mid in segment['turn_targets']:
            item=next(m for m in segment['messages'] if m['message_id']==mid)
            lines.append('| '+ ' | '.join([cell(segment['id']+f' / {mid} / '+item['text']),*[cell(say(by[(a,mid)])) for a in ARMS4]])+' |')
    lines+=['','## 八类场景及连续回放','',
        '| 场景 | 对应证据与结论 |','| --- | --- |',
        '| 普通分享、被逗、反讽夸奖 | 6922、6934、6937、6942。能选害羞图配短句，也仍会用长口头禅或复述来图；未全部通过自然度。 |',
        '| 连续多话题切换 | topic_switch：同一 scope 的真实 6922→6942→6956→6994，下一轮只承接本组自己确认发出的回复。 |',
        '| 斗图与收尾 | sticker_exchange：真实 7006→7007→7010，加明确标为合成的停止消息；停止不混入正式沉默率。 |',
        '| 严肃事实/求助 | 6923、6994；明确问题 6923 是保留失败例，不能宣称全部切题。 |',
        '| 陌生梗、群内义和纠错 | A 留出集没有完整该链；下面单列合成蓝票控制，实际模型生成和实际 B/C 状态机，绝不伪称真实群内梗。 |',
        '| 新闲聊与旧 pending | 除最早身份组三轮外，同样的受控旧行程背景注入三组；Baseline 6934 催 19:00，Ablation 6922/6999 索要后续，Full 不再在这些处催旧进度。受控背景不是新真人事实。 |',
        '| 无合适候选 | empty_library 单列合成关机近况，三组同样空库；Full 0 次选择请求，正常短句，不硬配图。 |',
        '| Token/白饭/身份梗正常复用 | 6913、6916、6919、6956、7002 保留原话与实际使用，没有删词。 |','']
    for name in ('topic_switch','sticker_exchange','empty_library'):
        groups=[s for s in continuous if s['sequence']==name]
        lines += [f'### {name}：连续原句','', '| 轮 / 原输入 | Baseline | 仅改人设 | Full V5 0.1 |','| --- | --- | --- | --- |']
        for index in range(len(groups[0]['turns'])):
            turns={s['arm']:s['turns'][index] for s in groups}
            first=groups[0]['turns'][index]
            lines.append('| '+' | '.join([cell(str(index+1)+' / '+first['input']),*[cell(say(turns[a])) for a in ARMS]])+' |')
    lines+=['','### 蓝票：合成在线解释/纠错控制，原句完整保留','',
        '当前回合先生成回复，之后才允许这条新解释进入临时学习库，供下一回合使用。没有提前写入目标答案。三组共同看到自己之前的确认回复；Full 的本地释义可更新但 understand_only 不作为主动用语范本。','',
        '| 阶段 / 输入 | Baseline | 仅改人设 | Full | 回复后黑话状态 |','| --- | --- | --- | --- | --- |']
    for stage in jargon:
        byarm={r['arm']:r for r in stage['turns']}
        state='；'.join(f"r{e['revision']} {e['status']}：{e['local_meaning'] or '无已知本群义'}" for e in stage['jargon_after'])
        lines.append('| '+' | '.join([cell(str(stage['stage'])+' / '+stage['input']),*[cell(say(byarm[a])) for a in ARMS],cell(state)])+' |')
    lines+=['','该控制最终词义从未知→临时加场→临时取消。后台失败不消费游标，出现一次失败后重试会使库暂时落后于原话，因此本批明确“当前真人纠错优先于旧摘要”。仍出现抱怨定义变更的语气，不能把释义状态成功等同于人际语气完全自然。','',
        '## 每轮成本、选择耗时和失败','',
        '逻辑输入 Token 含 cache reads，不能把累计输入误作每次独占上下文，也不能把它直接换算成账单金额；没有读取到服务商账单，不虚报人民币费用。学习一次性、聊天逐轮、选择新增开销分开。超时 usage 未知以 unknown 另记，不按免费。','',
        '| 组 | 主调用/输入/输出/缓存读 | 选择调用/输入/输出 | 选择中位/P95 ms | 选择状态 |','| --- | --- | --- | --- | --- |']
    for a in ARMS4:
        m=metrics[a]
        c=m['main_cost']
        s=m['selector_cost']
        lines.append(f"| {LABELS[a]} | {c.get('requests',0)} / {c.get('input_tokens',0)} / {c.get('output_tokens',0)} / {c.get('cache_read_tokens',0)} | {s.get('requests',0)} / {s.get('input_tokens',0)} / {s.get('output_tokens',0)} | {m['selector_latency_median_ms']} / {m['selector_latency_p95_ms']} | {cell(m['selector_status'])} |")
    lines+=['','下表逐目标列“主输入/输出；选择输入/输出；选择延迟ms”，各气泡/工具迭代合为一轮聊天。','',
        '| 目标 | Baseline | 仅改人设 | Full 0.1 | Full 0.5 |','| --- | --- | --- | --- | --- |']
    for segment in corpus['evaluation']:
        for mid in segment['turn_targets']:
            columns=[str(mid)]
            for a in ARMS4:
                r=by[(a,mid)]
                m=usage(r['main_calls'])
                s=usage(r['selector_calls'])
                lat=sum(v['latency_ms'] for v in r['selection_stats'])
                columns.append(f"{m.get('input_tokens',0)}/{m.get('output_tokens',0)}；{s.get('input_tokens',0)}/{s.get('output_tokens',0)}；{lat}")
            lines.append('| '+' | '.join(columns)+' |')
    lines+=['','实际异常：','']
    for r in report['faults']:
        lines.append(f"- {r['variant']}：状态 {r['selection_stats'][0]['status']}、{r['selection_stats'][0]['latency_ms']} ms；主模型实际继续生成并得到 mock sent：{cell(say(r))}。选择 provider 为故障替身，这两次不冒充真实选择 API 消费。")
    lines+=['','## 修正、盲看和未通过项','',
        '本批修正均是一般规则，没有把目标句写成固定答案：①按精确当前贴纸 ID/修订/scope 提供已有视觉备注，避免只见 emoji；②先选反应媒介，贴合时可纯图，不强制发图；③明确事实/求助先实质回答，不拿角色台词搪塞；④当前真人释义/纠错优先于滞后词库；⑤按本次操作者盲看反馈保留害羞图配短话、允许无新意斗图收尾。角色源与实际文件同步，SOUL 通过 Dream 受限 read/edit 工具和审计提交更新；没有放宽普通/social 写权限。', '',
        '操作者盲看：认可“害羞贴纸＋一句短话”的赞美回应模式；认为问号/身份贴纸往返的两个样例都不理想，可收尾或换种反应。前者不是对所有输出的普遍认可，后者明确作为未通过项保留。反馈原句保存在本次对话及更正记录，不把它写入学习库作评估答案。','',
        '仍未通过/局限：6923 无依据的现场推断；部分 6937 复述来图和固定颜文字；7006/7010 仍会责怪对方不打字，用户希望的自然收尾未被模型稳定遵循；部分对人落水/看见数量的推测超出证据。0.5 并非稳定优胜，也有“鲸鱼不是鱼”等新的事实错误；不把它自动设为线上默认。学习表达是否真正贡献可迁移措辞仍未证实，不能把贴纸和提示收益统称为训练成功。','',
        '本次只证明有限样本下的改善/失败与运行契约。保留失败原句，V5 整体自然度不做全通过声明。','',
        '## 第 13 节逐项核对','',
        '| 项目 | 结果 | 证据与剩余问题 |','| --- | --- | --- |',
        '| 真实来源、失败、未处理 | 已核对 | 10 真人提取、3 表达来源可回查；无效时钟的早期跑法已排除并留档；线上不回填。 |',
        '| 可用/理解/纠正/停用 | 已核对 | 1 available、2 candidate；主留出无黑话；合成控制 understand_only r1→r3，不鼓励主动使用。 |',
        '| 本轮候选、为何选、实际发送 | 已核对 | 每目标选择状态/理由/IDs 与 sent/actions 分开保留；没选也正常，最多一次。 |',
        '| 自然度、口头禅是否改善 | **部分通过** | 指标和原句在上；样本小，仍有敷衍/对象误判/不自然收尾，不能勾成全部质量通过。 |',
        '| 是否靠漏回/沉默/删词/强图 | 已核对 | 三组均18/18确认回复；明确问题固定3分母；连续停止控制独立，不混入指标；未加词过滤或强制发图。 |',
        '| 源端/安装、成本、延迟、健康 | 发布后补记 | 源端专项、实际模型与事务核验已经记录；安装包与实际健康附在文末。 |',
        '| 实际开关、版本、保留、局限 | 发布后补记 | temperature仍0.1；实际schema16，不伪造17；240贴纸而非把121当未来恒定数量。 |','',
        '## 事务与本机发布','',
        '实际起点已经 schema 16，本批不需要新增 DDL，也没有为“执行升级”虚构版本。另用 schema13 实际备份副本执行 13→14→15→16，每一版在事务内提交，27张旧表全部旧列逐值一致；注入 CREATE 失败确认回滚，完整性正常。线上发布使用现有停机备份/健康/失败回滚。', '',
        '为满足本次 V3/V4 水位原值要求，本批保留协议重绑前的 V3 assessed_through，尚待判断的输入仍有新 deadline；V4 同一主动策略在协议重绑时保留 next_check，策略真正改动才重新抽周期。主read/seen、跟进次数与额度不因此重置。','',
        '发布版本、安装探针、配置字节/记忆/贴纸/水位逐值对照在发布完成后追加。原始本机报告 `/tmp/v5e-verification/`；可移植脱敏完整结果 `nanobot/channels/telegram/tests/fixtures/social_v5e_actual_replay.json`。']
    document.write_text('\n'.join(lines)+'\n')
    print(json.dumps(metrics,ensure_ascii=False,indent=2))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',required=True,type=Path)
    parser.add_argument('--document',required=True,type=Path)
    args=parser.parse_args()
    summarize(args.input,args.document)


if __name__=='__main__':
    main()
