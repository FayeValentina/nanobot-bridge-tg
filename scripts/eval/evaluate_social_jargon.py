"""Run V5-C synthetic conversations with the actual configured model/web tools, no sends."""
from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from pathlib import Path

from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.web import WebFetchTool, WebSearchTool
from nanobot.bus.queue import MessageBus
from nanobot.channels.telegram.social.expressions import make_expression_provider
from nanobot.channels.telegram.social.jargon import JargonRequest
from nanobot.channels.telegram.social.models import ConversationScope, ObservedMessage
from nanobot.channels.telegram.social.runtime import TelegramSocialRuntime, encode_view
from nanobot.config.loader import load_config
from nanobot.config.schema import TelegramSocialConfig


async def evaluate(config_path: Path, output: Path) -> None:
    config = load_config(config_path)
    clock = [1000.]
    reports = []
    cases = [('public_meme','栓Q','又白跑了一趟，栓Q。'),
             ('local_special_meaning','蓝票','我们群把临时取消的聚会叫蓝票，不是车票。'),
             ('not_found','咕噜帧泡泡矩阵','这回又咕噜帧泡泡矩阵了，不知道什么意思。')]
    for label, term, text in cases:
        with tempfile.TemporaryDirectory(prefix='v5c-replay-') as directory:
            root = Path(directory)
            (root/'roles').mkdir()
            (root/'roles/guest.md').write_text('Isolated synthetic replay; no Telegram sender.')
            scope = ConversationScope('telegram',-1001,42)
            social = TelegramSocialConfig.model_validate({'enabled':True,'learning':{'minMessages':1},
                'scopes':[{'chatId':scope.chat_id,'threadId':scope.thread_id,'roleId':'guest'}]})
            runtime = TelegramSocialRuntime(social,root,MessageBus(),now=lambda:clock[0])
            provider, model = make_expression_provider(config,social.learning.jargon)
            runtime.jargon.set_provider(provider,model)
            tools = ToolRegistry()
            if config.tools.web.enable:
                tools.register(WebSearchTool(config.tools.web.search,proxy=config.tools.web.proxy,user_agent=config.tools.web.user_agent))
                tools.register(WebFetchTool(config.tools.web.fetch,proxy=config.tools.web.proxy,user_agent=config.tools.web.user_agent))
            runtime.jargon.tools = tools
            try:
                runtime.store.observe(ObservedMessage(scope,1,1,'human','Member',clock[0],text,learning_text=text),100)
                if label == 'local_special_meaning':
                    batch = runtime.store.learning.prepare(scope.session_key,'jargon')
                else:
                    view = runtime.store.view(scope.session_key,count=30,chars=12000,turn_id='turn')
                    runtime.store.note_seen(scope.session_key,'turn',encode_view(view))
                    runtime.jargon.jargon.apply(scope.session_key,'turn',JargonRequest(action='submit',term=term,message_ids=[1],research=True))
                    runtime.jargon.prepare_reviews([scope.session_key])
                    batch = runtime.store.db.execute("SELECT id FROM learning_batches WHERE kind='jargon'").fetchone()[0]
                assert batch
                await runtime.learning._run(runtime.jargon, batch)
                entries = runtime.jargon.jargon.entries(scope.session_key)
                row = dict(runtime.store.db.execute('SELECT * FROM learning_batches WHERE id=?',(batch,)).fetchone())
                row.pop('sources',None)
                checks = {'no_public_send_or_wake':runtime.bus.inbound_size==runtime.bus.outbound_size==0 and not runtime.store.db.execute('SELECT 1 FROM wakes').fetchone()}
                if label == 'public_meme':
                    checks['body_verified'] = bool(entries and entries[0]['public_meaning'] and entries[0]['source_links'])
                elif label == 'not_found':
                    checks['no_invented_meaning'] = all(not e['local_meaning'] and not e['public_meaning'] and e['status']=='candidate' for e in entries)
                else:
                    checks['understand_only'] = bool(entries and entries[0]['status']=='understand_only' and entries[0]['local_meaning']=='临时取消的聚会')
                    checks['no_network'] = row['search_calls']==row['fetch_calls']==0
                    if entries:
                        old = entries[0]
                        corrected_text = '纠正一下，蓝票在我们群专指临时加场，不是取消。'
                        runtime.store.observe(ObservedMessage(scope,2,2,'human','Member',clock[0],corrected_text,learning_text=corrected_text),100)
                        view = runtime.store.view(scope.session_key,count=30,chars=12000,turn_id='correction')
                        runtime.store.note_seen(scope.session_key,'correction',encode_view(view))
                        corrected = runtime.jargon.jargon.apply(scope.session_key,'correction',JargonRequest(action='explain',entry_id=str(old['id']),expected_revision=int(str(old['revision'])),term=term,
                            message_ids=[2],local_meaning='临时加场',explanation_quote=corrected_text))['jargon']
                        clock[0]+=301
                        runtime.store.observe(ObservedMessage(scope,101,1,'human','Member',clock[0],text,learning_text=text,is_edit=True),100)
                        new_batch=runtime.store.learning.prepare(scope.session_key,'jargon')
                        assert new_batch
                        await runtime.learning._run(runtime.jargon, new_batch)
                        live=runtime.jargon.jargon.get(scope.session_key,str(old['id']))
                        checks['correction_overrides_old_revision'] = live['local_meaning']=='临时加场' and live['revision']==corrected['revision']
                        entries=runtime.jargon.jargon.entries(scope.session_key)
                        row['correction_replay_batch']=dict(runtime.store.db.execute('SELECT model_calls,input_tokens,output_tokens,status FROM learning_batches WHERE id=?',(new_batch,)).fetchone())
                reports.append({'case':label,'synthetic_input':text,'term':term,'model':model,'batch':row,'entries':entries,'checks':checks})
                print(json.dumps({'case':label,'status':row['status'],'checks':checks,'search_calls':row['search_calls'],'fetch_calls':row['fetch_calls']},ensure_ascii=False),flush=True)
            finally:
                runtime.close()
        clock[0]+=301
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps({'mode':'synthetic_conversations_actual_model_and_web','reports':reports},ensure_ascii=False,indent=2)+'\n')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args()
    asyncio.run(evaluate(args.config,args.output))


if __name__=='__main__':
    main()
