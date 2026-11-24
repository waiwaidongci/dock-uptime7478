import {api, download} from '../api/client.js';
import {errorSlot} from '../components/common/layout.js';
import {table} from '../components/common/table.js';
import {amendmentActions, reissueStates} from '../constants/enums.js';
import {bindForm} from '../hooks/forms.js';
import {state} from '../stores/session.js';
import {esc, time} from '../utils/html.js';

export async function amendmentsPage() {
  const profile = state.profile;
  const [entries, batches] = await Promise.all([api('/amendments'), api('/reissues')]);
  const uploader = state.account.role === 'analyst'
    ? `<form id="amend-upload" class="inline-form"><label>修订 CSV<input name="file" type="file" accept=".csv,text/csv" required></label><button type="submit">登记修订并重编</button>${errorSlot}</form>`
    : '<p class="hint">分析员可导入维修班补报的修订 CSV。</p>';
  const rules = profile.amendment_rules?.map(rule => `<li>${esc(rule)}</li>`).join('') ?? '';
  const amendmentTable = table(entries.map(row => ({
    月台: row.dock,
    停机类型: row.kind,
    单号: row.record_id,
    修订号: row.revision_no,
    动作: amendmentActions[row.action] ?? row.action,
    改期开始: row.start ?? '—',
    改期结束: row.end ?? '—',
    状态: row.terminal_cancel ? '终态撤销' : (row.is_effective ? '当前生效' : '已被高修订号覆盖'),
    登记人: row.owner,
    登记时间: time(row.created),
  })));
  const batchTable = table(batches.map(batch => ({
    批次: {link: `#6/${batch.id}`, text: `#${batch.id}`},
    受影响月份: (batch.months || []).join('、'),
    状态: reissueStates[batch.state] ?? batch.state,
    修订条数: (batch.amendment_ids || []).length,
    创建: time(batch.created),
  })));
  return `<section><h2>维修班停机工单修订</h2><p class="hint">月报寄出后补报的改期或撤销按（月台、停机类型、单号）识别，乱序记录以最高修订号为准。</p><ul class="rules">${rules}</ul>${uploader}<button type="button" id="amend-example" class="secondary">下载修订样例 CSV</button><p id="amend-error" role="alert" class="error"></p></section><section><h2>修订流水</h2>${amendmentTable}</section><section><h2>重编批次</h2>${batchTable}</section>`;
}

export function bindAmendments(root, refresh) {
  bindForm(root, '#amend-upload', async form => {
    const file = form.elements.file.files[0];
    const result = await api('/amendments', {csv: await file.text()});
    state.status = `已登记 ${result.amendments.length} 条修订，重编月份：${result.months.join('、')}。`;
    location.hash = `6/${result.id}`;
  }, refresh);
  root.querySelector('#amend-example')?.addEventListener('click', async () => {
    try {await download('/amendments/example.csv', 'ningbo-dock-amendments-example.csv');}
    catch (error) {root.querySelector('#amend-error').textContent = error.message;}
  });
}
