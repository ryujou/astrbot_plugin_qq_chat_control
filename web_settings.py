import json
import math
from copy import deepcopy
from pathlib import Path

from quart import request

from .memory import policy


GROUP_SECTIONS = {
    '模型与人设': ['provider_id', 'persona_id', 'feedback_judge_provider_id'],
    '回复与频率': ['auto_only', 'member_limit_enabled', 'auto_reply', 'message_count', 'reply_probability', 'idle_reply', 'idle_reply_minutes'],
    '学习与表达': ['feedback_observe', 'feedback_adapt_mode', 'candidate_replies', 'banter_intensity', 'member_profiles', 'slang_learning', 'sticker_learning', 'feedback_bot_ids'],
    'QQ回应表情': ['reaction_observe', 'reaction_auto', 'reaction_interval_seconds', 'reaction_max_per_10min'],
    '群背景与资料': ['group_background', 'group_file_learning', 'group_album_learning', 'knowledge_document_mb', 'knowledge_image_mb'],
    '短期记忆': ['memory_override', 'history_turns', 'idle_hours', 'session_hours', 'group_context_minutes', 'group_context_messages'],
    '工作过程反馈': ['work_progress_mode', 'work_progress_interval_seconds'],
}
GLOBAL_SECTIONS = {
    '权限与额度': ['allowed_qq_ids', 'allowed_group_ids', 'management_group_id', 'member_limit_hours', 'member_limit_count'],
    '默认模型': ['default_provider_id', 'codex_provider_id', 'learning_embedding_provider_id', 'feedback_recheck_provider_id'],
    '通用短期记忆': ['history_turns', 'idle_hours', 'session_hours', 'group_context_minutes', 'group_context_messages'],
    '运行提示': ['quiet_group_errors'],
}
LIMITS = {
    'message_count': (1, None), 'reply_probability': (0, 100), 'idle_reply_minutes': (1, None),
    'work_progress_interval_seconds': (10, None), 'reaction_interval_seconds': (120, None),
    'reaction_max_per_10min': (1, 2), 'banter_intensity': (0, 100),
    'knowledge_document_mb': (1, None), 'knowledge_image_mb': (1, None),
    'member_limit_count': (1, None), 'history_turns': (1, None), 'group_context_messages': (1, None),
}
POSITIVE = {'member_limit_hours', 'idle_hours', 'session_hours', 'group_context_minutes'}
MODEL_KEYS = {'provider_id', 'feedback_judge_provider_id', 'default_provider_id', 'codex_provider_id', 'feedback_recheck_provider_id'}


