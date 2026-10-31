"""V5-C meanings, actual-body attribution, authority and shared background budgets."""
import asyncio
import json
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.web import WebFetchTool, WebSearchTool
from nanobot.bus.queue import MessageBus
from nanobot.channels.telegram.social.jargon import (
    JargonCandidate,
    JargonDiscovery,
    JargonRequest,
)
from nanobot.channels.telegram.social.memory import MemoryRequest
from nanobot.channels.telegram.social.models import ConversationScope, ObservedMessage
from nanobot.channels.telegram.social.runtime import TelegramSocialRuntime, encode_view
from nanobot.config.schema import TelegramSocialConfig
from nanobot.providers.base import LLMProvider, LLMResponse

A = ConversationScope('telegram', -1001, 42)


@pytest.fixture
def env(tmp_path):
    (tmp_path/'roles').mkdir()
    (tmp_path/'roles/guest.md').write_text('Guest')
    config = TelegramSocialConfig.model_validate({'enabled': True, 'learning': {'minMessages': 1},
        'scopes': [{'chatId': A.chat_id, 'threadId': A.thread_id, 'roleId': 'guest'}]})
    clock = [1000.]
    runtime = TelegramSocialRuntime(config, tmp_path, MessageBus(), now=lambda: clock[0])
    provider = AsyncMock(spec=LLMProvider)
    worker = runtime.jargon
    worker.set_provider(provider, 'test-jargon-model')
    registry = ToolRegistry()
    search, fetch = WebSearchTool(), WebFetchTool()
    search.execute = AsyncMock(return_value='No results')
    fetch.execute = AsyncMock()
    registry.register(search)
    registry.register(fetch)
    worker.tools = registry
    yield runtime, provider, search.execute, fetch.execute, clock
    runtime.close()


def add(runtime, mid=1, text='又白跑了一趟，栓Q', **changes):
    msg = ObservedMessage(A, mid, mid, 'human', 'Member', runtime.now(), text, learning_text=text)
    runtime.store.observe(replace(msg, **changes), 100)


def discover(mid=1, term='栓Q', excerpt='又白跑了一趟，栓Q', **changes):
    data = dict(term=term, message_id=mid, revision=1, sender_id='human', excerpt=excerpt,
        contextual=True, ordinary=False, local_only=False, sensitive=False, uncertain=True,
        needs_public=True, safe_to_use=True, hypothesis='可能是表达无奈的网络梗')
    data.update(changes)
    return JargonCandidate.model_validate(data)


def response(*candidates):
    return LLMResponse(content=JargonDiscovery(jargon=list(candidates)).model_dump_json())


async def run(runtime):
    batch = runtime.store.learning.prepare(A.session_key, 'jargon')
    assert batch
    await runtime.jargon.run(batch)
    return dict(runtime.store.db.execute('SELECT * FROM learning_batches WHERE id=?', (batch,)).fetchone())


def expose(runtime):
    view = runtime.store.view(A.session_key, count=30, chars=12000, turn_id='turn')
    runtime.store.note_seen(A.session_key, 'turn', encode_view(view))
    return runtime.memory.jargon


async def test_public_meme_verified_only_from_read_body_and_not_searched_again(env):
    runtime, provider, search, fetch, clock = env
    add(runtime)
    url = 'https://example.org/meme'
    quote = '栓Q是英语thank you的谐音，用来表达无奈或无语的情绪。'
    body = quote + '本文讲解网络用语的语境、来源和使用限制。' * 8
    search.return_value = f'1. 来源\n   {url}\n   搜索摘要不能当正文'
    fetch.return_value = json.dumps({'url':url,'status':200,'extractor':'readability','text':body})
    provider.chat.side_effect = [response(discover()), LLMResponse(content=json.dumps({'findings':[dict(term='栓Q', public_meaning='thank you 的谐音，用于无奈或无语', usage='轻松吐槽时可用',
        citations=[{'url':url,'quote':quote}], uncertain=False,safe_to_use=True,conflict=False)]},ensure_ascii=False))]
    batch = await run(runtime)
    assert batch['status']=='succeeded' and batch['search_calls']==batch['fetch_calls']==1 and batch['model_calls']==2
    entry = runtime.jargon.jargon.entries(A.session_key)[0]
    assert entry['status']=='usable' and entry['public_meaning'] and entry['local_meaning']==''
    assert entry['source_links']==[url] and quote in str(entry['inference_basis'])
    assert entry['research_revision']==entry['revision']
    clock[0]+=301
    add(runtime,2)
    provider.chat.side_effect = None
    provider.chat.return_value=response(discover(2))
    await run(runtime)
    assert search.await_count==1 and fetch.await_count==1
    assert runtime.bus.inbound_size==runtime.bus.outbound_size==0


