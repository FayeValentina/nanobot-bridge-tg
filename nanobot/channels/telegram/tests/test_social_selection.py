"""Local optional retrieval, receipt facts and the actual main-agent hook path."""
import json
from copy import deepcopy
from dataclasses import replace
from unittest.mock import AsyncMock, Mock

import pytest

from nanobot.bus.events import DeliveryReceipt
from nanobot.channels.telegram.social.models import MediaReference
from nanobot.channels.telegram.social.runtime import encode_view
from nanobot.channels.telegram.tests.test_social_memory import apply, expose
from nanobot.channels.telegram.tests.test_social_runtime import A, B, agent, message, social, wake
from nanobot.providers.base import LLMResponse, ToolCallRequest

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


def view(runtime, turn='turn', text='蓝票这回领先了哈哈', mid=1):
    runtime.store.observe(message(seq=mid, text=text), 500)
    return runtime.store.view(A.session_key, count=30, chars=12000, turn_id=turn)


def test_scope_scene_keyword_pool_caps_and_same_turn_cache(social, monkeypatch):
    seed(social)
    view(social)
    record = Mock(wraps=social.store.learning.record_selection)
    monkeypatch.setattr(social.store.learning, 'record_selection', record)
    selected = social.selector.select(A.session_key, 'turn')
    assert [(x.kind, x.entry_id) for x in selected] == [('expression','e-a'),('jargon','j-a')]
    assert social.selector.select(A.session_key, 'turn') is selected
    assert record.call_count == 1
    detail = record.call_args.kwargs
    assert detail['target_message_id'] == 1
    assert detail['calls'] == detail['input_tokens'] == detail['output_tokens'] == 0
    assert detail['latency_ms'] >= 0
    assert detail['explanation'] == {
        'input_messages': [[1, 1]],
        'candidates': [{'kind': c.kind, 'id': c.entry_id, 'selected': True} for c in selected],
    }
    row = social.store.db.execute('SELECT calls,candidate_count,status FROM expression_turns').fetchone()
    assert tuple(row) == (0, 2, 'retrieved')
    assert social.store.scopes.state(A.session_key)['seen_through'] == social.store.scopes.state(A.session_key)['read_through'] == 0


@pytest.mark.parametrize('learning_enabled', [False, True])
async def test_live_context_has_full_sticker_index_outside_expression_budget(social, learning_enabled):
    seed(social)
    view(social)
    social.config.learning.enabled = learning_enabled
    social.config.learning.selection_chars = 500
    blocks = await social.context_builder.build(A.session_key, 'turn', '')
    data = json.loads(blocks[0].content.split('\n', 1)[1])
    expected = {f's-{i}': f'场景{i}：' + ('得意 自信 调侃 嘴硬 领先' if i == 124 else '困倦 睡觉 晚安')
                for i in range(125)}
    assert data['sticker_library'] == expected
    assert list(data['sticker_library']) == sorted(expected)
    assert set(data['optional_expression_references']) == {'expressions', 'jargon'}
    social.store.stickers.annotate(A.session_key, {'s-124': '新版注释'})
    updated = await social.context_builder.build(A.session_key, 'turn', '')
    assert json.loads(updated[0].content.split('\n', 1)[1])['sticker_library']['s-124'] == '新版注释'


def test_actual_unclaimed_snapshot_selected_before_it_is_marked_seen(social):
    seed(social)
    data = view(social)
    social.store.db.execute("UPDATE views SET turn_id=NULL")
    assert social.selector.select(A.session_key, 'turn', encode_view(data))
    assert social.store.db.execute('SELECT seen FROM views').fetchone()[0] == 0


@pytest.mark.parametrize('change', ['message_edit','new_message','entry_revision','disabled','config','scope'])
def test_changed_values_reuse_first_selection_without_second_record(social, change):
    seed(social)
    view(social)
    selected = social.selector.select(A.session_key, 'turn')
    assert selected
    if change == 'message_edit':
        social.store.observe(replace(message(text='现在认真求助'), update_id=101, is_edit=True), 500)
    elif change == 'new_message':
        view(social, mid=2, text='认真求助')
    elif change == 'entry_revision':
        social.store.db.execute("UPDATE expressions SET revision=revision+1")
    elif change == 'disabled':
        social.store.db.execute("UPDATE expressions SET status='disabled'")
    elif change == 'scope':
        social.store.bind(A, 'another-version')
    else:
        social.store.learning.configure(social.config.learning.model_copy(update={'selection_expressions':0}))
    assert social.selector.select(A.session_key, 'turn') is selected
    assert social.store.db.execute('SELECT COUNT(*) FROM expression_turns').fetchone()[0] == 1
    assert 'reuse_status' not in social.store.learning.recent[A.session_key][-1]


