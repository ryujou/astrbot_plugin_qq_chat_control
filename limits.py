import asyncio
import contextvars
import functools
import json
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from astrbot.core.pipeline.scheduler import PipelineScheduler
from astrbot.core.provider.manager import ProviderManager


current_message = contextvars.ContextVar('qq_llm_usage_message', default=None)


class ChatLimitExceeded(PermissionError):
    def __init__(self, reset_at):
        self.reset_at = reset_at
        super().__init__('QQ member chat limit reached')


@dataclass
class MessageUsage:
    event: object
    ticket: object = None
    counted: bool = False
    active: int = 0
    notified: bool = False


class LLMLimits:
    def __init__(self, owner, path):
        self.owner = owner
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.hours = float(owner.config.get('member_limit_hours', 5))
        self.maximum = int(owner.config.get('member_limit_count', 5))
        if self.hours <= 0 or self.maximum < 1:
            raise ValueError('成员额度周期须大于零，每周期次数须至少为1')
        self.records = json.loads(self.path.read_text()) if self.path.exists() else {}
        for record in self.records.values():
            record['pending'] = 0
        self.providers = []
        self.tasks = set()
        self.execute = None
        self.load_provider = None

    def save(self):
        self.path.write_text(json.dumps({key: {'start': record['start'], 'used': record['used']}
            for key, record in self.records.items()}, ensure_ascii=False), encoding='utf-8')

    def reserve(self, key, now=None):
        now = time.time() if now is None else now
        record = self.records.get(key)
        if record is None or now >= record['start'] + self.hours * 3600:
            record = {'start': now, 'used': 0, 'pending': 0}
            self.records[key] = record
            self.save()
        if record['used'] + record['pending'] >= self.maximum:
            raise ChatLimitExceeded(record['start'] + self.hours * 3600)
        record['pending'] += 1
        return record

    def commit(self, usage):
        if usage.counted or usage.ticket is None:
            return
        usage.ticket['pending'] -= 1
        usage.ticket['used'] += 1
        usage.ticket = None
        usage.counted = True
        self.save()

    def release(self, usage):
        if usage.ticket is not None:
            usage.ticket['pending'] -= 1
            usage.ticket = None

    def is_limited(self, event):
        return str(event.get_sender_id()) not in self.owner.config.get('allowed_qq_ids', [])

    def admit(self, usage):
        event = usage.event
        sender = str(event.get_sender_id())
        group = str(event.get_group_id() or '')
        automatic = event.get_extra('qq_chat_automatic') is True
        if sender == str(event.get_self_id()):
            raise PermissionError('QQ bot messages cannot call LLMs')
        if group and group not in self.owner.config.get('allowed_group_ids', []):
            raise PermissionError('QQ group is not allowed to call LLMs')
        if self.owner.rule(event).get('auto_only') and not automatic:
            raise PermissionError('QQ group only permits automatic replies')
        if not self.is_limited(event):
            return
        if not group or not self.owner.rule(event).get('member_limit_enabled', True):
            raise PermissionError('QQ member chat is not enabled')
        if not automatic and not usage.counted and usage.ticket is None:
            key = ':'.join((event.get_platform_id(), group, sender))
            usage.ticket = self.reserve(key)

    async def begin(self, usage):
        try:
            self.admit(usage)
        except ChatLimitExceeded as error:
            event = usage.event
            if event.is_at_or_wake_command and not event.get_extra('qq_chat_automatic') and not usage.notified:
                usage.notified = True
                reset = datetime.fromtimestamp(error.reset_at, ZoneInfo('Asia/Shanghai'))
                await event.send(event.plain_result(
                    f'群聊额度已用完，添加额度或升级套餐，或等待使用量于{reset:%H:%M}重置').use_t2i(False))
            raise
        usage.active += 1
        task = asyncio.current_task()
        if task not in self.tasks:
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)

    def end(self, usage):
        usage.active -= 1
        if usage.active == 0:
            self.release(usage)

    def successful(self, result):
        return (not getattr(result, 'is_chunk', False)
                and bool((getattr(result, 'completion_text', '') or '').strip())
                and not getattr(result, 'tools_call_args', None)
                and not getattr(result, 'tools_call_name', None))

    def wrap_provider(self, provider):
        if any(item[0] is provider for item in self.providers):
            return
        original_chat = provider.text_chat
        original_stream = provider.text_chat_stream

        @functools.wraps(original_chat)
        async def chat(*args, **kwargs):
            usage = current_message.get()
            if usage is None:
                return await original_chat(*args, **kwargs)
            await self.begin(usage)
            tracked, started, response, completed = usage.active == 1, time.monotonic(), None, False
            context = getattr(provider, 'chat_only_context', None)
            token = context.set(self.is_limited(usage.event)) if context else None
            try:
                if usage.active == 1:
                    kwargs = self.owner.profiles.inject(usage.event, kwargs)
                    kwargs = self.owner.learning.slang.inject(usage.event, kwargs)
                    kwargs = self.owner.learning.policy.inject(usage.event, kwargs)
                result = await original_chat(*args, **kwargs)
                response, completed = result, not getattr(result, 'is_chunk', False)
                if self.successful(result):
                    self.commit(usage)
                    self.owner.profiles.replied(usage.event, result)
                    self.owner.learning.generated(usage.event)
                    if usage.active == 1:
                        await self.owner.learning.policy.deliver(usage.event)
                return result
            finally:
                if tracked:
                    self.owner.learning.record_reply_call(usage.event, provider, started, response, completed)
                if context:
                    context.reset(token)
                self.end(usage)

        @functools.wraps(original_stream)
        async def stream(*args, **kwargs):
            usage = current_message.get()
            if usage is None:
                async for result in original_stream(*args, **kwargs):
                    yield result
                return
            await self.begin(usage)
            tracked, started, response, completed = usage.active == 1, time.monotonic(), None, False
            context = getattr(provider, 'chat_only_context', None)
            if usage.active == 1:
                kwargs = self.owner.profiles.inject(usage.event, kwargs)
                kwargs = self.owner.learning.slang.inject(usage.event, kwargs)
                kwargs = self.owner.learning.policy.inject(usage.event, kwargs)
            generator = original_stream(*args, **kwargs)
            try:
                while True:
                    token = context.set(self.is_limited(usage.event)) if context else None
                    try:
                        result = await anext(generator)
                    except StopAsyncIteration:
                        break
                    finally:
                        if context:
                            context.reset(token)
                    if not getattr(result, 'is_chunk', False):
                        response, completed = result, True
                    if self.successful(result):
                        self.commit(usage)
                        self.owner.profiles.replied(usage.event, result)
                        self.owner.learning.generated(usage.event)
                        if usage.active == 1:
                            await self.owner.learning.policy.deliver(usage.event)
                    yield result
            finally:
                try:
                    await generator.aclose()
                finally:
                    if tracked:
                        self.owner.learning.record_reply_call(usage.event, provider, started, response, completed)
                    self.end(usage)

        provider.text_chat = chat
        provider.text_chat_stream = stream
        self.providers.append((provider, original_chat, original_stream, chat, stream))

    def wrap_providers(self):
        for provider in self.owner.context.get_all_providers():
            self.wrap_provider(provider)

    def install(self):
        self.execute = PipelineScheduler.execute
        self.load_provider = ProviderManager.load_provider

        @functools.wraps(self.execute)
        async def execute(scheduler, event):
            usage = None
            if event.get_platform_name() == 'aiocqhttp':
                self.wrap_providers()
                usage = event.get_extra('qq_llm_usage')
                if usage is None:
                    usage = MessageUsage(event)
                    event.set_extra('qq_llm_usage', usage)
            token = current_message.set(usage)
            error = None
            try:
                return await self.execute(scheduler, event)
            except BaseException as failure:
                error = failure
                if event.get_extra('qq_auto_opportunity') is not None and not event.get_extra('qq_auto_cancelled'):
                    event.set_extra('qq_auto_retryable', True)
                raise
            finally:
                try:
                    if usage is not None:
                        self.owner.learning.finish(event)
                        self.owner.learning.policy.interaction.finish_foreground(event)
                        progress = event.get_extra('qq_work_progress')
                        if progress:
                            await progress.finish(error)
                finally:
                    current_message.reset(token)
                    if usage is not None and usage.active == 0:
                        self.release(usage)

        @functools.wraps(self.load_provider)
        async def load_provider(manager, config):
            result = await self.load_provider(manager, config)
            self.wrap_providers()
            return result

        self.execute_wrapper = execute
        self.load_wrapper = load_provider
        PipelineScheduler.execute = execute
        ProviderManager.load_provider = load_provider
        self.wrap_providers()

    def close(self):
        if PipelineScheduler.execute is self.execute_wrapper:
            PipelineScheduler.execute = self.execute
        if ProviderManager.load_provider is self.load_wrapper:
            ProviderManager.load_provider = self.load_provider
        for provider, original_chat, original_stream, chat, stream in self.providers:
            if provider.text_chat is chat:
                provider.text_chat = original_chat
            if provider.text_chat_stream is stream:
                provider.text_chat_stream = original_stream
