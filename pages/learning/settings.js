let snapshot = null, dirty = false, saving = false, savedMessage = '';
const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

function field(meta, value) {
  const id = 'setting-' + meta.key, description = id + '-hint';
  const attrs = `id="${id}" name="${meta.key}" aria-describedby="${description}"`;
  let control;
  if (meta.type === 'bool') {
    control = `<label class="setting-check"><input ${attrs} type="checkbox" ${value?'checked':''}>开启</label>`;
  } else if (meta.choices) {
    const choices = [...meta.choices];
    if (!choices.some(c => c.value === value)) choices.push({value, label:value + '（当前值，未启用）'});
    control = `<select ${attrs}>${choices.map(c=>`<option value="${escape(c.value)}" ${c.value===value?'selected':''}>${escape(c.label)}</option>`).join('')}</select>`;
  } else if (meta.type === 'list') {
    control = `<textarea ${attrs} rows="3" placeholder="每行一个，也可用逗号分隔">${escape(value.join('\n'))}</textarea>`;
  } else {
    const numeric = ['int','float'].includes(meta.type);
    control = `<input ${attrs} type="${numeric?'number':'text'}" value="${escape(value)}" ${numeric?`required step="${meta.type==='int'?1:'any'}"`:''} ${meta.min!=null?`min="${meta.min}"`:''} ${meta.max!=null?`max="${meta.max}"`:''}>`;
  }
  return `<div class="setting-row"><div><label for="${id}">${escape(meta.label)}</label><p id="${description}" class="section-note">${escape(meta.hint || (meta.type==='list'?'每行一个，也可用逗号分隔。':''))}</p></div><div class="setting-control">${control}</div></div>`;
}

export function settings(data) {
  snapshot = data; dirty = false;
  const message = savedMessage || '修改后点击保存生效'; savedMessage = '';
  return `<div class="settings-switch" role="group" aria-label="设置范围"><button type="button" class="button" data-settings-area="group" aria-pressed="${data.area==='group'}">本群设置</button><button type="button" class="button" data-settings-area="global" aria-pressed="${data.area==='global'}">通用设置</button></div>
    <p class="section-note settings-intro">${data.area==='group'?'作用于左侧选中的群。群授权在通用设置的“允许聊天的群”中管理；平台和群号在 AstrBot 插件配置页维护。':'作用于整个插件。修改模型或权限前请核对范围；单独设置了短期记忆的群继续使用本群数值。'}保存会热重载插件，进行中的回复可能中断。</p>
    <form id="settingsForm">${data.sections.map((s,i)=>`<details class="settings-section" ${i<2?'open':''}><summary>${escape(s.title)}</summary>${s.fields.map(m=>field(m,data.values[m.key])).join('')}</details>`).join('')}
    <p class="section-note settings-fixed">${escape(data.fixed_affinity)}</p><div class="settings-save"><span id="settingsStatus" role="status">${escape(message)}</span><button type="submit" id="saveSettings" class="button" disabled>保存并生效</button></div></form>`;
}

function changes() {
  const form = document.getElementById('settingsForm'), result = {};
  if (!form || !snapshot) return result;
  for (const meta of snapshot.sections.flatMap(s=>s.fields)) {
    const input = form.elements.namedItem(meta.key);
    const value = meta.type==='bool'?input.checked:meta.type==='list'?input.value.split(/[\n,，]/).map(x=>x.trim()).filter(Boolean):['int','float'].includes(meta.type)?Number(input.value):input.value.trim();
    if (!same(value, snapshot.values[meta.key])) result[meta.key] = value;
  }
  return result;
}

export function settingsChanged() {
  if (saving || !document.getElementById('settingsForm')) return;
  dirty = Object.keys(changes()).length > 0;
  document.getElementById('saveSettings').disabled = !dirty;
  document.getElementById('settingsStatus').textContent = dirty ? '有修改尚未保存' : '修改后点击保存生效';
}

export function leaveSettings() {
  if (saving) return false;
  if (dirty && !window.confirm('有设置尚未保存，放弃修改并离开？')) return false;
  dirty = false;
  return true;
}

export async function saveSettings(bridge, scope) {
  if (saving || !snapshot || !document.getElementById('settingsForm').reportValidity()) return false;
  const updated = changes();
  const confirmPersona = !Object.hasOwn(updated,'persona_id') || window.confirm('更改或解除群人设会永久清理本群所有成员的聊天记录。确认保存？');
  if (!confirmPersona) return false;
  const controls = [...document.getElementById('settingsForm').elements];
  saving = true; controls.forEach(c=>c.disabled=true);
  document.getElementById('settingsStatus').textContent = '正在保存并重载插件…';
  try {
    const response = await bridge.apiPost('learning/save_settings', {...scope, area:snapshot.area, changes:updated,
      original:Object.fromEntries(Object.keys(updated).map(k=>[k,snapshot.values[k]])), confirm_persona_change:confirmPersona});
    if (response?.ok === false) throw new Error(response.message || '保存失败');
    savedMessage = (response?.data ?? response).message;
    dirty = false;
    return true;
  } finally {
    saving = false; controls.forEach(c=>c.disabled=false);
    if (dirty) settingsChanged();
  }
}

window.addEventListener('beforeunload', event=>{
  if (dirty || saving) { event.preventDefault(); event.returnValue = ''; }
});
