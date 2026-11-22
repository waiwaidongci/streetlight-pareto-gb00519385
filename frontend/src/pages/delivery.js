import {feedback} from '../components/common/fields.js';
import {panel, pill} from '../components/common/panels.js';
import {table} from '../components/common/table.js';
import {DELIVERY, SOURCE, STATUS} from '../constants/enums.js';
import {handler} from '../hooks/action.js';
import {session} from '../stores/session.js';
import {workspace} from '../stores/workspace.js';
import {button, el, link, p} from '../utils/dom.js';
import {date} from '../utils/format.js';
import {runPage} from './run.js';

function batchCell(item) {
  if (!item) return p('待补', 'empty');
  return el('div', {}, p(item.name, 'filename'), p(`${item.filename} · 收到 ${date(item.received_at)}`, 'subtle'));
}

export async function deliveryPage(route, refresh) {
  const {spec} = workspace;
  const message = feedback();
  const checklistData = spec.scheduled ? await workspace.checklist() : {items: [], handoff_boundary: null};
  const recover = button('恢复补跑', {class:'primary', 'data-testid':'recover'});
  recover.addEventListener('click', handler(recover, message, async () => {
    const result = await workspace.recover();
    message.textContent = `已补挂 ${result.promoted.length} 份、物化 ${result.materialized.length} 份、重发 ${result.retried.length} 份`;
    await refresh();
  }));
  const boundary = checklistData.handoff_boundary
    ? p(`连续交接止于首个缺口：${checklistData.handoff_boundary}（该日来源补齐前，后续日报不交接）`, 'feedback')
    : p('当前无未闭合缺口，日报连续可交接。', 'subtle');
  const checklistPanel = spec.scheduled ? panel('日报交付清单',
      session.canReview && el('div', {class:'actions'}, recover, message), boundary,
      table([
        {label:'报告日', render:row=>link(row.report_day, `#results/${row.run_id}`)},
        {label:'状态', render:row=>pill(STATUS[row.status] || row.status, row.status)},
        {label:'待补来源', render:row=>row.missing_sources.length
          ? row.missing_sources.map(key => SOURCE[key] || key).join('、') : '—'},
        {label:'灯组名册', render:row=>batchCell(row.roster)},
        {label:'热线批次', render:row=>batchCell(row.hotline)},
        {label:'发布', render:row=>row.delivery ? pill(DELIVERY[row.delivery.state] || row.delivery.state, row.delivery.state) : '—'},
        {label:'日报下载', render:row=>{
          if (!row.export_path) return p('未生成', 'empty');
          const dl = button('下载 CSV', {class:'quiet'});
          dl.addEventListener('click', handler(dl, message, () =>
            workspace.download(row.export_path, `${spec.short_name}-${row.run_id}.csv`)));
          return dl;
        }},
      ], checklistData.items, {caption:'逐日来源批次与交付状态', empty:'定时计划运行后在此列出交付清单。', pageSize:20})) : null;
  const delivery = spec.scheduled ? panel('站内分发回执', table([
    {key:'recipient',label:'收件团队'},
    {label:'回执',render:row=>pill(DELIVERY[row.state] || row.state, row.state)},
    {label:'尝试',render:row=>row.attempts},
    {key:'detail',label:'说明'}, {label:'时间',render:row=>date(row.updated_at || row.created_at)},
    {label:'查看',render:row=>link(`运行 ${row.run_id}`,`#results/${row.run_id}`)},
  ], await workspace.deliveries(), {caption:'站内收件箱', empty:'计划到时执行，回执将在此更新。'})) : null;
  return el('div',{}, panel(spec.pages[5], p('按运行批次下载 CSV，复核状态随报告保留。','subtle')),
    checklistPanel, delivery, spec.scheduled && await runPage({...route, scheduling:true}, refresh));
}
