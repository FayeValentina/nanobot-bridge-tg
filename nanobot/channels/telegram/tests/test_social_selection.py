"""V5-D bounded selection, receipt facts and the actual main-agent hook path."""
import asyncio
import json
import time
from copy import deepcopy
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from loguru import logger

from nanobot.bus.events import DeliveryReceipt
from nanobot.channels.telegram.social.models import MediaReference
from nanobot.channels.telegram.social.runtime import encode_view
from nanobot.channels.telegram.social.selection import Choice
from nanobot.channels.telegram.tests.test_social_memory import apply, expose
from nanobot.channels.telegram.tests.test_social_runtime import A, B, agent, message, social, wake
from nanobot.config.schema import Config
from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest

__all__ = ['social']


def seed(runtime):
    db = runtime.store.db
    now = runtime.now()
    with db:
        for scope in (A, B):
            suffix = 'a' if scope == A else 'b'
            db.execute("INSERT INTO expressions(id,scope,situation,expression,status,scene_tags,intent_category,created,updated) VALUES(?,?,?,?,?,?,?,?,?)",
                       ('e-' + suffix, scope.session_key, '朋友失误后互相开玩笑时反讽夸奖', '领先的方式有点特别', 'available', '["banter","praise"]', 'irony', now, now))
            db.execute("INSERT INTO jargon(id,scope,content,status,basis,revision,created,updated,term,local_meaning) VALUES(?,?,?,?,?,?,?,?,?,?)",
                       ('j-' + suffix, scope.session_key, '蓝票', 'understand_only', 'direct', 1, now, now, '蓝票', '临时加场'))
        for i in range(125):
            ref = MediaReference(kind='sticker', file_id=f'file-{i}', unique_id=f's-{i}')
            runtime.store.stickers.collect(A.session_key, ref, f'场景{i}：' + ('得意 自信 调侃 嘴硬 领先' if i == 124 else '困倦 睡觉 晚安'), 500)
    provider = AsyncMock(spec=LLMProvider)
    async def choose(**kwargs):
        data = json.loads(kwargs['messages'][1]['content'])
        selected = {c['kind']: c['id'] for c in reversed(data['candidates'])}
        return LLMResponse(content=json.dumps({'expressions':[selected['expression']], 'jargon':[selected['jargon']], 'stickers':[selected['sticker']], 'reason':'互相调侃且提到了蓝票'}))
    provider.chat.side_effect = choose
    runtime.selector.provider, runtime.selector.model = provider, 'selector-test'
    return provider


def view(runtime, turn='turn', text='蓝票这回领先了哈哈', mid=1):
    runtime.store.observe(message(seq=mid, text=text), 500)
    return runtime.store.view(A.session_key, count=30, chars=12000, turn_id=turn)


async def test_scope_scene_keyword_pool_caps_and_same_turn_cache(social):
    provider = seed(social)
    view(social)
    selected = await social.selector.select(A.session_key, 'turn')
    assert [(x.kind, x.entry_id) for x in selected] == [('expression','e-a'),('jargon','j-a'),('sticker','s-124')]
    payload = json.loads(provider.chat.call_args.kwargs['messages'][1]['content'])
    assert len(payload['candidates']) <= 10 + 20 + 40
    assert not any(c['id'].endswith('-b') for c in payload['candidates'])
    assert '领先' in [c for c in payload['candidates'] if c['kind']=='sticker'][0]['note']
    assert await social.selector.select(A.session_key,'turn') == selected
    assert provider.chat.await_count == 1
    row = social.store.db.execute('SELECT calls,candidate_count,status FROM expression_turns').fetchone()
    assert row['calls'] == 1 and row['candidate_count'] <= 70 and row['status']=='selected'
    assert social.store.learning.status(A.session_key)['recent_selections'][-1]['candidates']
    assert social.store.scopes.state(A.session_key)['seen_through'] == social.store.scopes.state(A.session_key)['read_through'] == 0


async def test_actual_unclaimed_snapshot_selected_before_it_is_marked_seen(social):
    provider = seed(social)
    data = view(social)
    social.store.db.execute("UPDATE views SET turn_id=NULL")
    assert await social.selector.select(A.session_key,'turn',encode_view(data))
    assert provider.chat.await_count == 1
    assert social.store.db.execute('SELECT seen FROM views').fetchone()[0] == 0


