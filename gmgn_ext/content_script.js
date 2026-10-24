// content_script.js — 注入到 gmgn.ai 页面，调接口并返回数据
(async function () {
  function getToken() {
    // GMGN token 存在 tgInfo.token.access_token
    try {
      const tg = JSON.parse(localStorage.getItem('tgInfo') || '{}');
      if (tg && tg.token && tg.token.access_token) return tg.token.access_token;
    } catch (e) {}
    // 兜底：遍历找 JWT
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i);
      const v = localStorage.getItem(k) || '';
      if (v.startsWith('eyJ') && v.length > 100) return v;
      // 嵌套 JSON 里找
      try {
        const o = JSON.parse(v);
        if (o.token && o.token.access_token) return o.token.access_token;
      } catch (e) {}
    }
    return '';
  }

  const sleep = ms => new Promise(r => setTimeout(r, ms));

  async function api(path, opts = {}) {
    const headers = Object.assign({
      'accept': 'application/json',
      'content-type': 'application/json'
    }, opts.headers || {});
    const token = getToken();
    if (token) headers['authorization'] = 'Bearer ' + token;
    let lastErr;
    for (let attempt = 0; attempt < 4; attempt++) {
      try {
        const r = await fetch(path, Object.assign({
          method: opts.method || 'GET',
          headers,
          credentials: 'include'
        }, opts));
        if (r.status === 429) {
          await sleep(500 * (attempt + 1));
          continue;
        }
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      } catch (e) {
        lastErr = e;
        await sleep(300);
      }
    }
    throw lastErr || new Error('请求失败');
  }

  // 提取钱包买入流水
  async function fetchBuys(wallet, maxRecords) {
    const all = [];
    let cursor = '';
    while (true) {
      let url = `/vas/api/v1/wallet_activity/sol?type=buy&wallet=${wallet}&limit=50`;
      if (cursor) url += `&cursor=${encodeURIComponent(cursor)}`;
      const j = await api(url);
      const list = (j.data && j.data.activities) || [];
      for (const it of list) all.push(it);
      const next = (j.data && j.data.next) || '';
      if (!next || !list.length) break;
      cursor = next;
      if (maxRecords && all.length >= maxRecords) break;
    }
    return all;
  }

  // 批量取代币信息（并发）
  async function fetchTokenInfo(addrs) {
    const out = {};
    const batches = [];
    for (let i = 0; i < addrs.length; i += 10) {
      batches.push(addrs.slice(i, i + 10));
    }
    // 并发 10 批
    const CONCURRENCY = 20;
    for (let i = 0; i < batches.length; i += CONCURRENCY) {
      const slice = batches.slice(i, i + CONCURRENCY);
      const results = await Promise.all(slice.map(batch =>
        api('/mrwapi/v1/multi_token_full_info', {
          method: 'POST',
          body: JSON.stringify({ chain: 'sol', addresses: batch })
        }).catch(() => null)
      ));
      for (const j of results) {
        if (!j) continue;
        const arr = j.data || [];
        for (const t of arr) {
          if (t.address) out[t.address] = t;
        }
      }
    }
    return out;
  }

  // 取 SOL 价格
  async function fetchSolPrice() {
    try {
      const j = await api('/api/v1/token_prices', {
        method: 'POST',
        body: JSON.stringify({ chain: 'sol', addresses: ['So11111111111111111111111111111111111111112'] })
      });
      const list = j.data && j.data.list || [];
      return list[0] ? Number(list[0].price) : 150;
    } catch (e) { return 150; }
  }

  // 调 hybrid holdings 接口拿 balance，判断持仓/清仓
  async function fetchBalance(wallet) {
    try {
      const did = localStorage.getItem('key_device_id') || 'web';
      const fpd = localStorage.getItem('key_fp_did') || '';
      const url = `/td/api/v1/wallets/hybrid/holdings?device_id=${did}&fp_did=${fpd}&client_id=gmgn_web_ext&from_app=gmgn&tz_name=Asia_Shanghai&tz_offset=28800&app_lang=zh-CN&os=web&worker=0`;
      const j = await api(url, {
        method: 'POST',
        body: JSON.stringify({ chain_wallets: [{chain:'sol', wallet_addresses:[wallet]}], order_by:'last_active_timestamp', direction:'desc' })
      });
      const list = (j.data && j.data.list) || [];
      const map = {};
      for (const w of list) {
        for (const h of (w.holdings || [])) {
          map[h.token_address] = Number(h.balance || 0);
        }
      }
      return map;
    } catch (e) { return {}; }
  }

  // 取面板总利润（holdings，翻页拿全）
  async function fetchHoldings(wallet) {
    try {
      const did = localStorage.getItem('key_device_id') || 'web';
      const fpd = localStorage.getItem('key_fp_did') || '';
      const base = `/pf/api/v1/wallet/sol/${wallet}/holdings?device_id=${did}&fp_did=${fpd}&tab_id=ext&client_id=gmgn_web_ext&from_app=gmgn&tz_name=Asia_Shanghai&tz_offset=28800&app_lang=zh-CN&os=web&worker=0&limit=50&order_by=last_active_timestamp&direction=desc&sellout=true&showsmall=true`;
      const map = {};
      let cursor = '';
      let page = 0;
      while (page < 20) {
        const url = base + (cursor ? `&cursor=${encodeURIComponent(cursor)}` : '');
        const j = await api(url);
        const list = (j.data && j.data.list) || [];
        for (const it of list) {
          const a = it.token && it.token.token_address;
          if (a) map[a] = { profit: it.total_profit, pnl: it.total_profit_pnl, raw: it };
        }
        const next = (j.data && j.data.next) || '';
        if (!next || !list.length) break;
        cursor = next;
        page++;
      }
      return map;
    } catch (e) {
      console.warn('holdings failed', e);
      return {};
    }
  }

  // 监听 popup 消息
  chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (msg.type === 'collect') {
      (async () => {
        try {
          const { wallet, days, maxRecords } = msg;
          const status = m => chrome.runtime.sendMessage({ type: 'status', msg: m });
          status('获取买入流水和面板数据...');
          const [buys, holdings, solPrice, balanceMap] = await Promise.all([
            fetchBuys(wallet, maxRecords),
            fetchHoldings(wallet),
            fetchSolPrice(),
            fetchBalance(wallet)
          ]);
          status(`共 ${buys.length} 条流水，取代币信息...`);

          // 去重代币地址（去掉空）
          const addrs = [...new Set(buys.map(b => (b.token && b.token.address) || '').filter(a => a && a.length > 20))];
          const tokenInfo = await fetchTokenInfo(addrs);

          // 每个代币只保留第一次买入（timestamp 最小的）
          const firstBuy = {};
          for (const b of buys) {
            const ta = (b.token && b.token.address) || '';
            if (!ta) continue;
            if (!firstBuy[ta] || b.timestamp < firstBuy[ta].timestamp) {
              firstBuy[ta] = b;
            }
          }
          const uniqueBuys = Object.values(firstBuy);

          // 组装行
          const rows = [];
          for (const b of uniqueBuys) {
            const ta = (b.token && b.token.address) || '';
            const ti = tokenInfo[ta] || {};
            const h = holdings[ta] || {};
            // 用 hybrid holdings 接口的 balance 判断：>0 持仓，=0 清仓
            const bal = Number(balanceMap[ta] || 0);
            const isHolding = bal > 0;
            // 市值 = price_usd * total_supply
            const mcap = (b.price_usd && b.token && b.token.total_supply)
              ? Number(b.price_usd) * Number(b.token.total_supply) : '';
            // 金额SOL：如果 quote token 是 SOL 直接用 quote_amount，否则用 USD/SOL价格换算
            const isSolQuote = b.quote_token && (b.quote_token.symbol === 'SOL' || b.quote_address === 'So11111111111111111111111111111111111111112');
            const amountSol = isSolQuote
              ? (b.quote_amount || '')
              : (b.cost_usd ? Number(b.cost_usd) / solPrice : '');
            rows.push({
              symbol: (b.token && b.token.symbol) || ti.symbol || '?',
              mcap: mcap,
              qty: b.token_amount || '',
              amountSol: amountSol,
              amountUsd: b.cost_usd || b.buy_cost_usd || '',
              profit: h.profit !== undefined ? h.profit : '',
              pnl: h.pnl !== undefined ? h.pnl : '',
              status: isHolding ? '持仓' : '清仓',
              buyTime: b.timestamp || '',
              created: ti.creation_timestamp || '',
              platform: b.launchpad_platform || ti.launchpad_platform || (ti.tpool && ti.tpool.exchange) || b.launchpad || '',
              address: ta
            });
          }
          sendResponse({ ok: true, rows, holdingsCount: Object.keys(holdings).length });
        } catch (e) {
          sendResponse({ ok: false, error: e.message });
        }
      })();
      return true; // 异步
    }
  });
})();
