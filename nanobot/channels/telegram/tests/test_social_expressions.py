"""V5-B extraction boundaries, revision tombstones and silent model scheduling."""
import asyncio
import json
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from nanobot.bus.queue import MessageBus
from nanobot.channels.telegram.social.expressions import (
    ExpressionCandidate,
    ExpressionEdit,
    ExpressionLibrary,
    ExtractionResult,
    make_expression_provider,
)
from nanobot.channels.telegram.social.jargon import JargonLibrary
from nanobot.channels.telegram.social.models import ConversationScope, ObservedMessage
from nanobot.channels.telegram.social.runtime import TelegramSocialRuntime
from nanobot.channels.telegram.social.store import SocialStore
from nanobot.config.schema import Config, TelegramSocialConfig, TelegramSocialLearningConfig
from nanobot.providers.base import LLMProvider, LLMResponse

A = ConversationScope("telegram", -1001, 42)
B = ConversationScope("telegram", -1002, 42)


@pytest.fixture
def setup(tmp_path):
    clock = [1000.]
    store = SocialStore(tmp_path, now=lambda: clock[0])
    store.learning.attach_libraries(ExpressionLibrary(store), JargonLibrary(store))
    store.bind(A, "1")
    store.bind(B, "1")
    store.learning.configure(TelegramSocialLearningConfig(min_messages=1))
    yield store, ExpressionLibrary(store), clock
    if not store.closed:
        store.close()


def add(store, mid=1, text="这下真是遥遥领先了", **changes):
    scope = changes.pop("scope", A)
    msg = ObservedMessage(scope, mid, mid, "human", "Member", store.now(), text, learning_text=text)
    store.observe(replace(msg, **changes), 100)


def candidate(mid=1, **changes):
    values = dict(message_id=mid, revision=1, sender_id="human", excerpt="这下真是遥遥领先了",
                  expression="这下真是遥遥领先了", situation="对方炫耀成绩却刚刚发生失误时顺势反讽",
                  tone="轻松反讽", intent_category="irony", scene_tags=["banter", "praise"],
                  suitable="双方熟悉且正在互相开玩笑", unsuitable="认真求助或遭遇挫折时不要反讽",
                  confidence="clear", reusable=True, concrete_scene=True, private_or_named=False,
                  abusive_or_mechanical=False, top_level=True)
    values.update(changes)
    return ExpressionCandidate.model_validate(values)


def commit(store, library, *items, scope=A):
    batch = store.learning.prepare(scope.session_key, "expression")
    assert batch
    store.learning.start(batch)
    ids = library.commit(batch, ExtractionResult(expressions=list(items)), library.generation(scope.session_key))
    return batch, ids


def test_available_evidence_and_empty_batch_move_only_expression_cursor(setup):
    store, library, clock = setup
    add(store)
    _, ids = commit(store, library, candidate())
    entry = library.get(A.session_key, ids[0])
    assert (entry["status"], entry["revision"], entry["occurrences"]) == ("available", 1, 1)
    evidence = entry["evidence"][0]
    assert (evidence["message_id"], evidence["revision"], evidence["sender_id"], evidence["source"]) == (1, 1, "human", "human_text")
    assert json.loads(entry["scene_tags"]) == ["banter", "praise"]
    assert store.scopes.state(A.session_key)["read_through"] == store.scopes.state(A.session_key)["seen_through"] == 0
    clock[0] += 301
    add(store, 2, "加油")
    batch, ids = commit(store, library)
    assert ids == []
    assert store.db.execute("SELECT result_count,status FROM learning_batches WHERE id=?", (batch,)).fetchone()[:] == (0, "succeeded")
    assert store.db.execute("SELECT message_id FROM learning_cursors WHERE scope=? AND kind='expression'", (A.session_key,)).fetchone()[0] == 2
    assert not store.db.execute("SELECT 1 FROM wakes").fetchone()