async def test_local_special_meaning_is_understand_only_and_never_searches(env):
    runtime, provider, search, fetch, _ = env
    text='我们群把临时取消的聚会叫蓝票，不是车票。'
    add(runtime,text=text)
    provider.chat.return_value=response(discover(term='蓝票',excerpt=text,local_only=True,local_meaning='临时取消的聚会',explanation_quote=text,needs_public=False,uncertain=False))
    assert (await run(runtime))['status']=='succeeded'
    entry=runtime.jargon.jargon.entries(A.session_key)[0]
    assert entry['local_meaning']=='临时取消的聚会' and entry['status']=='understand_only' and entry['authority']=='human'
    assert not entry['public_meaning'] and search.await_count==fetch.await_count==0
    context=json.loads(runtime.memory.context(A.session_key))
    assert context['jargon_understanding'][0]['status']=='understand_only'
    assert 'do NOT' in str(runtime.jargon.jargon.get(A.session_key,entry['id'])) or 'do not' in str(entry['use_policy'])


async def test_no_search_result_never_promotes_hypothesis_to_meaning(env):
    runtime, provider, search, fetch, clock = env
    text='这回又咕噜帧了'
    add(runtime,text=text)
    provider.chat.return_value=response(discover(term='咕噜帧',excerpt=text,hypothesis='猜测是某种出错，但不能确认'))
    batch=await run(runtime)
    entry=runtime.jargon.jargon.entries(A.session_key)[0]
    assert batch['status']=='succeeded' and entry['status']=='candidate'
    assert entry['local_meaning']==entry['public_meaning']=='' and entry['source_links']==[]
    assert search.await_count==1 and fetch.await_count==0
    clock[0]+=301
    add(runtime,2,text)
    provider.chat.return_value=response(discover(2,term='咕噜帧',excerpt=text))
    await run(runtime)
    assert search.await_count==1


async def test_explicit_correction_overwrites_inference_and_old_windows_cannot_restore_it(env):
    runtime, provider, _, _, clock = env
    add(runtime,text='今晚又蓝票了')
    provider.chat.return_value=response(discover(term='蓝票',excerpt='今晚又蓝票了',local_only=True,needs_public=False,hypothesis='可能指取消'))
    await run(runtime)
    entry=runtime.jargon.jargon.entries(A.session_key)[0]
    text='纠正一下，蓝票在我们群专指临时加场，不是取消。'
    add(runtime,2,text)
    memory=expose(runtime)
    updated=runtime.jargon.jargon.apply(A.session_key,'turn',JargonRequest(action='explain',entry_id=entry['id'],expected_revision=entry['revision'],
        term='蓝票',message_ids=[2],local_meaning='临时加场',explanation_quote=text),memory)['jargon']
    assert updated['revision']>entry['revision'] and updated['local_meaning']=='临时加场'
    clock[0]+=301
    add(runtime,1,'今晚又蓝票了',update_id=101,is_edit=True)
    provider.chat.return_value=response(discover(revision=2,term='蓝票',excerpt='今晚又蓝票了',local_only=True,needs_public=False,hypothesis='可能指取消'))
    # Source-edit invalidation does not touch corrected entry: its active evidence is message 2.
    await run(runtime)
    current=runtime.jargon.jargon.get(A.session_key,entry['id'])
    assert current['local_meaning']=='临时加场' and current['revision']==updated['revision']
    assert '取消' not in current['local_meaning']


@pytest.mark.parametrize('flags',[{'is_self':True},{'is_bot':True},{'is_forwarded':True},{'learning_text':None}])
async def test_context_or_self_ids_cannot_be_forged_as_learning_sources(env,flags):
    runtime,provider,search,_,_=env
    add(runtime,1)
    add(runtime,2,**flags)
    provider.chat.return_value=response(discover(2,needs_public=False))
    batch=await run(runtime)
    assert batch['status']=='failed' and not runtime.jargon.jargon.entries(A.session_key)
    assert search.await_count==0
    assert runtime.store.db.execute("SELECT event_id FROM learning_cursors WHERE kind='jargon'").fetchone()[0]==0


@pytest.mark.parametrize('term',['今天','明天','吃饭','公交','照片'])
async def test_ordinary_words_produce_empty_success(env,term):
    runtime,provider,search,_,_=env
    add(runtime,text=term)
    provider.chat.return_value=response(discover(term=term,excerpt=term,ordinary=True))
    batch=await run(runtime)
    assert batch['status']=='succeeded' and batch['result_count']==0 and search.await_count==0


