import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from astrbot.api import sp


def belongs(origin, platform, group):
    prefix = platform + ':GroupMessage:'
    return origin == prefix + group or (origin.startswith(prefix) and origin.endswith('_' + group))


async def clear_group(context, platform, group, provider):
    manager = context.conversation_manager
    conversations = await manager.get_conversations(platform_id=platform)
    origins = {c.user_id for c in conversations if belongs(c.user_id, platform, group)}
    providers = [p for p in context.get_all_providers() if hasattr(p, 'delete_chat_history')]
    for adapter in providers:
        if hasattr(adapter, 'chat_origins'):
            origins.update(origin for origin in adapter.chat_origins() if belongs(origin, platform, group))
    for adapter in providers:
        await adapter.delete_chat_history(origins)
    for origin in origins:
        await manager.delete_conversations_by_user_id(origin)
    builtin = context.get_registered_star('astrbot')
    cache = getattr(builtin.star_cls, 'group_chat_context', None) if builtin else None
    if cache:
        for origin in list(cache.raw_records):
            if belongs(origin, platform, group):
                await cache.remove_session(SimpleNamespace(unified_msg_origin=origin))
    with sqlite3.connect('data/data_v4.db') as db:
        db.execute('DELETE FROM platform_message_history WHERE platform_id=? AND user_id=?', (platform, group))


class PersonaBindings:
    def __init__(self, context, path):
        self.context = context
        self.path = path
        self.applied = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        self.lock = asyncio.Lock()
        self.clear_learning = None

    async def reconcile(self, rules, provider):
        desired = {platform + '|' + group: rule.get('persona_id', '') for (platform, group), rule in rules.items()}
        async with self.lock:
            for key in self.applied.keys() | desired.keys():
                if self.applied.get(key, '') != desired.get(key, ''):
                    platform, group = key.split('|', 1)
                    await clear_group(self.context, platform, group, provider)
                    if self.clear_learning:
                        self.clear_learning(platform, group)
            if desired != self.applied or not self.path.exists():
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(json.dumps(desired, ensure_ascii=False, indent=2), encoding='utf-8')
                self.applied = desired

    async def bind(self, event, persona_id):
        if not persona_id:
            return
        manager = self.context.conversation_manager
        origin = event.unified_msg_origin
        service = await sp.session_get(origin, 'session_service_config', {}) or {}
        if service.get('persona_id'):
            service = dict(service)
            service.pop('persona_id')
            await sp.session_put(origin, 'session_service_config', service)
        cid = await manager.get_curr_conversation_id(origin)
        if not cid:
            await manager.new_conversation(origin, event.get_platform_id(), persona_id=persona_id)
        else:
            conversation = await manager.get_conversation(origin, cid)
            if conversation.persona_id != persona_id:
                await manager.update_conversation(origin, cid, persona_id=persona_id)