@pytest.mark.parametrize("flags", [{"is_self": True}, {"is_bot": True}, {"is_forwarded": True}, {"learning_text": None}])
def test_nonhuman_forged_evidence_rolls_back_all_entries_and_cursor(setup, flags):
    store, library, _ = setup
    add(store)
    add(store, 2, **flags)
    batch = store.learning.prepare(A.session_key, "expression")
    store.learning.start(batch)
    with pytest.raises(ValueError, match="evidence"):
        library.commit(batch, ExtractionResult(expressions=[candidate(), candidate(2)]), 0)
    assert not library.list(A.session_key)
    assert store.db.execute("SELECT event_id FROM learning_cursors WHERE kind='expression' AND scope=?", (A.session_key,)).fetchone()[0] == 0


@pytest.mark.parametrize("changes", [{"revision": 2}, {"sender_id": "someone-else"}, {"excerpt": "from quote"}, {"expression": "from context"}])
def test_exact_evidence_validation(setup, changes):
    store, library, _ = setup
    add(store)
    with pytest.raises(ValueError, match="evidence"):
        commit(store, library, candidate(**changes))
    assert not library.list(A.session_key)


@pytest.mark.parametrize("text,changes", [
    ("你真棒", {}), ("本大爷遥遥领先", {}), ("Token 花完了", {}), ("白饭真好吃", {}),
    ("算力遥遥领先", {}), ("小张明天去医院", {"private_or_named": True}),
    ("这下真是遥遥领先了", {"concrete_scene": False}),
    ("这下真是遥遥领先了", {"abusive_or_mechanical": True}),
    ("> 这下真是遥遥领先了", {}), ('他说“这下真是遥遥领先了”', {}),
])
def test_pollution_and_noncontextual_templates_are_empty_success(setup, text, changes):
    store, library, _ = setup
    add(store, text=text)
    excerpt = text if "遥遥领先了" not in text else "这下真是遥遥领先了"
    _, ids = commit(store, library, candidate(excerpt=excerpt, expression=excerpt, **changes))
    assert ids == [] and not library.list(A.session_key)


def test_uncertainty_and_synonyms_merge_to_stable_id_and_occurrence(setup):
    store, library, clock = setup
    add(store)
    _, ids = commit(store, library, candidate(confidence="uncertain"))
    entry_id = ids[0]
    assert library.get(A.session_key, entry_id)["status"] == "candidate"
    clock[0] += 301
    add(store, 2, "你这水平已经登峰造极了")
    _, ids = commit(store, library, candidate(2, excerpt="你这水平已经登峰造极了", expression="你这水平已经登峰造极了", merge_id=entry_id))
    assert ids == [entry_id] and len(library.list(A.session_key)) == 1
    assert library.get(A.session_key, entry_id)["occurrences"] == 2
    assert library.get(A.session_key, entry_id)["status"] == "available"
    clock[0] += 301
    add(store, 3, "这下真是遥遥领先了啊")
    _, ids = commit(store, library, candidate(3, excerpt="这下真是遥遥领先了啊", expression="这下真是遥遥领先了啊"))
    assert ids == [entry_id] and library.get(A.session_key, entry_id)["occurrences"] == 3