def test_zero_relevance_candidates_are_not_injected(social):
    seed(social)
    view(social, text='unrelated zebra')
    assert social.expressions.library.candidates(A.session_key, ['zebra'], 10)
    assert social.jargon.jargon.candidates(A.session_key, ['zebra'], social.now(), 20)
    assert social.selector.candidates(A.session_key, social.selector.input(A.session_key, 'turn')) == []
    assert social.selector.select(A.session_key, 'turn') == []
    assert tuple(social.store.db.execute('SELECT status,calls,candidate_count FROM expression_turns').fetchone()) == ('empty', 0, 0)


@pytest.mark.parametrize('seeded', [False, True])
def test_empty_library_or_irrelevant_help_has_empty_status(social, seeded):
    if seeded:
        seed(social)
    view(social, text='认真求助：这份结果应该怎么理解？')
    assert social.selector.select(A.session_key, 'turn') == []
    assert tuple(social.store.db.execute('SELECT status,calls FROM expression_turns').fetchone()) == ('empty', 0)


@pytest.mark.parametrize('unavailable', ['disabled', 'no_source'])
def test_disabled_or_unavailable_status(social, unavailable):
    seed(social)
    if unavailable == 'disabled':
        view(social)
        social.config.learning.enabled = False
    assert social.selector.select(A.session_key, 'turn') == []
    assert tuple(social.store.db.execute('SELECT status,calls FROM expression_turns').fetchone()) == ('disabled_or_unavailable', 0)


@pytest.mark.parametrize('kind', ['expression', 'jargon'])
def test_inactive_or_changed_candidate_is_filtered(social, monkeypatch, kind):
    seed(social)
    view(social)
    pool = social.selector.candidates(A.session_key, social.selector.input(A.session_key, 'turn'))
    monkeypatch.setattr(social.selector, 'candidates', lambda *_: pool)
    if kind == 'expression':
        social.store.db.execute("UPDATE expressions SET status='disabled'")
    else:
        social.store.db.execute('UPDATE jargon SET revision=revision+1')
    selected = social.selector.select(A.session_key, 'turn')
    assert selected == [c for c in pool if c.kind != kind]
    assert not any(c.kind == kind for c in selected)
    detail = social.store.learning.recent[A.session_key][-1]
    assert detail['status'] == 'retrieved'
    assert next(c for c in detail['candidates'] if c['kind'] == kind)['selected'] is False


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
    seed(social)
    calls=[]
    social.delivery.sticker_sender=AsyncMock(return_value=500)
    loop=agent(tmp_path,social,[])
    async def model(**kwargs):
        calls.append(deepcopy(kwargs))
        assert len(social.selector.cache) == 1
        if len(calls)==1:
            return LLMResponse(content=None,tool_calls=[ToolCallRequest(id='sticker',name='social_send',arguments={'sticker_id':'s-0'})])
        return LLMResponse(content='')
    loop.provider.chat_stream_with_retry=AsyncMock(side_effect=model)
    await loop._dispatch(await wake(social,text='蓝票这回领先了哈哈'))
    assert social.store.db.execute('SELECT calls FROM expression_turns').fetchone()[0] == 0
    assert social.selector.cache == {}
    first=str(calls[0]['messages'])
    assert 'optional_expression_references' in first and 's-0' in first
    assert 'pending_matters' in first and 'recent_confirmed_usage' in first
    assert social.delivery.sticker_sender.await_count==1
    assert 'sticker_library' in first and 's-124' in first
    assert social.store.db.execute("SELECT COUNT(*) FROM expression_selections WHERE kind='sticker'").fetchone()[0] == 0
    assert social.store.db.execute("SELECT status FROM actions WHERE kind='sticker'").fetchone()[0] == 'sent'
    assert social.store.db.execute("SELECT COUNT(*) FROM expression_selections WHERE delivery_id IS NOT NULL").fetchone()[0] == 0
    saved=json.dumps(loop.sessions.get_or_create(A.session_key).messages)
    assert 'optional_expression_references' not in saved
    assert 'sticker_library' not in saved
    assert social.selector.recent_usage(A.session_key)['turns'][0]['stickers']==['s-0']