async def test_invented_public_citation_rolls_back_entries_and_cursor(env):
    runtime,provider,search,fetch,_=env
    add(runtime)
    url='https://example.org/body'
    search.return_value=url
    fetch.return_value=json.dumps({'url':url,'status':200,'extractor':'raw','text':'栓Q 的网页，但没有你编的那句话。'*20})
    provider.chat.side_effect=[response(discover()),LLMResponse(content=json.dumps({'findings':[dict(term='栓Q',public_meaning='编造的含义',usage='',citations=[{'url':url,'quote':'这段内容从来没有出现在读取的网页正文中'}],uncertain=False,safe_to_use=True,conflict=False)]},ensure_ascii=False))]
    batch=await run(runtime)
    assert batch['status']=='failed' and runtime.jargon.jargon.entries(A.session_key)==[]
    assert batch['search_calls']==1 and batch['fetch_calls']==1


async def test_search_snippets_without_body_are_not_verified(env):
    runtime,provider,search,fetch,_=env
    add(runtime)
    search.return_value='https://example.org/title 这个摘要给出了意思'
    fetch.return_value=json.dumps({'url':'https://example.org/title','status':200,'extractor':'raw','text':'只有标题'})
    provider.chat.return_value=response(discover())
    batch=await run(runtime)
    assert batch['status']=='succeeded' and batch['model_calls']==1
    entry=runtime.jargon.jargon.entries(A.session_key)[0]
    assert entry['status']=='candidate' and entry['source_links']==[] and not entry['public_meaning']


async def test_manual_query_submission_and_review_never_wake_or_search_foreground(env):
    runtime,provider,search,_,clock=env
    add(runtime)
    memory=expose(runtime)
    entry=runtime.jargon.jargon.apply(A.session_key,'turn',JargonRequest(action='submit',term='栓Q',message_ids=[1],research=True),memory)['jargon']
    assert entry['status']=='candidate' and search.await_count==provider.chat.await_count==0
    assert runtime.jargon.jargon.apply(A.session_key,'turn',JargonRequest(action='get',term='栓Q'),memory)['jargon']['id']==entry['id']
    before=runtime.store.learning.status(A.session_key)['cursors']
    runtime.jargon.prepare_reviews([A.session_key])
    batch=runtime.store.db.execute("SELECT id,purpose FROM learning_batches WHERE kind='jargon'").fetchone()
    assert batch['purpose']=='review'
    await runtime.jargon.run(batch['id'])
    assert search.await_count==1 and provider.chat.await_count==0  # No body => no speculative meaning model call.
    assert runtime.store.learning.status(A.session_key)['cursors']==before or runtime.store.db.execute("SELECT event_id FROM learning_cursors WHERE kind='jargon'").fetchone()[0]==0
    assert runtime.bus.inbound_size==runtime.bus.outbound_size==0
    assert not runtime.store.db.execute('SELECT 1 FROM wakes').fetchone()
    clock[0]+=301


async def test_correction_during_web_research_rejects_stale_result(env):
    runtime,provider,search,fetch,_=env
    add(runtime)
    memory=expose(runtime)
    entry=runtime.jargon.jargon.apply(A.session_key,'turn',JargonRequest(action='submit',term='栓Q',message_ids=[1],research=True),memory)['jargon']
    runtime.jargon.prepare_reviews([A.session_key])
    url='https://example.org/body'
    search.return_value=url
    entered,release=asyncio.Event(),asyncio.Event()
    async def delayed(**kwargs):
        entered.set()
        await release.wait()
        return json.dumps({'url':url,'status':200,'extractor':'raw','text':'栓Q公开正文'*30})
    fetch.side_effect=delayed
    provider.chat.return_value=LLMResponse(content='{"findings":[]}')
    batch=runtime.store.db.execute("SELECT id FROM learning_batches WHERE kind='jargon'").fetchone()[0]
    task=asyncio.create_task(runtime.jargon.run(batch))
    await entered.wait()
    updated=runtime.jargon.jargon.apply(A.session_key,'turn',JargonRequest(action='correct',entry_id=entry['id'],expected_revision=entry['revision'],origin='operator',local_meaning='群内仅表示收工',reason='操作者明确纠正'),memory)['jargon']
    release.set()
    await task
    assert runtime.store.db.execute('SELECT status FROM learning_batches WHERE id=?',(batch,)).fetchone()[0]=='failed'
    assert runtime.jargon.jargon.get(A.session_key,entry['id'])['local_meaning']=='群内仅表示收工'
    assert updated['authority']=='operator'


