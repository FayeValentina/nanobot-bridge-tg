"""V5-E controlled real-model replay. Never attaches a Telegram receiver or real sender.

Frozen A holdout stays immutable. Only extraction messages can enter the initial
learning DB; evaluation messages have learning_text=None in all three arms.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import shutil
import sqlite3
import time
from dataclasses import asdict
from pathlib import Path
from types import MethodType, SimpleNamespace
from typing import Any

from loguru import logger

from nanobot.agent.context import ContextBuilder, TranscriptInput
from nanobot.agent.hook import AgentHookContext, AgentRunHookContext
from nanobot.agent.runner import AgentRunner, AgentRunSpec
from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.context import RequestContext, current_request_context, request_context
from nanobot.agent.tools.message import MessageTool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.telegram_social import SOCIAL_TOOL_CLASSES
from nanobot.bus.events import DeliveryReceipt, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.telegram.social.db.wakes import WakeReason
from nanobot.channels.telegram.social.expressions import make_expression_provider
from nanobot.channels.telegram.social.hook import SocialExposureHook
from nanobot.channels.telegram.social.memory import MemoryRequest
from nanobot.channels.telegram.social.models import (
    ConversationScope,
    MediaReference,
    ObservedMessage,
    QuotedMessage,
)
from nanobot.channels.telegram.social.runtime import (
    SOCIAL_PROTOCOL,
    SOCIAL_TOOLS,
    TelegramSocialRuntime,
    encode_view,
)
from nanobot.config.loader import load_config
from nanobot.config.schema import Config, TelegramSocialConfig
from nanobot.providers.base import LLMProvider, LLMResponse
from nanobot.providers.factory import make_provider
from nanobot.runtime_context import RuntimeContextBlock, append_runtime_context
from nanobot.utils.llm_runtime import LLMRuntime

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / 'nanobot/channels/telegram/tests/fixtures'
SCOPES = {'main': ConversationScope('telegram', -1001), 'topic': ConversationScope('telegram', -1001, 42)}
ARMS = ('baseline', 'persona_only', 'full_v5')
CATCHPHRASES = ('本大爷', '白饭', 'token', '算力')
RELATED = {6913, 6916, 6919, 6956, 7002}  # 7002 has explicit 算力 text in the actual image.
QUESTIONS = {6913, 6923, 6994}
STICKER_OPPORTUNITIES = {6934, 6937, 6942, 6984, 6987, 7002, 7006, 7007, 7010}


def read_json(path: Path) -> Any:
    return json.loads(path.read_text())


def save(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)


def validate_split(corpus: dict[str, Any]) -> dict[str, Any]:
    extraction = {(s['scope'], m['message_id']) for s in corpus['extraction'] for m in s['messages']}
    holdout = {(s['scope'], m['message_id']) for s in corpus['evaluation'] for m in s['messages']}
    assert not extraction & holdout
    assert all(not m['learnable'] for s in corpus['evaluation'] for m in s['messages'])
    return {'extraction_messages': len(extraction), 'holdout_messages': len(holdout),
            'targets': sum(len(s['turn_targets']) for s in corpus['evaluation']), 'overlap': 0}


def compile_frozen_method(source: str, name: str, namespace: dict[str, Any]) -> Any:
    # Only the versioned repository fixture is executable, never model/user output.
    scope = dict(namespace)
    exec('from __future__ import annotations\n' + source, scope)
    return scope[name]


def persona_only(old: str, clean: str) -> str:
    result = old
    # Only catchphrase/style cleanup; retain old sticker search and pending guidance.
    for marker in ('2. 极其喜欢摸鱼', '3. 嘴上经常挂着', '3. 可以偶尔写一句角色表演', '6. 口头禅和颜文字', '1. 无论用户问什么'):
        old_line = next(line for line in old.splitlines() if line.startswith(marker))
        new_markers = {'2. 极其喜欢摸鱼':'2. 喜欢摸鱼', '3. 嘴上经常挂着':'3. “本大爷”',
                       '3. 可以偶尔写一句角色表演':'3. 角色动作', '6. 口头禅和颜文字':'6. 口头禅和颜文字',
                       '1. 无论用户问什么':'1. 保持“大肥鱼”'}
        new_line = next(line for line in clean.splitlines() if line.startswith(new_markers[marker]))
        if marker == '6. 口头禅和颜文字':
            new_line = '6. 口头禅和颜文字依语境极偶尔使用；不固定套用，也不硬禁词。正经做事时保留肥鱼的口吻，不把吐槽写得比答案还长。'
        result = result.replace(old_line, new_line)
    return result


class MeasuredProvider(LLMProvider):
    def __init__(self, delegate: LLMProvider):
        super().__init__(provider_name=delegate.provider_name)
        self.delegate = delegate
        self.generation = delegate.generation
        self.calls: list[dict[str, Any]] = []

    def get_default_model(self) -> str:
        return self.delegate.get_default_model()

    async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> LLMResponse:
        started = time.monotonic()
        call: dict[str, Any] = {'temperature': kwargs.get('temperature'), 'model': kwargs.get('model')}
        try:
            response = await self.delegate.chat(messages=messages, **kwargs)
            call.update({'usage': asdict(response.usage) if response.usage else None,
                         'finish_reason': response.finish_reason, 'content': response.content,
                         'tool_calls': [{'name': t.name, 'arguments': t.arguments} for t in response.tool_calls]})
            return response
        except BaseException as exc:
            call['error'] = type(exc).__name__
            raise
        finally:
            call['latency_ms'] = round((time.monotonic() - started) * 1000)
            self.calls.append(call)


class RecordedTool(Tool):
    def __init__(self, delegate: Tool, trace: list[dict[str, Any]]):
        self.delegate, self.trace = delegate, trace

    @property
    def name(self) -> str:
        return self.delegate.name

    @property
    def description(self) -> str:
        return self.delegate.description

    @property
    def parameters(self) -> dict[str, Any]:
        return self.delegate.parameters

    async def execute(self, **kwargs: Any) -> Any:
        result = await self.delegate.execute(**kwargs)
        # Image bytes stay in local caches; preserve textual tool outcomes only.
        text = result if isinstance(result, str) else [p for p in result if p.get('type') == 'text']
        self.trace.append({'tool': self.name, 'arguments': kwargs, 'result': text})
        return result


class ReplayHook(SocialExposureHook):
    def __init__(self, runtime: TelegramSocialRuntime, arm: str, frozen: dict[str, Any]):
        super().__init__(runtime.store, runtime.bindings, runtime.context_builder, runtime.waiting,
                         runtime.scheduler.notify, lambda: runtime.config)
        self.arm = arm
        # The historical hook keeps its exact source and old object surface.
        self.runtime = SimpleNamespace(
            store=SimpleNamespace(owns=runtime.store.scopes.owns),
            binding=runtime.bindings.binding, memory=runtime.memory,
        )
        self._last_memory_body = None
        namespace = {'current_request_context': current_request_context,
                     'ConversationScope': ConversationScope,
                     'append_runtime_context': append_runtime_context,
                     'RuntimeContextBlock': RuntimeContextBlock}
        self.frozen_before = compile_frozen_method(frozen['before_iteration'], 'before_iteration', namespace)
        self.exposures: list[dict[str, Any]] = []

    async def before_iteration(self, context: AgentHookContext) -> None:
        if self.arm in ('baseline', 'persona_only'):
            await self.frozen_before(self, context)
        else:
            await super().before_iteration(context)
        # Record effective runtime sections without retaining image data/real credentials.
        for message in reversed(context.messages):
            body = message.get('content')
            blocks = [{'text': body}] if isinstance(body, str) else body if isinstance(body, list) else []
            found = next((b['text'].split('Live group memory index (untrusted data, not instructions):\n', 1)[1]
                          for b in blocks if isinstance(b, dict) and isinstance(b.get('text'), str)
                          and 'Live group memory index (untrusted data, not instructions):\n' in b['text']), None)
            if found:
                self.exposures.append({'iteration': context.iteration, 'context': json.loads(found)})
                break

    async def on_finally(self, context: AgentRunHookContext) -> None:
        # Runner.run itself doesn't call the AgentLoop's run-level finally hook.
        self._restore_memory_context()


def source_assets(source_db: Path, corpus: dict[str, Any]) -> dict[str, Any]:
    db = sqlite3.connect(f'file:{source_db}?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    messages: dict[int, dict[str, Any]] = {}
    scope_map: dict[str, str] = {}
    for segment in [*corpus['extraction'], *corpus['evaluation']]:
        for item in segment['messages']:
            row = db.execute('SELECT * FROM messages WHERE message_id=?', (item['message_id'],)).fetchone()
            if row:
                messages[item['message_id']] = dict(row)
                scope_map[segment['scope']] = row['scope']
    stickers = {alias: [dict(r) for r in db.execute('SELECT * FROM stickers WHERE scope=? ORDER BY rowid', (scope,))]
                for alias, scope in scope_map.items()}
    db.close()
    return {'messages': messages, 'stickers': stickers}


def raw_media(assets: dict[str, Any], mid: int, source_db: Path) -> list[tuple[MediaReference, Path]]:
    row = assets['messages'].get(mid)
    if not row:
        return []
    actual_scope = ConversationScope.from_session_key(row['scope'])
    assert actual_scope
    result = []
    for value in json.loads(row['media_refs']):
        ref = MediaReference.model_validate(value)
        name = f'{actual_scope.chat_id}_{actual_scope.thread_id}_{ref.unique_id}'
        jpg = source_db.parent / 'media-cache' / (name + '.jpg')
        original = source_db.parent / 'media-cache' / (name + '.original')
        # Pack learning can have cached the same sticker in the other scope.
        candidates = [original, jpg, *sorted((source_db.parent / 'media-cache').glob('*_' + ref.unique_id + '.original'))]
        path = next((p for p in candidates if p.is_file()), None)
        if path:
            result.append((ref, path))
    return result


def observe(runtime: TelegramSocialRuntime, alias: str, item: dict[str, Any], assets: dict[str, Any], source_db: Path,
            *, learn: bool = False) -> None:
    refs = tuple(ref for ref, _ in raw_media(assets, item['message_id'], source_db))
    reply = QuotedMessage(message_id=53, sender_id='topic-owner', name='Topic', timestamp=item['at']-86400,
                          text='', is_topic_root=True) if item.get('reply_to') == 53 else None
    runtime.store.observe(ObservedMessage(SCOPES[alias], item['message_id'], item['message_id'], item['sender'], item['sender'],
        item['at'], item['text'], reply_to=item.get('reply_to'), reply=reply, media=tuple(item['media']), media_refs=refs,
        is_self=item['source']=='self', is_bot=item['source'] in ('self','peer_bot','bot'),
        learning_text=item['text'] if learn and item['learnable'] else None), 500)


def settings(config: Config) -> TelegramSocialConfig:
    learning = config.channels.model_dump(by_alias=True).get('telegram', {}).get('social', {}).get('learning', {})
    return TelegramSocialConfig.model_validate({'enabled': True, 'coalesceSeconds':0, 'cooldownSeconds':0,
        'sendCooldownSeconds':0, 'maxSendsPerHour':60, 'snapshotMessages':80, 'snapshotChars':24000,
        'learning':{**learning,'minMessages':1},
        'scopes':[{'chatId':s.chat_id,'threadId':s.thread_id,'roleId':'guest'} for s in SCOPES.values()]})


async def train(config: Config, corpus: dict[str, Any], assets: dict[str, Any], source_db: Path, directory: Path) -> dict[str, Any]:
    root = directory / 'training'
    (root/'roles').mkdir(parents=True,exist_ok=True)
    (root/'roles/guest.md').write_text('离线真人来源学习；不发送。')
    clock = [max(m['at'] for s in corpus['extraction'] for m in s['messages']) + 1]
    runtime = TelegramSocialRuntime(settings(config), root, MessageBus(), now=lambda:clock[0])
    try:
        sources=sorted(((segment['scope'],item) for segment in corpus['extraction'] for item in segment['messages']),key=lambda pair:pair[1]['at'])
        for alias,item in sources:
            clock[0]=float(item['at'])
            observe(runtime,alias,item,assets,source_db,learn=True)
        clock[0]+=1
        for worker in (runtime.expressions, runtime.jargon):
            provider, model = make_expression_provider(config, getattr(runtime.config.learning, worker.kind))
            worker.set_provider(provider, model)
            for scope in SCOPES.values():
                batch = runtime.store.learning.prepare(scope.session_key, worker.kind)
                if batch:
                    await runtime.learning._run(worker, batch)
                clock[0] += 301
        batches = [dict(r) for r in runtime.store.db.execute('SELECT * FROM learning_batches')]
        for row in batches:
            row.pop('sources', None)
        expressions = [runtime.expressions.library.get(s.session_key,str(e['id'])) for s in SCOPES.values()
                       for e in runtime.expressions.library.list(s.session_key)]
        jargon = [e for s in SCOPES.values() for e in runtime.jargon.jargon.entries(s.session_key)]
        allowed = {(s['scope'], m['message_id'], m['revision']) for s in corpus['extraction'] for m in s['messages'] if m['learnable']}
        checked = []
        for e in expressions:
            alias = next(a for a,s in SCOPES.items() if s.session_key==e['scope'])
            for source in e['evidence']:
                assert (alias,source['message_id'],source['revision']) in allowed
                checked.append([alias,source['message_id'],source['revision']])
        report={'batches':batches,'expressions':expressions,'jargon':jargon,'evidence_checked':checked,
                'learning_does_not_advance_main':all(runtime.store.scopes.state(s.session_key)['read_through']==0 and runtime.store.scopes.state(s.session_key)['seen_through']==0 for s in SCOPES.values()),
                'public_sends':runtime.store.db.execute('SELECT COUNT(*) FROM actions').fetchone()[0]}
        save(directory/'learning.json',report)
        return report
    finally:
        runtime.close()


def copy_library(runtime: TelegramSocialRuntime, training: Path, assets: dict[str, Any], full: bool) -> None:
    with runtime.store.db:
        for alias, rows in assets['stickers'].items():
            for row in rows:
                runtime.store.db.execute('INSERT INTO stickers(scope,id,media_ref,note,uses,updated) VALUES(?,?,?,?,?,?)',
                    (SCOPES[alias].session_key,row['id'],row['media_ref'],row['note'],row['uses'],row['updated']))
        if full:
            runtime.store.db.execute('ATTACH DATABASE ? AS learned',(str(training/'social.sqlite3'),))
            for table in ('expressions','expression_evidence','expression_guards','jargon','jargon_evidence','jargon_suppressions','jargon_resets'):
                runtime.store.db.execute(f'INSERT INTO {table} SELECT * FROM learned.{table}')
    if full:
        runtime.store.db.execute('DETACH DATABASE learned')


def seed_pending(runtime: TelegramSocialRuntime, scope: ConversationScope, before: float) -> None:
    # Explicit controlled background shared across arms, NOT an asserted new human fact.
    with runtime.store.db:
        runtime.store.db.execute("INSERT INTO memories(id,scope,kind,content,members,status,basis,revision,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?)",
            ('controlled-prior-plan',scope.session_key,'pending','之前想追问赏鲸结果、下午浪大不大，以及晚上19:00 Tarabumba 马戏演出是否赶得上。',
             '["participant-1"]','candidate','inference',1,before-86400,before-86400))


def native_paths(prefix: list[dict[str, Any]], assets: dict[str, Any], source_db: Path, root: Path) -> list[str]:
    paths = []
    for item in prefix:
        for ref, path in raw_media(assets,item['message_id'],source_db):
            target = root / f"message-{item['message_id']}-{ref.unique_id}.jpg"
            # Cache payloads may be webp. ContextBuilder reads their actual bytes.
            shutil.copy2(path,target)
            paths.append(str(target))
    return paths[-3:]


async def replay_one(config: Config, corpus: dict[str, Any], assets: dict[str, Any], frozen: dict[str, Any],
                     args: argparse.Namespace, segment: dict[str, Any], target: int, arm: str, delegate: LLMProvider,
                     selector_delegate: LLMProvider, *, variant: str = '', training_dir: Path | None = None,
                     library_empty: bool = False) -> dict[str, Any]:
    alias=segment['scope']
    scope=SCOPES[alias]
    index=next(i for i,m in enumerate(segment['messages']) if m['message_id']==target)
    prefix=segment['messages'][:index+1]
    assert prefix[-1]['message_id']==target
    assert all(m['at']<=prefix[-1]['at'] for m in prefix)
    suffix=f'-{variant}' if variant else ''
    root=args.output/'runs'/f'{arm}-{target}{suffix}'
    if root.exists():
        shutil.rmtree(root)
    (root/'roles').mkdir(parents=True,exist_ok=True)
    clean=(REPO/'docs/harness/groupmate.md').read_text()
    role=frozen['persona'] if arm=='baseline' else persona_only(frozen['persona'],clean) if arm=='persona_only' else clean
    (root/'roles/guest.md').write_text(role)
    # Supply the preserved historical SOUL alongside the portable replay data.
    soul=(args.data/'workspace/SOUL.md').read_text() if arm.startswith('full') else (args.data/'baseline-SOUL.md').read_text()
    if arm=='persona_only':
        soul+='\n- 肥鱼保留嘴硬摸鱼的性格；本大爷、白饭、Token、算力只在语境贴切时极偶尔自然出现，不作为固定尾缀。\n'
    (root/'SOUL.md').write_text(soul)
    clock=[prefix[-1]['at']+1]
    runtime=TelegramSocialRuntime(settings(config),root,MessageBus(),now=lambda:clock[0])
    measured=MeasuredProvider(delegate)
    selected=MeasuredProvider(selector_delegate)
    runtime.selector.provider,runtime.selector.model=selected,selector_delegate.get_default_model()
    trace=[]
    published=[]
    ordinary_texts=[]
    try:
        copy_library(runtime,training_dir or args.output/'training',assets,arm.startswith('full'))
        if library_empty:
            with runtime.store.db:
                for table in ('expression_guards','expression_evidence','expressions','jargon_evidence','jargon_suppressions','jargon_resets','jargon','stickers'):
                    runtime.store.db.execute(f'DELETE FROM {table}')
        if target not in {6913,6916,6919}:
            seed_pending(runtime,scope,clock[0])
        for item in prefix:
            observe(runtime,alias,item,assets,args.source_db)
            if item.get('confirmed_turn'):
                # Historical confirmed outputs in fixed context, grouped exactly
                # by the A fixture's receipts. Not model drafts or future replies.
                with runtime.store.db:
                    runtime.store.db.execute('INSERT INTO actions(id,scope,version,content,status,created,turn_id,kind,media_ref) VALUES(?,?,?,?,?,?,?,?,?)',
                        (f"history-{item['message_id']}",scope.session_key,runtime.store.scopes.state(scope.session_key)['version'],item['text'],'sent',item['at'],
                         item['confirmed_turn'],'sticker' if item['media']==['sticker'] else 'text',
                         raw_media(assets,item['message_id'],args.source_db)[0][0].model_dump_json() if item['media']==['sticker'] and raw_media(assets,item['message_id'],args.source_db) else None))
        before_events=runtime.store.db.execute('SELECT COUNT(*) FROM learning_events').fetchone()[0]
        assert before_events==0, 'Evaluation text must never be collected as training material'
        if arm in ('baseline','persona_only'):
            namespace={'json':json,'re':re,'MemoryRequest':MemoryRequest}
            runtime.memory.context=MethodType(compile_frozen_method(frozen['memory_context'],'context',namespace),runtime.memory)
        async def sender(destination: ConversationScope, content: str, reply_to: int | None) -> int:
            assert destination==scope
            published.append({'content':content,'reply_to':reply_to})
            return 800000+len(published)
        runtime.set_sender(sender)
        runtime.delivery.sticker_sender=sender
        async def loader(ref: MediaReference, limit: int) -> bytes:
            for item in prefix:
                for known,path in raw_media(assets,item['message_id'],args.source_db):
                    if known.unique_id==ref.unique_id:
                        return path.read_bytes()[:limit]
            for paths in (args.source_db.parent/'media-cache').glob('*_'+ref.unique_id+'.original'):
                return paths.read_bytes()[:limit]
            raise RuntimeError('No cached source image; do not infer pixels')
        runtime.media.loader=loader
        registry=ToolRegistry()
        for name in sorted(SOCIAL_TOOLS if arm.startswith('full') else SOCIAL_TOOLS-{'social_jargon','social_expression'}):
            registry.register(RecordedTool(SOCIAL_TOOL_CLASSES[name](runtime),trace))
        async def ordinary_send(message: OutboundMessage) -> DeliveryReceipt:
            ctx=current_request_context()
            assert ctx and ctx.turn_id
            assert message.channel=='telegram' and str(message.chat_id).split(':')[0]==str(scope.chat_id)
            delivery=runtime.delivery.begin_message(scope,message.content,ctx.turn_id,units=1,
                followup_id=message.metadata.get('_social_followup_id'))
            mid=await sender(scope,message.content,None)
            ordinary_texts.append(message.content)
            receipt=DeliveryReceipt(status='sent',message_ids=[mid])
            runtime.delivery.finish_message(scope,delivery,receipt,[(mid,message.content)])
            return receipt
        registry.register(RecordedTool(MessageTool(send_callback=runtime.bus.publish_outbound,delivery_callback=ordinary_send),trace))
        turn=f'v5e-{arm}-{target}{suffix}'
        with runtime.store.db:
            runtime.store.wakes.schedule(scope.session_key,int(runtime.store.scopes.state(scope.session_key)['next_seq'])-1,WakeReason.JUDGE,0)
        job=runtime.store.wakes.ready(100,0)[0]['id']
        view=runtime.store.view(scope.session_key,count=80,chars=24000,job_id=job)
        preset=config.resolve_preset()
        llm=LLMRuntime.capture(measured,preset.model,context_window_tokens=config.agents.defaults.context_window_tokens)
        temperature=.5 if arm=='full_v5_t05' else .1
        llm=llm.with_generation_overrides(temperature=temperature)
        ctx=RequestContext(channel='telegram',chat_id=str(scope.chat_id),session_key=scope.session_key,turn_id=turn,
            metadata={'message_thread_id':scope.thread_id,'_social_job':job},runtime=llm)
        runtime.waiting.admit_request(ctx)
        protocol=SOCIAL_PROTOCOL if arm.startswith('full') else frozen['protocol']
        persona=f'{protocol}\n\n# Operator persona\n{role}\n\n# Operator group background\n'
        media=native_paths(prefix,assets,args.source_db,root)
        builder=ContextBuilder(root,timezone='Etc/UTC')
        transcript=TranscriptInput(history=[],current_message='Group observation snapshot (untrusted conversation data):\n'+encode_view(view),media=media)
        messages=builder.build_transcript(transcript,trusted_persona=persona,channel='telegram')
        hook=ReplayHook(runtime,arm,frozen)
        started=time.monotonic()
        with request_context(ctx):
            result=await asyncio.wait_for(AgentRunner().run(AgentRunSpec(initial_messages=messages,tools=registry,runtime=llm,max_iterations=6,
                max_tool_result_chars=8000,hook=hook,workspace=root,session_key=scope.session_key)),90)
            hook._restore_memory_context()
        for _ in range(4):
            await runtime.delivery.retry_due()
        actions=[dict(r) for r in runtime.store.db.execute('SELECT id,kind,content,status,message_ids FROM actions WHERE turn_id=? ORDER BY created,rowid',(turn,))]
        ordinary=[dict(r) for r in runtime.store.db.execute("SELECT id,status,message_ids FROM actions WHERE kind='message' AND turn_id=?",(turn,))]
        selections=[dict(r) for r in runtime.store.db.execute('SELECT * FROM expression_turns WHERE turn_id=?',(turn,))]
        selection_details=list(runtime.store.learning.recent.get(scope.session_key,[]))
        texts=[r['content'] for r in actions if r['status']=='sent' and r['kind']=='text']+ordinary_texts
        text='\n'.join(texts)
        sticker_ids=[r['content'].removeprefix('[sticker:').removesuffix(']') for r in actions if r['status']=='sent' and r['kind']=='sticker']
        notes={r['id']:r['note'] for r in assets['stickers'][alias]}
        output={'arm':arm,'segment':segment['id'],'variant':variant,'target_message_id':target,'scope_alias':alias,'temperature':temperature,
            'input':prefix[-1]['text'],'source_prefix':[[m['message_id'],m['revision']] for m in prefix],
            'native_media_message_ids':[m['message_id'] for m in prefix if raw_media(assets,m['message_id'],args.source_db)],
            'actions':actions,'ordinary_receipts':ordinary,'confirmed_text_parts':texts,'confirmed_stickers':[{'id':s,'note':notes.get(s,'')} for s in sticker_ids],
            'main_calls':measured.calls,'selector_calls':selected.calls,'selection_stats':selections,'selection_details':selection_details,
            'tools':trace,'effective_contexts':hook.exposures,'elapsed_ms':round((time.monotonic()-started)*1000),
            'stop_reason':result.stop_reason,'error':result.error,'unsent_final':result.final_content,
            'automatic':{'has_confirmed_reply':bool(texts or sticker_ids),'unrelated_catchphrase':target not in RELATED and any(w in text.casefold() for w in CATCHPHRASES),
                         'catchphrase_terms':[w for w in CATCHPHRASES if w in text.casefold()],
                         'explicit_question':target in QUESTIONS,'sticker_opportunity':target in STICKER_OPPORTUNITIES},
            'isolation':{'source_only_through_target':True,'evaluation_learning_events':runtime.store.db.execute('SELECT COUNT(*) FROM learning_events').fetchone()[0],
                         'sender_is_stub':True,'no_receiver':True}}
        assert output['isolation']['evaluation_learning_events']==0
        save(args.output/'results'/f'{arm}-{target}{suffix}.json',output)
        print(json.dumps({'arm':arm,'target':target,'text':texts,'stickers':sticker_ids,'selection':[(r['status'],r['latency_ms']) for r in selections],'error':result.error},ensure_ascii=False),flush=True)
        return output
    finally:
        runtime.close()


async def evaluate(args: argparse.Namespace) -> None:
    args.output.mkdir(parents=True,exist_ok=True)
    corpus=read_json(FIXTURES/'social_v5_baseline.json')
    frozen=read_json(FIXTURES/'social_v5e_baseline_code.json')
    split=validate_split(corpus)
    assets=source_assets(args.source_db,corpus)
    config=load_config(args.config)
    if not (args.output/'learning.json').exists():
        await train(config,corpus,assets,args.source_db,args.output)
    preset=config.resolve_preset()
    # The main preset/temperature are kept fixed. No eval-specific role examples.
    main=make_provider(config,preset=preset)
    selector,_=make_expression_provider(config,settings(config).learning.selection)
    semaphore=asyncio.Semaphore(args.concurrency)
    async def one(segment: dict[str,Any],target: int,arm: str) -> dict[str,Any]:
        path=args.output/'results'/f'{arm}-{target}.json'
        if path.exists() and not args.rerun:
            return read_json(path)
        async with semaphore:
            return await replay_one(config,corpus,assets,frozen,args,segment,target,arm,main,selector)
    jobs=[]
    # Rotate arm order by target; do not let time-of-run always favor Full V5.
    for segment in corpus['evaluation']:
        for target in segment['turn_targets']:
            if args.target and target not in args.target:
                continue
            order=list(args.arm or ARMS)
            rotation=target%len(order)
            order=order[rotation:]+order[:rotation]
            jobs.extend(one(segment,target,arm) for arm in order)
    results=await asyncio.gather(*jobs)
    save(args.output/'replay.json',{'model':preset.model,'baseline_revision':frozen['revision'],'split':split,
         'mode':'fixed_real_holdout_actual_model_mock_sent_receipts','results':results})


def main() -> None:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',required=True,type=Path)
    parser.add_argument('--source-db',required=True,type=Path)
    parser.add_argument('--data',required=True,type=Path,help='Replay data with baseline-SOUL.md (preserved historical source) and workspace/SOUL.md')
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--concurrency',type=int,default=3,choices=range(1,5))
    parser.add_argument('--target',action='append',type=int)
    parser.add_argument('--arm',action='append',choices=(*ARMS,'full_v5_t05'))
    parser.add_argument('--rerun',action='store_true')
    args=parser.parse_args()
    logger.remove()
    logger.add(args.output/'runtime.log',rotation='20 MB',level='INFO')
    asyncio.run(evaluate(args))


if __name__=='__main__':
    main()
