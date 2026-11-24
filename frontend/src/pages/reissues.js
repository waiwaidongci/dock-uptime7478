import {api} from '../api/client.js';
import {empty, errorSlot} from '../components/common/layout.js';
import {table} from '../components/common/table.js';
import {decisions, reissueRunStates, reissueStates} from '../constants/enums.js';
import {bindForm} from '../hooks/forms.js';
import {state} from '../stores/session.js';
import {esc, time} from '../utils/html.js';

function metricDiff(diff) {
  if (!diff) return '';
  const before = diff.monthly_before;
  const after = diff.monthly_after;
  const rateDelta = before.rate == null || after.rate == null ? '—' : (after.rate - before.rate).toFixed(4);
  return table([{
    月份: diff.month,
    排班秒: `${before.scheduled_seconds} → ${after.scheduled_seconds}`,
    计划停机秒: `${before.planned_seconds} → ${after.planned_seconds}`,
    非计划停机秒: `${before.unplanned_seconds} → ${after.unplanned_seconds}`,
    班外停机秒: `${before.outside_seconds} → ${after.outside_seconds}`,
    可用秒: `${before.available_seconds} → ${after.available_seconds}`,
    可用率: `${before.rate ?? '—'}% → ${after.rate ?? '—'}%（Δ ${rateDelta} 个百分点）`,
  }]);
}

function diffCard(item) {
  if (!item.diff) {
    return `<p class="hint">${esc(item.month)} 暂无可对比的秒数差异。</p>`;
  }
  const rows = item.diff.changes.map(change => ({
    月台: change.dock,
    停机类型: change.kind,
    单号: change.record_id,
    核对状态: change.status,
    班内停机秒: `${change.before_in_shift_seconds} → ${change.after_in_shift_seconds}`,
    班内秒变化: change.delta_in_shift_seconds,
    月内停机秒: `${change.before_total_seconds} → ${change.after_total_seconds}`,
  }));
  return `<h4>仓配按工单核对（${esc(item.month)}）</h4>${metricDiff(item.diff)}${table(rows)}`;
}

export async function reissuesPage() {
  const batches = await api('/reissues');
  const detailId = state.runId;
  if (!detailId) {
    return `<section><h2>月报重编批次</h2><p class="hint">一次修订导入触发一次重编，覆盖全部受影响月份（含改期移出的旧月）；全部月份重算成功后才能批准一次性切换。</p>${table(batches.map(batch => ({
      批次: {link: `#6/${batch.id}`, text: `#${batch.id}`},
      受影响月份: (batch.months || []).join('、'),
      状态: reissueStates[batch.state] ?? batch.state,
      复核: batch.decision ? (decisions[batch.decision] ?? batch.decision) : '待复核',
      创建: time(batch.created),
    })))}</section>`;
  }
  const listed = batches.find(item => item.id === detailId);
  if (!listed && !batches.length) return empty('还没有重编批次。', '#2', '去登记修订');
  const full = await api(`/reissues/${detailId}`);
  if (!full) return empty('重编批次不存在。', '#6', '返回重编列表');
  const reviewer = state.account.role === 'reviewer';
  const analyst = state.account.role === 'analyst';
  const monthTable = table(full.months_detail.map(item => ({
    月份: item.month,
    重算状态: reissueRunStates[item.state] ?? item.state,
    新版本号: item.version_no || '—',
    新分析任务: item.run_id ? {link: `#4/${item.run_id}`, text: `#${item.run_id}`} : '—',
    错误: item.error ?? '—',
  })));
  const amendmentTable = table((full.amendments_detail || []).map(row => ({
    月台: row.dock, 停机类型: row.kind, 单号: row.record_id, 修订号: row.revision_no,
    动作: row.action, 新区间: row.start ? `${row.start} ~ ${row.end}` : '—',
  })));
  let actions = '';
  if (full.state === 'failed' && analyst) {
    actions = `<form id="retry" class="inline-form"><button type="submit">重试失败月份</button>${errorSlot}</form><p class="hint">已成功的月份保留计算结果，重试不会重复发布。</p>`;
  }
  if (full.state === 'ready' && reviewer && !full.decision) {
    actions = `<form id="reissue-review"><label>复核意见<textarea name="note" required minlength="2" maxlength="1000"></textarea></label><button type="submit" value="approved">批准并一次性切换月报</button> <button type="submit" value="returned" class="secondary">退回复算</button>${errorSlot}</form>`;
  }
  const reviewNote = full.decision
    ? `<p class="notice">${decisions[full.decision] ?? full.decision} · ${esc(full.reviewer ?? '')} · ${time(full.reviewed)}<br>${esc(full.review_note ?? '')}</p>` : '';
  const errorNote = full.error ? `<p class="error" role="alert">${esc(full.error)}</p><p class="hint">重编失败期间全套旧版月报保持生效，已寄出的旧版仍可下载。</p>` : '';
  const diffs = full.months_detail.map(diffCard).join('');
  const downloads = full.decision === 'approved'
    ? `<p class="hint">切换后可到 <a href="#7/${esc(full.months[0])}">月报版本与分发</a> 下载历史版本或查收补发。</p>` : '';
  return `<section><h2>重编批次 #${full.id} · ${reissueStates[full.state] ?? full.state}</h2><p class="hint">受影响月份：${(full.months || []).map(esc).join('、')}</p>${errorNote}${reviewNote}${actions}${downloads}</section><section><h3>月份重算与切换</h3>${monthTable}</section><section><h3>本批修订</h3>${amendmentTable}</section><section><h3>停机秒数变化</h3>${diffs}</section>`;
}

export function bindReissues(root, refresh) {
  bindForm(root, '#retry', async () => {
    const id = location.hash.split('/')[1];
    await api(`/reissues/${id}/retry`, {});
    state.status = '已重新排队，仅失败月份会补算。';
  }, refresh);
  bindForm(root, '#reissue-review', async (form, button) => {
    const id = location.hash.split('/')[1];
    await api(`/reissues/${id}/review`, {decision: button.value, note: form.elements.note.value});
    state.status = button.value === 'approved' ? '受影响月报已一次性切换新版。' : '批次已退回。';
  }, refresh);
}
