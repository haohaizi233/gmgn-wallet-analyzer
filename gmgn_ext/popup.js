// popup.js
const $ = id => document.getElementById(id);

function extractAddress(s) {
  if (!s) return '';
  const m = s.match(/[1-9A-HJ-NP-Za-km-z]{32,44}/);
  return m ? m[0] : s.trim();
}

function fmt(n, digits = 2) {
  if (n === '' || n === null || n === undefined) return '';
  const v = Number(n);
  if (isNaN(v)) return n;
  return v.toLocaleString('zh-CN', { maximumFractionDigits: digits });
}

// 大数字缩写：K/M/B
function fmtCompact(n, digits = 2) {
  if (n === '' || n === null || n === undefined) return '';
  const v = Number(n);
  if (isNaN(v)) return n;
  const abs = Math.abs(v);
  if (abs >= 1e9) return (v / 1e9).toFixed(digits) + 'B';
  if (abs >= 1e6) return (v / 1e6).toFixed(digits) + 'M';
  if (abs >= 1e3) return (v / 1e3).toFixed(digits) + 'K';
  return v.toFixed(digits);
}

function fmtPct(p) {
  if (p === '' || p === null || p === undefined) return '';
  const v = Number(p) * 100;
  return (v >= 0 ? '+' : '') + v.toFixed(2) + '%';
}

function fmtTime(ts) {
  if (!ts) return '';
  const d = new Date(Number(ts) * 1000);
  const p = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function fmtDelta(buyTs, createdTs) {
  if (!buyTs || !createdTs) return '';
  const diff = Number(buyTs) - Number(createdTs);
  if (isNaN(diff)) return '';
  const d = Math.floor(diff / 86400);
  const h = Math.floor((diff % 86400) / 3600);
  const m = Math.floor((diff % 3600) / 60);
  const s = diff % 60;
  if (d > 0) return `${d}天${h}时${m}分${s}秒`;
  if (h > 0) return `${h}时${m}分${s}秒`;
  if (m > 0) return `${m}分${s}秒`;
  return `${s}秒`;
}

let rows = [];

function renderRows() {
  const tb = $('tbody');
  tb.innerHTML = '';
  const frag = document.createDocumentFragment();
  for (const r of rows) {
    const tr = document.createElement('tr');
    const pnlNum = Number(r.pnl);
    const cls = pnlNum >= 0 ? 'pos' : 'neg';
    tr.innerHTML = `
      <td>${r.symbol}</td>
      <td>${r.mcap ? '$' + fmtCompact(r.mcap) : ''}</td>
      <td>${fmtCompact(r.qty)}</td>
      <td>${fmt(r.amountSol, 3)}</td>
      <td>${fmt(r.amountUsd, 2)}</td>
      <td class="${cls}">${r.profit !== '' ? (Number(r.profit)>=0?'+':'') + fmt(r.profit, 2) : ''}</td>
      <td class="${cls}">${fmtPct(r.pnl)}</td>
      <td>${fmtDelta(r.buyTime, r.created)}</td>
      <td>${r.platform || '其他'}</td>
      <td>${fmtTime(r.buyTime)}</td>
      <td>${fmtTime(r.created)}</td>
    `;
    frag.appendChild(tr);
  }
  tb.appendChild(frag);
  $('count').textContent = `共 ${rows.length} 条买入记录`;
}

async function getActiveTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab;
}

$('start').onclick = async () => {
  const addr = extractAddress($('addr').value);
  if (!addr) { alert('请输入钱包地址或链接'); return; }
  const days = parseInt($('days').value) || 0;
  const limit = parseInt($('limit').value) || 0;
  const btn = $('start');
  btn.disabled = true; btn.textContent = '采集中...';
  $('status').textContent = '正在连接 GMGN 页面...';

  const tab = await getActiveTab();
  if (!tab || !tab.url || !tab.url.includes('gmgn.ai')) {
    alert('请先在浏览器打开 https://gmgn.ai 并登录');
    btn.disabled = false; btn.textContent = '开始采集';
    return;
  }

  try {
    const resp = await chrome.tabs.sendMessage(tab.id, {
      type: 'collect', wallet: addr, days, maxRecords: limit
    });
    if (!resp || !resp.ok) throw new Error((resp && resp.error) || '未收到响应');
    rows = resp.rows;
    renderRows();
    $('status').textContent = `完成：${rows.length} 条记录，面板总利润覆盖 ${resp.holdingsCount} 个代币`;
  } catch (e) {
    $('status').textContent = '失败：' + e.message;
    alert('采集失败：' + e.message + '\n\n请确认已打开 gmgn.ai 页面且已登录。');
  }
  btn.disabled = false; btn.textContent = '开始采集';
};

