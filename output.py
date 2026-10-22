import re
import time
from aiocqhttp.exceptions import ActionFailed, ApiError
from astrbot.api import logger
from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain, ResultContentType
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent

GROUP_ERROR_PREFIX = re.compile(
    r'^(?:LLM\s*响应错误|All chat models failed|Error occurred while processing agent'
    r'|Traceback \(most recent call last\)|[\w.]+(?:Error|Exception)\s*[:：]'
    r'|:\(\s*在调用插件|插件\s+\S+\s*:'
    r'|额度查询失败|CC Switch账号状态读取或切换失败'
    r'|登录文件已切换，但|当前会话没有使用本地Codex Provider'
    r'|You(?: have|\x27ve) (?:hit|reached) (?:your |the )?(?:usage |rate )?limit'
    r'|insufficient_quota|rate_limit_exceeded)',
    re.IGNORECASE,
)


def is_group_error_message(chain):
    return isinstance(chain, MessageChain) and (
        getattr(chain, 'result_content_type', None) == ResultContentType.AGENT_RUNNER_ERROR
        or bool(GROUP_ERROR_PREFIX.match(chain.get_plain_text().lstrip())))


async def confirm_text_delivery(bot, chain, event, group, since):
    if not event or not event.get('self_id') or not group or not str(group).isdigit():
        return None
    if not chain.chain or not all(isinstance(part, Plain) for part in chain.chain):
        return None
    expected = ''.join(part.text for part in chain.chain if part.text.strip())
    result = await bot.get_group_msg_history(group_id=int(group), count=10, self_id=event['self_id'])
    for message in result.get('messages', []):
        if str(message.get('sender', {}).get('user_id')) != str(event['self_id']):
            continue
        if not since - 1 <= message.get('time', 0) <= time.time() + 1:
            continue
        parts = message.get('message', [])
        if not isinstance(parts, list) or not all(part.get('type') == 'text' for part in parts):
            continue
        if ''.join(part.get('data', {}).get('text', '') for part in parts) == expected:
            return message.get('message_id')
    return None



class QuietOutput:
    def __init__(self):
        self.on_confirmed = None
        self._qq_send_message = AiocqhttpMessageEvent.__dict__['send_message']
        self._qq_send_streaming = AiocqhttpMessageEvent.send_streaming

        async def quiet_send(cls, bot, message_chain, event=None, is_group=False, session_id=None):
            if is_group and is_group_error_message(message_chain):
                logger.warning('Suppressed QQ group error output: group=%s error=%s',
                               session_id, message_chain.get_plain_text())
                return
            started = time.time()
            try:
                return await self._qq_send_message.__func__(
                    cls, bot, message_chain, event, is_group, session_id)
            except ActionFailed as error:
                detail = error.result.get('message', '')
                if not (is_group and error.retcode == 1200
                        and 'Timeout: NTEvent serviceAndMethod:NodeIKernelMsgService/sendMsg' in detail):
                    raise
                try:
                    message_id = await confirm_text_delivery(bot, message_chain, event, session_id, started)
                except (ApiError, OSError, TimeoutError) as lookup_error:
                    logger.warning('QQ发送回执超时，群历史核对失败：group=%s error=%s',
                                   session_id, type(lookup_error).__name__)
                    raise error from lookup_error
                if message_id is None:
                    raise
                logger.warning('QQ发送回执超时，已核对群历史确认发出，不重发：group=%s message_id=%s',
                               session_id, message_id)
                if self.on_confirmed:
                    self.on_confirmed(message_chain, message_id, session_id)

        async def quiet_stream(qq_event, generator, use_fallback=False):
            async def filtered():
                failed = False
                async for chain in generator:
                    if qq_event.get_group_id() and (failed or is_group_error_message(chain)):
                        if not failed:
                            logger.warning('Suppressed QQ group streaming error: group=%s error=%s',
                                           qq_event.get_group_id(), chain.get_plain_text())
                        failed = True
                        continue
                    yield chain
            return await self._qq_send_streaming(qq_event, filtered(), use_fallback)

        self._quiet_qq_send = classmethod(quiet_send)
        self._quiet_qq_stream = quiet_stream
        AiocqhttpMessageEvent.send_message = self._quiet_qq_send
        AiocqhttpMessageEvent.send_streaming = quiet_stream

    def close(self):
        if AiocqhttpMessageEvent.__dict__['send_message'] is self._quiet_qq_send:
            AiocqhttpMessageEvent.send_message = self._qq_send_message
        if AiocqhttpMessageEvent.send_streaming is self._quiet_qq_stream:
            AiocqhttpMessageEvent.send_streaming = self._qq_send_streaming