async def test_empty_retrieval_does_not_block_main_text_generation(tmp_path, social):
    social.config.learning.catchphrases = ["本大爷", "白饭", "Token", "算力"]
    seed(social)
    social.set_sender(AsyncMock(return_value=601))
    loop = agent(tmp_path, social, [
        LLMResponse(content=None, tool_calls=[ToolCallRequest(id='answer', name='social_send', arguments={'text':'Token 用量要看实际账单；我这里看不到。'})]),
        LLMResponse(content=''),
    ])
    await loop._dispatch(await wake(social, text='Token 用量在哪看？'))
    assert social.delivery.sender.await_count == 1
    assert 'Token' in social.delivery.sender.call_args.args[1]
    assert tuple(social.store.db.execute('SELECT status,calls FROM expression_turns').fetchone()) == ('empty', 0)
    assert social.selector.recent_usage(A.session_key)['catchphrases'] == {'Token':1}


def test_restart_without_cache_cannot_record_a_second_selection(social, monkeypatch):
    seed(social)
    view(social)
    assert social.selector.select(A.session_key, 'turn')
    social.selector.cache.clear()
    source = Mock(side_effect=AssertionError('persisted selection must short circuit input'))
    monkeypatch.setattr(social.selector, 'input', source)
    assert social.selector.select(A.session_key, 'turn') == []
    source.assert_not_called()
    assert social.store.db.execute('SELECT COUNT(*) FROM expression_turns').fetchone()[0] == 1


def test_prompt_character_budget_and_final_caps(social):
    seed(social)
    view(social)
    social.config.learning.selection_chars = 500
    social.store.db.execute("UPDATE jargon SET local_meaning=?", ('临时加场' * 90,))
    chosen = social.selector.select(A.session_key, 'turn')
    payload = social.selector.context(chosen)
    assert len(json.dumps(payload, ensure_ascii=False)) <= 500
    assert 0 < len(chosen) < 2
    assert len(chosen) == sum(map(len, payload.values()))
    assert social.store.db.execute('SELECT COUNT(*) FROM expression_selections').fetchone()[0] == len(chosen)
    assert social.store.learning.recent[A.session_key][-1]['status'] == 'retrieved'


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


def test_retrieval_order_is_truncated_to_each_kind_limit(social):
    seed(social)
    db = social.store.db
    with db:
        for index in range(4):
            db.execute("INSERT INTO expressions(id,scope,situation,expression,status,scene_tags,intent_category,created,updated) SELECT ?,scope,situation,expression,status,scene_tags,intent_category,created,updated FROM expressions WHERE id='e-a'", (f'e-extra-{index}',))
            db.execute("INSERT INTO jargon(id,scope,content,status,basis,revision,created,updated,term,local_meaning) SELECT ?,scope,content,status,basis,revision,created,updated,term,local_meaning FROM jargon WHERE id='j-a'", (f'j-extra-{index}',))
            ref = MediaReference(kind='sticker', file_id=f'extra-{index}', unique_id=f's-extra-{index}')
            social.store.stickers.collect(A.session_key, ref, '哈哈 调侃 嘴硬 领先', 500)
    view(social)
    social.config.learning.selection_expressions = 2
    social.config.learning.selection_jargon = 2
    pool = social.selector.candidates(A.session_key, social.selector.input(A.session_key, 'turn'))
    assert {c.kind for c in pool} == {'expression', 'jargon'}
    chosen = social.selector.select(A.session_key, 'turn')
    for kind in ('expression', 'jargon'):
        assert len([c for c in pool if c.kind == kind]) > 2
        assert [c for c in chosen if c.kind == kind] == [c for c in pool if c.kind == kind][:2]


def test_cache_is_bounded_to_100_turns(social):
    for index in range(101):
        social.selector.select(A.session_key, f'turn-{index}')
    assert len(social.selector.cache) == 100
    assert (A.session_key, 'turn-0') not in social.selector.cache
    assert (A.session_key, 'turn-100') in social.selector.cache


def test_record_selection_write_error_propagates(social, monkeypatch):
    seed(social)
    view(social)
    monkeypatch.setattr(social.store.learning, 'record_selection', Mock(side_effect=RuntimeError('write failed')))
    with pytest.raises(RuntimeError, match='write failed'):
        social.selector.select(A.session_key, 'turn')