@pytest.mark.parametrize("action", ["disable", "modify"])
def test_manual_revision_old_source_similar_wording_and_semantic_merge_never_revive(setup, tmp_path, action):
    store, library, clock = setup
    add(store)
    _, ids = commit(store, library, candidate())
    entry_id = ids[0]
    edit = {"expression": "领先的方式有点特别"} if action == "modify" else {}
    revised = library.change(A.session_key, ExpressionEdit(action=action, entry_id=entry_id, expected_revision=1, **edit))
    assert revised["revision"] == 2
    with pytest.raises(ValueError, match="expected_revision"):
        library.change(A.session_key, ExpressionEdit(action="disable", entry_id=entry_id, expected_revision=1))
    # Restart keeps both wording guard and source suppression.
    store.close()
    reopened = SocialStore(tmp_path, now=lambda: clock[0])
    reopened.learning.attach_libraries(ExpressionLibrary(reopened), JargonLibrary(reopened))
    try:
        reopened.learning.configure(TelegramSocialLearningConfig(min_messages=1))
        library = ExpressionLibrary(reopened)
        for mid, wording, merge in [(1, "这下真是遥遥领先了", None), (2, "这下真是遥遥领先了啊", None), (3, "你这水平已经登峰造极了", entry_id)]:
            clock[0] += 301
            add(reopened, mid, wording, is_edit=mid == 1, update_id=100 + mid)
            _, results = commit(reopened, library, candidate(mid, revision=2 if mid == 1 else 1, excerpt=wording, expression=wording, merge_id=merge))
            assert results == []
        assert len(library.list(A.session_key)) == 1
        assert library.get(A.session_key, entry_id)["expression"] == revised["expression"]
        if action == "disable":
            assert library.get(A.session_key, entry_id)["status"] == "disabled"
    finally:
        reopened.close()


def test_manual_edit_during_request_rejects_entire_stale_result(setup):
    store, library, clock = setup
    add(store)
    _, ids = commit(store, library, candidate())
    clock[0] += 301
    add(store, 2)
    batch = store.learning.prepare(A.session_key, "expression")
    store.learning.start(batch)
    generation = library.generation(A.session_key)
    library.change(A.session_key, ExpressionEdit(action="disable", entry_id=ids[0], expected_revision=1))
    with pytest.raises(ValueError, match="changed during"):
        library.commit(batch, ExtractionResult(expressions=[candidate(2)]), generation)
    assert library.get(A.session_key, ids[0])["revision"] == 2


def test_available_source_edit_expires_entry_and_foreign_scope_cannot_merge(setup):
    store, library, _ = setup
    add(store)
    _, ids = commit(store, library, candidate())
    add(store, update_id=100, is_edit=True, text="改过了")
    assert library.get(A.session_key, ids[0])["status"] == "expired"
    add(store, scope=B, update_id=200)
    with pytest.raises(ValueError, match="scope"):
        commit(store, library, candidate(merge_id=ids[0]), scope=B)


@pytest.fixture
def runtime(tmp_path):
    (tmp_path / "roles").mkdir()
    (tmp_path / "roles" / "guest.md").write_text("Guest")
    clock = [1000.]
    config = TelegramSocialConfig.model_validate({"enabled": True,
        "scopes": [{"chatId": A.chat_id, "threadId": A.thread_id, "roleId": "guest"}]})
    instance = TelegramSocialRuntime(config, tmp_path, MessageBus(), now=lambda: clock[0])
    provider = AsyncMock(spec=LLMProvider)
    provider.chat.return_value = LLMResponse(content='{"expressions": []}')
    instance.expressions.set_provider(provider, "test-model")
    yield instance, provider, clock
    instance.close()


async def test_worker_default_window_priority_interval_concurrency_and_silence(runtime):
    instance, provider, clock = runtime
    for mid in range(1, 10):
        add(instance.store, mid)
    instance.store.learning.collect_due([A.session_key])
    assert instance.store.learning.prepare(A.session_key, "expression") is None
    add(instance.store, 10)
    instance.store.learning.collect_due([A.session_key])
    worker = instance.learning
    worker.main_busy = lambda: True
    worker.start_due([A.session_key])
    assert worker.task is None
    worker.main_busy = lambda: False
    worker.start_due([A.session_key])
    task = worker.task
    assert task is not None
    worker.start_due([A.session_key])
    assert worker.task is task
    await task
    assert provider.chat.await_count == 1
    assert instance.bus.inbound_size == 0 and instance.bus.outbound_size == 0
    assert not instance.store.db.execute("SELECT 1 FROM wakes").fetchone()
    for mid in range(11, 21):
        add(instance.store, mid)
    worker.start_due([A.session_key])
    assert worker.task is task
    clock[0] += 301
    instance.store.learning.collect_due([A.session_key])
    worker.start_due([A.session_key])
    await worker.task
    assert provider.chat.await_count == 2
    assert instance.store.scopes.state(A.session_key)["seen_through"] == instance.store.scopes.state(A.session_key)["read_through"] == 0


