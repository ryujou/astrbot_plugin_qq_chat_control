import base64
import asyncio
import json
import mimetypes
import time
from pathlib import Path

from quart import jsonify, request

from .evaluation import comparison
from .profiles import affinity_value, AFFINITY_RECOVERY_SECONDS, AFFINITY_SILENCE_THRESHOLD
from .web_settings import WebSettings

from astrbot.api import logger
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter import AiocqhttpAdapter


class LearningWeb:
    def __init__(self, owner):
        self.owner = owner
        self.settings = WebSettings(owner)

    def register(self):
        for name in ('bootstrap', 'overview', 'replies', 'reply', 'stickers', 'image', 'slang', 'members', 'background', 'choices',
                     'reactions', 'reaction_patterns', 'reaction_actions', 'settings', 'save_settings', 'presence', 'presence_control', 'avatar_image'):
            handler = self.settings.get if name == 'settings' else self.settings.save if name == 'save_settings' else getattr(self, name)

            async def view(handler=handler):
                try:
                    return jsonify(ok=True, data=await handler())
                except ValueError as error:
                    return jsonify(ok=False, message=str(error)), 400
                except FileNotFoundError:
                    return jsonify(ok=False, message='图片文件已不存在'), 404
                except Exception:
                    logger.exception('群聊学习看板请求失败')
                    return jsonify(ok=False, message='请求失败，请查看 AstrBot 日志'), 500

            self.owner.context.register_web_api('/astrbot_plugin_qq_chat_control/learning/' + name,
                                                view, ['POST'] if name in {'save_settings', 'presence_control'} else ['GET'], '群聊学习看板：' + name)

    async def presence(self):
        return self.owner.learning.presence.snapshot(self.scope()[:2])

    async def avatar_image(self):
        self.scope()
        row = self.owner.learning.presence.db.execute('SELECT path FROM presence_images WHERE id=?', (int(request.args['id']),)).fetchone()
        if row is None:
            raise ValueError('头像不存在')
        return dict(src='data:image/jpeg;base64,'+base64.b64encode(Path(row['path']).read_bytes()).decode())

    async def presence_control(self):
        body = await request.get_json()
        scope = str(body.get('platform', '')), str(body.get('group_id', ''))
        if scope not in self.owner.rules or body.get('action') not in {'pause', 'resume', 'collect', 'use'}:
            raise ValueError('无效的群或操作')
        presence = self.owner.learning.presence
        if body['action'] in {'collect', 'use'}:
            if body['action'] == 'use' and (body.get('confirm') is not True or type(body.get('image_id')) is not int or body['image_id'] < 1):
                raise ValueError('请确认修改账号头像并选择候选')
            presence.start_manual(scope, body['action'], body.get('image_id'))
            return dict(message='已开始补充候选' if body['action']=='collect' else '已开始更换头像，请查看动作结果')
        key = body.get('key')
        if key not in {s['key'] for s in presence.snapshot(scope)['states']}:
            raise ValueError('自主形象状态不存在')
        if presence.task and not presence.task.done():
            presence.task.cancel()
            await asyncio.gather(presence.task, return_exceptions=True)
        state = presence.state(key)
        state['paused'] = body['action'] == 'pause'
        state['reason'] = '已手动暂停' if state['paused'] else '已恢复，仍遵守冷却和开关'
        state.pop('pending', None)
        if not state['paused']:
            state.pop('current', None)
            if key.startswith('avatar:'):
                account = key.split(':', 1)[1]
                if account.isdigit():
                    (presence.folder / (account+'-current.jpg')).unlink(missing_ok=True)
        presence.save(key, state)
        return dict(message=state['reason'])

    def scope(self):
        key = request.args.get('platform', ''), request.args.get('group_id', '')
        if key not in self.owner.rules:
            raise ValueError('请选择已配置的群')
        return (*key, self.owner.rules[key].get('persona_id', ''))

    def page(self):
        page = int(request.args.get('page', '1'))
        if page < 1:
            raise ValueError('页码必须大于0')
        return page, 20, (page - 1) * 20

    def reaction_args(self):
        days = int(request.args.get('days', '7'))
        if days not in (7, 30):
            raise ValueError('统计范围须为7天或30天')
        return self.scope()[:2], days, self.page()[0]

    async def reactions(self):
        scope, days, page = self.reaction_args()
        return self.owner.learning.reactions.observations(scope, days, request.args.get('emoji', ''), request.args.get('category', ''), page)

    async def reaction_patterns(self):
        return self.owner.learning.reactions.pattern_snapshot(self.scope()[:2], request.args.get('emoji', ''), request.args.get('category', ''))

    async def reaction_actions(self):
        scope, days, page = self.reaction_args()
        return self.owner.learning.reactions.action_snapshot(scope, days, page, request.args.get('emoji', ''), request.args.get('category', ''))

    @property
    def db(self):
        return self.owner.learning.open()

    def members_for(self, scope):
        prefix = ':'.join(scope[:2]) + ':'
        items = []
        for key, value in self.owner.profiles.records.items():
            if not key.startswith(prefix):
                continue
            sender = key[len(prefix):]
            silenced = self.owner.profiles.blocked(scope[:2], sender)
            until = (value['affinity_recovery_at'] + (AFFINITY_SILENCE_THRESHOLD - value['affinity'])
                     * AFFINITY_RECOVERY_SECONDS) if silenced else None
            items.append(dict(user_id=sender, name=value['name'], profile=value['profile'],
                affinity=affinity_value(value), silenced=silenced, silence_until=until,
                last_update=value['last_update'], last_seen=value['last_seen'], pending_samples=len(value['samples'])))
        return items

    def terms_for(self, scope):
        items = []
        for row in self.db.execute('SELECT term,senses,updated FROM slang_terms WHERE platform=? AND group_id=? '
                                   'AND persona=? ORDER BY updated DESC', scope):
            senses = json.loads(row['senses'])
            for sense in senses:
                sense['confirmed'] = (sense['rounds'] >= 2 and len(sense['evidence']) >= 3
                    and len({e['sender'] for e in sense['evidence']}) >= 2 and sense['confidence'] >= .8)
            items.append(dict(term=row['term'], senses=senses, updated=row['updated']))
        return items

    async def bootstrap(self):
        live, accounts, connection_errors = {}, [], []
        for inst in self.owner.context.platform_manager.platform_insts:
            if not isinstance(inst, AiocqhttpAdapter):
                continue
            platform = inst.meta().id
            try:
                client = inst.get_client()
                login = await client.call_action('get_login_info')
                groups = await client.call_action('get_group_list')
                accounts.append(dict(platform=platform, user_id=str(login['user_id']), name=login.get('nickname', '')))
                live[platform] = {str(group['group_id']): group for group in groups}
            except Exception:
                logger.exception('学习看板查询 QQ 连接失败：%s', platform)
                connection_errors.append(platform)
        groups = []
        for (platform, group_id), rule in self.owner.rules.items():
            info = live.get(platform, {}).get(group_id, {})
            allowed = group_id in self.owner.config['allowed_group_ids']
            groups.append(dict(platform=platform, group_id=group_id, name=info.get('group_name') or '群 ' + group_id,
                member_count=info.get('member_count'), joined=group_id in live[platform] if platform in live else None,
                allowed=allowed, persona=rule.get('persona_id', ''), provider=rule.get('provider_id', ''),
                mode=rule.get('feedback_adapt_mode', '关闭'), feedback=bool(rule.get('feedback_observe')),
                slang=bool(rule.get('slang_learning')), stickers=bool(rule.get('sticker_learning')),
                reaction_observe=self.owner.learning.reactions.enabled((platform, group_id)),
                reaction_auto=self.owner.learning.reactions.enabled((platform, group_id), 'reaction_auto'),
                profiles=bool(rule.get('member_profiles'))))
        return dict(groups=groups, accounts=accounts, connection_errors=connection_errors, updated=time.time())

    async def overview(self):
        scope = self.scope()
        replies = dict(self.db.execute('SELECT COUNT(*) total,SUM(status=\'settled\') settled,'
            'SUM(status=\'observing\') observing,AVG(score) mean_score FROM replies '
            'WHERE platform=? AND group_id=? AND persona=?', scope).fetchone())
        query, values = self.owner.learning.policy.stickers.scope_query(*scope[:2])
        images = dict(self.db.execute('SELECT status,COUNT(*) FROM stickers WHERE '
                                     + query + ' GROUP BY status', values).fetchall())
        members, terms = self.members_for(scope), self.terms_for(scope)
        state = self.owner.learning.policy.state(scope[:2])
        actions = self.db.execute('SELECT COUNT(*),SUM(updated) FROM reply_actions WHERE platform=? '
                                 'AND group_id=? AND persona=?', scope).fetchone()
        evaluation = comparison(self.owner.learning, scope)
        topics = list(dict.fromkeys(clue for item in terms for sense in item['senses'] if sense['confirmed']
                                   for clue in sense['clues']))
        activity = [dict(row) for row in self.db.execute("SELECT date(created,'unixepoch','+8 hours') day,COUNT(*) replies,"
            'AVG(score) score FROM replies WHERE platform=? AND group_id=? AND persona=? AND created>=? '
            "GROUP BY date(created,'unixepoch','+8 hours') ORDER BY day", (*scope, time.time() - 7 * 86400))]
        styles = [dict(name=key, samples=arm['n']) for key, arm in state['arms'].items()]
        recent = [self.reply_row(row) for row in self.db.execute('SELECT r.*,a.action,a.updated trained FROM replies r '
            'LEFT JOIN reply_actions a ON a.reply_id=r.id WHERE r.platform=? AND r.group_id=? AND r.persona=? '
            'ORDER BY r.created DESC LIMIT 5', scope)]
        return dict(replies=replies, images=images, members=len(members),
            profiled_members=sum(bool(m['profile']) for m in members), terms=len(terms),
            confirmed_terms=sum(any(s['confirmed'] for s in item['senses']) for item in terms),
            actions=actions[0], processed=actions[1] or 0, trained=sum(arm['n'] for arm in state['arms'].values()),
            reviews=state['reviews'], last_review=state['last_review'], lessons=state['lessons'],
            topics=topics, styles=styles, comparison=evaluation['phases'][:2], evaluation=evaluation, activity=activity, recent=recent, updated=time.time())

    @staticmethod
    def reply_row(row):
        data = dict(row)
        for key in ('summary', 'context', 'action'):
            if key in data:
                data[key] = json.loads(data[key]) if data[key] else None
        return data

    async def replies(self):
        scope = self.scope()
        page, size, offset = self.page()
        query, values = 'r.platform=? AND r.group_id=? AND r.persona=?', list(scope)
        search, outcome = request.args.get('q', '').strip(), request.args.get('outcome', '')
        if search:
            query += ' AND r.body LIKE ?'
            values.append('%' + search + '%')
        if outcome:
            if outcome not in {'feedback', 'active_no_response', 'uncertain', 'insufficient', 'partial_delivery', 'analysis_failed', 'observing'}:
                raise ValueError('未知结算状态')
            query += ' AND ' + ('r.status=?' if outcome == 'observing' else 'r.outcome=?')
            values.append(outcome)
        total = self.db.execute('SELECT COUNT(*) FROM replies r WHERE ' + query, values).fetchone()[0]
        rows = self.db.execute('SELECT r.*,a.action,a.updated trained FROM replies r LEFT JOIN reply_actions a '
            'ON a.reply_id=r.id WHERE ' + query + ' ORDER BY r.created DESC LIMIT ? OFFSET ?', (*values, size, offset))
        return dict(items=[self.reply_row(row) for row in rows], total=total, page=page, page_size=size)

    async def reply(self):
        scope = self.scope()
        reply_id = int(request.args.get('id', '0'))
        row = self.db.execute('SELECT r.*,a.action,a.updated trained,a.propensity,a.candidates FROM replies r '
            'LEFT JOIN reply_actions a ON a.reply_id=r.id WHERE r.platform=? AND r.group_id=? AND r.persona=? '
            'AND r.id=?', (*scope, reply_id)).fetchone()
        if row is None:
            raise ValueError('本群没有这条回复')
        result = self.reply_row(row)
        result['candidates'] = json.loads(result['candidates']) if result['candidates'] else []
        result['outgoing'] = [r[0] for r in self.db.execute('SELECT message_id FROM outgoing WHERE platform=? '
            'AND group_id=? AND reply_id=?', (*scope[:2], reply_id))]
        result['feedback'] = [dict(r) for r in self.db.execute('SELECT f.*,m.sender,m.text,m.parent,m.ts '
            'FROM feedback f LEFT JOIN messages m ON m.platform=f.platform AND m.group_id=f.group_id AND m.message_id=f.message_id '
            'WHERE f.platform=? AND f.group_id=? AND f.reply_id=? ORDER BY m.ts', (*scope[:2], reply_id))]
        return result

    async def background(self):
        scope = self.scope()[:2]
        background = self.owner.learning.background
        db = background.db
        page, size, offset = self.page()
        rows = [dict(r) for r in db.execute('SELECT source_id,kind,name,status,reason,cursor FROM group_sources WHERE platform=? AND group_id=? ORDER BY kind,name LIMIT ? OFFSET ?', (*scope, size, offset))]
        counts = dict(db.execute('SELECT status,COUNT(*) FROM group_sources WHERE platform=? AND group_id=? GROUP BY status', scope).fetchall())
        return dict(state=background.state(scope), worker=dict(stage=background.stage, enabled=background.enabled(scope), scheduled=background.task is not None, running=background.task is not None and not background.task.done(), active_replies=sum(not t.done() for t in self.owner.limits.tasks)), intensity=self.owner.rules[scope].get('banter_intensity', 30),
                    expressions=self.owner.learning.expressions.snapshot(scope),
                    items=rows, counts=counts, total=sum(counts.values()), page=page, page_size=size)

    async def choices(self):
        scope = self.scope()[:2]
        db = self.owner.learning.policy.interaction.db
        page, size, offset = self.page()
        rows = [dict(r) for r in db.execute('SELECT * FROM interaction_choices WHERE platform=? AND group_id=? ORDER BY created DESC LIMIT ? OFFSET ?', (*scope, size, offset))]
        for row in rows:
            row['candidates'] = json.loads(row['candidates'])
            row['sources'] = json.loads(row['sources'])
            row['details'] = json.loads(row['details'] or '{}')
        total = db.execute('SELECT COUNT(*) FROM interaction_choices WHERE platform=? AND group_id=?', scope).fetchone()[0]
        return dict(items=rows, total=total, page=page, page_size=size,
                    opportunity=self.owner.learning.opportunities.state(scope),
                    threshold=int(self.owner.rules[scope].get('message_count', 10)))

    async def stickers(self):
        scope = self.scope()
        page, size, offset = self.page()
        query, values = self.owner.learning.policy.stickers.scope_query(*scope[:2])
        status, search = request.args.get('status', ''), request.args.get('q', '').strip()
        if status:
            if status not in {'ready', 'pending', 'rejected', 'failed'}:
                raise ValueError('未知图片状态')
            query += ' AND status=?'
            values.append(status)
        if search:
            query += ' AND (description LIKE ? OR visible_text LIKE ? OR scene LIKE ?)'
            values.extend(['%' + search + '%'] * 3)
        total = self.db.execute('SELECT COUNT(*) FROM stickers WHERE ' + query, values).fetchone()[0]
        rows = self.db.execute('SELECT id,status,created,seen,description,visible_text,tags,scene,uses,last_used,reward_sum,reward_n '
            'FROM stickers WHERE ' + query + ' ORDER BY created DESC LIMIT ? OFFSET ?', (*values, size, offset))
        items = [dict(row) for row in rows]
        for item in items:
            item['tags'] = json.loads(item['tags']) if item['tags'] else []
        return dict(items=items, total=total, page=page, page_size=size)

    async def image(self):
        scope = self.scope()
        query, values = self.owner.learning.policy.stickers.scope_query(*scope[:2])
        row = self.db.execute("SELECT path,platform,group_id FROM stickers WHERE " + query + " AND id=? AND status='ready'",
                              (*values, int(request.args.get('id', '0')))).fetchone()
        if row is None:
            raise ValueError('当前表情包池没有这张可用图片')
        root = (self.owner.learning.policy.stickers.root / row['platform'] / row['group_id']).resolve()
        path = Path(row['path']).resolve()
        if not path.is_relative_to(root):
            raise ValueError('图片不属于记录的来源图库')
        mime = mimetypes.guess_type(path.name)[0]
        return dict(src='data:' + mime + ';base64,' + base64.b64encode(path.read_bytes()).decode('ascii'))

    async def slang(self):
        return dict(items=self.terms_for(self.scope()))

    async def members(self):
        items = self.members_for(self.scope())
        search = request.args.get('q', '').strip().lower()
        items = [m for m in items if not search or search in (m['user_id'] + m['name'] + m['profile']).lower()]
        items.sort(key=lambda m: m['last_seen'], reverse=True)
        page, size, offset = self.page()
        return dict(items=items[offset:offset + size], total=len(items), page=page, page_size=size)
