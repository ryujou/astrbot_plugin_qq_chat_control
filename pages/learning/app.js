import {settings, settingsChanged, leaveSettings, saveSettings} from './settings.js';

const bridge = window.AstrBotPluginPage;
const $ = id => document.getElementById(id);
const escape = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const date = value => value ? new Date(value * 1000).toLocaleString('zh-CN', {hour12:false}) : '尚未更新';
const num = value => value == null ? '—' : Number(value).toLocaleString('zh-CN', {maximumFractionDigits:2});
const percent = value => value == null ? '—' : (value * 100).toFixed(0) + '%';
const outcomes = {feedback:'有互动反馈', active_no_response:'群活跃但无接话', uncertain:'反馈不确定', insufficient:'样本不足', partial_delivery:'发送不完整', analysis_failed:'判断失败', observing:'观察中', prepared:'待发送'};
const imageStates = {ready:'可用',pending:'待理解',rejected:'已排除',failed:'理解失败'};
const intents = {positive:'相关接话',annoyance:'明确反感',correction:'纠错',unknown:'不确定',neutral:'中性'};
const causes = {fact:'事实',understanding:'理解',expression:'表达',sticker:'配图',unknown:'原因未知'};
const purposes = {reply:'回复生成',scene:'场景判断',feedback:'反馈判断',feedback_recheck:'Codex复核',review:'经验整理',slang:'黑话学习',sticker_choose:'选图判断',sticker_describe:'图片理解',group_expressions:'场景表达提炼',candidate_generate:'候选生成',candidate_review:'候选评审',candidate_freshness:'语境复查'};
const phases = {candidate:'三候选择优',baseline:'原策略',adaptive:'学习策略',requested:'主动索取表情包',scene_failed:'场景判断降级'};
let groups = [], current = null, tab = 'overview', page = 1, query = '', filter = '', generation = 0, detailGeneration = 0;
let reactionDays = 7, reactionEmoji = '', reactionCategory = '', reactionView = 'summary';
let settingsArea = 'group';
const reactionStates = {pending:'待分类',classified:'已分类',uncertain:'不确定',missing:'缺少原文',failed:'失败',expired:'已淘汰',ignored:'已撤销或仅机器人',active:'已确认',suspended:'暂停',judging:'判断中',sending:'发送中',sent:'已送达',unknown:'发送结果未知',cancelled:'已取消',skipped:'已跳过'};
Object.assign(purposes, {limit_notice:'额度结束语',self_presence:'名片与头像',reaction_decide:'语义贴表情',reaction_classify:'回应分类',reaction_patterns:'回应用法提炼',reaction_feedback:'回应效果判断'});

async function api(endpoint, params = {}) {
  const result = await bridge.apiGet('learning/' + endpoint, params);
  if (result?.ok === false) throw new Error(result.message || '读取失败');
  return result?.data ?? result;
}
function scope() { return {platform: current.platform, group_id: current.group_id}; }
function empty(title, note = '') { return `<div class="empty"><h3>${escape(title)}</h3><p>${escape(note)}</p></div>`; }
function badge(value, good = false) { return `<span class="badge${good?' good':''}">${escape(value)}</span>`; }
function score(value) { return `<span class="score ${value > 0 ? 'positive' : value < 0 ? 'negative' : ''}">${num(value)}</span>`; }
function showError(error) { $('error').textContent = error.message || String(error); $('error').hidden = false; }
function applyTheme(context) { document.documentElement.dataset.theme = context?.theme === 'dark' ? 'dark' : 'light'; }

