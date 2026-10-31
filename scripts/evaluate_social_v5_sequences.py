"""Continuous V5-E follow-ons. Real held-out turns and explicitly labelled controls."""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import shutil
from pathlib import Path

from loguru import logger

from nanobot.bus.queue import MessageBus
from nanobot.channels.telegram.social.models import ObservedMessage
from nanobot.channels.telegram.social.runtime import TelegramSocialRuntime
from nanobot.config.loader import load_config
from nanobot.providers.factory import make_provider
from scripts.evaluate_social_v5 import (
    ARMS,
    FIXTURES,
    SCOPES,
    make_expression_provider,
    read_json,
    replay_one,
    save,
    settings,
    source_assets,
)


def message(mid, text, at):
    return {'message_id':mid,'revision':1,'seq':mid,'at':at,'sender':'participant-1','source':'synthetic_human_text',
            'text':text,'media':[],'reply_to':None,'learnable':False,'confirmed_turn':None}


def append_reply(history, result, assets, alias, stage, arm_index):
    # The next turn sees its OWN confirmed reply, never another arm's output.
    for index, text in enumerate(result['confirmed_text_parts']):
        item=message(9000000+arm_index*100000+stage*100+index,text,history[-1]['at']+.1*(index+1))
        item.update({'sender':'SELF','source':'self','confirmed_turn':f'generated-{arm_index}-{stage}'})
        history.append(item)
    for index, sticker in enumerate(result['confirmed_stickers']):
        mid=9500000+arm_index*100000+stage*100+index
        row=next(r for r in assets['stickers'][alias] if r['id']==sticker['id'])
        item=message(mid,'[sticker]',history[-1]['at']+.1)
        item.update({'sender':'SELF','source':'self','media':['sticker'],'confirmed_turn':f'generated-{arm_index}-{stage}'})
        assets['messages'][mid]={'scope':row['scope'],'media_refs':'['+row['media_ref']+']'}
        history.append(item)