chrome.runtime.onMessage.addListener((msg) => {
  if (msg.type === 'status') $('status').textContent = msg.msg;
});

function nowStr() {
  const d = new Date();
  const p = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}${p(d.getMonth()+1)}${p(d.getDate())}_${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}`;
}

function fmtDeltaSec(buyTs, createdTs) {
  if (!buyTs || !createdTs) return '';
  return Number(buyTs) - Number(createdTs);
}

$('exportCsv').onclick = () => {
  if (!rows.length) { alert('暂无数据'); return; }
  const heads = ['币种','市值(买入时)','买入数量','金额(SOL)','金额(USD)','总利润率','总利润(USD)','状态','创建时间','买入时间','时差(秒)','平台','代币地址'];
  const lines = [heads.join(',')];
  for (const r of rows) {
    lines.push([
      r.symbol,
      r.mcap || '',
      r.qty ? Number(r.qty).toFixed(2) : '',
      r.amountSol ? Number(r.amountSol).toFixed(2) : '',
      r.amountUsd ? Number(r.amountUsd).toFixed(2) : '',
      r.pnl !== '' ? (Number(r.pnl)*100).toFixed(2)+'%' : '',
      r.profit !== '' ? (Number(r.profit)>=0?'+':'') + Number(r.profit).toFixed(2) : '',
      r.status || '',
      fmtTime(r.created),
      fmtTime(r.buyTime),
      fmtDeltaSec(r.buyTime, r.created),
      r.platform || '',
      r.address || ''
    ].join(','));
  }
  download(`gmgn_buys_${nowStr()}.csv`, '\ufeff' + lines.join('\n'), 'text/csv');
};

$('exportXlsx').onclick = () => {
  if (!rows.length) { alert('暂无数据'); return; }
  const heads = ['币种','市值(买入时)','买入数量','金额(SOL)','金额(USD)','总利润率','总利润(USD)','状态','创建时间','买入时间','时差','平台','代币地址'];
  let html = `<html xmlns:x="urn:schemas-microsoft-com:office:excel"><head><meta charset="UTF-8"><style>
    table{border-collapse:collapse;font-size:14px;}
    th,td{border:1px solid #999;padding:8px 12px;text-align:center;}
    th{background:#b8cce4;font-weight:bold;font-size:15px;}
  </style></head><body><table>`;
  html += '<tr>';
  for (const h of heads) html += `<th>${h}</th>`;
  html += '</tr>';
  for (const r of rows) {
    const pnlNum = Number(r.pnl);
    const color = pnlNum >= 0 ? '#00b050' : '#ff0000';
    const profitColor = Number(r.profit) >= 0 ? '#00b050' : '#ff0000';
    const profitText = r.profit !== '' ? (Number(r.profit)>=0?'+':'') + Number(r.profit).toFixed(2) : '';
    const pnlText = r.pnl !== '' ? (pnlNum>=0?'+':'') + (pnlNum*100).toFixed(2)+'%' : '';
    html += '<tr>';
    html += `<td>${r.symbol}</td>`;
    html += `<td>${r.mcap ? Number(r.mcap).toFixed(3) : ''}</td>`;
    html += `<td>${r.qty ? Number(r.qty).toFixed(3) : ''}</td>`;
    html += `<td>${r.amountSol ? Number(r.amountSol).toFixed(2) : ''}</td>`;
    html += `<td>${r.amountUsd ? Number(r.amountUsd).toFixed(2) : ''}</td>`;
    html += `<td style="color:${color}">${pnlText}</td>`;
    html += `<td style="color:${profitColor}">${profitText}</td>`;
    html += `<td style="color:${r.status==='清仓'?'#999':'#4a9eff'}">${r.status || ''}</td>`;
    html += `<td>${fmtTime(r.created)}</td>`;
    html += `<td>${fmtTime(r.buyTime)}</td>`;
    html += `<td>${fmtDelta(r.buyTime, r.created)}</td>`;
    html += `<td>${r.platform || ''}</td>`;
    html += `<td style="font-size:11px;">${r.address || ''}</td>`;
    html += '</tr>';
  }
  html += '</table></body></html>';
  download(`gmgn_buys_${nowStr()}.xls`, html, 'application/vnd.ms-excel');
};

function download(filename, content, mime) {
  const blob = new Blob([content], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = filename; a.click();
  URL.revokeObjectURL(url);
}