@pytest.mark.parametrize('change', ['message_edit','new_message','entry_revision','disabled','sticker_note','config','scope','provider_config'])
async def test_changed_values_reuse_first_selection_without_second_call(social, change):
    provider = seed(social)
    view(social)
    selected = await social.selector.select(A.session_key,'turn')
    assert selected
    if change=='message_edit':
        social.store.observe(replace(message(text='现在认真求助'),update_id=101,is_edit=True),500)
    elif change=='new_message':
        view(social,mid=2,text='认真求助')
    elif change=='entry_revision':
        social.store.db.execute("UPDATE expressions SET revision=revision+1")
    elif change=='disabled':
        social.store.db.execute("UPDATE expressions SET status='disabled'")
    elif change=='sticker_note':
        social.store.stickers.annotate(A.session_key,{'s-124':'严肃的另一张图'})
    elif change=='scope':
        social.store.bind(A,'another-version')
    elif change=='provider_config':
        social.selector.configure_provider(Config())
    else:
        social.store.learning.configure(social.config.learning.model_copy(update={'selection_stickers':0}))
    assert await social.selector.select(A.session_key,'turn') is selected
    assert provider.chat.await_count == 1
    assert 'reuse_status' not in social.store.learning.recent[A.session_key][-1]


@pytest.mark.parametrize('content', ['not json', '', 'x' * 8001])
async def test_invalid_content_degrades_to_empty_without_retry(social,content):
    provider=seed(social)
    view(social)
    provider.chat.side_effect=None
    provider.chat.return_value=LLMResponse(content=content)
    assert await social.selector.select(A.session_key,'turn') == []
    assert await social.selector.select(A.session_key,'turn') == []
    assert provider.chat.await_count == 1
    assert social.store.db.execute('SELECT status FROM expression_turns').fetchone()[0]=='error'


async def test_timeout_is_hard_even_if_adapter_swallows_cancellation(social):
    provider=seed(social)
    view(social)
    social.config.learning.selection_timeout_seconds=.05
    release=asyncio.Event()
    async def stuck(**kwargs):
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return LLMResponse(content='{"stickers":["s-124"]}')
    provider.chat.side_effect=stuck
    start=time.monotonic()
    assert await social.selector.select(A.session_key,'turn')==[]
    assert time.monotonic()-start < .3
    assert social.store.db.execute('SELECT status FROM expression_turns').fetchone()[0]=='timeout'
    release.set()
    await asyncio.sleep(.01)
    assert not social.store.db.execute('SELECT 1 FROM expression_selections').fetchone()
    assert provider.chat.await_count==1


async def test_no_model_for_empty_library_and_empty_is_valid_for_serious_help(social):
    provider=AsyncMock(spec=LLMProvider)
    social.selector.provider=provider
    view(social,text='认真求助：这份结果应该怎么理解？')
    assert await social.selector.select(A.session_key,'turn')==[]
    provider.chat.assert_not_awaited()
    assert social.store.db.execute('SELECT calls FROM expression_turns').fetchone()[0]==0
    provider=seed(social)
    view(social,turn='serious',mid=2,text='认真求助：这份结果应该怎么理解？')
    provider.chat.side_effect=None
    provider.chat.return_value=LLMResponse(content='{"reason":"认真求助，无需玩梗"}')
    assert await social.selector.select(A.session_key,'serious')==[]
    assert social.store.db.execute("SELECT status FROM expression_turns WHERE turn_id='serious'").fetchone()[0]=='empty'


async def test_inflight_inactive_entry_is_dropped(social):
    provider=seed(social)
    view(social)
    async def response(**kwargs):
        social.store.db.execute("UPDATE expressions SET status='disabled'")
        return LLMResponse(content='{"expressions":["e0"]}')
    provider.chat.side_effect=response
    assert await social.selector.select(A.session_key,'turn')==[]
    assert not social.store.db.execute('SELECT 1 FROM expression_selections').fetchone()
    detail = social.store.learning.recent[A.session_key][-1]
    assert detail['status'] == 'empty' and detail['dropped'] == 1


@pytest.mark.parametrize('change', ['message', 'scope', 'config'])
async def test_inflight_input_changes_keep_selection(social, change):
    provider = seed(social)
    view(social)
    async def response(**kwargs):
        if change == 'message':
            view(social, mid=2, text='新话题')
        elif change == 'scope':
            social.store.bind(A, 'another-version')
        else:
            social.store.learning.configure(social.config.learning.model_copy(update={'selection_chars': 2000}))
        return LLMResponse(content='{"stickers":["s2"]}')
    provider.chat.side_effect = response
    chosen = await social.selector.select(A.session_key, 'turn')
    assert [(c.kind, c.entry_id) for c in chosen] == [('sticker', 's-124')]
    assert provider.chat.await_count == 1


