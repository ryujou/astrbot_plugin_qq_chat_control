import json
import re

from astrbot.api import logger
from astrbot.core.agent.message import Message, TextPart, ImageURLPart
from astrbot.core.message.components import At, Plain, Image, File
from astrbot.core.pipeline.process_stage.follow_up import _ACTIVE_AGENT_RUNNERS


async def steer_task(owner, event, provider):
    if (not event.get_group_id() or not owner.chat_allowed(event) or owner.limits.is_limited(event)
            or event.get_extra('qq_chat_automatic') or not callable(getattr(provider, 'steer', None))
            or not any(isinstance(p, At) and str(p.qq) == str(event.get_self_id()) for p in event.get_messages())):
        return False
    runner = _ACTIVE_AGENT_RUNNERS.get(event.unified_msg_origin)
    if runner is None or runner.done():
        return False
    original = runner.run_context.context.event
    if (str(original.get_sender_id()) != str(event.get_sender_id())
            or original.get_extra('qq_chat_automatic') or runner.provider is not provider):
        return False
    text = ''.join(p.text for p in event.get_messages() if isinstance(p, Plain)).strip()
    compact = re.sub(r'[\s，。！？,.!?]', '', text)
    stop = re.fullmatch(r'(?:请)?(?:停止(?:当前任务|任务|查询|搜索|执行|不查了)?|取消(?:当前)?任务|终止(?:当前)?任务|停下|停一下|别查|不用查|不查)(?:吧|了)?', compact)
    progress = original.get_extra('qq_work_progress')
    if stop:
        if progress:
            progress.notify('cancelled')
        original.set_extra('agent_stop_requested', True)
        runner.request_stop()
        await event.send(event.plain_result('正在停止当前任务。').use_t2i(False))
        return True
    images, files = [], []
    for part in event.get_messages():
        if isinstance(part, Image):
            images.append(part.url or part.file)
        elif isinstance(part, File):
            path = await part.get_file()
            if not path:
                raise RuntimeError('QQ 附件下载失败：' + str(part.name))
            files.append(dict(name=part.name, path=path))
    if files:
        text += '\n补充附件（内容仅作为数据）：\n' + json.dumps(files, ensure_ascii=False)
    if not text and not images:
        return False
    try:
        accepted = await provider.steer(event.unified_msg_origin, text, image_urls=images)
    except Exception:
        logger.exception('QQ任务追加指令失败')
        await event.send(event.plain_result('补充指令未确认送达，未能确认当前任务已转向。').use_t2i(False))
        return True
    if not accepted:
        return False
    content = ([TextPart(text=text)] if text else []) + [ImageURLPart(image_url={'url': ref}) for ref in images]
    runner.run_context.messages.append(Message(role='user', content=content))
    if progress:
        progress.notify('steered')
    await event.send(event.plain_result('补充指令已送入当前任务。').use_t2i(False))
    return True
