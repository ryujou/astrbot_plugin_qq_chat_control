import json
import re
import time
from collections import defaultdict
from difflib import SequenceMatcher

import numpy as np


def average(values):
    return sum(values) / len(values) if values else None


def comparison(learning, scope, now=None):
    now = time.time() if now is None else now
    since = now - 14 * 86400
    db = learning.open()
    rows = [dict(row) for row in db.execute(
        'SELECT r.id,r.created,r.sent,r.completed,r.body,r.status,r.outcome,r.score,r.summary,a.action,a.created decision_time '
        'FROM replies r JOIN reply_actions a ON a.reply_id=r.id AND a.platform=r.platform '
        'AND a.group_id=r.group_id AND a.persona=r.persona '
        'WHERE r.platform=? AND r.group_id=? AND r.persona=? AND r.created>=? ORDER BY COALESCE(r.sent,r.created),r.id', (*scope, since))]
    buckets = {phase: [] for phase in ('baseline', 'adaptive', 'requested', 'scene_failed', 'candidate')}
    previous = []
    pairs = defaultdict(lambda: defaultdict(list))
    causes = defaultdict(int)
    for row in rows:
        action = json.loads(row['action'])
        summary = json.loads(row['summary'] or '{}')
        row.update(action=action, summary=summary)
        row['usable'] = (row['status'] == 'settled' and row['outcome'] in {'feedback', 'active_no_response', 'uncertain'}
                         and (row['score'] is not None or summary.get('correction_people', 0) > 0))
        row['repeat'] = False
        if row['sent'] is not None:
            text = re.sub(r'\W+', '', re.sub(r'\[[^\]]*\]', '', row['body'] or '')).lower()
            recent = [item for item in previous[-10:] if 0 <= row['sent'] - item[0] <= 600]
            row['repeat'] = any((text and text == old_text)
                or (len(text) >= 12 and len(old_text) >= 12 and SequenceMatcher(None, text, old_text).ratio() >= .9)
                or (action.get('sticker_id') is not None and action['sticker_id'] == sticker)
                for _, old_text, sticker in recent)
            previous.append((row['sent'], text, action.get('sticker_id')))
        phase = action['phase']
        if phase in buckets:
            buckets[phase].append(row)
        if row['usable']:
            for cause, count in summary.get('causes', {}).items():
                causes[cause] += count
            if phase in {'baseline', 'adaptive'} and action.get('mode') == '对照' and action.get('policy_version'):
                pairs[(action['policy_version'], int(row['decision_time'] // 1800))][phase].append(row)

    stats = []
    for phase, items in buckets.items():
        valid = [r for r in items if r['usable'] and phase != 'scene_failed']
        sent = [r for r in items if r['sent'] is not None]
        stats.append(dict(phase=phase, total=len(items), samples=len(valid),
            observing=sum(r['status'] != 'settled' for r in items), excluded=len(items) - len(valid),
            unknown=sum(r['outcome'] in {'uncertain', 'insufficient'} and not r['usable'] for r in items),
            failures=sum(r['outcome'] in {'analysis_failed', 'partial_delivery'} for r in items),
            participants=average([r['summary'].get('new_participants', 0) for r in valid]),
            annoyance=average([float(r['summary'].get('annoyance_people', 0) > 0) for r in valid]),
            correction=average([float(r['summary'].get('correction_people', 0) > 0) for r in valid]),
            repetition=average([float(r['repeat']) for r in sent]),
            first_seconds=average([max(0, r['sent'] - r['created']) for r in sent]),
            complete_seconds=average([max(0, r['completed'] - r['created']) for r in sent if r['completed'] is not None])))

    # Pair adjacent 15-minute periods, then resample whole days to avoid treating replies as independent.
    daily = defaultdict(list)
    periods = []
    for (version, block), phases in sorted(pairs.items()):
        if not all(phases.get(p) for p in ('baseline', 'adaptive')):
            continue
        counts = {p: len(phases[p]) for p in ('baseline', 'adaptive')}
        delta = average([r['summary'].get('new_participants', 0) for r in phases['adaptive']]) - average(
            [r['summary'].get('new_participants', 0) for r in phases['baseline']])
        day = int((block * 1800 + 8 * 3600) // 86400)
        daily[day].append(delta)
        periods.append(dict(start=block * 1800, policy_version=version, **counts, participants_delta=delta))
    values = [average(v) for v in daily.values()]
    evidence = dict(status='insufficient', message='证据不足：需要覆盖至少7个有配对时段的日期；这只是覆盖要求，不保证效果成立。',
                    paired_days=len(values), paired_periods=len(periods), participants_delta=average(values), interval=None)
    if len(values) >= 7:
        rng = np.random.default_rng(0)
        means = rng.choice(values, size=(2000, len(values)), replace=True).mean(axis=1)
        lower, upper = map(float, np.quantile(means, [.025, .975]))
        evidence['interval'] = [lower, upper]
        if lower > 0:
            evidence.update(status='positive_trend', message='新增接话呈正向趋势；还需同时查看反感、纠错和未知比例，不代表因果提升。')
        elif upper < 0:
            evidence.update(status='negative_trend', message='新增接话呈负向趋势；系统保留对照，不据此自动扩大探索或修改发言频率。')
        else:
            evidence.update(status='inconclusive', message='已覆盖多个日期，但差异区间跨过0，尚不能判断哪种策略更好。')
    warnings = []
    baseline, adaptive = stats[:2]
    if baseline['samples'] and adaptive['samples']:
        for field, label in (('annoyance', '明确反感'), ('correction', '纠错'), ('repetition', '重复')):
            if adaptive[field] is not None and baseline[field] is not None and adaptive[field] > baseline[field]:
                warnings.append(f'学习策略的{label}比例较高；这是描述性差异，需继续观察。')
        if adaptive['unknown'] / adaptive['total'] > baseline['unknown'] / baseline['total']:
            warnings.append('学习策略的未知样本占比更高，有效样本的均值可能存在选择偏差。')
    costs = [dict(row) for row in db.execute(
        "SELECT purpose,provider,COUNT(*) calls,SUM(status='failed') failures,SUM(status='interrupted') interrupted,"
        "SUM(status='legacy' AND success=0) legacy_failures,AVG(seconds) seconds,"
        'SUM(input_tokens IS NOT NULL) usage_known,SUM(input_tokens) input_tokens,'
        'SUM(cached_tokens) cached_tokens,SUM(output_tokens) output_tokens FROM model_calls '
        'WHERE platform=? AND group_id=? AND persona=? AND created>=? GROUP BY purpose,provider', (*scope, since))]
    phase_costs = [dict(row) for row in db.execute(
        "SELECT json_extract(a.action,'$.phase') phase,COUNT(*) calls,SUM(c.success=0) failures,AVG(c.seconds) seconds "
        'FROM model_calls c JOIN reply_actions a ON a.reply_id=c.reply_id AND a.platform=c.platform '
        'AND a.group_id=c.group_id AND a.persona=c.persona '
        'WHERE c.platform=? AND c.group_id=? AND c.persona=? AND c.created>=? '
        "AND c.purpose IN ('reply','scene','sticker_choose') GROUP BY phase", (*scope, since))]
    versions = []
    for version in sorted({r['action'].get('policy_version', 'unknown') for r in rows}):
        items = [r for r in rows if r['action'].get('policy_version', 'unknown') == version]
        valid = [r for r in items if r['usable']]
        versions.append(dict(version=version, total=len(items), samples=len(valid),
            participants=average([r['summary'].get('new_participants', 0) for r in valid]),
            annoyance=sum(r['summary'].get('annoyance_people', 0)>0 for r in valid)))
    return dict(versions=versions, phases=stats, evidence=evidence, warnings=warnings, periods=periods[-48:], costs=costs, phase_costs=phase_costs,
                causes=dict(causes), window_days=14,
                cost_note='回复与学习LLM调用从1.11.0起记录，不含向量检索、成员画像及工具费用。Token只统计Provider实际返回值，缺失用量和金额为未知。后台共享判断不分摊给原/学习策略。',
                repeat_note='10分钟内与最近10条实际回复的规范化文本相同、长文本相似度≥0.9或使用同一张图，记为重复；不判定是否合理。')