async def evaluate(args):
    corpus=read_json(FIXTURES/'social_v5_baseline.json')
    frozen=read_json(FIXTURES/'social_v5e_baseline_code.json')
    config=load_config(args.config)
    source=source_assets(args.source_db,corpus)
    originals={m['message_id']:m for s in corpus['evaluation'] for m in s['messages']}
    main=make_provider(config,preset=config.resolve_preset())
    selector,_=make_expression_provider(config,settings(config).learning.selection)
    reports=[]
    chosen=args.arm or list(ARMS)

    async def sequence(name, alias, targets, initial=(), stop=False, empty=False):
        async def arm_run(arm_index,arm):
            assets=copy.deepcopy(source)
            history=[copy.deepcopy(originals[mid]) for mid in initial]
            turns=[]
            for stage,mid in enumerate(targets):
                item=copy.deepcopy(originals[mid])
                history.append(item)
                segment={'id':name,'scope':alias,'messages':history}
                result=await replay_one(config,corpus,assets,frozen,args,segment,mid,arm,main,selector,
                                        variant=name,library_empty=empty)
                turns.append(result)
                append_reply(history,result,assets,alias,stage,arm_index)
            if stop or empty:
                mid=810000+arm_index*100+(1 if stop else 2)
                text='先不斗图了，我歇会儿，这条不用回。' if stop else '实验记录已归档，我先关电脑了。'
                history.append(message(mid,text,max(m['at'] for m in history)+60 if history else 1793500000.))
                result=await replay_one(config,corpus,assets,frozen,args,{'id':name,'scope':alias,'messages':history},mid,
                                        arm,main,selector,variant=name,library_empty=empty)
                turns.append(result)
            return {'sequence':name,'arm':arm,'origin':'real_holdout_own_reply_continuation' if targets else 'explicit_synthetic_control',
                    'synthetic_stop':stop,'turns':turns}
        return await asyncio.gather(*(arm_run(i,a) for i,a in enumerate(chosen)))

    # Same-scope real messages, with generated replies instead of historical SELF replies.
    if not args.jargon_only:
        reports.extend(await sequence('topic_switch','topic',[6922,6942,6956,6994]))
        reports.extend(await sequence('sticker_exchange','topic',[7006,7007,7010],initial=[7005],stop=True))
        reports.extend(await sequence('empty_library','main',[],empty=True))
        existing=read_json(args.output/'continuous.json') if (args.output/'continuous.json').exists() else []
        reports=[r for r in existing if r['arm'] not in chosen]+reports
        save(args.output/'continuous.json',reports)
    if args.skip_jargon:
        return

    # The holdout has no local-jargon correction series. Keep this control out of
    # the real-chat rates. Learning occurs only AFTER the current reply, never before.
    base=args.output/'jargon-sequence-training'
    if base.exists():
        shutil.rmtree(base)
    shutil.copytree(args.output/'training',base)
    clock=[1793500000.]
    teacher=TelegramSocialRuntime(settings(config),base,MessageBus(),now=lambda:clock[0])
    provider,model=make_expression_provider(config,teacher.config.learning.jargon)
    teacher.jargon.set_provider(provider,model)
    histories={a:[] for a in chosen}
    assets_by_arm={a:copy.deepcopy(source) for a in chosen}
    rows=[]
    words=['又蓝票了。你知道这里的蓝票是什么意思吗？',
           '我们群的蓝票专指临时加场，不是取消。',
           '纠正一下，蓝票在我们群现在专指临时取消，不是加场。',
           '那今晚又蓝票了，你现在理解成什么？']
    try:
        for stage,text in enumerate(words):
            mid=820000+stage
            at=1793500000.+stage*400
            item=message(mid,text,at)
            async def run_arm(index,arm):
                histories[arm].append(copy.deepcopy(item))
                result=await replay_one(config,corpus,assets_by_arm[arm],frozen,args,
                    {'id':'jargon_correction','scope':'main','messages':histories[arm]},mid,arm,main,selector,
                    variant='jargon_correction',training_dir=base)
                append_reply(histories[arm],result,assets_by_arm[arm],'main',stage,index)
                return result
            responses=await asyncio.gather(*(run_arm(i,a) for i,a in enumerate(chosen)))
            # Current evaluation turn was already generated. Only now may its
            # explicit new explanation become evidence for a FUTURE turn.
            clock[0]=at+1
            teacher.store.observe(ObservedMessage(SCOPES['main'],mid,mid,'participant-1','Controlled member',at,text,learning_text=text),500)
            batch=teacher.store.learning.prepare(SCOPES['main'].session_key,'jargon')
            if batch:
                await teacher.jargon.run(batch)
            entries=teacher.jargon.jargon.entries(SCOPES['main'].session_key)
            rows.append({'stage':stage,'origin':'explicit_synthetic_online_correction_control','input':text,'turns':responses,
                         'learned_after_reply':True,'jargon_after':entries,
                         'batch':dict(teacher.store.db.execute('SELECT kind,status,model_calls,input_tokens,output_tokens,latency_ms,source_count,result_count FROM learning_batches WHERE id=?',(batch,)).fetchone()) if batch else None})
            save(args.output/'jargon-sequence.json',rows)
        final=teacher.jargon.jargon.entries(SCOPES['main'].session_key)
        assert any(e['term']=='蓝票' and str(e['local_meaning']).startswith('临时取消') for e in final),final
    finally:
        teacher.close()
    print(json.dumps({'continuous_sequences':len(reports),'jargon_stages':len(rows),'no_real_delivery':True}))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('config','source-db','data','output'):
        parser.add_argument('--'+name,required=True,type=Path)
    parser.add_argument('--jargon-only',action='store_true')
    parser.add_argument('--skip-jargon',action='store_true')
    parser.add_argument('--arm',action='append',choices=ARMS)
    args=parser.parse_args()
    logger.remove()
    logger.add(args.output/'sequence-runtime.log',level='INFO')
    asyncio.run(evaluate(args))


if __name__=='__main__':
    main()