async def test_failure_retry_budget_and_interval_survive_restart(runtime):
    instance, provider, clock = runtime
    for mid in range(1, 11):
        add(instance.store, mid)
    instance.store.learning.collect_due([A.session_key])
    provider.chat.side_effect = RuntimeError("secret-provider-key-must-not-persist")
    worker = instance.learning
    worker.start_due([A.session_key])
    await worker.task
    assert provider.chat.await_count == 1
    instance.close()
    restored = TelegramSocialRuntime(instance.config, instance.root, MessageBus(), now=lambda: clock[0])
    restored.expressions.set_provider(provider, "test-model")
    restored.store.learning.recover()
    worker = restored.learning
    try:
        worker.start_due([A.session_key])
        assert worker.task is None  # Durable five-minute deadline, not an in-memory timer.
        clock[0] += 301
        worker.start_due([A.session_key])
        await worker.task
        clock[0] += 301
        worker.start_due([A.session_key])
        assert provider.chat.await_count == 2
        row = restored.store.db.execute("SELECT * FROM learning_batches WHERE kind='expression'").fetchone()
        assert row["status"] == "failed" and row["reason"] == "provider_exception" and row["attempts"] == 2
        assert "secret" not in row["reason"]
        assert restored.store.db.execute("SELECT event_id FROM learning_cursors WHERE kind='expression'").fetchone()[0] == 0
    finally:
        restored.close()


async def test_worker_yields_network_wait_and_scope_revocation_cancels_commit(runtime):
    instance, provider, _ = runtime
    for mid in range(1, 11):
        add(instance.store, mid)
    entered, release = asyncio.Event(), asyncio.Event()

    async def wait(**kwargs):
        entered.set()
        await release.wait()
        return LLMResponse(content=ExtractionResult(expressions=[candidate()]).model_dump_json())

    provider.chat.side_effect = wait
    instance.store.learning.collect_due([A.session_key])
    instance.learning.start_due([A.session_key])
    await entered.wait()
    # run_due completes while the background provider is waiting.
    await asyncio.wait_for(instance.run_due(), .5)
    instance.config.enabled = False
    release.set()
    await instance.learning.task
    assert not instance.expressions.library.list(A.session_key)


def test_provider_inherits_selected_preset_without_main_fallbacks(monkeypatch):
    seen = []
    monkeypatch.setattr("nanobot.channels.telegram.social.expressions.make_provider", lambda config, preset: seen.append((config, preset)) or AsyncMock(spec=LLMProvider))
    config = Config()
    config.agents.defaults.model = "selected-model"
    config.agents.defaults.provider = "openai"
    _, model = make_expression_provider(config, TelegramSocialLearningConfig().expression)
    assert model == "selected-model"
    assert seen[0][1].provider == "openai" and seen[0][1].max_tokens == 2048
    assert seen[0][0].agents.defaults.fallback_models == []


