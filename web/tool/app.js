/* Mirsad: draws the exported screens. It computes no verdicts; every number, threshold and
   label shown here comes from data/screens.json, data/changes.json or data/meta.json. */
(() => {
  'use strict';

  // Status colours carry no red or green meaning beyond status itself: the map has no price colour mode.
  const STATUS = {
    pass: { label: 'Pass', symbol: '✓', fill: '#1f9d55', text: '#04140a' },
    needs_review: { label: 'Needs review', symbol: '?', fill: '#f59e0b', text: '#0a0b0d' },
    fail: { label: 'Fail', symbol: '✕', fill: '#b3261e', text: '#ffffff' },
    insufficient_data: { label: 'Insufficient data', symbol: '–', fill: '#1f232a', text: '#e5e7eb' },  // the dark hatch on the tile
  };
  const NEAR_STATUSES = ['pass', 'needs_review'];
  const NEAR_LINE = 'rgba(245, 246, 248, 0.75)';  // within the margin of a limit: a light outline
  const SELECT_LINE = '#f5f6f8';                    // the selected tile
  const REVIEW_NEAR = '#b45309';                    // needs review within the margin: deep amber, hatched
  const REVIEW_NEAR_TEXT = '#fffbeb';
  const HATCH = '#fde68a';
  const UNKNOWN_BG = '#1f232a';                     // insufficient data: dark hatch, so it reads as unknown
  const UNKNOWN_HATCH = '#8b929c';
  const DIM = '#1a1d22';                            // filtered out
  const NEUTRAL = '#15181d';                        // sector and root tiles
  const TILE_GAP = '#0a0b0d';                       // the seam between tiles: the page colour
  const PARENT_INK = '#c4c9d0';
  const UNSIZED = '#3a404a';
  // URL flags: ?display=1 is the kiosk view, ?embed=1 the landing page's preview (removed with the preview).
  const PARAMS = new URLSearchParams(location.search);
  const DISPLAY = PARAMS.get('display') === '1';
  const EMBED = PARAMS.get('embed') === '1';
  if (DISPLAY) document.documentElement.classList.add('kiosk');
  if (EMBED) document.documentElement.classList.add('embed');
  // A move smaller than this reads as flat. It is a display threshold; the scale and the data are unchanged.
  const FLAT = 0.0005;
  const STALE_DAYS = 4;                // export older than this shows a warning in the banner
  const BASIS = {
    disclosed: { short: 'Disclosed interest', long: 'Disclosed interest income, trailing 12 months', confidence: 'standard' },
    disclosed_partial: {
      short: 'Disclosed, partial',
      long: 'Interest income summed over segments: partial, so it supports a pass only well below the limit', confidence: 'lower',
    },
    annual_fallback: {
      short: 'Annual figure',
      long: 'Latest annual interest income, older than 12 months', confidence: 'lower',
    },
    net_investment_income: {
      short: 'Net investment income',
      long: 'Net investment income, not gross interest: supports a pass only well below the limit', confidence: 'lower',
    },
    upper_bound_cash: {
      short: 'Upper bound on cash',
      long: 'No interest income disclosed: an upper bound from cash and interest-bearing securities', confidence: 'lower',
    },
    upper_bound_total_assets: {
      short: 'Upper bound on assets',
      long: 'No interest income or investments disclosed: an upper bound from total assets', confidence: 'lower',
    },
    none: { short: 'No figure', long: 'No interest income basis available', confidence: 'none' },
  };
  const CAUSE = {
    methodology_change: 'Methodology changed',
    override_added: 'Override added',
    override_expired: 'Override expired',
    override_removed: 'Override removed',
    event_8k: 'Spin-off or disposition reported (8-K)',
    new_filing: 'New filing: reported figures changed',
    price_move: 'Market cap moved across a limit',
    other: 'Other',
  };
  const STATUS_CHIPS = [['pass', 'Pass', '✓'], ['needs_review', 'Needs review', '?'], ['fail', 'Fail', '✕'], ['insufficient_data', 'Insufficient data', '–']];
  const SMALL = window.matchMedia('(max-width: 960px)');

  const $ = (id) => document.getElementById(id);
  const el = {
    subtitle: $('subtitle'), banner: $('banner'), loadError: $('load-error'), app: $('app'),
    q: $('q'), qResults: $('q-results'), go: $('go'),
    fnMap: $('fn-map'), fnList: $('fn-list'), fnChanges: $('fn-changes'), toolsToggle: $('tools-toggle'),
    drawer: $('drawer'), drawerClose: $('drawer-close'),
    counts: $('counts'), reset: $('reset'), statusAsof: $('status-asof'), statusHover: $('status-hover'), statusClock: $('status-clock'),
    nearOnly: $('near-only'), count: $('count'), sector: $('sector'), controls: $('controls'),
    unsized: $('unsized'), unsizedCount: $('unsized-count'), unsizedList: $('unsized-list'),
    treemap: $('treemap'), listBody: $('list-body'), listCount: $('list-count'), listQ: $('list-q'),
    listWrap: $('list-wrap'), sort: $('sort'), exportCsv: $('export-csv'), detail: $('detail'),
    changesPanel: $('changes-panel'), changes: $('changes'), changesPassOnly: $('changes-pass-only'),
    changesMore: $('changes-more'), thresholds: $('thresholds'), nearDesc: $('near-desc'),
    mcapDesc: $('mcap-desc'), dataDesc: $('data-desc'), disclaimer: $('disclaimer'),
  };

  let meta = null;
  let screens = [];
  let changes = [];
  let indexEvents = [];
  let byTicker = new Map();
  let members = [];
  let selected = null;
  let plotBound = false;
  let changesLimit = 40;
  let filters = null;
  let lastShown = [];
  let sectorFilter = 'all';

  /* ---------- formatting and small helpers ---------- */

  const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
  const pct = (value, digits = 2) => (value == null ? 'n/a' : `${(value * 100).toFixed(digits)}%`);
  const trimPct = (value) => `${Number((value * 100).toFixed(2))}%`;
  const priceText = (value) => {
    if (value == null) return 'n/a';
    if (Math.abs(value) < FLAT) return `► ${(Math.abs(value) * 100).toFixed(2)}%`;
    const arrow = value > 0 ? '▲' : '▼';
    const sign = value > 0 ? '+' : '−';
    return `${arrow} ${sign}${(Math.abs(value) * 100).toFixed(2)}%`;
  };
  const money = (value) => {
    if (value == null) return 'n/a';
    const abs = Math.abs(value);
    if (abs >= 1e9) return `$${(value / 1e9).toFixed(2)} bn`;
    if (abs >= 1e6) return `$${(value / 1e6).toFixed(1)} m`;
    return `$${Math.round(value).toLocaleString('en-US')}`;
  };
  const number = (value) => (value == null ? 'n/a' : Number(value).toLocaleString('en-US', { maximumFractionDigits: 2 }));
  const badge = (status) => {
    const s = STATUS[status] || { label: status };
    return `<span class="st st-${esc(status)}">${esc(s.label)}</span>`;
  };
  const isNear = (s) => NEAR_STATUSES.includes(s.status) && s.near_threshold;
  const lowerBasis = (s) => BASIS[s.interest_income.key].confidence === 'lower';
  const flagOn = (s, key) => {
    switch (key) {
      case 'denominator': return s.flags.denominator_disagreement;
      case 'spot': return s.flags.spot_divergence;
      case 'share': return s.flags.share_count;
      case 'stale': return s.flags.stale_balance_sheet;
      case 'override': return s.flags.override;
      default: return false;
    }
  };
  const marginText = () => {
    const near = meta.near_threshold;
    return near.mode === 'relative' ? `${trimPct(near.margin)} of the limit` : `${number(near.margin)} points of the limit`;
  };

  /* ---------- filters and scope ---------- */

  function readFilters() {
    const checked = (name) => [...document.querySelectorAll(`input[name="${name}"]:checked`)].map((i) => i.value);
    filters = {
      listQ: el.listQ.value.trim().toLowerCase(),
      statuses: new Set(checked('status')),
      near: el.nearOnly.checked,
      bases: new Set(checked('basis')),
      flags: new Set(checked('flag')),
    };
  }

  // The sector selector: 'all' (default) or one sector, linked as ?sector=Name. It narrows the set the map, the list,
  // the count and the CSV read from; the status filters work within it.
  const sectorOf = (s) => s.sector || 'Unclassified';
  const scopeMembers = () => (sectorFilter === 'all' ? members : members.filter((s) => sectorOf(s) === sectorFilter));
  function writeSectorUrl() {
    const url = new URL(location.href);
    if (sectorFilter === 'all') url.searchParams.delete('sector'); else url.searchParams.set('sector', sectorFilter);
    history.replaceState(null, '', url);
  }
  function setSector(name) {
    sectorFilter = name;
    el.sector.value = name;
    writeSectorUrl();
    refresh();
  }
  function fillSectorSelector() {
    const counts = new Map();
    for (const s of members) counts.set(sectorOf(s), (counts.get(sectorOf(s)) || 0) + 1);
    const names = [...counts.keys()].sort((a, b) => a.localeCompare(b));
    el.sector.innerHTML = '<option value="all">All sectors</option>' + names.map((name) =>
      `<option value="${esc(name)}">${esc(name)} (${counts.get(name)})</option>`).join('');
    const requested = PARAMS.get('sector');
    sectorFilter = requested && counts.has(requested) ? requested : 'all';
    el.sector.value = sectorFilter;
    if (sectorFilter === 'all' && requested) writeSectorUrl();
  }

  function visible(s) {
    if (!filters.statuses.has(s.status)) return false;
    if (filters.near && !isNear(s)) return false;
    if (!filters.bases.has(s.interest_income.key)) return false;
    if (filters.flags.size && ![...filters.flags].some((key) => flagOn(s, key))) return false;
    return true;
  }

  /* ---------- drawer: the tools are hidden until asked for ---------- */

  // The drawer overlays the right edge of the page. It never changes the size or place of the map.
  function setDrawer(open) {
    const on = open && !DISPLAY;
    el.drawer.classList.toggle('open', on);
    el.drawer.inert = !on;
    el.toolsToggle.setAttribute('aria-expanded', String(on));
  }
  const drawerOpen = () => el.drawer.classList.contains('open');

  /* ---------- hover and status line ---------- */

  // The hovered stock is named on the status line, so nothing is drawn over the map.
  function showHover(s) {
    el.statusHover.textContent = `${s.ticker}  ·  ${STATUS[s.status].label}  ·  ${money(s.spot_market_cap)}`;
  }
  function clearHover() {
    el.statusHover.textContent = '';
  }
  function tickClock() {
    el.statusClock.textContent = new Date().toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  }

  /* ---------- treemap ---------- */

  // On the tile: the ticker, the status glyph and a stale-balance-sheet mark. Near a limit is the outline or hatch.
  function tileLabel(s) {
    const stale = flagOn(s, 'stale') ? ' !' : '';
    return `${s.ticker} ${STATUS[s.status].symbol}${stale}`;
  }

  function drawTreemap() {
    const sectors = new Map();
    for (const s of scopeMembers()) {
      const key = s.sector || 'Unclassified';
      if (!sectors.has(key)) sectors.set(key, []);
      sectors.get(key).push(s);
    }
    const sized = scopeMembers().filter((s) => s.spot_market_cap != null);
    const total = sized.reduce((sum, s) => sum + s.spot_market_cap, 0);
    const ids = ['all'];
    const labels = ['S&P 500'];
    const parents = [''];
    const values = [total];
    const colours = [NEUTRAL];
    const textColours = [PARENT_INK];
    const lines = [TILE_GAP];
    const lineWidth = [1];
    const shape = [''];
    const fg = [HATCH];
    const bg = [NEUTRAL];
    const solidity = [0.4];

    for (const [sector, stocks] of [...sectors.entries()].sort((a, b) => a[0].localeCompare(b[0]))) {
      const sectorValue = stocks.reduce((sum, s) => sum + (s.spot_market_cap || 0), 0);
      ids.push(`sector:${sector}`);
      labels.push(sector);
      parents.push('all');
      values.push(sectorValue);
      colours.push(NEUTRAL);
      textColours.push(PARENT_INK);
      lines.push(TILE_GAP);
      lineWidth.push(1);
      shape.push('');
      fg.push(HATCH);
      bg.push(NEUTRAL);
      solidity.push(0.4);
      for (const s of stocks.filter((x) => x.spot_market_cap != null)) {
        const near = isNear(s);
        const hatched = near && s.status === 'needs_review';
        const unknown = s.status === 'insufficient_data';
        const shown = visible(s);
        const fill = !shown ? DIM : hatched ? REVIEW_NEAR : unknown ? UNKNOWN_BG : STATUS[s.status].fill;
        ids.push(s.ticker);
        labels.push(tileLabel(s));
        parents.push(`sector:${sector}`);
        values.push(s.spot_market_cap);
        colours.push(fill);
        textColours.push(hatched ? REVIEW_NEAR_TEXT : unknown ? STATUS.insufficient_data.text : STATUS[s.status].text);
        const isSelected = s.ticker === selected;
        lines.push(isSelected ? SELECT_LINE : near && s.status === 'pass' ? NEAR_LINE : TILE_GAP);
        lineWidth.push(isSelected ? 3 : near && s.status === 'pass' ? 1.25 : 1);
        shape.push(hatched || unknown ? '/' : '');
        fg.push(hatched ? HATCH : unknown ? UNKNOWN_HATCH : HATCH);
        bg.push(fill);
        solidity.push(hatched ? 0.22 : 0.45);
      }
    }

    const trace = {
      type: 'treemap',
      ids,
      labels,
      parents,
      values,
      branchvalues: 'total',
      hovertemplate: '<extra></extra>',
      marker: {
        colors: colours,
        line: { color: lines, width: lineWidth },
        pattern: { shape, fgcolor: fg, bgcolor: bg, solidity },
      },
      hoverlabel: { bgcolor: 'rgba(0,0,0,0)', bordercolor: 'rgba(0,0,0,0)', font: { size: 1, color: 'rgba(0,0,0,0)' } },
      textfont: { size: DISPLAY ? 16 : 13, color: textColours, family: '"Mirsad Mono", ui-monospace, monospace' },
      tiling: { pad: 2 },
      pathbar: { visible: false },
    };
    const layout = {
      margin: { l: 0, r: 0, t: 0, b: 0 },
      paper_bgcolor: 'rgba(0,0,0,0)',
      plot_bgcolor: 'rgba(0,0,0,0)',
      font: { family: '"Mirsad Condensed", sans-serif', color: PARENT_INK },
      uniformtext: { minsize: 9, mode: 'hide' },
    };
    // The kiosk is a display, not a tool: no mode bar.
    const config = { responsive: true, displaylogo: false, displayModeBar: !DISPLAY, modeBarButtonsToRemove: ['lasso2d', 'select2d'] };
    Plotly.react(el.treemap, [trace], layout, config).then(() => {
      if (plotBound) return;
      plotBound = true;
      el.treemap.on('plotly_click', (ev) => {
        const point = ev.points && ev.points[0];
        if (!point) return;
        const id = point.data.ids[point.pointNumber];
        // Plotly drills into any node it is clicked on. The map is kept whole, so reset the view on every click; a leaf
        // click then opens its stock. Plotly applies its own drill-down after this handler returns, so reset on the next tick.
        setTimeout(() => Plotly.restyle(el.treemap, { level: 'all' }), 0);
        if (DISPLAY || !byTicker.has(id)) return;
        // In the landing page's preview, a tile opens that company in the full tool.
        if (EMBED && window.top !== window.self) {
          window.top.location.href = new URL(`./#${encodeURIComponent(id)}`, location.href).href;
          return;
        }
        openDetail(id);
      });
      el.treemap.on('plotly_hover', (ev) => {
        const point = ev.points && ev.points[0];
        const id = point && point.data.ids[point.pointNumber];
        if (byTicker.has(id)) showHover(byTicker.get(id)); else clearHover();
      });
      el.treemap.on('plotly_unhover', clearHover);
    });
  }

  /* ---------- watchlist ---------- */

  // Pinned tickers are kept in this browser's storage. Nothing is sent anywhere.
  const WATCH_KEY = 'halal-heatmap-watchlist';
  const loadWatch = () => {
    try { return new Set(JSON.parse(localStorage.getItem(WATCH_KEY) || '[]')); } catch (e) { return new Set(); }
  };
  let watch = loadWatch();
  const saveWatch = () => {
    try { localStorage.setItem(WATCH_KEY, JSON.stringify([...watch])); } catch (e) { /* storage blocked: pins last for this page only */ }
  };
  const togglePin = (ticker) => {
    if (watch.has(ticker)) watch.delete(ticker); else watch.add(ticker);
    saveWatch();
    refresh();
    if (selected === ticker) openDetail(ticker);
  };
  const pinButton = (ticker) => {
    const on = watch.has(ticker);
    return `<button type="button" class="pin-toggle" data-pin="${esc(ticker)}" aria-pressed="${on}">` +
      `<span aria-hidden="true">${on ? '★' : '☆'}</span> Pin</button>`;
  };

  /* ---------- list ---------- */

  // Sorting only changes the order of the rows shown. Pinned tickers come first in every order.
  const STATUS_ORDER = ['pass', 'needs_review', 'fail', 'insufficient_data'];
  const sortStocks = (list) => {
    const change = (s) => (s.daily_change ? s.daily_change.pct : null);
    const byName = (a, b) => a.ticker.localeCompare(b.ticker);
    const orders = {
      watch: byName,
      ticker: byName,
      status: (a, b) => STATUS_ORDER.indexOf(a.status) - STATUS_ORDER.indexOf(b.status) || byName(a, b),
      cap: (a, b) => (b.spot_market_cap || 0) - (a.spot_market_cap || 0) || byName(a, b),
      change: (a, b) => {
        const va = change(a); const vb = change(b);
        if (va == null && vb == null) return byName(a, b);
        if (va == null) return 1;
        if (vb == null) return -1;
        return vb - va || byName(a, b);
      },
    };
    const cmp = orders[el.sort.value] || byName;
    return list.slice().sort((a, b) => (watch.has(b.ticker) - watch.has(a.ticker)) || cmp(a, b));
  };

  const csvCell = (value) => {
    const text = value == null ? '' : String(value);
    return /[",\n\r]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
  };
  // Exports only the fields the list shows, for the rows currently shown.
  function downloadCsv(list) {
    const head = ['ticker', 'name', 'status', 'near_limit', 'interest_basis', 'lower_confidence', 'spot_market_cap',
      'price_change_fraction', 'price_change_date', 'screen_date'];
    const rows = list.map((s) => [
      s.ticker, s.name, s.status, isNear(s) ? 'yes' : 'no', s.interest_income.key, lowerBasis(s) ? 'yes' : 'no',
      s.spot_market_cap, s.daily_change ? s.daily_change.pct : '', s.daily_change ? s.daily_change.date : '', s.screen_date,
    ]);
    const text = [head, ...rows].map((r) => r.map(csvCell).join(',')).join('\r\n');
    const url = URL.createObjectURL(new Blob([text], { type: 'text/csv' }));
    const a = document.createElement('a');
    a.href = url;
    a.download = `mirsad-${meta.screen_date}.csv`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  function drawList(visibleStocks) {
    el.listBody.innerHTML = visibleStocks.map((s) => {
      const near = isNear(s) ? `<span class="mark" aria-hidden="true">◆</span> Yes` : 'No';
      const pinned = watch.has(s.ticker);
      return `<tr>
        <td><button type="button" class="pin" data-pin="${esc(s.ticker)}" aria-pressed="${pinned}" aria-label="${pinned ? 'Unpin' : 'Pin'} ${esc(s.ticker)}">${pinned ? '★' : '☆'}</button></td>
        <td><button type="button" class="stock" data-ticker="${esc(s.ticker)}">${esc(s.ticker)}</button></td>
        <td>${esc(s.name)}</td>
        <td>${badge(s.status)}</td>
        <td>${near}</td>
        <td>${esc(BASIS[s.interest_income.key].short)}${lowerBasis(s) ? ' <span class="badge lower">lower confidence</span>' : ''}</td>
        <td class="num">${s.daily_change ? esc(priceText(s.daily_change.pct)) : 'n/a'}</td>
      </tr>`;
    }).join('');
    el.listCount.textContent = visibleStocks.length;
  }

  /* ---------- counts, chips and the refresh ---------- */

  function filtersActive() {
    return filters.statuses.size !== document.querySelectorAll('input[name="status"]').length
      || filters.near || filters.bases.size !== document.querySelectorAll('input[name="basis"]').length
      || filters.flags.size > 0 || sectorFilter !== 'all' || filters.listQ !== '';
  }

  function refresh() {
    readFilters();
    const scoped = scopeMembers();
    const shown = scoped.filter(visible);
    const q = filters.listQ;
    const listed = q ? shown.filter((s) => `${s.ticker} ${s.name}`.toLowerCase().includes(q)) : shown;
    el.count.textContent = sectorFilter === 'all'
      ? `Showing ${shown.length} of ${members.length} stocks`
      : `Showing ${shown.length} of ${scoped.length} stocks in ${sectorFilter}`;
    el.reset.hidden = !filtersActive();
    drawTreemap();
    lastShown = sortStocks(listed);
    drawList(lastShown);
    syncCounts();
  }

  // Status counts from the export. Each chip is the legend for its colour and a status filter.
  function drawCounts() {
    const by = meta.counts.by_status;
    el.counts.innerHTML = STATUS_CHIPS.map(([key, label, glyph]) =>
      `<button type="button" class="chip st-${key}" data-status="${key}" aria-pressed="true"${DISPLAY ? ' disabled' : ''}>` +
      `<span class="chip-n">${by[key] ?? 0}</span><span class="chip-l"><span aria-hidden="true">${glyph}</span> ${label}</span></button>`).join('');
  }
  function syncCounts() {
    el.counts.querySelectorAll('[data-status]').forEach((chip) => {
      const box = document.querySelector(`input[name="status"][value="${chip.dataset.status}"]`);
      chip.setAttribute('aria-pressed', String(Boolean(box && box.checked)));
    });
  }

  function resetFilters() {
    el.q.value = '';
    el.listQ.value = '';
    el.nearOnly.checked = false;
    document.querySelectorAll('input[name="basis"]').forEach((i) => { i.checked = true; });
    document.querySelectorAll('input[name="status"]').forEach((i) => { i.checked = true; });
    document.querySelectorAll('input[name="flag"]').forEach((i) => { i.checked = false; });
    if (sectorFilter !== 'all') setSector('all'); else refresh();
  }

  /* ---------- command search ---------- */

  // Ticker first, then ticker prefix, then company name. Searches the whole export, so a stock outside the index still opens.
  function matchesFor(query) {
    const needle = query.trim().toLowerCase();
    if (!needle) return [];
    const rank = (s) => {
      const ticker = s.ticker.toLowerCase();
      if (ticker === needle) return 0;
      if (ticker.startsWith(needle)) return 1;
      if (s.name.toLowerCase().includes(needle)) return 2;
      return null;
    };
    return screens.map((s) => ({ s, r: rank(s) })).filter((x) => x.r !== null)
      .sort((a, b) => a.r - b.r || a.s.ticker.localeCompare(b.s.ticker)).slice(0, 8).map((x) => x.s);
  }
  let cmdActive = -1;
  let cmdList = [];
  function renderResults() {
    const query = el.q.value.trim();
    cmdList = matchesFor(query);
    cmdActive = cmdList.length ? Math.min(cmdActive, cmdList.length - 1) : -1;
    if (!query) {
      el.qResults.hidden = true;
      el.q.setAttribute('aria-expanded', 'false');
      el.q.removeAttribute('aria-activedescendant');
      return;
    }
    el.qResults.hidden = false;
    el.q.setAttribute('aria-expanded', 'true');
    el.qResults.innerHTML = cmdList.length
      ? cmdList.map((s, i) => `<li role="option" id="q-opt-${i}" data-ticker="${esc(s.ticker)}" aria-selected="${i === cmdActive}">` +
        `<span class="res-t">${esc(s.ticker)}</span><span class="res-n">${esc(s.name)}</span>${badge(s.status)}</li>`).join('')
      : `<li class="res-none">No stock matches “${esc(query)}”</li>`;
    el.q.setAttribute('aria-activedescendant', cmdActive >= 0 ? `q-opt-${cmdActive}` : '');
  }
  function closeResults() {
    cmdActive = -1;
    el.qResults.hidden = true;
    el.q.setAttribute('aria-expanded', 'false');
    el.q.removeAttribute('aria-activedescendant');
  }
  function chooseStock(ticker) {
    closeResults();
    el.q.value = ticker;
    openDetail(ticker);
  }
  function goSearch() {
    const query = el.q.value.trim();
    if (!query) return;
    const list = matchesFor(query);
    if (!list.length) { renderResults(); el.q.focus(); return; }
    chooseStock(list[cmdActive >= 0 && cmdActive < list.length ? cmdActive : 0].ticker);
  }

  /* ---------- click-through detail ---------- */

  function factsTable(facts) {
    const rows = facts.map((f) => {
      const period = f.period_start ? `${esc(f.period_start)} to ${esc(f.period_end)}` : esc(f.period_end);
      const value = f.unit === 'USD' ? money(f.value) : number(f.value);
      return `<tr>
        <td>${esc(f.input)} / ${esc(f.role)}</td>
        <td>${esc(f.tag)}${f.dimensions ? `<br><span class="muted">${esc(f.dimensions)}</span>` : ''}</td>
        <td>${period}</td>
        <td class="num">${value}</td>
        <td>${esc(f.form)} ${esc(f.accession)}<br><span class="muted">filed ${esc(f.filed)}</span></td>
      </tr>`;
    }).join('');
    return `<details class="sec"><summary>XBRL facts used <span class="count">${facts.length}</span></summary>
      <div class="sec-body"><div class="table-scroll"><table>
        <thead><tr><th scope="col">Input</th><th scope="col">Tag</th><th scope="col">Period</th>
          <th scope="col" class="num">Value</th><th scope="col">Filing</th></tr></thead>
        <tbody>${rows}</tbody></table></div></div></details>`;
  }

  function ratioTable(s) {
    // Each ratio is a block: its name across the full width, then the values with a small label on each cell.
    const rows = ['debt', 'cash', 'impure_income'].map((key) => {
      const r = s.ratios[key];
      if (r.ratio == null) return `<tbody><tr class="ratio-head"><th colspan="4" scope="colgroup">${esc(r.label)}</th></tr><tr><td colspan="4" class="muted">not available</td></tr></tbody>`;
      const result = r.passed ? 'Within limit' : 'Over limit';
      const near = r.near_threshold ? ' <span class="mark" aria-hidden="true">◆</span> near' : '';
      return `<tbody>
        <tr class="ratio-head"><th colspan="4" scope="colgroup">${esc(r.label)}</th></tr>
        <tr class="ratio-vals">
          <td data-label="Value" class="num">${pct(r.ratio)}</td>
          <td data-label="Limit" class="num">${esc(r.operator)} ${trimPct(r.limit)}</td>
          <td data-label="Headroom" class="num">${(r.headroom * 100).toFixed(2)} points <span class="muted">(${pct(r.headroom_rel, 0)} of limit)</span></td>
          <td data-label="Result">${result}${near}</td>
        </tr>
        <tr class="figures"><td colspan="4" class="muted">${money(r.numerator)} ÷ ${money(r.denominator)}, ${esc(r.denominator_basis)}</td></tr>
      </tbody>`;
    }).join('');
    return `<table class="ratios">${rows}</table>`;
  }

  function marketCapTable(s) {
    const rows = ['spot', 'avg_12m', 'avg_36m'].map((name) => {
      const d = s.market_cap.denominators[name];
      const driving = name === s.market_cap.driving ? ' <span class="mark">(drives verdict)</span>' : '';
      const window = d.start ? `${esc(d.start)} to ${esc(d.end)}` : 'n/a';
      return `<tr>
        <th scope="row">${name === 'spot' ? 'Spot' : name === 'avg_12m' ? '12-month average' : '36-month average'}${driving}</th>
        <td class="num">${money(d.value)}</td>
        <td class="num">${d.observations ?? 'n/a'}</td>
        <td>${d.status ? badge(d.status) : 'n/a'}</td>
      </tr>
      <tr class="figures"><td colspan="4" class="muted">${window}; debt ${pct(d.debt_ratio)}, cash ${pct(d.cash_ratio)} of market cap</td></tr>`;
    }).join('');
    return `<table class="mcap"><thead><tr><th scope="col">Market cap</th><th scope="col" class="num">Value</th>
      <th scope="col" class="num">Days</th><th scope="col">Would be</th></tr></thead>
      <tbody>${rows}</tbody></table>`;
  }

  function flagChips(s) {
    const chips = [];
    if (s.flags.denominator_disagreement) chips.push(['≠', 'Denominator-sensitive: the three market cap windows give different verdicts']);
    if (s.flags.spot_divergence) chips.push(['↕', `Spot divergence: spot and the 12-month average market cap are ${number(s.spot_divergence.factor)}× apart`]);
    if (s.flags.share_count) chips.push(['#', 'Share count flagged']);
    if (s.flags.stale_balance_sheet) chips.push(['!', 'Balance sheet out of date']);
    if (s.flags.override) chips.push(['✎', 'Override applied']);
    if (isNear(s)) chips.push(['◆', `Within ${marginText()}`]);
    if (s.flags.debt_implausible) chips.push(['', 'Debt looks understated: interest expense is high against tagged debt']);
    if (s.flags.investments_assumed_zero) chips.push(['', 'Investment tags absent, treated as 0']);
    if (s.flags.debt_assumed_zero) chips.push(['', 'Debt tags absent, treated as 0']);
    if (s.flags.annual_interest_income) chips.push(['', 'Annual interest income in use']);
    if (s.flags.unlisted_share_classes) chips.push(['', 'Unlisted share classes included in market cap']);
    if (!chips.length) return '<p class="muted">No flags.</p>';
    return `<div class="flagline">${chips.map(([g, text]) => `<span class="flag">${g ? `<span aria-hidden="true">${esc(g)}</span> ` : ''}${esc(text)}</span>`).join('')}</div>`;
  }

  function overrideBlock(s) {
    const o = s.override;
    if (!o) return '';
    const state = o.state === 'active' ? 'Active' : 'Expired';
    return `<h3>Override</h3>
      <dl class="kv">
        <dt>State</dt><dd>${esc(state)}${o.decision ? `, decision ${esc(o.decision)}` : ''}</dd>
        <dt>Reason</dt><dd>${esc(o.reason || 'not in overrides.yaml any more')}</dd>
        <dt>Reviewer</dt><dd>${esc(o.reviewer)}</dd>
        <dt>Dated</dt><dd>${esc(o.date)}</dd>
        <dt>Expires</dt><dd>${esc(o.expires_on)} (after ${meta.override_expiry_days} days)</dd>
      </dl>`;
  }

  function notesBlock(notes) {
    const entries = Object.entries(notes || {});
    if (!entries.length) return '';
    return `<h3>Notes on missing inputs</h3><ul>${entries.map(([k, v]) => `<li><b>${esc(k)}:</b> ${esc(v)}</li>`).join('')}</ul>`;
  }

  function interestBlock(s) {
    const b = s.interest_income;
    const info = BASIS[b.key] || BASIS.none;
    const confidence = info.confidence === 'lower'
      ? '<span class="badge lower">Lower confidence</span>'
      : '';
    let extra = '';
    if (b.upper_bound) {
      const base = b.upper_bound.base.replace(/_/g, ' ');
      extra += `<dt>Upper bound</dt><dd>${money(b.upper_bound.value)}, the most it can be: ${esc(base)} × ${trimPct(b.upper_bound.yield_ceiling)} yield ceiling</dd>`;
    }
    if (b.annual) {
      extra += `<dt>Annual figure</dt><dd>to ${esc(b.annual.period_end)}, filed ${esc(b.annual.filed)} (${esc(b.annual.accession)})</dd>`;
    }
    if (b.max_ratio_for_pass != null) {
      extra += `<dt>Supports a pass</dt><dd>only below ${pct(b.max_ratio_for_pass)} of revenue</dd>`;
    }
    const source = b.source === 'filing_xbrl' ? 'the filing’s own XBRL' : b.source === 'companyfacts' ? 'SEC companyfacts' : 'none';
    return `<h3>Interest income basis ${confidence}</h3>
      <p>${esc(info.long)}.</p>
      <dl class="kv">
        <dt>Figure</dt><dd>${money(b.value)}</dd>
        <dt>Read from</dt><dd>${esc(source)}${b.dimensional ? ', summed over dimension members' : ''}</dd>
        <dt>Tags</dt><dd>${b.tags.length ? b.tags.map(esc).join('<br>') : 'none'}</dd>
        ${extra}
      </dl>`;
  }

  function openDetail(ticker) {
    const s = byTicker.get(ticker);
    if (!s) return;
    selected = ticker;
    if (plotBound) drawTreemap(); // redraw so the selected tile carries the highlight outline
    const nearFlag = isNear(s) ? ' <span class="near-flag">Near a limit</span>' : '';
    const change = s.daily_change
      ? `${priceText(s.daily_change.pct)} on ${esc(s.daily_change.date)}`
      : (meta.daily_change.included ? 'not available' : 'not included in this export');
    const sharesInfo = s.shares.classes
      ? `${s.shares.classes.length} classes; ${s.shares.unlisted ? `${pct(s.shares.unlisted)} unlisted` : 'listed'}`
      : esc(s.shares.source);
    const section = (title, body, open = false, count = '') => `
      <details class="sec"${open ? ' open' : ''}>
        <summary>${title}${count !== '' ? ` <span class="count">${count}</span>` : ''}</summary>
        <div class="sec-body">${body}</div>
      </details>`;
    el.detail.innerHTML = `
      <div class="ident">
        <h2>${esc(s.ticker)} <span class="ident-name">${esc(s.name)}</span></h2>
        ${pinButton(s.ticker)}
      </div>
      <p class="sector muted">${esc(s.sector || 'No sector')} · ${esc(s.sub_industry || 'no sub-industry')}</p>
      <div class="verdict">${badge(s.status)}${nearFlag}</div>
      <p class="reason">${esc(s.reason)}</p>
      ${flagChips(s)}
      <dl class="kv key-facts">
        <div><dt>Screen date</dt><dd>${esc(s.screen_date)}</dd></div>
        <div><dt>Market cap (spot)</dt><dd>${money(s.spot_market_cap)}</dd></div>
        <div><dt>Driving market cap</dt><dd>${esc(s.market_cap.driving)}</dd></div>
        <div><dt>Last-close price change</dt><dd>${change}</dd></div>
      </dl>
      ${section('Financial ratios', ratioTable(s), true, '3')}
      ${section('Market cap', `${marketCapTable(s)}<p class="muted">Market cap is shares × closing price. The average covers ${esc(meta.market_cap.window_months[s.market_cap.driving] ?? '')} months of trading days, with at least ${pct(meta.market_cap.min_coverage, 0)} of them present.</p>`)}
      ${section('Interest income', interestBlock(s))}
      ${section('Inputs', `
        <dl class="kv">
          <dt>Debt</dt><dd>${money(s.inputs.debt.value)}${s.inputs.debt.assumed_zero ? ' (assumed 0)' : ''}${s.inputs.debt.note ? `<br><span class="muted">${esc(s.inputs.debt.note)}</span>` : ''}</dd>
          <dt>Cash and securities</dt><dd>${money(s.inputs.cash_and_securities.value)}${s.inputs.cash_and_securities.note ? `<br><span class="muted">${esc(s.inputs.cash_and_securities.note)}</span>` : ''}</dd>
          <dt>Revenue</dt><dd>${money(s.inputs.revenue)}</dd>
          <dt>Impure income</dt><dd>${money(s.inputs.impure_income)}</dd>
          <dt>Interest expense</dt><dd>${money(s.inputs.interest_expense)}</dd>
          <dt>Total liabilities</dt><dd>${money(s.inputs.total_liabilities)}</dd>
          <dt>Total assets</dt><dd>${money(s.inputs.total_assets)}</dd>
          <dt>Financing receivables</dt><dd>${money(s.inputs.financing_receivables.value)}${s.inputs.financing_receivables.share_of_assets != null ? ` (${pct(s.inputs.financing_receivables.share_of_assets)} of assets)` : ''}</dd>
          <dt>Shares</dt><dd>${sharesInfo}${s.shares.rejected ? `; ${s.shares.rejected} count(s) rejected` : ''}${s.shares.jump ? '; large jump between filings, for review' : ''}</dd>
        </dl>
        ${notesBlock(s.inputs.notes)}
        ${overrideBlock(s)}`)}
      ${section('Filing used', `
        <dl class="kv">
          <dt>Form</dt><dd>${esc(s.filing.form)}</dd>
          <dt>Filed</dt><dd>${esc(s.filing.filed)}</dd>
          <dt>Period end</dt><dd>${esc(s.filing.period_end)}</dd>
          <dt>Accession</dt><dd>${s.filing.url ? `<a href="${esc(s.filing.url)}" rel="noopener">${esc(s.filing.accession)}</a>` : esc(s.filing.accession)}</dd>
        </dl>`)}
      ${section('Business screen', `
        <p>${esc(s.business.result)}${s.business.category ? `, ${esc(s.business.category)}` : ''}. <span class="muted">${esc(s.business.rule)}</span></p>
        ${s.balance_sheet.post_balance_sheet_event ? `<p><b>Event after the balance sheet date:</b> ${esc(s.balance_sheet.post_balance_sheet_event)}</p>` : ''}
        ${s.spot_divergence.factor != null ? `<p class="muted">Spot and 12-month average market cap are ${number(s.spot_divergence.factor)}× apart.</p>` : ''}`)}
      ${factsTable(s.facts)}
    `;
    setDrawer(true);
    el.detail.focus({ preventScroll: true });
    // On a phone the drawer covers the map: bring the stock to the top of the drawer, past the filters. Only the drawer
    // scrolls, so the page header and the map stay where they are.
    if (SMALL.matches) el.drawer.scrollTo({ top: el.detail.offsetTop - el.drawer.querySelector('.drawer-head').offsetHeight });
    history.replaceState(null, '', `#${encodeURIComponent(ticker)}`);
  }

  /* ---------- recent changes ---------- */

  function changeItem(c) {
    const into = c.new_status === 'pass' && c.old_status !== 'pass';
    const outOf = c.old_status === 'pass' && c.new_status !== 'pass';
    const tag = into ? '<span class="badge into">Into pass</span>' : outOf ? '<span class="badge outof">Out of pass</span>' : '';
    const crossings = (c.crossings || []).map((x) => {
      const label = (meta.thresholds[x.ratio] || {}).label || x.ratio;
      return `<li>${esc(label)} ${esc(x.direction)} its limit: ${pct(x.before.ratio)} → ${pct(x.after.ratio)}</li>`;
    }).join('');
    const known = byTicker.has(c.ticker);
    return `<li class="${into ? 'into' : outOf ? 'outof' : ''}">
      <div class="meta">${esc(c.screen_date)} · was screened ${esc(c.previous_screen_date)}</div>
      <div><button type="button" class="stock" ${known ? `data-ticker="${esc(c.ticker)}"` : 'disabled'}>${esc(c.ticker)}</button>
        ${esc(c.name)} ${tag}</div>
      <div>${badge(c.old_status)} → ${badge(c.new_status)}</div>
      <div><b>${esc(CAUSE[c.cause] || c.cause)}.</b> <span class="muted">${esc(c.cause_detail)}</span></div>
      <div>${esc(c.new_reason)}</div>
      ${crossings ? `<ul>${crossings}</ul>` : ''}
    </li>`;
  }

  function indexItem(e) {
    const verb = e.kind === 'added' ? 'Added to the S&P 500' : 'Removed from the S&P 500';
    const known = byTicker.has(e.ticker);
    return `<li>
      <div class="meta">${esc(e.event_date)} · index change</div>
      <div><button type="button" class="stock" ${known ? `data-ticker="${esc(e.ticker)}"` : 'disabled'}>${esc(e.ticker)}</button> ${esc(e.name)}</div>
      <div>${verb}. Not a status change.</div>
    </li>`;
  }

  function drawChanges() {
    const passOnly = el.changesPassOnly.checked;
    const statusItems = changes
      .filter((c) => !passOnly || ((c.new_status === 'pass') !== (c.old_status === 'pass')))
      .map((c) => ({ date: c.screen_date, ticker: c.ticker, html: changeItem(c) }));
    const indexItems = passOnly ? [] : indexEvents.map((e) => ({ date: e.event_date, ticker: e.ticker, html: indexItem(e) }));
    const all = [...statusItems, ...indexItems].sort((a, b) => (a.date === b.date ? a.ticker.localeCompare(b.ticker) : (a.date < b.date ? 1 : -1)));
    const shown = all.slice(0, changesLimit);
    el.changes.innerHTML = shown.length ? shown.map((i) => i.html).join('') : '<li class="muted">No status changes recorded yet.</li>';
    el.changesMore.hidden = all.length <= changesLimit;
    el.changesMore.textContent = `Show all ${all.length}`;
  }

  /* ---------- banner, footer and header ---------- */

  function fillChrome() {
    el.subtitle.textContent = `Screened as of ${meta.screen_date}.`;
    el.statusAsof.textContent = meta.screen_date;
    drawCounts();
    const notices = [];
    // A warning only: the page still shows the export. The threshold is a display constant, not screen data.
    const ageDays = (Date.now() - Date.parse(meta.screen_date)) / 86400000;
    if (ageDays > STALE_DAYS) {
      notices.push(`The data is from ${meta.screen_date}, ${Math.floor(ageDays)} days ago. It may be out of date: check that the screen workflow is running.`);
    }
    if (!meta.config_matches) {
      notices.push(`These results were screened under methodology ${meta.config_hash}, but the current config.yaml is ${meta.current_config_hash}. Re-run the screen before relying on them.`);
    }
    if (!meta.daily_change.included) {
      notices.push('The last-close price change is not in this export.');
    }
    if (meta.counts.screened_before_latest_date) {
      notices.push(`${meta.counts.screened_before_latest_date} stock(s) were last screened before ${meta.screen_date}.`);
    }
    if (notices.length) {
      el.banner.innerHTML = notices.map((n) => `<p>${esc(n)}</p>`).join('');
      el.banner.hidden = false;
    }
    const unsized = meta.unsized || [];
    el.unsizedCount.textContent = unsized.length;
    el.unsizedList.innerHTML = unsized.length
      ? unsized.map((u) => `<tr><td>${esc(u.ticker)}</td><td>${esc(u.name)}</td><td>${badge(u.status)}</td><td>${esc(u.reason)}</td></tr>`).join('')
      : '<tr><td colspan="4" class="muted">Every stock on the list has a current market cap.</td></tr>';
    el.unsized.hidden = unsized.length === 0;
    el.disclaimer.textContent = meta.disclaimer;
    el.thresholds.innerHTML = Object.values(meta.thresholds).map((t) => {
      const comparison = t.operator === '<' ? 'below' : 'at or below';
      return `<li>${esc(t.label)}: ${esc(comparison)} ${trimPct(t.limit)}</li>`;
    }).join('') + '<li>Impure income is interest income only.</li>';
    el.nearDesc.textContent = `Near a limit: a passing ratio within ${marginText()} (${meta.near_threshold.mode} mode). Shown only for pass and needs review.`;
    const m = meta.market_cap;
    const driving = m.driving.startsWith('avg_')
      ? `${m.window_months[m.driving]}-month average of daily market cap`
      : 'latest close';
    el.mcapDesc.textContent = `Verdicts use the ${driving}: closing price × reported shares, with at least ${pct(m.min_coverage, 0)} of expected trading days present. Spot is the latest close, no older than ${m.max_spot_age_days} days; the 36-month average is also stored. Spot and averaged market cap are flagged when they differ by more than ${number(m.spot_divergence_factor)}×.`;
    el.dataDesc.textContent = `Methodology ${meta.config_hash}. Generated ${meta.generated_at ? new Date(meta.generated_at).toLocaleString() : 'unknown'}. Constituents: ${meta.constituents_as_of || 'n/a'}. Screens: ${meta.run_ids.length} run(s), latest ${meta.screen_date}. Filings and XBRL: SEC EDGAR. Prices: yfinance (unofficial), last-close price change only, from the two closes before each screen date. Override reviewers are shown as neutral labels.`;
  }

  // The map fills what the header, the notice banner and the status line leave of the first screen. Measured, so a
  // notice or a narrow header does not push the status line off the screen.
  function fitMap() {
    const head = document.querySelector('header.top').offsetHeight;
    const notice = el.banner.hidden ? 0 : el.banner.offsetHeight;
    const status = document.getElementById('status-line').offsetHeight;
    document.documentElement.style.setProperty('--chrome', `${head + notice + status}px`);
  }

  /* ---------- wiring ---------- */

  function wire() {
    el.controls.addEventListener('change', refresh);
    el.sector.addEventListener('change', () => setSector(el.sector.value));
    el.reset.addEventListener('click', resetFilters);
    el.listQ.addEventListener('input', refresh);
    el.sort.addEventListener('change', refresh);
    el.exportCsv.addEventListener('click', () => downloadCsv(lastShown));
    el.changesPassOnly.addEventListener('change', drawChanges);
    el.changesMore.addEventListener('click', () => { changesLimit = Infinity; drawChanges(); });

    // Status chips are the legend and a filter at once: each toggles its status checkbox in the tools.
    el.counts.addEventListener('click', (ev) => {
      const chip = ev.target.closest('[data-status]');
      if (!chip || DISPLAY) return;
      const box = document.querySelector(`input[name="status"][value="${chip.dataset.status}"]`);
      box.checked = !box.checked;
      refresh();
    });

    // Tools: a toggle, a close button, and Escape. None of them moves the map.
    el.toolsToggle.addEventListener('click', () => setDrawer(!drawerOpen()));
    el.drawerClose.addEventListener('click', () => { setDrawer(false); el.toolsToggle.focus(); });
    el.fnMap.addEventListener('click', () => setDrawer(false));
    el.fnList.addEventListener('click', () => {
      setDrawer(true);
      el.listWrap.open = true;
      el.listQ.focus();
    });
    el.fnChanges.addEventListener('click', () => {
      setDrawer(true);
      el.changesPanel.open = true;
      el.changesPanel.scrollIntoView({ block: 'start' });
    });

    // Command search: type a ticker or a name, move with the arrow keys, Enter opens the stock.
    // The first match is chosen by default, so Enter opens it; the arrow keys move the choice.
    el.q.addEventListener('input', () => { cmdActive = 0; renderResults(); });
    el.q.addEventListener('keydown', (ev) => {
      if (ev.key === 'ArrowDown' || ev.key === 'ArrowUp') {
        if (!cmdList.length) return;
        ev.preventDefault();
        const step = ev.key === 'ArrowDown' ? 1 : -1;
        cmdActive = (cmdActive + step + cmdList.length) % cmdList.length;
        renderResults();
      } else if (ev.key === 'Enter') {
        ev.preventDefault();
        goSearch();
      } else if (ev.key === 'Escape') {
        closeResults();
      }
    });
    el.go.addEventListener('click', goSearch);
    el.qResults.addEventListener('click', (ev) => {
      const item = ev.target.closest('[data-ticker]');
      if (item) chooseStock(item.dataset.ticker);
    });
    document.addEventListener('click', (ev) => {
      if (!ev.target.closest('.cmd')) closeResults();
    });
    document.addEventListener('keydown', (ev) => {
      const typing = ['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement.tagName);
      if (ev.key === '/' && !typing && !DISPLAY) {
        ev.preventDefault();
        el.q.focus();
      } else if (ev.key === 'Escape' && drawerOpen() && !typing) {
        setDrawer(false);
      }
    });

    // Pins and stock links work from the list, the detail panel and the changes list.
    el.listBody.addEventListener('click', (ev) => {
      const pin = ev.target.closest('[data-pin]');
      if (pin) togglePin(pin.dataset.pin);
    });
    el.detail.addEventListener('click', (ev) => {
      const pin = ev.target.closest('[data-pin]');
      if (pin) togglePin(pin.dataset.pin);
    });
    document.addEventListener('click', (ev) => {
      const button = ev.target.closest('[data-ticker]');
      if (button && button.closest('#q-results') === null) openDetail(button.dataset.ticker);
    });
    el.treemap.addEventListener('mouseleave', clearHover);
    el.listWrap.open = !SMALL.matches;
    fitMap();
    window.addEventListener('resize', fitMap);
  }

  async function getJSON(path) {
    const response = await fetch(path, { cache: 'no-cache' });
    if (!response.ok) throw new Error(`${path} returned ${response.status}`);
    return response.json();
  }

  // Kiosk view for a wall display: the map only, larger labels, and a reload when a new export is deployed.
  // Static Pages can only be polled, so this checks meta.json every ten minutes.
  function watchForNewExport(loadedAt) {
    setInterval(async () => {
      try {
        const response = await fetch('../data/meta.json', { cache: 'no-store' });
        if (!response.ok) return;
        const latest = await response.json();
        if (latest.generated_at !== loadedAt) location.reload();
      } catch (error) {
        // Offline or blocked: keep showing what is already on screen.
      }
    }, 10 * 60 * 1000);
  }

  async function start() {
    tickClock();
    setInterval(tickClock, 1000);
    try {
      const [screenData, changeData, metaData] = await Promise.all([
        getJSON('../data/screens.json'), getJSON('../data/changes.json'), getJSON('../data/meta.json'),
      ]);
      meta = metaData;
      screens = screenData.screens;
      changes = changeData.changes;
      indexEvents = changeData.index_events;
    } catch (error) {
      el.loadError.textContent = `Could not load the data (${error.message}). Run .venv/bin/halal-heatmap export, then serve this folder over HTTP: python3 -m http.server 8000 --directory web, and open http://localhost:8000/tool/.`;
      el.loadError.hidden = false;
      el.subtitle.textContent = 'No data loaded.';
      return;
    }
    if (DISPLAY) watchForNewExport(meta.generated_at);
    byTicker = new Map(screens.map((s) => [s.ticker, s]));
    members = screens.filter((s) => s.in_index);
    fillSectorSelector();
    fillChrome();
    wire();
    readFilters();
    el.app.hidden = false;
    refresh();
    drawChanges();
    // A ?sector= link is for looking at one sector, so it opens the tools with that sector selected.
    if (sectorFilter !== 'all') setDrawer(true);
    openFromHash();
    window.addEventListener('hashchange', openFromHash);
  }

  function openFromHash() {
    const ticker = decodeURIComponent(location.hash.slice(1));
    if (byTicker.has(ticker) && ticker !== selected) openDetail(ticker);
  }

  start();
})();