def test_recent_usage_is_sent_only_all_paths_grouped_by_turn(social):
    social.config.learning.catchphrases = ["本大爷", "白饭", "Token", "算力"]
    seed(social)
    social.config.send_cooldown_seconds = 0
    db=social.store.db
    for index,(turn,status,text) in enumerate([('one','sent','本大爷吃白饭'),('one','sent','Token 没花多少'),('bad','unknown','没发出去算力'),('two','sent','换个句式')]):
        with db:
            db.execute('INSERT INTO actions(id,scope,version,content,status,created,turn_id) VALUES(?,?,?,?,?,?,?)',
                       (f'a{index}',A.session_key,social.store.scopes.state(A.session_key)['version'],text,status,social.now()-10+index,turn))
    ref=social.store.stickers.get(A.session_key,'s-124')
    with db:
        db.execute("INSERT INTO actions(id,scope,version,content,status,created,turn_id,kind,media_ref) VALUES(?,?,?,?,?,?,?,?,?)",
                   ('st',A.session_key,social.store.scopes.state(A.session_key)['version'],'file-124','sent',social.now()-5,'one','sticker',ref.model_dump_json()))
    delivery = social.delivery.begin_message(A, '普通 message：Token 计费', 'three', units=1, followup_id=None)
    assert delivery is not None
    social.delivery.finish_message(A, delivery, DeliveryReceipt('sent', (100,)), [(100, '普通 message：Token 计费')])
    delivery = social.delivery.begin_message(A, '不能算已发送的口头禅', 'uncertain', units=1, followup_id=None)
    assert delivery is not None
    social.delivery.finish_message(A, delivery, DeliveryReceipt('unknown', (101,)), [(101, '不能算已发送的口头禅')])
    usage=social.selector.recent_usage(A.session_key)
    assert len(usage['turns'])==3
    assert usage['catchphrases']=={'本大爷':1,'白饭':1,'Token':2}
    assert 'file-124' not in str(usage) and 's-124' in str(usage)
    assert '不能算已发送' not in str(usage) and '没发出去' not in str(usage)
    assert next(t for t in usage['turns'] if t['turn_id']=='one')['texts']==['Token 没花多少','本大爷吃白饭']
    assert social.selector.recent_usage(B.session_key)['turns']==[]


def test_recent_usage_with_empty_catchphrases_does_not_count_persona_words(social):
    social.config.learning.catchphrases = []
    delivery = social.delivery.begin_message(A, '本大爷来了', 'turn', units=1, followup_id=None)
    assert delivery is not None
    social.delivery.finish_message(A, delivery, DeliveryReceipt('sent', (100,)), [(100, '本大爷来了')])
    usage = social.selector.recent_usage(A.session_key)
    assert usage['turns'][0]['texts'] == ['本大爷来了']
    assert usage['catchphrases'] == {}


def test_pending_is_separate_and_does_not_rank_from_member_or_active_bonus(social):
    ctx=expose(social,text='周末去海边，记得问我行程')
    pending=apply(social,ctx,kind='pending',content='周末去海边',message_ids=[1],member_ids=['123'],basis='direct')['memory']
    apply(social,ctx,action='followup',memory_id=pending['id'],pending_question='行程准备好了？',followup_state='active',followup_at=social.now()+1000)
    data=json.loads(social.memory.context(A.session_key,topic_text='哈哈今天的猫猫图笑死我了'))
    assert data['pending_matters']==[]
    related=json.loads(social.memory.context(A.session_key,topic_text='周末去海边改到几点？'))
    assert related['pending_matters'][0]['id']==pending['id']
    assert all(x['id']!=pending['id'] for x in related['memories'])


