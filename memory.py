import asyncio
import json
import sqlite3
import time
from astrbot.core.utils.active_event_registry import active_event_registry


DEFAULTS = dict(history_turns=10, idle_hours=2, session_hours=12,
                group_context_minutes=30, group_context_messages=30)


def policy(config, rule):
    values = {key: config.get(key, value) for key, value in DEFAULTS.items()}
    if rule.get('memory_override', False):
        values.update({key: rule.get(key, value) for key, value in values.items()})
    if any(float(value) <= 0 for value in values.values()):
        raise ValueError('短期记忆的时长、轮数和条数必须大于 0')
    return values


def recent_turns(history, limit):
    starts = [i for i, message in enumerate(history) if message.get('role') == 'user']
    if not limit:
        return []
    return history[starts[-limit]:] if len(starts) > limit else history


class ShortMemory:
    def __init__(self, context, path):
        self.context, self.path = context, path
        self.sessions = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        self.record_times = {}
        self.locks = {}
        self.owners = {}

    async def acquire(self, origin):
        task = asyncio.current_task()
        if self.owners.get(origin) is task:
            return
        lock = self.locks.setdefault(origin, asyncio.Lock())
        await lock.acquire()
        self.owners[origin] = task

        def release(_):
            self.owners.pop(origin, None)
            lock.release()
        task.add_done_callback(release)

    def cache(self):
        builtin = self.context.get_registered_star('astrbot')
        return getattr(builtin.star_cls, 'group_chat_context', None) if builtin else None

    async def clear_provider_history(self, origin):
        for provider in self.context.get_all_providers():
            if hasattr(provider, 'delete_chat_history'):
                await provider.delete_chat_history({origin})

    async def prune_archived(self, platforms):
        manager = self.context.conversation_manager
        removed = 0
        for platform in platforms:
            conversations = await manager.get_conversations(platform_id=platform)
            for conversation in conversations:
                origin = conversation.user_id
                if not origin.startswith((platform + ':GroupMessage:', platform + ':FriendMessage:')):
                    continue
                current = await manager.get_curr_conversation_id(origin)
                if not current or conversation.cid == current or active_event_registry._events.get(origin):
                    continue
                await manager.delete_conversation(origin, conversation.cid)
                self.sessions.pop(conversation.cid, None)
                removed += 1
        if removed:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.sessions, ensure_ascii=False), encoding='utf-8')
        return removed

    async def prepare(self, event, settings, now=None):
        now = time.time() if now is None else now
        origin = event.unified_msg_origin
        await self.acquire(origin)
        manager = self.context.conversation_manager
        cid = await manager.get_curr_conversation_id(origin)
        if not cid:
            return
        conversation = await manager.get_conversation(origin, cid)
        history = json.loads(conversation.history)
        previous = self.sessions.get(cid)
        state = previous or dict(start=getattr(conversation, 'created_at', now),
                                 last=getattr(conversation, 'updated_at', now))
        expired = (now - state['last'] >= float(settings['idle_hours']) * 3600
                   or now - state['start'] >= float(settings['session_hours']) * 3600)
        # Reserve one round for the incoming question and answer. Cut at user boundaries
        # so assistant tool calls and their results are never split apart.
        retained = [] if expired else recent_turns(history, int(settings['history_turns']) - 1)
        if previous is None or retained != history:
            await self.clear_provider_history(origin)
        if retained != history:
            await manager.update_conversation(origin, cid, history=retained, token_usage=0)
        if expired:
            state = dict(start=now, last=now)
            cache = self.cache()
            if cache:
                await cache.remove_session(event)
            self.record_times.pop(origin, None)
        state['last'] = now
        self.sessions[cid] = state
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.sessions, ensure_ascii=False), encoding='utf-8')

    async def prune_group(self, event, settings, now=None):
        now = time.time() if now is None else now
        cutoff = now - float(settings['group_context_minutes']) * 60
        limit = int(settings['group_context_messages'])
        cache = self.cache()
        origin = event.unified_msg_origin
        if cache:
            async with cache._get_lock(origin):
                records = cache.raw_records.get(origin)
                ids = cache._record_ids.get(origin)
                times = self.record_times.setdefault(origin, {})
                current = event.get_extra('_group_context_record_id')
                if current:
                    times[current] = now
                if records is not None and ids is not None:
                    pairs = [(key, text) for key, text in zip(ids, records)
                             if times.get(key, 0) > cutoff]
                    # The current message is the prompt, not one of the prior 30 messages.
                    pairs = pairs[-(limit + (1 if current in ids else 0)):]
                    records.clear(); records.extend(text for _, text in pairs)
                    ids.clear(); ids.extend(key for key, _ in pairs)
                    self.record_times[origin] = {key: times[key] for key, _ in pairs}
        with sqlite3.connect('data/data_v4.db') as db:
            args = (event.get_platform_id(), str(event.get_group_id()))
            db.execute("DELETE FROM platform_message_history WHERE platform_id=? AND user_id=? "
                       "AND (julianday(created_at)-2440587.5)*86400 <= ?", (*args, cutoff))
            db.execute('DELETE FROM platform_message_history WHERE platform_id=? AND user_id=? '
                       'AND id NOT IN (SELECT id FROM platform_message_history '
                       'WHERE platform_id=? AND user_id=? ORDER BY created_at DESC, id DESC LIMIT ?)',
                       (*args, *args, limit))