class WebSettings:
    def __init__(self, owner):
        self.owner = owner
        self.schema = json.loads(Path(__file__).with_name('_conf_schema.json').read_text(encoding='utf-8'))

    def current(self):
        return json.loads(Path(self.owner.config.config_path).read_text(encoding='utf-8-sig'))

    def select(self, config, area, scope):
        if area == 'global':
            return config, {k: v for k, v in self.schema.items() if k != 'group_rules'}, GLOBAL_SECTIONS
        if area != 'group':
            raise ValueError('请选择本群设置或通用设置')
        rule = next((r for r in config.get('group_rules', [])
            if (str(r['platform_id']), str(r['group_id'])) == scope), None)
        if rule is None:
            raise ValueError('该群规则已不存在，请刷新群列表')
        fields = self.schema['group_rules']['templates']['group']['items']
        return rule, {k: v for k, v in fields.items() if k not in {'platform_id', 'group_id'}}, GROUP_SECTIONS

    def choices(self):
        models = [dict(value=p.provider_config['id'], label=p.provider_config['id'] + ' · ' + p.get_model())
                  for p in self.owner.context.get_all_providers()]
        embeddings = [dict(value=p.provider_config['id'], label=p.provider_config['id'])
                      for p in self.owner.context.get_all_embedding_providers()]
        personas = [dict(value=p['name'], label=p['name']) for p in self.owner.context.persona_manager.personas_v3]
        return models, embeddings, personas

    async def get(self):
        area = request.args.get('area', 'group')
        scope = request.args.get('platform', ''), request.args.get('group_id', '')
        values, schema, sections = self.select(self.current(), area, scope)
        models, embeddings, personas = self.choices()
        fields = {}
        for key, meta in schema.items():
            item = dict(key=key, label=meta['description'], type=meta['type'], hint=meta.get('hint', ''))
            if key in MODEL_KEYS:
                item['choices'] = models
            elif key == 'learning_embedding_provider_id':
                item['choices'] = embeddings
            elif key == 'persona_id':
                item['choices'] = personas
            elif 'options' in meta:
                item['choices'] = [dict(value=x, label=x) for x in meta['options']]
            if key in MODEL_KEYS | {'learning_embedding_provider_id', 'persona_id'}:
                item['choices'] = [dict(value='', label='未指定（沿用原配置规则）'), *item['choices']]
            if key in LIMITS:
                item['min'], item['max'] = LIMITS[key]
            if key in POSITIVE:
                item['min'] = 0
            fields[key] = item
        return dict(area=area, values={k: deepcopy(values.get(k, v.get('default'))) for k, v in schema.items()},
            sections=[dict(title=title, fields=[fields[k] for k in keys if k in fields]) for title, keys in sections.items()],
            fixed_affinity='好感低于20时静默，每3小时恢复1点，到20恢复响应，继续恢复至50。这些数值为固定规则。')

    def validate_field(self, key, value, meta):
        kind = meta['type']
        valid = ((kind == 'bool' and type(value) is bool) or (kind == 'int' and type(value) is int)
            or (kind == 'float' and type(value) in (int, float) and math.isfinite(value))
            or (kind == 'string' and isinstance(value, str))
            or (kind == 'list' and isinstance(value, list) and all(isinstance(x, str) and x.strip() for x in value)))
        if not valid or 'options' in meta and value not in meta['options']:
            raise ValueError(meta['description'] + '：格式或选项无效')
        if key in LIMITS:
            low, high = LIMITS[key]
            if value < low or high is not None and value > high:
                boundary = f'须在{low}到{high}之间' if high is not None else f'至少为{low}'
                raise ValueError(meta['description'] + '：' + boundary)
        if key in POSITIVE and value <= 0:
            raise ValueError(meta['description'] + '：必须大于0')
        if key in MODEL_KEYS and value:
            self.owner.provider(value)
        if key == 'learning_embedding_provider_id' and value not in {'', *[p.provider_config['id'] for p in self.owner.context.get_all_embedding_providers()]}:
            raise ValueError('向量模型不存在或未启用')
        if key == 'persona_id' and value and self.owner.context.persona_manager.get_persona_v3_by_id(value) is None:
            raise ValueError('群人设不存在')

    async def save(self):
        body = await request.get_json()
        if not isinstance(body, dict) or not isinstance(body.get('changes'), dict) or not isinstance(body.get('original'), dict):
            raise ValueError('设置请求格式错误，请刷新页面后重试')
        area, scope = body.get('area'), (str(body.get('platform', '')), str(body.get('group_id', '')))
        config = self.current()
        values, schema, _ = self.select(config, area, scope)
        for key, value in body['changes'].items():
            if key not in schema:
                raise ValueError('不支持修改此配置项：' + key)
            if key not in body['original'] or body['original'][key] != values.get(key, schema[key].get('default')):
                raise ValueError(schema[key]['description'] + '已被其他页面修改，请重新读取设置后再保存')
            self.validate_field(key, value, schema[key])
        changes = {k: v for k, v in body['changes'].items() if v != values.get(k, schema[k].get('default'))}
        if not changes:
            return dict(saved=False, message='没有需要保存的修改')
        if 'persona_id' in changes and body.get('confirm_persona_change') is not True:
            raise ValueError('更改人设会永久清理本群聊天记录，请先确认此影响')
        values.update(changes)
        if not config.get('default_provider_id'):
            raise ValueError('请选择默认聊天模型')
        policy(config, {})
        for rule in config.get('group_rules', []):
            policy(config, rule)
            if rule.get('feedback_adapt_mode', '关闭') != '关闭' and not rule.get('feedback_observe'):
                raise ValueError('启用学习或对照策略时，须同时开启回复反馈观察')
        self.owner.config.save_config(config)
        success, error = await self.owner.context._star_manager.reload('astrbot_plugin_qq_chat_control')
        if not success:
            raise ValueError('配置已保存，但插件重载失败，请在插件管理中检查：' + str(error))
        return dict(saved=True, message='设置已保存并生效')
