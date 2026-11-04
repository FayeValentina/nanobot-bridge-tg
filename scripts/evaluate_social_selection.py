"""V5-D actual-model generation in temporary scopes; all sends are local stubs."""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import tempfile
import time
from dataclasses import asdict
from pathlib import Path

from nanobot.agent.hook import AgentHook, AgentHookContext
from nanobot.agent.loop import AgentLoop
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.telegram_social import TelegramSocialTool
from nanobot.bus.queue import MessageBus
from nanobot.channels.telegram.social.expressions import make_expression_provider
from nanobot.channels.telegram.social.models import ConversationScope, ObservedMessage
from nanobot.channels.telegram.social.runtime import SOCIAL_TOOLS, TelegramSocialRuntime
from nanobot.config.loader import load_config
from nanobot.config.schema import TelegramSocialConfig
from nanobot.providers.factory import make_provider
from nanobot.session.manager import SessionManager


class Capture(AgentHook):
    def __init__(self):
        super().__init__()
        self.iterations = []

    async def after_iteration(self, context: AgentHookContext) -> None:
        self.iterations.append({'iteration':context.iteration,'usage':asdict(context.usage) if context.usage else None,
            'tools':[{'name':call.name,'arguments':call.arguments} for call in context.tool_calls],
            'error':context.error,'final':context.final_content})


async def evaluate(config_path: Path, source_db: Path, output: Path) -> None:
    config=load_config(config_path)
    connection=sqlite3.connect(f'file:{source_db}?mode=ro',uri=True)
    connection.row_factory=sqlite3.Row
    # Read the full largest annotated scope, never modify its records or cursors.
    source_scope=connection.execute("SELECT scope FROM stickers WHERE note<>'' GROUP BY scope ORDER BY COUNT(*) DESC LIMIT 1").fetchone()[0]
    stickers=[dict(r) for r in connection.execute('SELECT * FROM stickers WHERE scope=?',(source_scope,))]
    connection.close()
    reports=[]
    selection_provider, selection_model=make_expression_provider(config,TelegramSocialConfig().learning.selection)
    cases=[('banter','我啥也没干，怎么又赢了哈哈'),
           ('serious_help','认真求助：这段 Python 为什么报 IndexError？代码是 xs=[]; print(xs[0])。'),
           ('token_topic','这个月 Token 到底花了多少，你能看到实际账单吗？')]
    for label,text in cases:
        with tempfile.TemporaryDirectory(prefix='v5d-generation-') as directory:
            root=Path(directory)
            (root/'social/roles').mkdir(parents=True)
            (root/'social/roles/guest.md').write_text((Path(__file__).resolve().parents[1]/'docs/harness/groupmate.md').read_text())
            scope=ConversationScope('telegram',-1001,42)
            settings=TelegramSocialConfig.model_validate({'enabled':True,'coalesceSeconds':0,'cooldownSeconds':0,'sendCooldownSeconds':0,
                'scopes':[{'chatId':scope.chat_id,'threadId':scope.thread_id,'roleId':'guest'}]})
            bus=MessageBus()
            runtime=TelegramSocialRuntime(settings,root/'social',bus)
            runtime.selector.provider, runtime.selector.model=selection_provider,selection_model
            delivered=[]
            async def sender(target, content, reply_to):
                delivered.append({'content':content,'reply_to':reply_to})
                return 1000+len(delivered)
            runtime.set_sender(sender)
            runtime.delivery.sticker_sender=sender
            with runtime.store.db:
                for row in stickers:
                    runtime.store.db.execute('INSERT INTO stickers(scope,id,media_ref,note,uses,updated) VALUES(?,?,?,?,?,?)',
                        (scope.session_key,row['id'],row['media_ref'],row['note'],0,row['updated']))
                now=runtime.now()
                # Synthetic references exercise the selector, not learned online facts.
                runtime.store.db.execute("INSERT INTO expressions(id,scope,situation,expression,status,scene_tags,intent_category,created,updated) VALUES(?,?,?,?,?,?,?,?,?)",
                    ('synthetic-irony',scope.session_key,'双方熟悉，朋友自夸或意外获胜时轻松调侃','领先的方式有点特别','available','["banter","praise"]','irony',now,now))
                runtime.store.db.execute("INSERT INTO memories(id,scope,kind,content,members,status,basis,revision,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    ('synthetic-old-plan',scope.session_key,'pending','旧行程：周末去海边，需要催问进度','["member"]','candidate','inference',1,now-86400,now-86400))
            registry=ToolRegistry()
            for name in SOCIAL_TOOLS:
                registry.register(TelegramSocialTool(runtime,name))
            capture=Capture()
            preset=config.resolve_preset()
            provider=make_provider(config,preset=preset)
            workspace=root/'workspace'
            workspace.mkdir()
            loop=AgentLoop(bus=bus,provider=provider,model=preset.model,workspace=workspace,max_iterations=4,
                context_window_tokens=config.agents.defaults.context_window_tokens,session_manager=SessionManager(workspace,sessions_root=root/'sessions'),
                tool_registry=registry,tools_config=config.tools.model_copy(update={'enabled_tools':list(SOCIAL_TOOLS)}),
                session_policy_resolver=runtime.policy_for_session,request_admission=runtime.waiting.admit_request,
                hook_factories=[runtime.hook_factory,lambda _:capture])
            started=time.monotonic()
            try:
                # Enter at the existing post-V3 wake boundary; this probe tests D,
                # without running another real receiver or changing V3 admission.
                runtime.store.observe(ObservedMessage(scope,1,1,'member','群友',runtime.now(),text,learning_text=text),500)
                job=runtime.store.wake(scope.session_key,1,100,now=runtime.now())
                assert job
                await runtime._publish_wake(scope,job,1)
                incoming=await asyncio.wait_for(bus.consume_inbound(),2)
                await asyncio.wait_for(loop._dispatch(incoming),90)
                # Paced multi-bubble sends normally drain in the scheduler. This
                # harness has no running receiver/scheduler, so drain locally.
                for _ in range(3):
                    await runtime.delivery.retry_due()
                stats=[dict(row) for row in runtime.store.db.execute('SELECT * FROM expression_turns')]
                actions=[dict(row) for row in runtime.store.db.execute('SELECT kind,content,status FROM actions')]
                reports.append({'case':label,'synthetic_input':text,'model':preset.model,'selector_model':runtime.selector.model,
                    'library_stickers':len(stickers),'elapsed_seconds':round(time.monotonic()-started,3),'selection_stats':stats,
                    'selection_details':runtime.store.learning.status(scope.session_key)['recent_selections'],
                    'actions':actions,'delivered_to_stub_only':delivered,'main_iterations':capture.iterations,
                    'checks':{'at_most_one_selector_call':sum(r['calls'] for r in stats)<=1,
                              'deadline_under_2100ms':all(r['latency_ms']<2100 for r in stats),
                              'no_old_plan_in_reply':all('海边' not in d['content'] and '行程' not in d['content'] for d in delivered),
                              'no_public_sender':True}})
                print(json.dumps({'case':label,'selector':stats,'actions':actions},ensure_ascii=False),flush=True)
            finally:
                runtime.close()
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps({'mode':'synthetic_conversations_actual_model_stub_delivery','reports':reports},ensure_ascii=False,indent=2)+'\n')


def main() -> None:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',required=True,type=Path)
    parser.add_argument('--source-db',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args()
    asyncio.run(evaluate(args.config,args.source_db,args.output))


if __name__=='__main__':
    main()