def test_legacy_slang_uses_same_ids_and_corrections_are_versioned(env):
    runtime,_,search,_,_=env
    text='蓝票就是临时取消的聚会'
    add(runtime,text=text)
    expose(runtime)
    entry=runtime.memory.apply(A.session_key,'turn',MemoryRequest(action='record',kind='slang',term='蓝票',content=text,basis='direct',message_ids=[1]))['memory']
    assert entry['status']=='understand_only' and entry['authority']=='human'
    assert runtime.jargon.jargon.find(A.session_key,'蓝票')==entry['id']
    add(runtime,2,'蓝票就是临时加场')
    expose(runtime)
    corrected=runtime.memory.apply(A.session_key,'turn',MemoryRequest(action='correct',kind='slang',memory_id=entry['id'],content='蓝票就是临时加场',basis='direct',message_ids=[2]))['memory']
    assert corrected['revision']>entry['revision'] and corrected['local_meaning']=='蓝票就是临时加场'
    assert search.await_count==0
    with pytest.raises(ValueError,match='retired'):
        runtime.memory.apply(A.session_key,'turn',MemoryRequest(action='record',kind='slang',term='蓝票',content=text,message_ids=[1]))


async def test_expression_and_jargon_share_one_task_and_durable_interval(env):
    runtime, provider, search, _, clock = env
    add(runtime)
    expression_provider = AsyncMock(spec=LLMProvider)
    expression_provider.chat.return_value = LLMResponse(content='{"expressions":[]}')
    runtime.expressions.set_provider(expression_provider, 'test-expression')
    provider.chat.return_value = response()
    runtime.store.learning.collect_due([A.session_key])
    runtime.expressions.start_due([A.session_key])
    first = runtime.learning_task
    runtime.jargon.start_due([A.session_key])
    assert runtime.learning_task is first and runtime.jargon.task is None
    await first
    runtime.jargon.start_due([A.session_key])
    assert runtime.jargon.task is None
    clock[0] += 301
    runtime.jargon.start_due([A.session_key])
    await runtime.jargon.task
    assert expression_provider.chat.await_count == provider.chat.await_count == 1
    assert search.await_count == 0


def test_local_and_public_meanings_remain_separate_on_conflict(env):
    from nanobot.channels.telegram.social.jargon import PublicFinding, ReadPage

    runtime, _, _, _, _ = env
    text = '我们群把临时加场叫蓝票'
    add(runtime, text=text)
    memory = expose(runtime)
    entry = runtime.jargon.jargon.apply(A.session_key, 'turn', JargonRequest(action='explain', term='蓝票', message_ids=[1],
        local_meaning='临时加场', explanation_quote=text), memory)['jargon']
    quote = '蓝票在这个公开材料中表示一种蓝色的纸质票据。'
    runtime.jargon.jargon.research(A.session_key, entry['id'], PublicFinding(term='蓝票', public_meaning='蓝色的纸质票据',
        citations=[{'url':'https://example.org/ticket','quote':quote}], uncertain=False, safe_to_use=True, conflict=True),
        [ReadPage(url='https://example.org/ticket', text=quote, extractor='raw', at=1000.)], entry['revision'])
    current = runtime.jargon.jargon.get(A.session_key, entry['id'])
    assert current['local_meaning'] == '临时加场'
    assert current['public_meaning'] == '蓝色的纸质票据'
    assert current['status'] == 'understand_only' and current['meaning_status'] == 'divergent'


async def test_network_budgets_and_read_pages_survive_failed_attempt(env):
    runtime, provider, search, fetch, clock = env
    add(runtime)
    quote = '栓Q是英语thank you的谐音，用来表达无奈或无语的情绪。'
    url = 'https://example.org/body'
    search.return_value = url
    fetch.return_value = json.dumps({'url':url, 'status':200, 'extractor':'raw', 'text':quote*8})
    invented = LLMResponse(content=json.dumps({'findings':[dict(term='栓Q',public_meaning='错误释义', usage='',
        citations=[{'url':url, 'quote':'这句话并不存在于实际抓取和阅读的正文中'}], uncertain=False,safe_to_use=True,conflict=False)]},ensure_ascii=False))
    provider.chat.side_effect = [response(discover()), invented, response(discover()), invented]
    first = await run(runtime)
    assert first['status'] == 'failed'
    clock[0] += 301
    second = await run(runtime)
    assert second['status'] == 'failed' and second['attempts'] == 2 and second['model_calls'] == 4
    assert second['search_calls'] == second['fetch_calls'] == 1
    assert search.await_count == fetch.await_count == 1
    assert runtime.jargon.jargon.entries(A.session_key) == []