async def test_end_to_end_sticker_without_search_receipt_and_transient_prompt(tmp_path,social):
    selector=seed(social)
    calls=[]
    social.delivery.sticker_sender=AsyncMock(return_value=500)
    loop=agent(tmp_path,social,[])
    async def model(**kwargs):
        calls.append(deepcopy(kwargs))
        assert len(social.selector.cache) == 1
        if len(calls)==1:
            return LLMResponse(content=None,tool_calls=[ToolCallRequest(id='sticker',name='social_sticker',arguments={'action':'send','sticker_id':'s-124'})])
        return LLMResponse(content='')
    loop.provider.chat_stream_with_retry=AsyncMock(side_effect=model)
    await loop._dispatch(await wake(social,text='蓝票这回领先了哈哈'))
    assert selector.chat.await_count==1
    assert social.selector.cache == {}
    first=str(calls[0]['messages'])
    assert 'optional_expression_references' in first and 's-124' in first
    assert 'pending_matters' in first and 'recent_confirmed_usage' in first
    assert social.delivery.sticker_sender.await_count==1
    row=social.store.db.execute("SELECT delivery_id FROM expression_selections WHERE kind='sticker'").fetchone()
    assert row and row[0]
    assert social.store.db.execute('SELECT status FROM actions WHERE id=?',(row[0],)).fetchone()[0]=='sent'
    assert social.store.db.execute("SELECT COUNT(*) FROM expression_selections WHERE kind!='sticker' AND delivery_id IS NOT NULL").fetchone()[0]==0
    saved=json.dumps(loop.sessions.get_or_create(A.session_key).messages)
    assert 'optional_expression_references' not in saved
    assert social.selector.recent_usage(A.session_key)['turns'][0]['stickers']==['s-124']


async def test_timeout_and_provider_error_do_not_block_main_text_generation(tmp_path,social):
    social.config.learning.catchphrases = ["本大爷", "白饭", "Token", "算力"]
    selector=seed(social)
    selector.chat.side_effect=RuntimeError('offline')
    social.set_sender(AsyncMock(return_value=601))
    loop=agent(tmp_path,social,[
        LLMResponse(content=None,tool_calls=[ToolCallRequest(id='answer',name='social_send',arguments={'text':'Token 用量要看实际账单；我这里看不到。'})]),
        LLMResponse(content=''),
    ])
    await loop._dispatch(await wake(social,text='Token 用量在哪看？'))
    assert selector.chat.await_count==1
    assert social.delivery.sender.await_count==1
    assert 'Token' in social.delivery.sender.call_args.args[1]
    assert social.store.db.execute('SELECT status FROM expression_turns').fetchone()[0]=='error'
    assert social.selector.recent_usage(A.session_key)['catchphrases']=={'Token':1}


async def test_restart_without_cache_cannot_make_a_second_call(social):
    provider=seed(social)
    view(social)
    assert await social.selector.select(A.session_key,'turn')
    social.selector.cache.clear()
    assert await social.selector.select(A.session_key,'turn')==[]
    assert provider.chat.await_count==1


async def test_prompt_character_budget_and_final_caps(social):
    provider=seed(social)
    view(social)
    social.config.learning.selection_chars=500
    chosen=await social.selector.select(A.session_key,'turn')
    payload=social.selector.context(chosen)
    assert len(json.dumps(payload,ensure_ascii=False))<=500
    assert len(payload['expressions'])<=3 and len(payload['jargon'])<=5 and len(payload['stickers'])<=5
    assert len(chosen)==sum(map(len,payload.values()))
    assert provider.chat.await_count==1


def test_exact_incoming_sticker_notes_are_scope_and_revision_bound(social):
    seed(social)
    ref=social.store.stickers.get(A.session_key,'s-124')
    social.store.observe(message(seq=1,text='[non-text message]',media=('sticker',),media_refs=(ref,)),500)
    social.store.view(A.session_key,count=30,chars=12000,turn_id='visual')
    source=social.selector.input(A.session_key,'visual')
    assert source[0].known_sticker_notes==['场景124：得意 自信 调侃 嘴硬 领先']
    # Identical platform ID alone does not grant access to another scope's notes.
    social.store.observe(message(B,seq=2,text='[non-text message]',media=('sticker',),media_refs=(ref,)),500)
    social.store.view(B.session_key,count=30,chars=12000,turn_id='other')
    assert social.selector.input(B.session_key,'other')[0].known_sticker_notes==[]
    social.store.observe(replace(message(seq=1,text='changed'),update_id=201,is_edit=True),500)
    assert social.selector.input(A.session_key,'visual')[0].known_sticker_notes==[]


