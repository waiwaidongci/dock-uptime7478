import {api} from '../api/client.js';
import {empty, errorSlot} from '../components/common/layout.js';
import {decisions} from '../constants/enums.js';
import {bindForm} from '../hooks/forms.js';
import {state} from '../stores/session.js';
import {esc, time} from '../utils/html.js';
import {chooseRun, currentRun} from './analysis.js';

export async function review() {
  const {runs, run} = await currentRun();
  if (!run) return empty('先运行分析。', '#3', '配置分析');
  const gate = run.result?.can_publish === false ? '<p class="notice">当前结果未达到发布门槛，可退回复算。</p>' : '';
  const form = run.result && !run.decision && state.account.role === 'reviewer'
    ? `<form id="review" data-id="${run.id}"><label>复核意见<textarea name="note" required minlength="2" maxlength="1000"></textarea></label><button type="submit" value="approved" ${gate ? 'disabled' : ''}>批准发布</button> <button type="submit" value="returned" class="secondary">退回复算</button>${errorSlot}</form>`
    : '';
  return `<section>${chooseRun(runs, run.id)}<h2>${esc(state.profile.decision)}</h2><p>${esc(run.result?.headline || '分析完成后可复核。')}</p>${gate}${run.decision ? `<p class="notice">${decisions[run.decision]} · ${esc(run.reviewer)} · ${time(run.reviewed)}<br>${esc(run.review_note)}</p>` : ''}${form}<p><a href="#4/${run.id}">打开结果及口径</a> · <a href="#7">查看月报版本与分发</a></p></section>`;
}

export function bindDispatch(root, refresh) {
  bindForm(root, '#review', async (form, button) => {
    await api(`/runs/${form.dataset.id}/review`, {decision: button.value, note: form.elements.note.value});
    state.status = '复核意见已保存。';
  }, refresh);
}
