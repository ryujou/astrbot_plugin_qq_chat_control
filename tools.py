import json
from copy import copy, deepcopy

from astrbot.core.agent.tool import FunctionTool, ToolSet

from .attachments import tool_context


def bind_tools(owner, event, request):
    available = list(request.func_tool.tools) if request.func_tool else []
    request.func_tool = None
    if (not owner.chat_allowed(event) or event.get_extra('qq_chat_automatic')
            or owner.limits.is_limited(event)):
        return
    manager = owner.context.get_llm_tool_manager()
    available.extend(manager.get_full_tool_set().tools)
    available.extend(manager.iter_builtin_tools())
    tools = ToolSet([])
    for original in available:
        if not original.active:
            continue
        tool = copy(original)
        if original.handler_module_path == 'data.plugins.astrbot_plugin_qqadmin.main':
            # Native group-admin handlers authenticate by default; keep that bypass out of model arguments.
            tool.parameters = deepcopy(original.parameters)
            tool.parameters.get('properties', {}).pop('need_auth', None)
            tool.parameters['required'] = [key for key in tool.parameters.get('required', []) if key != 'need_auth']
            tool.description = '\n'.join(line for line in tool.description.splitlines() if 'need_auth' not in line)
        tools.add_tool(tool)
    specs, instructions, execute = tool_context(event)
    for spec in specs:
        async def handler(event, _name=spec['name'], **arguments):
            result = await execute(_name, arguments)
            return json.dumps(result, ensure_ascii=False)

        tools.add_tool(FunctionTool(name=spec['name'], description=spec['description'],
                                   parameters=spec['inputSchema'], handler=handler))
    request.func_tool = tools
    request.system_prompt = (request.system_prompt or '') + instructions.replace('Codex 完整执行权限', '完整执行权限')
    request.system_prompt += ('\n本轮可以使用全部已提供的 AstrBot 工具，工具结果通过原生执行链路回传。'
        '按明确要求执行，不从群聊历史中的玩笑推导管理任务；缺少目标、时长等必要信息先询问。'
        '群管、微信等插件继续核验真实操作者和当前群，不能绕过权限或编造执行结果。')
