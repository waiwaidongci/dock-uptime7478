import {api, download} from '../api/client.js';
import {empty, errorSlot} from '../components/common/layout.js';
import {table} from '../components/common/table.js';
import {bindForm} from '../hooks/forms.js';
import {state} from '../stores/session.js';
import {esc, time} from '../utils/html.js';

async function monthOptions(selected) {
  const batches = await api('/reissues');
  const months = [...new Set(batches.flatMap(batch => batch.months || []))].sort().reverse();
  const chosen = selected || months[0] || state.profile.options?.[0]?.default || '2026-10';
  return {months, chosen};
}

function diffView(diff) {
  if (!diff) return '<p class="hint">该版本为月份首版，无前后对比。</p>';
  const b = diff.monthly_before;
  const a = diff.monthly_after;
  const rows = diff.changes.map(change => ({
    月台: change.dock,
    停机类型: change.kind,
    单号: change.record_id,
    核对状态: change.status,
    班内停机秒: `${change.before_in_shift_seconds} → ${change.after_in_shift_seconds}`,
    班内秒变化: change.delta_in_shift_seconds,
    月内停机秒: `${change.before_total_seconds} → ${change.after_total_seconds}`,
  }));
  return `<h3>按工单核对停机秒数（${esc(diff.month)}）</h3>${table([{
    排班秒: `${b.scheduled_seconds} → ${a.scheduled_seconds}`,
    计划停机秒: `${b.planned_seconds} → ${a.planned_seconds}`,
    非计划停机秒: `${b.unplanned_seconds} → ${a.unplanned_seconds}`,
    班外停机秒: `${b.outside_seconds} → ${a.outside_seconds}`,
    可用秒: `${b.available_seconds} → ${a.available_seconds}`,
    可用率: `${b.rate ?? '—'}% → ${a.rate ?? '—'}%`,
  }])}${table(rows)}`;
}

async function inboxSection() {
  const box = await api('/delivery');
  const reviewer = state.account.role === 'reviewer';
  const stop = reviewer && box.schedules.some(row => row.active)
    ? `<form id="stop" class="inline-form"><label>启用中的计划<select name="schedule">${box.schedules
        .filter(row => row.active)
        .map(row => `<option value="${row.id}">#${row.id} · ${esc(row.recipient)}</option>`).join('')}</select></label><button type="submit" class="secondary">停止计划</button>${errorSlot}</form>`
    : '';
  const planTable = table(box.schedules.map(row => ({
    计划: row.id,
    任务: row.run_id,
    收件组: row.recipient,
    频率: row.cadence === 'daily' ? '每日' : '单次',
    计划时间: time(row.next_at),
    状态: row.active ? '启用' : '关闭',
  })));
  const inboxTable = table(box.deliveries.map(row => ({
    投递: row.id,
    月份: row.month ?? '—',
    收件组: row.recipient,
    报告: {link: `#4/${row.run_id}`, text: `分析 #${row.run_id}`},
    来源: row.reissue_id ? {link: `#6/${row.reissue_id}`, text: `重编 #${row.reissue_id} 补发`} : '计划投递',    入箱时间: time(row.delivered),
  })));
  return `${planTable}${stop}<p class="hint">重编切换后按历史收件组补发的新版月报也在这里；同批次同月份同收件组只入箱一份。</p>${inboxTable}`;
}

export async function versionsPage() {
  const selected = state.month;
  const {months, chosen} = await monthOptions(selected);
  if (!months.length) {
    return `<section><h2>月报版本</h2>${empty('还没有重编批次。先在「停机工单修订」导入修订，批准切换后这里会出现月报版本。', '#2', '去登记修订')}</section><section><h2>站内收件箱与投递计划</h2>${await inboxSection()}</section>`;
  }
  const picker = `<form id="month-pick" class="inline-form"><label>月份<select name="month">${months
      .map(month => `<option value="${esc(month)}" ${month === chosen ? 'selected' : ''}>${esc(month)}</option>`).join('')}</select></label></form>`;
  let payload;
  try {
    payload = await api(`/reports/months/${chosen}/versions`);
  } catch (error) {
    return `<section><h2>月报版本</h2>${picker}<p class="error">${esc(error.message)}</p></section>`;
  }
  const reviewer = state.account.role === 'reviewer';
  const versionRows = payload.versions.map(version => ({
    版本: `v${version.version_no}`,
    分析任务: {link: `#4/${version.run_id}`, text: `#${version.run_id}`},
    状态: version.current ? '当前生效' : '历史版本（已寄出，仍可下载）',
    收件组: (version.recipient_snapshot || []).join('、') || '—',
    切换时间: time(version.created),
    下载: {link: `/api/reports/versions/${version.id}/report.csv`, text: '原版 CSV'},
  }));
  const current = payload.current_version;
  const scheduleForm = reviewer && current
    ? `<form id="version-schedule" class="inline-form" data-run="${current.run_id}"><label>站内收件组<input name="recipient" value="${esc(state.profile.recipient)}" minlength="2" maxlength="100" required></label><label>频率<select name="cadence"><option value="once">单次</option><option value="daily">每日同一时间</option></select></label><label>首次投递时间<input name="scheduled_at" type="datetime-local"></label><button type="submit">按当前版本建立投递</button>${errorSlot}</form>`
    : '';
  return `<section><h2>${esc(chosen)} 月报版本</h2>${picker}<p class="hint">每次重编批准后生成新版本并一次性切换；历史版本行永久保留，已寄出的月报仍可按原版下载。</p>${table(versionRows)}${scheduleForm}<p id="version-error" class="error" role="alert"></p></section><section>${diffView(payload.diff)}</section><section><h2>站内收件箱与投递计划</h2>${await inboxSection()}</section>`;
}

export function bindVersions(root, refresh) {
  root.querySelector('#month-pick select')?.addEventListener('change', event => {
    location.hash = `7/${event.target.value}`;
  });
  bindForm(root, '#version-schedule', async form => {
    const data = Object.fromEntries(new FormData(form));
    data.scheduled_at = data.scheduled_at ? new Date(data.scheduled_at).toISOString() : null;
    await api(`/runs/${form.dataset.run}/schedules`, data);
    state.status = '投递计划已建立。';
  }, refresh);
  bindForm(root, '#stop', async form => {
    await api(`/schedules/${form.elements.schedule.value}`, null, 'DELETE');
    state.status = '已停止投递。';
  }, refresh);
  root.querySelectorAll('a[href^="/api/"]').forEach(link => {
    link.addEventListener('click', async event => {
      event.preventDefault();
      try {
        await download(link.getAttribute('href').replace('/api', ''),
                       `${state.profile.title}-版本${Date.now()}.csv`);
      } catch (error) {
        root.querySelector('#version-error').textContent = error.message;
      }
    });
  });
}
