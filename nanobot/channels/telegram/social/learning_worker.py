"""Shared learning batch selection, checks and failure bookkeeping."""
from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import TYPE_CHECKING

from loguru import logger

from nanobot.channels.telegram.social.bindings import ScopeBindings
from nanobot.channels.telegram.social.db.connection import transaction
from nanobot.channels.telegram.social.expressions import redacted_error_detail
from nanobot.channels.telegram.social.models import ConversationScope
from nanobot.channels.telegram.social.store import SocialStore

if TYPE_CHECKING:
    from nanobot.channels.telegram.social.expressions import ExpressionLearner
    from nanobot.channels.telegram.social.jargon_learning import JargonLearner


class LearningProviderError(Exception):
    """The provider returned an error response (billing, rate limit, outage). The attempt is refunded."""


class LearningWorker:
    """One in-flight learning batch at a time: selection, common checks, failure bookkeeping and logs."""

    def __init__(self, store: SocialStore, bindings: ScopeBindings,
                 processors: tuple[ExpressionLearner, JargonLearner], judge_busy: Callable[[], bool]) -> None:
        self.store = store
        self.bindings = bindings
        self.processors = processors
        self.judge_busy = judge_busy
        self.main_busy: Callable[[], bool] = lambda: False
        self.task: asyncio.Task[None] | None = None

    def busy(self) -> bool:
        return bool(self.main_busy() or self.judge_busy() or self.store.wakes.any_active())

    def cancel(self) -> None:
        if self.task is not None and not self.task.done():
            self.task.cancel()

    def start_due(self, scopes: list[str]) -> None:
        if (not scopes or not self.store.learning.config.enabled
                or self.task is not None and not self.task.done() or self.busy()):
            return
        # Durable global start interval also covers failed/cancelled requests and restarts.
        last = self.store.learning.last_batch_due()[0]
        if last is not None and self.store.now() < last:
            return
        processors = sorted(self.processors, key=lambda processor: self.store.learning.earliest_batch(
            processor.kind, 1 + getattr(self.store.learning.config, processor.kind).max_retries)[0])
        for processor in processors:
            if processor.provider is None:
                continue
            settings = getattr(self.store.learning.config, processor.kind)
            for row in self.store.learning.pending_batches(processor.kind):
                if row["scope"] in scopes and row["attempts"] < 1 + settings.max_retries:
                    self.task = asyncio.create_task(self._run(processor, str(row["id"])), name=f"social-{processor.kind}-learning")
                    return

    async def _run(self, processor: ExpressionLearner | JargonLearner, batch: str) -> None:
        error = ""
        error_detail: str | None = None
        kind = processor.kind
        try:
            row = self.store.learning.batch(batch)
            if row is None or processor.provider is None:
                return
            scope = ConversationScope.from_session_key(str(row["scope"]))
            settings = getattr(self.store.learning.config, kind)
            if (scope is None or (binding := self.bindings.binding(scope)) is None or binding.paused
                    or self.busy() or row["attempts"] >= 1 + settings.max_retries):
                return
            await processor.process(row, scope)
        except asyncio.CancelledError:
            error = "interrupted"
            raise
        except LearningProviderError as exc:
            with transaction(self.store.db):
                self.store.learning.refund_attempt(batch)
            logger.warning("Social learning batch={} kind={} provider error={}; attempt refunded", batch, kind, exc)
        except TimeoutError as exc:
            error = "timeout"
            error_detail = redacted_error_detail(exc)
            logger.warning("Social learning batch={} kind={} error={}: {}", batch, kind, error, error_detail)
        except ValueError as exc:
            error = "invalid_or_changed_input"
            error_detail = redacted_error_detail(exc)
            logger.opt(exception=True).warning("Social learning batch={} kind={} error={}: {}", batch, kind, error, error_detail)
        except Exception as exc:
            error = "provider_exception"
            error_detail = redacted_error_detail(exc)
            logger.opt(exception=True).warning("Social learning batch={} kind={} error={}: {}", batch, kind, error, error_detail)
        finally:
            if not self.store.closed:
                with transaction(self.store.db):
                    current = self.store.learning.batch_status(batch)
                    if current and current["status"] == "running" and error:
                        self.store.learning.fail_batch(error, error_detail, self.store.now(), batch)
                        sources = json.loads(current["sources"])
                        self.store.learning.count(str(current["scope"]), f"{kind}:failed", 1, sources[0]["message_id"], sources[-1]["message_id"])
                    if current and current["status"] == "succeeded":
                        self.store.learning.clear_batch_error(batch)
