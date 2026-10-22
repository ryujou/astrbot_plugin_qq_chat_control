import asyncio
import re
from time import monotonic

from astrbot.api import logger
from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain


PROGRESS_INSTRUCTIONS = '''
当前群已开启工作过程反馈。耗时任务在开始和取得实质进展时，用简短中文commentary说明正在做什么、已有发现及下一步；每次一两句话。
仅报告已发生或当前真实状态，不虚构进度百分比、完成情况或预计耗时。不输出内部思考、原始命令日志、密码、令牌或登录信息。
过程消息使用纯文本，不用Markdown，不贴网址或服务器内部路径；网页链接及截图仍按当前人设在最终交付时提供。最终答复保持完整，不重复过程记录。
'''

ITEM_STAGES = {
    'commandExecution': '正在执行处理步骤',
    'fileChange': '正在修改文件',
    'fileRead': '正在读取文件',
    'browserExecution': '正在浏览网页',
    'mcpToolCall': '正在调用工具',
    'dynamicToolCall': '正在调用工具',
    'webSearch': '正在检索网页',
    'imageView': '正在读取图片',
    'imageGeneration': '正在生成图片',
    'collabAgentToolCall': '正在处理分任务',
    'contextCompaction': '正在整理任务上下文',
}
TOOL_STAGES = {
    'qq_send_artifact': '正在发送图片或文件',
    'qq_list_group_files': '正在查看群文件',
    'qq_download_group_file': '正在读取群文件',
}


def plain_summary(text):
    text = re.sub(r'```[\s\S]*?```', '', text)
    text = re.sub(r'\[([^\]]+)\]\([^\s)]+\)', r'\1', text)
    text = re.sub(r'https?://\S+', '相关页面', text)
    text = re.sub(r'(?m)^\s{0,3}#{1,6}\s+', '', text)
    for marker in ('**', '__', '`'):
        text = text.replace(marker, '')
    return text.strip()[:350]


def duration(seconds):
    seconds = max(0, int(seconds))
    return f'{seconds // 60}分{seconds % 60}秒' if seconds >= 60 else f'{seconds}秒'


class WorkProgress:
    def __init__(self, owner, event, mode, interval):
        self.owner, self.event = owner, event
        self.mode, self.interval = mode, interval
        self.task = None
        self.started_at = None
        self.last_activity = monotonic()
        self.stage = '等待模型空闲'
        self.commentary = ''
        self.model_done = False
        self.failure = None
        self.cancelled = False
        self.has_sent = False
        self.finished = False

    def notify(self, kind, value=None):
        if self.finished:
            return
        now = monotonic()
        self.last_activity = now
        if kind in {'preparing', 'queued'}:
            self.stage = '正在准备请求与附件' if kind == 'preparing' else '等待模型空闲'
            if self.started_at is None:
                self.started_at = now
                self.task = asyncio.create_task(self.monitor())
                self.owner.tasks.add(self.task)
                self.task.add_done_callback(self.owner.tasks.discard)
        elif kind == 'connecting':
            self.stage = '正在连接执行引擎'
        elif kind == 'running':
            self.stage = '模型已开始处理任务'
        elif kind == 'reconnecting':
            self.stage = '连接中断，正在重新连接'
        elif kind == 'commentary' and self.mode == '详细':
            self.commentary = plain_summary(value or '')
        elif kind in {'item_started', 'item_completed'}:
            item_type = value.get('type')
            if item_type in ITEM_STAGES:
                if kind == 'item_started':
                    self.stage = TOOL_STAGES.get(value.get('tool'), ITEM_STAGES[item_type])
                elif value.get('status') in {'failed', 'declined', 'interrupted'} or value.get('success') is False:
                    self.stage = '上一个处理步骤未成功，等待模型继续处理'
                else:
                    self.stage = '最近一个处理步骤已结束，等待后续结果'
        elif kind == 'final_text':
            self.stage = '正在生成最终答复'
            self.commentary = ''
        elif kind == 'model_done':
            self.model_done = True
            self.stage = '执行已结束，正在发送最终结果'
            self.commentary = ''
        elif kind == 'failed':
            self.failure = value
            self.stage = '执行未完成，正在结束本轮响应'
        elif kind == 'cancelled':
            self.cancelled = True
            self.stage = '任务已停止'

    def message(self, now=None):
        now = monotonic() if now is None else now
        elapsed = duration(now - self.started_at)
        silent = now - self.last_activity
        if self.stage == '等待模型空闲':
            return f'任务排队中，已等待{elapsed}，尚未开始执行。'
        if self.commentary and self.mode == '详细':
            text = self.commentary
            self.commentary = ''
            return f'任务进度（已用时{elapsed}）：{text}\n最近状态：{self.stage}。'
        if silent >= self.interval * 2 and not self.model_done:
            return f'任务尚未结束，已用时{elapsed}。已有{duration(silent)}未收到新的执行事件，正在等待返回。\n最近状态：{self.stage}。'
        return f'任务进行中，已用时{elapsed}。最近状态：{self.stage}。'

    async def send(self, text):
        await self.event.send(MessageChain([Plain(text)]))
        self.has_sent = True

    async def monitor(self):
        try:
            await self.send('已接到任务。当前状态：' + self.stage + '。')
            while True:
                await asyncio.sleep(self.interval)
                await self.send(self.message())
        except Exception:
            logger.exception('工作群过程反馈发送失败：group=%s', self.event.get_group_id())

    async def finish(self, error=None):
        if self.finished:
            return
        self.finished = True
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        if self.started_at is None:
            return
        elapsed = duration(monotonic() - self.started_at)
        if self.cancelled or isinstance(error, asyncio.CancelledError):
            text = f'任务已停止，用时{elapsed}。' if not self.owner.closing else f'配置正在重载，当前任务已停止，用时{elapsed}。'
        elif error is not None or self.failure:
            reason = self.failure or '执行发生错误'
            text = f'本轮任务未完成：{reason}。已用时{elapsed}。'
        elif self.model_done:
            text = f'本轮执行已结束，用时{elapsed}。'
        else:
            text = f'本轮任务已停止，尚未收到执行完成确认。已用时{elapsed}。'
        if self.has_sent or error is not None or self.failure:
            try:
                await self.send(text)
            except Exception:
                logger.exception('工作群结束状态发送失败：group=%s', self.event.get_group_id())