def test_recorded_actual_extraction_matches_reviewed_history_and_excludes_self(setup):
    from pathlib import Path

    store, library, _ = setup
    fixtures = Path(__file__).parent / "fixtures"
    corpus = json.loads((fixtures / "social_v5_baseline.json").read_text())
    actual = json.loads((fixtures / "social_v5b_actual_extraction.json").read_text())
    scopes = {"main": A, "topic": B}
    known = {}
    update = 0
    for segment in corpus["extraction"]:
        for msg in segment["messages"]:
            update += 1
            human = msg["learnable"] and msg["source"] == "reviewed_human_text"
            scope = scopes[segment["scope"]]
            known[(scope.session_key, msg["message_id"])] = msg
            add(store, msg["message_id"], msg["text"], scope=scope, update_id=update,
                sender_id=msg["sender"], is_self=msg["source"] == "self",
                learning_text=msg["text"] if human else None)
    for result in actual["results"]:
        scope = scopes[result["scope"]]
        items = []
        for entry in result["entries"]:
            evidence = entry["evidence"][0]
            original = known[(scope.session_key, evidence["message_id"])]
            assert original["learnable"] and original["source"] == "reviewed_human_text"
            items.append(candidate(evidence["message_id"], **{field: entry[field] for field in ("situation", "expression", "tone", "intent_category", "suitable", "unsuitable")},
                revision=evidence["revision"], sender_id=evidence["sender_id"], excerpt=evidence["excerpt"],
                scene_tags=json.loads(entry["scene_tags"]), confidence="clear" if entry["status"] == "available" else "uncertain"))
        batch, ids = commit(store, library, *items, scope=scope)
        sources = json.loads(store.db.execute("SELECT sources FROM learning_batches WHERE id=?", (batch,)).fetchone()[0])
        assert all(known[(scope.session_key, source["message_id"])]["learnable"] for source in sources)
        assert len(ids) == len(result["entries"])
    assert sum(len(library.list(s.session_key)) for s in scopes.values()) == 3
    assert actual["checks"] == {"evidence_lookup": 3, "self_exclusion": True, "disable_no_revival": 3, "similar_no_revival": 3}


def test_window_is_capped_at_eighty_even_with_larger_collection_queue(setup):
    store, _, _ = setup
    store.learning.configure(TelegramSocialLearningConfig(window_messages=200))
    for mid in range(1, 101):
        add(store, mid)
    batch = store.learning.prepare(A.session_key, "expression")
    row = store.db.execute("SELECT source_count,sources FROM learning_batches WHERE id=?", (batch,)).fetchone()
    assert row[0] == len(json.loads(row[1])) == 80


def test_duplicate_evidence_does_not_inflate_occurrence_and_enable_is_explicit(setup):
    store, library, _ = setup
    add(store)
    _, ids = commit(store, library, candidate(), candidate())
    assert len(ids) == 1
    entry_id = ids[0]
    assert library.get(A.session_key, entry_id)["occurrences"] == 1
    library.change(A.session_key, ExpressionEdit(action="disable", entry_id=entry_id, expected_revision=1))
    enabled = library.change(A.session_key, ExpressionEdit(action="enable", entry_id=entry_id, expected_revision=2))
    assert (enabled["status"], enabled["revision"], enabled["occurrences"]) == ("available", 3, 1)
    assert library.payload(A.session_key)["protected_wording"]


async def test_zero_retry_setting_and_disabled_provider_do_not_call_main(runtime, monkeypatch):
    instance, provider, clock = runtime
    instance.config.learning.expression.max_retries = 0
    instance.store.learning.configure(instance.config.learning)
    for mid in range(1, 11):
        add(instance.store, mid)
    instance.store.learning.collect_due([A.session_key])
    provider.chat.return_value = LLMResponse(content="not JSON")
    instance.learning.start_due([A.session_key])
    await instance.learning.task
    clock[0] += 301
    instance.learning.start_due([A.session_key])
    assert provider.chat.await_count == 1
    args = provider.chat.call_args.kwargs
    assert args["max_tokens"] == 2048 and args["tools"] is None and args["tool_choice"] == "none"
    monkeypatch.setattr("nanobot.channels.telegram.social.expressions.make_expression_provider", lambda *_: (_ for _ in ()).throw(ValueError("unconfigured")))
    instance.expressions.configure_provider(Config())
    assert instance.expressions.provider is None
    instance.learning.start_due([A.session_key])
    assert provider.chat.await_count == 1