@pytest.mark.parametrize('exception,level,status', [
    (TimeoutError('slow'), 'INFO', 'timeout'), (RuntimeError('boom'), 'WARNING', 'error'),
])
async def test_selector_failure_logs_and_keeps_single_call_cache(social, exception, level, status):
    provider = seed(social)
    view(social)
    provider.chat.side_effect = exception
    records = []
    sink = logger.add(lambda message: records.append(message.record))
    try:
        assert await social.selector.select(A.session_key, 'turn') == []
        assert await social.selector.select(A.session_key, 'turn') == []
    finally:
        logger.remove(sink)
    record = next(r for r in records if 'Social selector scope=' in r['message'])
    assert record['level'].name == level
    assert (record['exception'] is not None) == (level == 'WARNING')
    assert A.session_key in record['message'] and 'turn=turn' in record['message']
    assert social.store.db.execute('SELECT status FROM expression_turns').fetchone()[0] == status
    assert provider.chat.await_count == 1


@pytest.mark.parametrize('content,dropped,expected', [
    ('{"jargon":[{"id":"j1","note":"x"}],"stickers":["s2"]}', 0, [('jargon', 'j-a'), ('sticker', 's-124')]),
    ('{"jargon":["not-provided"],"stickers":["s2"]}', 1, [('sticker', 's-124')]),
    ('{"jargon":["s2"],"stickers":["s2"]}', 1, [('sticker', 's-124')]),
    ('{"expressions":["e0","e0","e0","e0"],"extra":true}', 0, [('expression', 'e-a')]),
    ('{"jargon":[{}, {"id":7}],"stickers":[{"id":"s2"}]}', 0, [('sticker', 's-124')]),
])
async def test_tolerant_selection_keeps_valid_candidates(social, content, dropped, expected):
    provider = seed(social)
    view(social)
    provider.chat.side_effect = None
    provider.chat.return_value = LLMResponse(content=content)
    chosen = await social.selector.select(A.session_key, 'turn')
    assert [(c.kind, c.entry_id) for c in chosen] == expected
    detail = social.store.learning.recent[A.session_key][-1]
    assert detail['status'] == 'selected' and detail['dropped'] == dropped
    assert provider.chat.await_count == 1


@pytest.mark.parametrize('log_line,content', [
    (208, '{"expressions":[],"jargon":[{"id":"j1","note":"codex"}],"stickers":["s2"],"reason":"算力/用量吐槽"}'),
    (425, '{"expressions":[],"jargon":[{"id":"j1","note":"codex"}],"stickers":["s2"],"reason":"算力/用量吐槽"}'),
])
async def test_recorded_validation_errors_keep_sticker_selection(social, log_line, content):
    # Exact response.content from /tmp/p7/container-72h.log; both pools were sticker-only.
    provider = seed(social)
    social.config.learning.selection_expressions = 0
    social.config.learning.selection_jargon = 0
    view(social, turn=f'log-{log_line}')
    pool = social.selector.candidates(A.session_key, social.selector.input(A.session_key, f'log-{log_line}'))
    assert Choice.model_validate_json(content).ids('stickers') == ['s2']
    provider.chat.side_effect = None
    provider.chat.return_value = LLMResponse(content=content)
    chosen = await social.selector.select(A.session_key, f'log-{log_line}')
    payload = json.loads(provider.chat.call_args.kwargs['messages'][1]['content'])
    assert {c['kind'] for c in payload['candidates']} == {'sticker'}
    assert chosen == [pool[2]]
    detail = social.store.learning.recent[A.session_key][-1]
    assert detail['status'] == 'selected' and detail['dropped'] == 1


@pytest.mark.parametrize('finish_reason,tool_calls', [
    ('length', []), ('stop', [ToolCallRequest(id='x', name='message', arguments={})]),
])
async def test_invalid_envelope_stays_error(social, finish_reason, tool_calls):
    provider = seed(social)
    view(social)
    provider.chat.side_effect = None
    provider.chat.return_value = LLMResponse(content='{"stickers":["s2"]}', finish_reason=finish_reason, tool_calls=tool_calls)
    assert await social.selector.select(A.session_key, 'turn') == []
    assert social.store.learning.recent[A.session_key][-1]['status'] == 'error'


async def test_valid_choices_are_truncated_to_selection_limit(social):
    provider = seed(social)
    view(social)
    source = social.selector.input(A.session_key, 'turn')
    pool = social.selector.candidates(A.session_key, source)
    refs = [f's{i}' for i, c in enumerate(pool) if c.kind == 'sticker'][:6]
    provider.chat.side_effect = None
    provider.chat.return_value = LLMResponse(content=json.dumps({'stickers': refs}))
    chosen = await social.selector.select(A.session_key, 'turn')
    assert chosen == [pool[int(ref[1:])] for ref in refs[:social.config.learning.selection_stickers]]
    assert social.store.learning.recent[A.session_key][-1]['dropped'] == 0
