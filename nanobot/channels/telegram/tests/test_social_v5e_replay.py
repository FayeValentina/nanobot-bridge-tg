"""Acceptance harness guards: historical clock, split and true persona-only ablation."""
import copy
from unittest.mock import AsyncMock

import pytest

from nanobot.channels.telegram.social.expressions import ExpressionLearner
from nanobot.channels.telegram.social.jargon_learning import JargonLearner
from nanobot.providers.base import LLMProvider
from scripts.evaluate_social_v5 import (
    FIXTURES,
    REPO,
    persona_only,
    read_json,
    train,
    validate_split,
)


def test_holdout_source_overlap_or_learnable_flag_is_rejected():
    corpus=read_json(FIXTURES/'social_v5_baseline.json')
    assert validate_split(corpus)=={'extraction_messages':39,'holdout_messages':61,'targets':18,'overlap':0}
    bad=copy.deepcopy(corpus)
    bad['evaluation'][0]['messages'][0]['learnable']=True
    with pytest.raises(AssertionError):
        validate_split(bad)
    bad=copy.deepcopy(corpus)
    bad['evaluation'][0]['messages'].append(copy.deepcopy(corpus['extraction'][0]['messages'][0]))
    bad['evaluation'][0]['scope']=corpus['extraction'][0]['scope']
    with pytest.raises(AssertionError):
        validate_split(bad)


def test_persona_only_keeps_old_search_pending_and_protocol():
    frozen=read_json(FIXTURES/'social_v5e_baseline_code.json')
    clean=(REPO/'docs/harness/groupmate.md').read_text()
    ablation=persona_only(frozen['persona'],clean)
    assert '经常挂着' not in ablation and '动不动' not in ablation
    assert '接话时先用 social_sticker 搜索' in ablation
    assert '本轮推荐的贴纸索引' not in ablation
    assert '最近真实发过' not in ablation
    assert '未完成待办和可选表达' not in ablation
    assert 'optional_expression_references' not in frozen['before_iteration']
    assert 'score += 12' in frozen['memory_context']


async def test_training_replays_every_human_at_source_time_without_consuming_holdout(tmp_path,monkeypatch):
    from nanobot.config.schema import Config

    corpus=read_json(FIXTURES/'social_v5_baseline.json')
    sources=[]
    async def empty_success(worker,batch):
        row=worker.store.db.execute('SELECT sources FROM learning_batches WHERE id=?',(batch,)).fetchone()
        import json
        sources.extend(json.loads(row[0]))
        worker.store.learning.finish(batch,success=True)
    monkeypatch.setattr(ExpressionLearner,'run',empty_success)
    monkeypatch.setattr(JargonLearner,'run',empty_success)
    monkeypatch.setattr('scripts.evaluate_social_v5.make_expression_provider',lambda *_:(AsyncMock(spec=LLMProvider),'test'))
    report=await train(Config(),corpus,{'messages':{},'stickers':{}},tmp_path/'unused.sqlite3',tmp_path)
    assert len(report['batches'])==4
    assert sorted(b['source_count'] for b in report['batches'])==[3,3,7,7]
    allowed={(m['message_id'],m['revision']) for s in corpus['extraction'] for m in s['messages'] if m['learnable']}
    assert {(s['message_id'],s['revision']) for s in sources}==allowed
    assert all(s['source']=='human_text' for s in sources)
    assert report['learning_does_not_advance_main'] and report['public_sends']==0


def test_release_rebind_preserves_judge_cursor_and_active_proactive_deadline(tmp_path):
    from nanobot.bus.queue import MessageBus
    from nanobot.channels.telegram.social.models import ObservedMessage
    from nanobot.channels.telegram.social.runtime import TelegramSocialRuntime
    from nanobot.config.schema import Config
    from scripts.evaluate_social_v5 import SCOPES, settings

    (tmp_path/'roles').mkdir()
    (tmp_path/'roles/guest.md').write_text('Guest')
    cfg=settings(Config())
    cfg.scopes[0].proactive.enabled=True
    now=[1000.]
    runtime=TelegramSocialRuntime(cfg,tmp_path,MessageBus(),now=lambda:now[0])
    scope=SCOPES['main']
    try:
        runtime.store.observe(ObservedMessage(scope,1,1,'member','Member',now[0],'new'),500)
        with runtime.store.db:
            runtime.store.db.execute('UPDATE judge_state SET assessed_through=0,pending_due=1002 WHERE scope=?',(scope.session_key,))
        due=runtime.proactive.status(scope.session_key)['next_check']
        runtime.store.bind(scope,'new-protocol')
        runtime.proactive.sync(scope,cfg.scopes[0])
        assert runtime.store.judge_through(scope.session_key)==0
        assert runtime.proactive.status(scope.session_key)['next_check']==due
        assert runtime.store.db.execute('SELECT pending_due FROM judge_state WHERE scope=?',(scope.session_key,)).fetchone()[0]==1000
        # A real policy change still reschedules; a mere protocol rebind does not.
        cfg.scopes[0].proactive.min_interval_seconds=60
        cfg.scopes[0].proactive.max_interval_seconds=60
        runtime.proactive.sync(scope,cfg.scopes[0])
        assert runtime.proactive.status(scope.session_key)['next_check']==1060
    finally:
        runtime.close()
