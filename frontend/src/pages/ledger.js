import { get, post } from '../api/client.js';
import { table } from '../components/common/data.js';
import { intro } from '../components/common/frame.js';
import { formError } from '../components/common/forms.js';
import { Role, statusName } from '../constants/enums.js';
import { bindForm, notify } from '../hooks/actions.js';
import { esc } from '../utils/html.js';

const voucherFields = [
  {key:'voucher_no', label:'更正单号', example:'GZ-LT-20261123-01'},
  {key:'source_batch', label:'错记扣回批次', example:'GZ-2611-01'},
  {key:'target_batch', label:'补记入账批次', example:'YX-2611-02'},
  {key:'amount_cents', label:'物料费（整数分）', example:'12503'},
  {key:'reason', label:'更正原因', required:false, example:'领退料台账错记调整'},
];

function control(field) {
  const required = field.required === false ? '' : 'required';
  return `<label class="field"><span>${esc(field.label)}</span>
    <input type="text" name="${esc(field.key)}" ${required} placeholder="${esc(field.example ?? '')}"></label>`;
}

function formValues(form) {
  const data = new FormData(form);
  const payload = {};
  for (const field of voucherFields) {
    const raw = data.get(field.key);
    if (raw === null || raw === '') continue;
    payload[field.key] = field.key === 'amount_cents' ? Number(raw) : raw;
  }
  return payload;
}

const statusClass = value => `<span class="status status-${esc(value)}">${esc(statusName[value] || value)}</span>`;

export async function ledgerPage(spec, user) {
  const writable = user.role !== Role.VIEWER;
  const [vouchers, reconcile, accounts] = await Promise.all([
    get('/ledger/vouchers?limit=50'),
    get('/ledger/reconcile'),
    get('/ledger/accounts'),
  ]);
  const voucherColumns = [
    {key:'voucher_no', label:'更正单号'}, {key:'source_batch', label:'扣回批次'},
    {key:'target_batch', label:'补入批次'}, {key:'amount_cents', label:'金额（分）'},
    {key:'status', label:'状态', html: row => statusClass(row.status)},
    {key:'attempts', label:'重试次数'}, {key:'last_error', label:'备注'},
  ];
  const accountColumns = [
    {key:'batch_code', label:'批次'}, {key:'balance_cents', label:'物料余额（分）'},
    {key:'version', label:'版本'},
  ];
  return `${intro('领退料更正单', '把错记的物料费从一批线香双边挪到另一批，借贷同生共死，总额对账到分。余额不足自动挂起，补足后重试，原单号重试不重复扣款。',
    `<span class="count">${reconcile.waiting_count} <small>张挂起</small></span>`)}
  <section class="panel"><div class="panel-heading"><h2>对账</h2></div>
    <div class="metric-grid">
      <div><b>${esc(reconcile.debit_total_cents)}</b><span>借方补入合计（分）</span></div>
      <div><b>${esc(reconcile.credit_total_cents)}</b><span>贷方扣回合计（分）</span></div>
      <div><b class="${reconcile.difference_cents === 0 ? 'ok' : 'warn'}">${esc(reconcile.difference_cents)}</b><span>对账差额（分）</span></div>
      <div><b>${esc(reconcile.account_total_cents)}</b><span>账户余额合计（分）</span></div>
    </div></section>
  ${writable ? `<div class="tools"><details><summary>登记领退料更正单</summary><form id="voucher-form"><div class="form-grid">
    ${voucherFields.map(control).join('')}</div>${formError}<button class="primary">提交并入账</button></form></details>
    <details><summary>手动重试挂起单</summary><form id="retry-form"><p>worker 每轮也会自动重试；手动触发可立即解开已补足的单据。</p><button class="primary" type="button" id="retry-btn">立即重试</button>${formError}</form></details></div>` : ''}
  <section class="panel"><div class="panel-heading"><h2>更正单</h2></div>
    ${table(voucherColumns, vouchers.items, '暂无更正单。分析员或主管可在上方登记。')}</section>
  <section class="panel"><div class="panel-heading"><h2>批次物料账户</h2></div>
    ${table(accountColumns, accounts, '暂无账户，登记更正单后按观测台账物化期初余额。')}</section>`;
}

export function wireLedger(root, refresh) {
  bindForm(root.querySelector('#voucher-form'), async form => {
    const view = await post('/ledger/vouchers', formValues(form));
    notify(view.created ? `更正单 ${view.voucher_no} 已${statusName[view.status]}` : `原单 ${view.voucher_no} 已存在，未重复扣款`);
    refresh();
  });
  const retry = root.querySelector('#retry-btn');
  if (retry) retry.addEventListener('click', async () => {
    retry.disabled = true;
    try {
      const report = await post('/ledger/vouchers/retry', {});
      notify(`本轮处理 ${report.processed} 张单据`);
      refresh();
    } catch (err) {
      const error = root.querySelector('#retry-form [role="alert"]');
      if (error) { error.textContent = err.message; error.hidden = false; }
    } finally { retry.disabled = false; }
  });
}