async def test_disabled_and_disputed_terms_never_auto_reactivate(env):
    runtime, provider, search, _, clock = env
    add(runtime)
    provider.chat.return_value = response(discover(disputed=True,needs_public=False))
    await run(runtime)
    entry = runtime.jargon.jargon.entries(A.session_key)[0]
    assert entry['status'] == 'disabled'
    clock[0] += 301
    add(runtime,2)
    provider.chat.return_value = response(discover(2))
    await run(runtime)
    assert runtime.jargon.jargon.get(A.session_key,entry['id'])['status'] == 'disabled'
    assert search.await_count == 0


def test_operator_correction_is_locked_and_scope_and_revision_are_checked(env):
    runtime, _, _, _, _ = env
    add(runtime)
    memory = expose(runtime)
    entry = runtime.jargon.jargon.apply(A.session_key,'turn',JargonRequest(action='submit',term='栓Q',message_ids=[1]),memory)['jargon']
    with pytest.raises(ValueError, match='expected_revision'):
        runtime.jargon.jargon.apply(A.session_key,'turn',JargonRequest(action='disable',entry_id=entry['id'],expected_revision=99),memory)
    changed = runtime.jargon.jargon.apply(A.session_key,'turn',JargonRequest(action='correct',entry_id=entry['id'],expected_revision=entry['revision'],
        local_meaning='本群只表示收工',origin='operator',reason='操作者明确说明'),memory)['jargon']
    assert changed['authority'] == 'operator' and changed['status'] == 'understand_only'
    with pytest.raises(ValueError, match='scope'):
        runtime.jargon.jargon.get('telegram:another',entry['id'])
    with pytest.raises(ValueError, match='retired'):
        runtime.jargon.jargon.apply(A.session_key,'turn',JargonRequest(action='submit',term='栓Q',entry_id=entry['id'],expected_revision=changed['revision'],message_ids=[1]),memory)


def test_legacy_candidate_can_be_confirmed_with_explicit_human_evidence(env):
    runtime, _, _, _, _ = env
    add(runtime, text='蓝票就是临时加场')
    expose(runtime)
    first = runtime.memory.apply(A.session_key, 'turn', MemoryRequest(action='record',kind='slang',term='蓝票',content='蓝票指临时加场',message_ids=[1]))['memory']
    assert first['status']=='candidate' and not first['local_meaning']
    confirmed = runtime.memory.apply(A.session_key,'turn',MemoryRequest(action='confirm',memory_id=first['id'],basis='direct',message_ids=[1]))['memory']
    assert confirmed['status']=='understand_only' and confirmed['local_meaning']=='蓝票指临时加场'
    listed = runtime.memory.apply(A.session_key,'turn',MemoryRequest(action='list',kind='slang',memory_id=first['id']))['memories']
    assert len(listed)==1 and listed[0]['evidence'][0]['message_id']==1


def test_understanding_only_cannot_be_attributed_as_natural_use(env):
    runtime, _, _, _, _ = env
    add(runtime,text='蓝票就是临时加场')
    memory = expose(runtime)
    entry = runtime.jargon.jargon.apply(A.session_key,'turn',JargonRequest(action='explain',term='蓝票',message_ids=[1],local_meaning='临时加场',explanation_quote='蓝票就是临时加场'),memory)['jargon']
    with pytest.raises(ValueError,match='selected entry'):
        runtime.store.learning.record_selection(A.session_key,'selection',target_message_id=1,candidates=1,selected=[('jargon',entry['id'],entry['revision'])],explanation={})
    with pytest.raises(ValueError,match='only current usable'):
        runtime.store.learning.confirm_use(A.session_key,'selection','jargon',entry['id'],entry['revision'],'receipt')


def test_frozen_actual_model_and_network_report_has_traceable_sources():
    from pathlib import Path

    report = json.loads((Path(__file__).parent/'fixtures/social_v5c_actual_scenarios.json').read_text())
    assert report['mode']=='synthetic_conversations_actual_model_and_web'
    for case in report['reports']:
        assert all(case['checks'].values())
        read_urls = {p['url'] for p in case['batch']['read_pages']}
        for entry in case['entries']:
            assert set(entry['source_links']).issubset(read_urls)
            assert entry['status'] in {'candidate','understand_only','usable','disabled'}
            if case['case']=='not_found':
                assert not entry['local_meaning'] and not entry['public_meaning']