function renderGroups() {
  const search = $('groupSearch').value.trim().toLowerCase();
  $('groupList').innerHTML = groups.filter(g => (g.name + g.group_id).toLowerCase().includes(search)).map(g => {
    const selected = current && g.platform === current.platform && g.group_id === current.group_id;
    const state = !g.allowed ? '已停用' : g.joined === false ? '未入群' : g.feedback ? g.mode + '模式' : '学习关闭';
    return `<button class="group-button ${selected?'active':''}" data-group="${escape(g.platform+':'+g.group_id)}" ${selected?'aria-current="true"':''}><span class="group-icon">${escape(g.group_id.slice(-2))}</span><span><span class="group-name" title="${escape(g.name)}">${escape(g.name)}</span><span class="group-id">${escape(g.group_id)} · ${state}</span></span></button>`;
  }).join('') || '<p class="section-note">没有匹配的群聊</p>';
}
function renderHeader() {
  $('groupTitle').textContent = current.name;
  $('groupMeta').textContent = [current.group_id, current.persona || '未绑定人设', current.provider || '默认模型', current.member_count == null ? '' : current.member_count + ' 位成员'].filter(Boolean).join(' / ');
  const states = [['回复观察',current.feedback],['策略 '+current.mode,current.mode!=='关闭'],['表情包采集',current.stickers],['黑话学习',current.slang],['成员画像',current.profiles]];
  $('groupStatus').innerHTML = states.map(([name,on]) => `<span class="status-item ${on && current.allowed?'on':''}"><span class="dot"></span>${escape(name)}${name.startsWith('策略')?'':on?'开启':'关闭'}</span>`).join('');
  const note = !current.allowed ? '本群已停用：机器人不响应、不采集、不学习。这里仅查看已保存的资料。'
    : current.joined === false ? '当前机器人账号尚不在这个群。配置已保留，暂时无法接收消息、采集或继续学习。'
    : current.joined == null ? '暂未获取到 QQ 连接状态。以下为已保存资料，不能据此判断机器人在线。'
    : !current.feedback ? '本群未开启反馈学习。查看看板不会开启采集或改变群配置。' : '';
  $('connectionNotice').textContent = note;
  $('connectionNotice').hidden = !note;
}
async function choose(group) {
  current = group; page = 1; query = ''; filter = '';
  reactionEmoji='';reactionCategory='';
  if ($('detail').open) $('detail').close();
  renderGroups(); renderHeader(); await load();
}
function metric(label, value, note) { return `<div><div class="metric-label">${label}</div><div class="metric-value">${value}</div><div class="metric-note">${note}</div></div>`; }
function replyLinks(ids) { return ids.map(id => `<button class="link-button" data-reply="${id}">回复 #${id}</button>`).join(' · '); }
function background(data) {
  const s=data.state, states={ready:'已读',pending:'待处理',excluded:'已排除',failed:'失败'};
  return expressionPanel(data.expressions)+`<section class="panel"><h2>${escape(s.name||'尚未读取群名')}</h2><p>${data.worker?.active_replies?"等待前台回复结束":data.worker?.running?"后台处理中":"等待下一次扫描"} · 整活尺度上限 ${num(data.intensity)} · 公告同步 ${date(s.synced)}</p><p class="message-body">${escape(s.summary||'群背景尚待总结')}</p><small>摘要来源：${escape((s.sources||[]).join('、'))}</small>${Object.entries(s.errors||{}).map(([k,v])=>`<p class="notice error">${escape(k)}：${escape(v)}（保留上次资料）</p>`).join('')}<h3>群公告</h3>${(s.notices||[]).map(n=>`<article class="lesson"><small>${date(n.published)} · ${n.pinned==null?'置顶未知':n.pinned?'置顶':'非置顶'}</small><p class="message-body">${escape(n.text)}</p></article>`).join('')||'<p>尚无已读取的公告</p>'}<h3>历史群习惯摘要</h3>${(s.habits||[]).map(h=>`<article class="lesson"><p>${escape(h.text)}</p><small>${escape(h.evidence.map(e=>e.text).join(' / '))}</small></article>`).join('')||'<p>证据尚不足</p>'}</section><section class="panel"><h2>群文件与相册背景</h2><p>${Object.entries(data.counts).map(([k,v])=>`${states[k]||escape(k)} ${v}`).join(' · ')||'尚未建立目录'}</p><p class="section-note">视频、音频和超限资料直接排除；相册图片不进入表情包池。</p><ul class="mini-list">${data.items.map(i=>`<li><div>${escape(i.name)}<small class="reply-meta">${i.kind==='file'?'群文件':'相册'} · ${escape(i.reason||'')} · 已处理片段 ${i.cursor}</small></div>${badge(states[i.status]||i.status,i.status==='ready')}</li>`).join('')}</ul></section>`+pager(data);
}
function expressionPanel(data) {
  if (!data) return '';
  const m=data.meta||{}, labels={pending:'待验证',active:'可用',suspended:'暂停'};
  return `<section class="panel"><h2>场景表达学习</h2><p>${data.enabled?'后台自动学习':'已停用'} · 待提炼 ${num(m.pending||0)} / 500 · 已处理 ${num(m.processed||0)} · 淘汰 ${num(m.dropped||0)} · 失败 ${num(m.failed||0)}</p><p class="section-note">每15分钟检查，积累10条后提炼；素材最长保留24小时。最近成功：${date(m.completed)}</p>${m.last_error?`<p class="notice error">${escape(m.last_error)}</p>`:''}${data.items.map(e=>`<article class="lesson"><h3>${escape(e.scene)} · ${labels[e.status]||escape(e.status)}</h3><p>${escape(e.style)}</p><p class="section-note">边界：${escape(e.boundary)}</p><small>${escape(e.id)} · 真人证据：${escape(e.evidence.map(v=>v.text).join(' / '))}<br>实际使用正向 ${num(e.positive||0)} · 相关负向 ${num(e.negative||0)}</small></article>`).join('')||'<p>尚未形成场景表达；已有群习惯保留为历史摘要。</p>'}</section>`;
}
function choices(data) {
  const o=data.opportunity||{}, stages={idle:'等待累计',running:'正在生成或评审',retry:'等待下一条普通消息重试一次',sending:'已开始发送'};
  const counter=`<section class="panel"><h2>自动回复机会</h2><p>累计 ${num(o.count||0)} / ${num(data.threshold)} · ${stages[o.status]||'尚无计数'}${o.status==='running'||o.status==='retry'?` · 第 ${num(o.attempt)} 次尝试`:''}</p><p class="section-note">生成期间最多积累下一轮消息；不会连续补发。评分不足、换题或明确停止会结束本轮。</p></section>`;
  return counter+(data.items.map(r=>{
    const t=r.details?.timings||{}, names={retrieval:'检索',generation:'生成',review:'评审',freshness:'语境复查',first_send:'触发至首次送达'};
    return `<section class="panel"><div class="section-head"><h2>${escape(r.reason)}</h2><small>${date(r.created)} · ${num(r.seconds)} 秒</small></div><p class="section-note">${Object.entries(t).map(([k,v])=>`${names[k]||escape(k)} ${num(v)} 秒`).join(' · ')||'旧记录未分段计时'}</p>${r.candidates.map(c=>`<article class="lesson"><h3>候选 ${escape(c.id??'—')}${r.selected!=null&&r.selected===c.id?' · 已选中':''}</h3><p class="message-body">${escape(c.text||c.raw||'')}</p><small>${escape(c.style)} · 尺度 ${num(c.intensity)} · 评审 ${num(c.score)} · ${escape(c.rejected||c.review?.reason||'尚未完成评审')}<br>确认采用表达：${escape((c.used_expression_ids||[]).join('、')||'无')} · 经验：${escape((c.used_lesson_ids||[]).join('、')||'无')}</small></article>`).join('')||'<p>未形成完整候选</p>'}<small>知识来源：${escape((r.sources.sources||[]).map(s=>s.name+' #'+s.position).join('、')||'群背景与聊天')}<br>语境已检查至：${escape(r.details?.validated_through?.at(-1)||'旧记录未保存')} · 策略 ${escape(r.details?.policy_version||'旧记录')}</small></section>`;
  }).join('')||empty('还没有候选择优记录'))+pager(data);
}

