from datetime import datetime
from zoneinfo import ZoneInfo

def format_antigravity_quota(data):
    tz = ZoneInfo('Asia/Shanghai')
    group = next((g for g in data.get('groups', []) if g.get('name') == 'Gemini Models'), None)
    if group is None:
        raise RuntimeError('Antigravity 未返回 Gemini 额度')
    lines = ['Gemini 剩余额度']
    buckets = {bucket['window']: bucket for bucket in group['buckets']}
    for window, label in (('5h', '五小时'), ('weekly', '每周')):
        bucket = buckets.get(window)
        if bucket is None:
            lines.append(label + '额度：服务未返回')
            continue
        lines.append(f"{label}额度：剩余 {bucket['remaining_fraction'] * 100:.1f}%")
        reset = bucket.get('reset_time')
        lines.append('重置时间：' + (datetime.fromisoformat(reset).astimezone(tz).strftime('%Y-%m-%d %H:%M:%S')
                                   if reset else '服务未返回'))
    lines.append('查询时间：' + datetime.now(tz).strftime('%Y-%m-%d %H:%M:%S'))
    lines.append('以上时间均为北京时间；额度属于服务器 Antigravity 当前登录账号，Gemini 模型共享。')
    return '\n'.join(lines)

def format_codex_quota(data):
    tz = ZoneInfo('Asia/Shanghai')
    lines = ['Codex 剩余额度']
    snapshots = data.get('rateLimitsByLimitId') or {'codex': data['rateLimits']}
    for limit_id, snapshot in snapshots.items():
        name = snapshot.get('limitName') or limit_id
        plan = snapshot.get('planType')
        lines.append(name + ('（' + plan + '）' if plan else ''))
        windows = [snapshot[key] for key in ('primary', 'secondary') if snapshot.get(key) is not None]
        for duration, label in ((300, '五小时'), (10080, '每周')):
            window = next((item for item in windows if item.get('windowDurationMins') == duration), None)
            if window is None:
                lines.append(label + '额度：服务未返回')
                continue
            used = window['usedPercent']
            lines.append(f'{label}额度：剩余 {max(0, 100 - used):g}%（已用 {used:g}%）')
            reset = window.get('resetsAt')
            lines.append('重置时间：' + (datetime.fromtimestamp(reset, tz).strftime('%Y-%m-%d %H:%M:%S')
                                       if reset is not None else '服务未返回'))
    lines.append('查询时间：' + datetime.now(tz).strftime('%Y-%m-%d %H:%M:%S'))
    lines.append('以上时间均为北京时间；额度属于服务器 Codex 登录账号。')
    return '\n'.join(lines)