function overview(data) {
  const r = data.replies;
  const lessons = data.lessons.map(item => `<article class="lesson"><h3>${escape(item.scene)}${item.status==='suspended'?' · 暂停使用，待自动复核':''}</h3><p>${escape(item.advice)}</p><small>依据：${replyLinks(item.reply_ids)}</small></article>`).join('');
  const comparison = data.comparison.map(item => `<div><p>${phases[item.phase]} · ${item.samples} 条有效样本</p><strong>${num(item.participants)}</strong><small>平均新增接话人数</small><p>反感 ${percent(item.annoyance)} · 纠错 ${percent(item.correction)}<br>重复 ${percent(item.repetition)} · 首次发送 ${num(item.first_seconds)} 秒<br>完整结束 ${num(item.complete_seconds)} 秒<br>共 ${item.total} 条 · 未知 ${item.unknown} · 失败 ${item.failures} · 观察中 ${item.observing}</p></div>`).join('');
  return `<div class="metrics">${metric('已结算回复',num(r.settled||0),`${r.observing||0} 条观察中 · 共 ${r.total} 条`)}${metric('已确认用语',num(data.confirmed_terms),`本群共 ${data.terms} 个词条`)}${metric('可用表情包',num(data.images.ready||0),`${data.images.pending||0} 张待理解`)}${metric('成员画像',num(data.profiled_members),`${data.members} 位成员已有记录`)}</div>
    <div class="overview-grid"><div><section class="panel"><div class="section-head"><h2>本群的聊天画像</h2><small>${data.reviews} 轮经验整理</small></div><p class="section-note">依据本群已保存的词义、互动反馈和成员资料形成；基础人设由配置中的「${escape(current.persona||'默认人设')}」决定。</p><div class="chip-row">${data.topics.map(topic=>`<span class="chip">${escape(topic)}</span>`).join('')}</div>${lessons || empty('还没有形成群聊经验','需要真实回复结算后，由判断模型结合多条本群案例提炼。')}
    <p class="section-note">最近整理：${date(data.last_review)}</p></section><section class="panel"><div class="section-head"><h2>近期回复</h2><button class="link-button" data-open-tab="replies">查看全部</button></div>${data.recent.length?`<ul class="mini-list">${data.recent.map(item=>`<li><div><button class="link-button reply-text" data-reply="${item.id}">${escape(item.body||'尚无完整正文')}</button><div class="reply-meta">${date(item.created)} · ${outcomes[item.outcome||item.status]||escape(item.status)}</div></div>${score(item.score)}</li>`).join('')}</ul>`:empty('还没有回复记录','本群下一次真实回复会在开启观察后进入记录。')}</section></div>
    <div><section class="panel"><div class="section-head"><h2>原策略与学习策略</h2></div><p class="section-note">比较每次回复带来的不同群友接话，而非只看消息总量。</p><div class="comparison">${comparison}</div><p class="section-note">${escape(data.evaluation.evidence.message)}<br>近14天：${data.evaluation.evidence.paired_days} 个配对日期，${data.evaluation.evidence.paired_periods} 个配对时段。${data.evaluation.evidence.interval?`日均新增接话差异 ${num(data.evaluation.evidence.participants_delta)}，95%日级重采样区间 [${data.evaluation.evidence.interval.map(num).join('，')}]`:''}<br>${escape(data.evaluation.repeat_note)}<br>${data.evaluation.warnings.map(escape).join('<br>')}</p></section>
    <section class="panel"><div class="section-head"><h2>策略更新</h2><small>本群独立参数</small></div><ul class="mini-list"><li><span>实际行动</span><span>${data.actions} 次</span></li><li><span>已处理结算</span><span>${data.processed} 次</span></li><li><span>有效策略更新</span><span>${data.trained} 次</span></li></ul><p class="section-note">群安静、判断失败或发送不完整不作为低分训练。旧回复没有行动记录时，不补造训练样本。</p></section>
    ${evaluationPanel(data.evaluation)}<section class="panel"><div class="section-head"><h2>近 7 天记录</h2></div>${data.activity.length?data.activity.map(day=>`<div class="day-row"><span>${escape(day.day)}</span><span>${day.replies} 条 · 均分 ${num(day.score)}</span></div>`).join(''):'<p class="section-note">近 7 天尚无回复记录。</p>'}</section></div></div>`;
}
function evaluationPanel(data) {
  const separate=data.phases.filter(item=>['requested','scene_failed'].includes(item.phase));
  return `<section class="panel"><div class="section-head"><h2>自动检查与调用用量</h2><small>近 ${data.window_days} 天</small></div>
    <p class="section-note">${separate.map(item=>`${phases[item.phase]} ${item.total} 条`).join(' · ')}<br>${Object.entries(data.causes).map(([key,value])=>`${causes[key]||escape(key)} ${value} 人次`).join(' · ')||'尚无已判明的纠错或反感原因'}</p>
    <p class="section-note">${(data.versions||[]).map(item=>`策略 ${escape(item.version)}：${item.total} 条记录 / ${item.samples} 条有效，平均接话 ${num(item.participants)} 人，明确反感 ${item.annoyance} 条`).join('<br>')}</p>
    <ul class="mini-list">${data.costs.map(item=>`<li><div>${purposes[item.purpose]||escape(item.purpose)}<div class="reply-meta">${escape(item.provider)}</div></div><span>${item.calls} 次 / 请求失败 ${item.failures}<br>取消或超时中断 ${item.interrupted||0} / 历史未分类失败 ${item.legacy_failures||0}<br>均耗时 ${num(item.seconds)} 秒<br>已知Token ${num(item.input_tokens==null?null:item.input_tokens+item.cached_tokens+item.output_tokens)}<br>用量覆盖 ${item.usage_known}/${item.calls}</span></li>`).join('')||'<li>尚无新版调用记录</li>'}</ul>
    <p class="section-note">${escape(data.cost_note)}</p>
    <p class="section-note">${data.phase_costs.map(item=>`${phases[item.phase]||escape(item.phase)}：回复及选图/场景调用 ${item.calls} 次，失败 ${item.failures}`).join('<br>')}</p>
    <details><summary>最近配对时段</summary><ul class="mini-list">${data.periods.slice(-8).reverse().map(item=>`<li><span>${date(item.start)}<br>版本 ${escape(item.policy_version)}</span><span>原 ${item.baseline} / 学习 ${item.adaptive}<br>新增接话差 ${num(item.participants_delta)}</span></li>`).join('')||'<li>尚无相邻时段同时包含有效原策略和学习策略样本</li>'}</ul></details></section>`;
}
function presence(data) {
  return `<section class="panel"><h2>名片与头像</h2><p class="section-note">本群自动名片：${data.card_enabled?'开启':'关闭'} · 账号自动头像：${data.avatar_enabled?'开启':'关闭'} · 主形象：${escape(data.persona||'未指定')}。开关和时间间隔在设置中调整。账号头像所有群共用。</p>
    ${(data.states||[]).map(s=>`<article><h3>${s.key.startsWith('card:')?'本群名片':'账号头像'}</h3><p>${escape(s.current??'尚未确认')} · ${s.pending?'等待读回确认':s.paused?'暂停':'自动判断'}<br>${escape(s.reason||'')}<br>下次可判断：${s.next?date(s.next):'有新资料时'}</p><button class="button" data-presence-key="${escape(s.key)}" data-presence-action="${s.paused&&!s.pending?'resume':'pause'}">${s.paused&&!s.pending?'恢复自动判断':'暂停自动覆盖'}</button></article>`).join('')||'<p class="section-note">尚无记录。可开启自动流程，也可先手动补充头像候选。</p>'}</section>
    <section class="panel"><h2>头像候选</h2><p class="section-note">每轮最多补充5张合格图片，不立即换头像。${escape(data.operation_error||"")} ${data.operation?"正在"+(data.operation==="collect"?"补充候选":"更换头像")+"…":""}</p><button class="button" data-avatar-action="collect" ${data.operation?"disabled":""}>补充头像候选</button>${data.images.map(i=>`<article><span data-avatar="${i.id}" class="section-note">正在加载头像…</span><p>${escape(i.persona)} · ${escape(i.site)} #${escape(i.post_id)}<br>${escape(i.reason)}<br>${escape(i.source)}</p><button class="button" data-avatar-action="use" data-avatar-id="${i.id}" ${data.operation||i.persona!==data.persona?"disabled":""}>使用这张</button></article>`).join('')||'<p class="section-note">指定主形象后可点击补充候选，自动换头像开关可保持关闭。</p>'}</section>
    <section class="panel"><h2>最近动作</h2>${data.actions.map(a=>`<p>${date(a.created)} · ${escape(a.status)} · ${escape(a.value)}<br>${escape(a.reason)}</p>`).join('')||'<p class="section-note">暂无动作</p>'}</section>`;
}
function toolbar(options = '') {
  return `<form id="searchForm" class="toolbar"><input id="itemSearch" type="search" maxlength="200" value="${escape(query)}" placeholder="${tab==='members'?'搜索称呼、QQ 或画像':tab==='slang'?'搜索词语、释义或场景':'搜索回复或表情内容'}" aria-label="搜索当前资料">${options?`<select id="itemFilter" aria-label="筛选状态"><option value="">全部状态</option>${options}</select>`:''}<button class="button" type="submit">搜索</button></form>`;
}
function pager(data) {
  const pages = Math.max(1,Math.ceil(data.total/data.page_size));
  return `<div class="pager"><span>共 ${data.total} 条 · ${data.page} / ${pages} 页</span><button class="button" data-page="${data.page-1}" ${data.page<=1?'disabled':''}>上一页</button><button class="button" data-page="${data.page+1}" ${data.page>=pages?'disabled':''}>下一页</button></div>`;
}
function options(values) { return Object.entries(values).map(([value,label])=>`<option value="${value}" ${filter===value?'selected':''}>${label}</option>`).join(''); }
function replies(data) {
  return toolbar(options(outcomes).replace(/<option value="prepared"[^]*?<\/option>/,'')) + (data.items.length?`<div class="table-wrap"><table><thead><tr><th>回复与策略</th><th>互动分</th><th>结算状态</th><th class="reply-time">时间</th><th>依据</th></tr></thead><tbody>${data.items.map(item=>`<tr><td><span class="reply-text">${escape(item.body||'尚无完整正文')}</span><div class="reply-meta">#${item.id} · ${item.action?`${phases[item.action.phase]||escape(item.action.phase)} / ${escape(item.action.style)}`:'未记录行动'}${item.trained?' · 已处理结算':''}</div></td><td>${score(item.score)}</td><td>${badge(outcomes[item.outcome||item.status]||item.status,item.outcome==='feedback')}</td><td class="reply-time"><small>${date(item.created)}</small></td><td><button class="link-button" data-reply="${item.id}">展开</button></td></tr>`).join('')}</tbody></table></div>`:empty(query||filter?'没有符合条件的回复':'还没有回复评分','未结算、样本不足与判断失败都显示为「—」，不会冒充零分。')) + pager(data);
}
function stickers(data) {
  return toolbar(options(imageStates)) + '<p class="section-note">学习群共用表情包池，按当前群语境选图；使用和反馈次数按图片汇总。停用群仅展示本群归档。</p>' + (data.items.length?`<div class="gallery">${data.items.map(item=>`<article class="sticker"><div class="sticker-visual" data-image="${item.status==='ready'?item.id:''}"><span>${item.status==='ready'?'读取图片…':imageStates[item.status]}</span></div><div class="sticker-body"><div class="section-head">${badge(imageStates[item.status],item.status==='ready')}<small>#${item.id}</small></div><h3>${escape(item.description||'尚无可用描述')}</h3>${item.visible_text?`<p>图中文字：${escape(item.visible_text)}</p>`:''}<p class="muted">${escape(item.scene||'未形成适用场景')}</p><div class="chip-row">${item.tags.map(tag=>`<span class="chip">${escape(tag)}</span>`).join('')}</div><small>群友使用 ${item.seen} 次 · 机器人使用 ${item.uses} 次<br>有效反馈 ${item.reward_n} 次 · 平均奖励 ${item.reward_n?num(item.reward_sum/item.reward_n):'—'}</small></div></article>`).join('')}</div>`:empty(query||filter?'没有符合条件的表情包':'表情包池还没有图片','开启学习和表情包功能后，各群采集的合适图片进入共享池；普通照片、工作截图会被排除。')) + pager(data);
}
function slang(data) {
  const search=query.toLowerCase();
  const items=data.items.filter(item=>!search||JSON.stringify(item).toLowerCase().includes(search));
  return toolbar() + `<p class="result-count">${items.length} 个词条 · 候选释义仍在积累证据</p><div class="term-list">${items.map(item=>`<article class="panel"><div class="term-head"><h3>${escape(item.term)}</h3><small>${item.senses.length} 种语境</small></div>${item.senses.map(sense=>`<div class="sense">${badge(sense.confirmed?'已确认':'候选',sense.confirmed)} <small>置信度 ${percent(sense.confidence)} · ${sense.rounds} 轮观察</small><p>${escape(sense.meaning)}</p><p class="section-note">适用语境：${escape(sense.scene)}</p><div class="chip-row">${sense.clues.map(clue=>`<span class="chip">${escape(clue)}</span>`).join('')}</div><details><summary>查看 ${sense.evidence.length} 条原话证据</summary>${sense.evidence.map(e=>`<blockquote class="evidence">${escape(e.text)}<small>QQ ${escape(e.sender)} · ${date(e.ts)} · 消息 ${escape(e.message_id)}</small></blockquote>`).join('')}</details></div>`).join('')}</article>`).join('')||empty('还没有匹配的群内用语','词义需要结合本群真实上下文判断。缺少证据时保持候选，不按字面猜测。')}</div>`;
}
function members(data) {
  return toolbar() + `<p class="section-note">开启成员画像的群内，好感低于 20 时静默；每 3 小时恢复 1 点，到 20 解除静默，继续恢复至 50。高于 50 不随时间下降。好感与回复互动分分别计算。</p><br>` + (data.items.length?`<div class="member-grid">${data.items.map(item=>`<article class="panel member"><h3>${escape(item.name)}</h3><div class="member-id">QQ ${escape(item.user_id)}</div><p class="member-profile">${escape(item.profile||'尚未形成画像，正在积累发言。')}</p><div class="member-foot"><span>关系值 ${num(item.affinity)} / 100</span><span>${item.pending_samples} 条待总结</span></div>${item.silenced?`<p>${badge('静默中',false)} <small>预计解除：${date(item.silence_until)}</small></p>`:''}<small>最近总结：${date(item.last_update)}<br>最近发言：${date(item.last_seen)}</small></article>`).join('')}</div>`:empty('还没有成员画像','本群开启画像后，需要积累有效文字发言。这里不推测尚未观察到的成员信息。')) + pager(data);
}
function reactionIcon(emoji) {
  return emoji?.symbol ? `<span class="reaction-icon" aria-hidden="true">${escape(emoji.symbol)}</span>` : emoji?.image ? `<img class="reaction-icon" src="${escape(emoji.image)}" alt="${escape(emoji.name)}" loading="lazy" referrerpolicy="no-referrer">` : '<span class="reaction-icon">?</span>';
}
function reactions(data) {
  const options=(items,value)=>items.map(([id,label])=>`<option value="${escape(id)}" ${String(value)===String(id)?'selected':''}>${escape(label)}</option>`).join('');
  const controls=`<div class="toolbar reaction-filters"><label>统计范围<select id="reactionDays">${options([[7,'最近七天'],[30,'最近三十天']],reactionDays)}</select></label><label>表情<select id="reactionEmoji">${options([['','全部表情'],...data.emojis.map(e=>[e.emoji_id,e.emoji.name+' · '+e.emoji_id])],reactionEmoji)}</select></label><label>内容类别<select id="reactionCategory">${options([['','全部类别'],...data.categories.map(c=>[c,c])],reactionCategory)}</select></label><label>查看<select id="reactionView">${options([['summary','用法与分布'],['evidence','观察证据'],['actions','机器人动作']],reactionView)}</select></label></div>`;
  const counts=['pending','classified','uncertain','missing','failed'].map(k=>badge(`${reactionStates[k]} ${data.counts[k]||0}`)).join(' ');
  const header=`<p class="section-note">感知学习${data.enabled?'已开启':'已停用'} · 智能发送${data.auto_enabled?'已开启':'已停用'} · 判断间隔至少 ${data.interval} 秒，滚动十分钟最多 ${data.maximum} 次。跟贴次数不等于喜欢程度，未知身份不计入可核实人数。</p><div class="reaction-status">${counts} ${badge('累计淘汰 '+(data.meta.dropped||0))}</div><p class="section-note">最近分类 ${date(data.meta.classified_at)} · 最近提炼 ${date(data.meta.summarized_at)}${data.meta.last_error?' · '+escape(data.meta.last_error):''}</p>`;
  if(reactionView==='actions')return controls+header+`<h2 class="section-title">机器人动作</h2><p class="section-note">外部插件动作单列记录，不计入智能发送额度，也不作为真人偏好。</p>`+(data.actions.items.length?data.actions.items.map(a=>`<article class="panel reaction-record"><div class="reaction-heading">${reactionIcon(a.emoji)}<div><h3>${escape(a.emoji?.name||'未选择表情')} ${a.emoji_id?'<small>#'+escape(a.emoji_id)+'</small>':''}</h3><small>${date(a.created)} · ${a.source==='external'?'原有插件':'语义判断'} · ${reactionStates[a.status]||escape(a.status)} · ${num(a.seconds)} 秒</small></div></div><p>${escape(a.reason)}</p>${a.details?.target?`<blockquote>${escape(a.details.target.text||'图片消息')}</blockquote>`:''}<p class="section-note">目标 ${escape(a.message_id||'未选择')} · 参考用法 ${escape(a.details?.used_pattern_ids?.join('、')||'默认先验 / 未采用群内用法')} · 判断截至 ${escape(a.details?.validated_through||'—')}<br>后续反馈：${a.source==='external'?'仅记录动作':outcomes[a.outcome]||(a.status==='sent'?'观察中':'未形成效果证据')}</p>${(a.feedback||[]).map(f=>`<p>${badge(intents[f.intent]||f.intent)} ${escape(f.evidence_text)} <small>${escape(f.reason)}</small></p>`).join('')}</article>`).join(''):empty('还没有机器人回应动作','有新消息、满足频率并判断适合时，才会智能贴表情。'))+pager(data.actions);
  if(reactionView==='evidence')return controls+header+`<h2 class="section-title">回应观察证据</h2>`+(data.items.length?data.items.map(r=>`<article class="panel reaction-record"><div class="reaction-heading">${reactionIcon(r.emoji)}<div><h3>${escape(r.emoji.name)} <small>#${escape(r.emoji_id)}</small></h3><small>${date(r.updated)} · ${reactionStates[r.status]||escape(r.status)} · 当前 ${r.count} 个回应</small></div></div><blockquote>${escape(r.text||'缺少可理解原文，不推测内容类别')}</blockquote><p>${(r.categories||[]).map(c=>badge(c)).join(' ')} ${escape(r.meaning)}</p><p class="section-note">${escape(r.reason||r.error||'等待积累与分类')}<br>可核实真人 ${r.actors.length} 人 · ${r.actors.length?'QQ '+escape(r.actors.join('、')):'身份未知'}${r.participants.length?' · 平台标识 '+r.participants.length+' 个（不能等同 QQ 人数）':''} · 机器人回应 ${r.bot_count}<br>消息 ${escape(r.message_id)} · 来源 ${date(r.source_ts)}</p><details><summary>附近语境与适用边界</summary><p>${escape(r.boundary||'尚未提炼')}</p>${r.context.map(m=>`<p class="section-note">${escape(m.text)}</p>`).join('')}${r.image_descriptions.map(t=>`<p>${escape(t)}</p>`).join('')}</details></article>`).join(''):empty('等待回应证据','收到真人贴表情后积累；筛选无匹配时也会显示为空。'))+pager(data);
  const emojis=data.emojis.filter(e=>(!reactionEmoji||e.emoji_id===reactionEmoji)&&(!reactionCategory||e.categories[reactionCategory]));
  const cards=emojis.map(e=>`<article class="panel"><div class="reaction-heading">${reactionIcon(e.emoji)}<div><h3>${escape(e.emoji.name)} <small>#${escape(e.emoji_id)}</small></h3><small>${e.messages} 条被回应消息 · 可核实 ${e.verified_people} 人</small></div></div><div class="reaction-bars">${Object.entries(e.categories).map(([c,n])=>`<div><span>${escape(c)}</span><meter min="0" max="${e.messages}" value="${n}"></meter><span>${n}</span></div>`).join('')}</div><p class="section-note">最近使用 ${date(e.last_used)} · ${e.unknown_messages} 条尚无可核实操作者</p></article>`).join('');
  const patterns=data.patterns.items.map(p=>`<article class="panel reaction-record"><div class="reaction-heading">${reactionIcon(p.emoji)}<div><h3>${escape(p.emoji.name)} · ${escape(p.meaning)}</h3>${badge(p.status==='pending'?'待验证':reactionStates[p.status],p.status==='active')} <small>${escape(p.id)}</small></div></div><p>场景：${escape(p.scene)}</p><p>边界：${escape(p.boundary)}</p><details><summary>${p.evidence.length} 条必要证据</summary>${p.evidence.map(e=>`<blockquote>${escape(e.text)}<br><small>${date(e.source_ts)} · ${e.actors.length?'可核实 QQ '+escape(e.actors.join('、')):'身份未知'} · 消息 ${escape(e.message_id)}</small></blockquote>`).join('')}</details></article>`).join('');
  return controls+header+`<h2 class="section-title">真人常用表情与内容分布</h2>`+(cards?`<div class="reaction-grid">${cards}</div>`:empty('等待本群回应数据','默认示例不会计入真人使用统计。'))+`<h2 class="section-title">本群用法</h2>`+(patterns||empty('尚未形成可展示的群内用法','至少两名可核实真人、两个五分钟片段支持才会确认，单人刷屏不算群共识。'))+`<details class="panel"><summary>默认先验 · 不是本群已确认偏好</summary><div class="reaction-priors">${data.patterns.priors.map(p=>`<p>${reactionIcon(p.emoji)} <strong>${escape(p.emoji.name)} #${p.id}</strong> ${escape(p.meaning)}</p>`).join('')}</div></details>`;
}
async function load() {
  if(!current && tab!=='settings') return;
  if(!current) settingsArea='global';
  const token=++generation, params={...(current?scope():{}),page,q:query};
  if(tab==='settings') params.area=settingsArea;
  if(tab==='replies') params.outcome=filter;
  if(tab==='stickers') params.status=filter;
  if(tab==='reactions')Object.assign(params,{days:reactionDays,emoji:reactionEmoji,category:reactionCategory});
  $('error').hidden=true; $('content').setAttribute('aria-busy','true');
  $('content').innerHTML=empty('正在读取本群资料…');
  document.querySelectorAll('[data-tab]').forEach(button=>{
    button.classList.toggle('active',button.dataset.tab===tab);
    if(button.dataset.tab===tab) button.setAttribute('aria-current','page'); else button.removeAttribute('aria-current');
  });
  try {
    const selectedTab=tab;
    const data=selectedTab==='reactions'?await Promise.all([api('reactions',params),api('reaction_patterns',params),api('reaction_actions',params)]).then(([d,patterns,actions])=>({...d,patterns,actions})):await api(tab,params);
    if(token!==generation) return;
    $('content').innerHTML=({overview,replies,stickers,slang,members,background,choices,reactions,settings,presence})[selectedTab](data);
    if(selectedTab==='presence'&&data.operation)setTimeout(()=>{if(token===generation)load();},3000);
    if(selectedTab==='presence')for(const node of document.querySelectorAll('[data-avatar]')){
      api('avatar_image',{...params,id:node.dataset.avatar}).then(async data=>{
        const img=new Image(96,96);img.alt='头像候选';img.src=data.src;
        await img.decode();
        if(token===generation)node.replaceWith(img);
      }).catch(error=>{if(token===generation){node.textContent='头像加载失败';showError(error);}});
    }
    if(selectedTab==='settings'&&!current)document.querySelector('[data-settings-area="group"]').disabled=true;
    $('updated').textContent='更新于 '+new Date().toLocaleTimeString('zh-CN',{hour12:false});
    if(selectedTab==='stickers') loadImages(token,params);
  } catch(error) {if(token===generation){showError(error);$('content').innerHTML=empty('资料读取失败','请点击右上角刷新重试。');}}
  finally {if(token===generation)$('content').setAttribute('aria-busy','false');}
}
async function loadImages(token,params) {
  const images=[...document.querySelectorAll('[data-image]')].filter(node=>node.dataset.image);
  await Promise.all(images.map(async node=>{
    try {
      const data=await api('image',{platform:params.platform,group_id:params.group_id,id:node.dataset.image});
      if(token!==generation)return;
      const image=document.createElement('img');image.alt='表情包 #'+node.dataset.image;image.src=data.src;
      node.replaceChildren(image);
    }catch(error){if(token===generation)node.textContent=error.message;}
  }));
}
async function detail(id) {
  const selectedScope=scope(), key=current.platform+':'+current.group_id, token=++detailGeneration;
  $('detailTitle').textContent='回复 #'+id; $('detailContent').innerHTML=empty('正在读取评分依据…');
  if(!$('detail').open)$('detail').showModal();
  try {
    const data=await api('reply',{...selectedScope,id});
    if(token!==detailGeneration||key!==current.platform+':'+current.group_id||!$('detail').open)return;
    const context=data.context?.map(item=>typeof item==='string'?item:[item.sender,item.text||item.content].filter(Boolean).join('：')).join('\n')||'没有保留的触发上下文';
    const summary=data.summary||{};
    $('detailContent').innerHTML=`<div class="detail-meta"><span>${date(data.created)}</span><span>${outcomes[data.outcome||data.status]||escape(data.status)}</span><span>互动分 ${num(data.score)}</span></div><section><h3>触发上下文</h3><div class="message-body">${escape(context)}</div></section><section><h3>机器人实际回复</h3><div class="message-body">${escape(data.body||'尚无完整正文')}</div><p class="section-note">QQ 发送 ID：${escape(data.outgoing.join('、')||'尚无发送回执')}</p></section><section><h3>行动与结算</h3><p>${data.action?`${phases[data.action.phase]||escape(data.action.phase)} / ${escape(data.action.style)} / ${{text:'文字',caption:'配文与图片',sticker:'纯表情包'}[data.action.format]||escape(data.action.format)}`:'未记录行动，不用于补造策略训练'}</p><div class="detail-meta"><span>新增接话 ${summary.new_participants??'—'} 人</span><span>明确反感 ${summary.annoyance_people??'—'} 人</span><span>纠错 ${summary.correction_people??'—'} 人</span></div>${data.action?`<p class="section-note">${data.candidates.length} 个候选 · 选择概率 ${percent(data.propensity)} · ${data.trained?'已处理结算':'尚未处理结算'}</p>`:''}</section><section><h3>群友反馈与判断依据</h3>${data.feedback.length?data.feedback.map(item=>`<article class="feedback-item"><div>${badge(intents[item.intent]||item.intent)} <small>${{direct:'直接引用',indirect:'间接引用',semantic:'话题相关'}[item.method]||escape(item.method)} · 权重 ${num(item.weight)} · 归属置信度 ${percent(item.attribution_confidence)} · 意图置信度 ${percent(item.intent_confidence)}</small></div><p>${escape(item.text||'没有保留文字正文')}</p><div class="reason">判断理由：${escape(item.reason)}<br>原因：${causes[item.cause]||'历史记录未知'} · ${percent(item.cause_confidence)}${item.evidence_text?' · 原话：'+escape(item.evidence_text):''}<br>判断模型：${escape(item.judge_provider||'历史记录未保存')}<br>QQ ${escape(item.sender||'未知')} · 消息 ${escape(item.message_id)}${item.parent?' · 引用 '+escape(item.parent):''}</div></article>`).join(''):'<p class="section-note">尚无可归因的反馈。这不等同于零分。</p>'}</section>`;
  } catch(error){if(token===detailGeneration&&key===current.platform+':'+current.group_id&&$('detail').open)$('detailContent').innerHTML=empty('无法读取这条回复',error.message);}
}
async function refresh() {
  $('refresh').disabled=true;
  try {
    const data=await api('bootstrap'); groups=data.groups;
    $('account').textContent=data.accounts.length?data.accounts.map(a=>a.name+' · '+a.user_id).join(' / '):'暂未连接机器人账号';
    const selected=groups.find(g=>current&&g.platform===current.platform&&g.group_id===current.group_id)
      ||groups.find(g=>g.allowed&&g.feedback)||groups.find(g=>g.allowed)||groups[0];
    if(selected){current=selected;renderGroups();renderHeader();await load();}
    else{$('groupTitle').textContent='尚未配置群聊';if(tab==='settings')await load();else{$('content').innerHTML=empty('先配置一个群','在 QQ 聊天控制的群规则中添加要查看的群聊。');$('content').setAttribute('aria-busy','false');}}
  } catch(error){showError(error);} finally{$('refresh').disabled=false;}
}
document.addEventListener('click',async event=>{
  const button=event.target.closest('button');if(!button)return;
  if(button.dataset.avatarAction){
    const action=button.dataset.avatarAction;
    if(action==='use'&&!window.confirm('将使用这张图片更换账号头像，所有群都会看到。确定更换？'))return;
    button.disabled=true;
    try{const r=await bridge.apiPost('learning/presence_control',{...scope(),action,image_id:action==='use'?Number(button.dataset.avatarId):null,confirm:action==='use'});if(r?.ok===false)throw new Error(r.message);await load();}catch(error){showError(error);button.disabled=false;}
    return;
  }
  if(button.dataset.presenceKey){
    button.disabled=true;
    try{const r=await bridge.apiPost('learning/presence_control',{...scope(),key:button.dataset.presenceKey,action:button.dataset.presenceAction});if(r?.ok===false)throw new Error(r.message);await load();}catch(error){showError(error);button.disabled=false;}
    return;
  }
  if((button.dataset.group||button.dataset.tab||button.dataset.openTab||button.dataset.settingsArea)&&!leaveSettings())return;
  if(button.dataset.settingsArea){settingsArea=button.dataset.settingsArea;await load();return;}
  if(button.dataset.group){const group=groups.find(g=>g.platform+':'+g.group_id===button.dataset.group);if(group)await choose(group);}
  else if(button.dataset.tab||button.dataset.openTab){tab=button.dataset.tab||button.dataset.openTab;query='';filter='';page=1;await load();}
  else if(button.dataset.reply)await detail(button.dataset.reply);
  else if(button.dataset.page){page=Number(button.dataset.page);await load();}
});
document.addEventListener('submit',async event=>{
  if(event.target.id==='settingsForm'){
    event.preventDefault();$('error').hidden=true;
    try{if(await saveSettings(bridge,current?scope():{}))await refresh();}catch(error){showError(error);}
    return;
  }
  if(event.target.id!=='searchForm')return;event.preventDefault();
  query=$('itemSearch').value.trim();filter=$('itemFilter')?.value||'';page=1;await load();
});
document.addEventListener('change',async event=>{
  if(event.target.closest('#settingsForm')){settingsChanged();return;}
  if(['reactionDays','reactionEmoji','reactionCategory','reactionView'].includes(event.target.id)){
    reactionDays=Number($('reactionDays').value);reactionEmoji=$('reactionEmoji').value;reactionCategory=$('reactionCategory').value;reactionView=$('reactionView').value;page=1;await load();return;
  }
  if(event.target.id==='itemFilter'){filter=event.target.value;query=$('itemSearch').value.trim();page=1;await load();}
});
$('groupSearch').addEventListener('input',renderGroups);
document.addEventListener('input',event=>{if(event.target.closest('#settingsForm'))settingsChanged();});
$('refresh').addEventListener('click',()=>{if(leaveSettings())refresh();});
$('closeDetail').addEventListener('click',()=>$('detail').close());
async function init(){
  try {
    if(!bridge)throw new Error('请从 AstrBot 后台 → 插件 → QQ 聊天控制 → learning 页面打开看板。');
    applyTheme(await bridge.ready());bridge.onContext(applyTheme);await refresh();
  }catch(error){showError(error);$('content').innerHTML=empty('看板未连接',error.message);$('content').setAttribute('aria-busy','false');}
}
await init();
