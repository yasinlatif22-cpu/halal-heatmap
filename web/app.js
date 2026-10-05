/* Halal Heatmap: draws the exported screens. It computes no verdicts; every number, threshold and
   label shown here comes from data/screens.json, data/changes.json or data/meta.json. */
(() => {
  'use strict';

  const STATUS = {
    pass: { label: 'Pass', symbol: '✓', fill: '#8fd19e' },
    needs_review: { label: 'Needs review', symbol: '?', fill: '#f5d76e' },
    fail: { label: 'Fail', symbol: '✕', fill: '#ef8a80' },
    insufficient_data: { label: 'Insufficient data', symbol: '–', fill: '#c9ced4' },
  };
  const NEAR_STATUSES = ['pass', 'needs_review'];
  const NEAR_LINE = '#f2c200';         // pass within the margin: yellow border
  const REVIEW_NEAR = '#d9a900';       // needs review within the margin: darker yellow, hatched
  const HATCH = '#6b4f00';
  const DIM = '#ececec';
  const NEUTRAL = '#e7ecf2';
  const UNSIZED = '#d0d5dc';
  const FLAG_ORDER = ['denominator', 'spot', 'share', 'stale', 'override'];
  const FLAG_GLYPH = { denominator: '≠', spot: '↕', share: '#', stale: '!', override: '✎' };
  const FLAG_LABEL = {
    denominator: 'Denominator-sensitive: the three market cap windows give different verdicts',
    spot: 'Spot divergence: spot and averaged market cap differ by more than the set factor',
    share: 'Share count flagged: rejected or a large jump between filings',
    stale: 'Balance sheet out of date: spin-off or disposition after the balance sheet date',
    override: 'Override applied: a manual decision resolves a needs-review result',
  };
  // Every basis except disclosed gross interest is lower confidence, on the tile, panel and legend.
  const BASIS = {
    disclosed: { short: 'Disclosed interest', filter: 'Disclosed interest (gross)', long: 'Disclosed interest income, trailing 12 months', confidence: 'standard' },
    disclosed_partial: {
      short: 'Disclosed, partial', filter: 'Disclosed, summed over segments (partial)',
      long: 'Interest income summed over segments: partial, so it supports a pass only well below the limit', confidence: 'lower',
    },
    annual_fallback: {
      short: 'Annual figure', filter: 'Annual figure (older than 12 months)',
      long: 'Latest annual interest income, older than 12 months', confidence: 'lower',
    },
    net_investment_income: {
      short: 'Net investment income', filter: 'Net investment income',
      long: 'Net investment income, not gross interest: supports a pass only well below the limit', confidence: 'lower',
    },
    upper_bound_cash: {
      short: 'Upper bound on cash', filter: 'Upper bound on cash',
      long: 'No interest income disclosed: an upper bound from cash and interest-bearing securities', confidence: 'lower',
    },
    upper_bound_total_assets: {
      short: 'Upper bound on assets', filter: 'Upper bound on total assets',
      long: 'No interest income or investments disclosed: an upper bound from total assets', confidence: 'lower',
    },
    none: { short: 'No figure', filter: 'No interest income figure', long: 'No interest income basis available', confidence: 'none' },
  };
  const BASIS_GLYPH = '○';
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
  const UP = [46, 125, 50];
  const DOWN = [198, 40, 40];
  const WHITE = [255, 255, 255];
  const SMALL = window.matchMedia('(max-width: 960px)');

  const $ = (id) => document.getElementById(id);
  const el = {
    subtitle: $('subtitle'), banner: $('banner'), loadError: $('load-error'), app: $('app'),
    q: $('q'), nearOnly: $('near-only'), reset: $('reset'), count: $('count'),
    unsized: $('unsized'), unsizedCount: $('unsized-count'), unsizedList: $('unsized-list'),
    colourChange: $('colour-change'), colourChangeLabel: $('colour-change-label'),
    legend: $('legend'), treemap: $('treemap'), listBody: $('list-body'), listCount: $('list-count'),
    listWrap: $('list-wrap'), detail: $('detail'), changes: $('changes'), changesPassOnly: $('changes-pass-only'),
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

  /* ---------- formatting and small helpers ---------- */

  const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
  const pct = (value, digits = 2) => (value == null ? 'n/a' : `${(value * 100).toFixed(digits)}%`);
  const trimPct = (value) => `${Number((value * 100).toFixed(2))}%`;
  const signedPct = (value) => (value == null ? 'n/a' : `${value >= 0 ? '+' : ''}${(value * 100).toFixed(2)}%`);
  const money = (value) => {
    if (value == null) return 'n/a';
    const abs = Math.abs(value);
    if (abs >= 1e9) return `$${(value / 1e9).toFixed(2)} bn`;
    if (abs >= 1e6) return `$${(value / 1e6).toFixed(1)} m`;
    return `$${Math.round(value).toLocaleString('en-US')}`;
  };
  const number = (value) => (value == null ? 'n/a' : Number(value).toLocaleString('en-US', { maximumFractionDigits: 2 }));
  const badge = (status) => {
    const s = STATUS[status] || { label: status, symbol: '' };
    return `<span class="badge st-${esc(status)}"><span aria-hidden="true">${s.symbol}</span>${esc(s.label)}</span>`;
  };
  const isNear = (s) => NEAR_STATUSES.includes(s.status) && s.near_threshold;
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
  const mix = (from, to, t) => `rgb(${from.map((c, i) => Math.round(c + (to[i] - c) * t)).join(',')})`;
  const changeColour = (pctValue, scale) => {
    if (pctValue == null) return UNSIZED;
    const t = Math.min(1, Math.abs(pctValue) / scale);
    return pctValue >= 0 ? mix(WHITE, UP, t) : mix(WHITE, DOWN, t);
  };
  const changeScale = () => {
    const values = members.filter((s) => s.daily_change).map((s) => Math.abs(s.daily_change.pct)).sort((a, b) => a - b);
    if (!values.length) return 0.01;
    return Math.max(values[Math.floor((values.length - 1) * 0.9)], 0.001);
  };

  /* ---------- filters ---------- */

  function readFilters() {
    const checked = (name) => [...document.querySelectorAll(`input[name="${name}"]:checked`)].map((i) => i.value);
    filters = {
      q: el.q.value.trim().toLowerCase(),
      statuses: new Set(checked('status')),
      near: el.nearOnly.checked,
      bases: new Set(checked('basis')),
      flags: new Set(checked('flag')),
    };
  }

  function visible(s) {
    if (!filters.statuses.has(s.status)) return false;
    if (filters.near && !isNear(s)) return false;
    if (!filters.bases.has(s.interest_income.key)) return false;
    if (filters.flags.size && ![...filters.flags].some((key) => flagOn(s, key))) return false;
    if (filters.q && !`${s.ticker} ${s.name}`.toLowerCase().includes(filters.q)) return false;
    return true;
  }

  /* ---------- treemap ---------- */

  const lowerBasis = (s) => BASIS[s.interest_income.key].confidence === 'lower';

  function tileLabel(s) {
    const near = isNear(s) ? ' ◆' : '';
    const basis = lowerBasis(s) ? ` ${BASIS_GLYPH}` : '';
    const glyphs = FLAG_ORDER.filter((key) => flagOn(s, key)).map((key) => FLAG_GLYPH[key]).join(' ');
    return `${s.ticker} ${STATUS[s.status].symbol}${near}${basis}${glyphs ? ` ${glyphs}` : ''}`;
  }

  function hoverText(s) {
    const lines = [
      `<b>${esc(s.ticker)}</b> ${esc(s.name)}`,
      `${esc(STATUS[s.status].label)}${isNear(s) ? ' · within the margin of a limit' : ''}`,
      `Market cap (spot) ${money(s.spot_market_cap)}`,
      `Daily change ${s.daily_change ? `${signedPct(s.daily_change.pct)} on ${esc(s.daily_change.date)}` : 'not available'}`,
      `Basis: ${esc(BASIS[s.interest_income.key].short)}${lowerBasis(s) ? ' (lower confidence)' : ''}`,
      'Click for the audit record',
    ];
    return lines.join('<br>');
  }

  function leafColour(s, scale) {
    if (!visible(s)) return DIM;
    if (colourMode() === 'change') return changeColour(s.daily_change ? s.daily_change.pct : null, scale);
    return STATUS[s.status].fill;
  }

  function colourMode() {
    return el.colourChange.checked ? 'change' : 'status';
  }

  function drawTreemap() {
    const scale = changeScale();
    const sectors = new Map();
    for (const s of members) {
      const key = s.sector || 'Unclassified';
      if (!sectors.has(key)) sectors.set(key, []);
      sectors.get(key).push(s);
    }
    const sized = members.filter((s) => s.spot_market_cap != null);
    const total = sized.reduce((sum, s) => sum + s.spot_market_cap, 0);
    const ids = ['all'];
    const labels = ['S&P 500'];
    const parents = [''];
    const values = [total];
    const colours = [NEUTRAL];
    const lines = ['#ffffff'];
    const lineWidth = [1];
    const shape = [''];
    const fg = [HATCH];
    const bg = [NEUTRAL];
    const solidity = [0.4];
    const custom = [`<b>S&P 500</b><br>${sized.length} stocks on the map`];

    for (const [sector, stocks] of [...sectors.entries()].sort((a, b) => a[0].localeCompare(b[0]))) {
      const sectorValue = stocks.reduce((sum, s) => sum + (s.spot_market_cap || 0), 0);
      ids.push(`sector:${sector}`);
      labels.push(sector);
      parents.push('all');
      values.push(sectorValue);
      colours.push(NEUTRAL);
      lines.push('#ffffff');
      lineWidth.push(1);
      shape.push('');
      fg.push(HATCH);
      bg.push(NEUTRAL);
      solidity.push(0.4);
      custom.push(`<b>${esc(sector)}</b>`);
      for (const s of stocks.filter((x) => x.spot_market_cap != null)) {
        const near = isNear(s);
        const hatched = near && s.status === 'needs_review';
        ids.push(s.ticker);
        labels.push(tileLabel(s));
        parents.push(`sector:${sector}`);
        values.push(s.spot_market_cap);
        colours.push(hatched ? REVIEW_NEAR : leafColour(s, scale));
        lines.push(near && s.status === 'pass' ? NEAR_LINE : '#ffffff');
        lineWidth.push(near && s.status === 'pass' ? 4 : 1);
        shape.push(hatched ? '/' : '');
        fg.push(HATCH);
        bg.push(hatched ? REVIEW_NEAR : leafColour(s, scale));
        solidity.push(0.45);
        custom.push(hoverText(s));
      }
    }

    const trace = {
      type: 'treemap',
      ids,
      labels,
      parents,
      values,
      branchvalues: 'total',
      customdata: custom,
      hovertemplate: '%{customdata}<extra></extra>',
      marker: {
        colors: colours,
        line: { color: lines, width: lineWidth },
        pattern: { shape, fgcolor: fg, bgcolor: bg, solidity },
      },
      textfont: { size: 13 },
      tiling: { pad: 2 },
      pathbar: { visible: true, thickness: 22 },
    };
    const layout = {
      margin: { l: 0, r: 0, t: 0, b: 0 },
      paper_bgcolor: 'rgba(0,0,0,0)',
      plot_bgcolor: 'rgba(0,0,0,0)',
      font: { family: 'system-ui, sans-serif', color: '#17202a' },
      uniformtext: { minsize: 9, mode: 'hide' },
    };
    const config = { responsive: true, displaylogo: false, modeBarButtonsToRemove: ['lasso2d', 'select2d'] };
    Plotly.react(el.treemap, [trace], layout, config).then(() => {
      if (plotBound) return;
      plotBound = true;
      el.treemap.on('plotly_click', (ev) => {
        const point = ev.points && ev.points[0];
        if (!point) return;
        const id = point.data.ids[point.pointNumber];
        if (byTicker.has(id)) openDetail(id);
      });
    });
    drawLegend(scale);
  }

  /* ---------- legend, list, counts ---------- */

  function drawLegend(scale) {
    const swatch = (cls, glyph) => `<span class="swatch ${cls}" aria-hidden="true">${glyph}</span>`;
    if (colourMode() === 'change') {
      el.legend.innerHTML = `
        <span class="item"><span class="gradient" style="background:linear-gradient(to right, rgb(${DOWN}), #fff, rgb(${UP}))"></span>
          <span>−${trimPct(scale)} to +${trimPct(scale)} daily change, dimmed tiles are filtered out</span></span>
        <span class="item">${swatch('pass-near', '◆')} Within ${esc(marginText())}: yellow border</span>
        <span class="item">${swatch('review-near', '◆')} Needs review within ${esc(marginText())}: hatched</span>
        <span class="item"><span class="glyph" aria-hidden="true">${BASIS_GLYPH}</span> Lower-confidence interest basis</span>`;
    } else {
      el.legend.innerHTML = `
        <span class="item">${swatch('pass', '✓')} Pass</span>
        <span class="item">${swatch('pass-near', '◆')} Pass within ${esc(marginText())}: green with yellow border</span>
        <span class="item">${swatch('review', '?')} Needs review</span>
        <span class="item">${swatch('review-near', '◆')} Needs review within ${esc(marginText())}: darker, hatched</span>
        <span class="item">${swatch('fail', '✕')} Fail</span>
        <span class="item">${swatch('insufficient', '–')} Insufficient data</span>
        <span class="item"><span class="glyph" aria-hidden="true">${BASIS_GLYPH}</span> Lower-confidence interest basis (not disclosed gross interest)</span>
        <span class="item">${FLAG_ORDER.map((k) => `<span title="${esc(FLAG_LABEL[k])}">${FLAG_GLYPH[k]} ${esc(FLAG_LABEL[k].split(':')[0])}</span>`).join(' · ')}</span>`;
    }
  }

  function drawList(visibleStocks) {
    el.listBody.innerHTML = visibleStocks.map((s) => {
      const near = isNear(s) ? `<span class="mark" aria-hidden="true">◆</span> Yes` : 'No';
      return `<tr>
        <td><button type="button" class="stock" data-ticker="${esc(s.ticker)}">${esc(s.ticker)}</button></td>
        <td>${esc(s.name)}</td>
        <td>${badge(s.status)}</td>
        <td>${near}</td>
        <td>${esc(BASIS[s.interest_income.key].short)}${lowerBasis(s) ? ' <span class="badge lower">lower confidence</span>' : ''}</td>
        <td class="num">${s.daily_change ? esc(signedPct(s.daily_change.pct)) : 'n/a'}</td>
      </tr>`;
    }).join('');
    el.listCount.textContent = visibleStocks.length;
  }

  function refresh() {
    readFilters();
    const shown = members.filter(visible);
    el.count.textContent = `Showing ${shown.length} of ${members.length} stocks`;
    drawTreemap();
    drawList(shown);
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
    return `<details><summary>XBRL facts used (${facts.length})</summary>
      <div class="table-scroll"><table>
        <thead><tr><th scope="col">Input</th><th scope="col">Tag</th><th scope="col">Period</th>
          <th scope="col" class="num">Value</th><th scope="col">Filing</th></tr></thead>
        <tbody>${rows}</tbody></table></div></details>`;
  }

  function ratioTable(s) {
    const rows = ['debt', 'cash', 'impure_income'].map((key) => {
      const r = s.ratios[key];
      if (r.ratio == null) return `<tr><th scope="row">${esc(r.label)}</th><td colspan="5">not available</td></tr>`;
      const result = r.passed ? 'Within limit' : 'Over limit';
      const near = r.near_threshold ? ' <span class="mark" aria-hidden="true">◆</span> near' : '';
      return `<tr>
        <th scope="row">${esc(r.label)}</th>
        <td class="num">${pct(r.ratio)}</td>
        <td class="num">${esc(r.operator)} ${trimPct(r.limit)}</td>
        <td class="num">${(r.headroom * 100).toFixed(2)} points<br><span class="muted">${pct(r.headroom_rel, 0)} of limit</span></td>
        <td>${result}${near}</td>
        <td class="muted">${money(r.numerator)} ÷ ${money(r.denominator)}<br>${esc(r.denominator_basis)}</td>
      </tr>`;
    }).join('');
    return `<table><thead><tr><th scope="col">Ratio</th><th scope="col" class="num">Value</th>
      <th scope="col" class="num">Limit</th><th scope="col" class="num">Headroom</th><th scope="col">Result</th>
      <th scope="col">Figures</th></tr></thead><tbody>${rows}</tbody></table>`;
  }

  function marketCapTable(s) {
    const rows = ['spot', 'avg_12m', 'avg_36m'].map((name) => {
      const d = s.market_cap.denominators[name];
      const driving = name === s.market_cap.driving ? ' <span class="mark">(drives verdict)</span>' : '';
      const window = d.start ? `${esc(d.start)} to ${esc(d.end)}` : 'n/a';
      return `<tr>
        <th scope="row">${name === 'spot' ? 'Spot' : name === 'avg_12m' ? '12-month average' : '36-month average'}${driving}</th>
        <td class="num">${money(d.value)}</td>
        <td>${window}</td>
        <td class="num">${d.observations ?? 'n/a'}</td>
        <td>${d.status ? badge(d.status) : 'n/a'}</td>
        <td class="num">${pct(d.debt_ratio)}</td>
        <td class="num">${pct(d.cash_ratio)}</td>
      </tr>`;
    }).join('');
    return `<table><thead><tr><th scope="col">Market cap</th><th scope="col" class="num">Value</th>
      <th scope="col">Window</th><th scope="col" class="num">Days</th><th scope="col">Would be</th>
      <th scope="col" class="num">Debt</th><th scope="col" class="num">Cash</th></tr></thead>
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
    const verdictBadge = badge(s.status);
    const nearBadge = isNear(s) ? ` <span class="badge lower"><span aria-hidden="true">◆</span> Near a limit</span>` : '';
    const change = s.daily_change
      ? `${signedPct(s.daily_change.pct)} on ${esc(s.daily_change.date)}`
      : (meta.daily_change.included ? 'not available' : 'not included in this export');
    const sharesInfo = s.shares.classes
      ? `${s.shares.classes.length} classes; ${s.shares.unlisted ? `${pct(s.shares.unlisted)} unlisted` : 'listed'}`
      : esc(s.shares.source);
    el.detail.innerHTML = `
      <h2>${esc(s.ticker)} · ${esc(s.name)}</h2>
      <p class="muted">${esc(s.sector || 'No sector')} · ${esc(s.sub_industry || 'no sub-industry')}</p>
      <p>${verdictBadge}${nearBadge}</p>
      <p><b>Reason.</b> ${esc(s.reason)}</p>
      ${flagChips(s)}
      <dl class="kv">
        <dt>Screen date</dt><dd>${esc(s.screen_date)}</dd>
        <dt>Driving market cap</dt><dd>${esc(s.market_cap.driving)}</dd>
        <dt>Daily change</dt><dd>${change}</dd>
        <dt>Methodology</dt><dd>${esc(s.config_hash)}</dd>
      </dl>
      <h3>Financial ratios</h3>
      ${ratioTable(s)}
      <h3>Market cap</h3>
      ${marketCapTable(s)}
      <p class="muted">Market cap is shares × closing price. The average covers ${esc(meta.market_cap.window_months[s.market_cap.driving] ?? '')} months of trading days, with at least ${pct(meta.market_cap.min_coverage, 0)} of them present.</p>
      ${interestBlock(s)}
      <h3>Inputs</h3>
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
      ${overrideBlock(s)}
      <h3>Filing used</h3>
      <dl class="kv">
        <dt>Form</dt><dd>${esc(s.filing.form)}</dd>
        <dt>Filed</dt><dd>${esc(s.filing.filed)}</dd>
        <dt>Period end</dt><dd>${esc(s.filing.period_end)}</dd>
        <dt>Accession</dt><dd>${s.filing.url ? `<a href="${esc(s.filing.url)}" rel="noopener">${esc(s.filing.accession)}</a>` : esc(s.filing.accession)}</dd>
      </dl>
      <h3>Business screen</h3>
      <p>${esc(s.business.result)}${s.business.category ? `, ${esc(s.business.category)}` : ''}. <span class="muted">${esc(s.business.rule)}</span></p>
      ${s.balance_sheet.post_balance_sheet_event ? `<p><b>Event after the balance sheet date:</b> ${esc(s.balance_sheet.post_balance_sheet_event)}</p>` : ''}
      ${s.spot_divergence.factor != null ? `<p class="muted">Spot and 12-month average market cap are ${number(s.spot_divergence.factor)}× apart.</p>` : ''}
      ${factsTable(s.facts)}
    `;
    el.detail.focus({ preventScroll: true });
    if (SMALL.matches) el.detail.scrollIntoView({ behavior: 'smooth', block: 'start' });
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
    const generated = meta.generated_at ? new Date(meta.generated_at).toLocaleString() : 'unknown';
    el.subtitle.textContent = `Screened as of ${meta.screen_date}. Methodology ${meta.config_hash}. Generated ${generated}.`;
    const notices = [];
    if (!meta.config_matches) {
      notices.push(`These results were screened under methodology ${meta.config_hash}, but the current config.yaml is ${meta.current_config_hash}. Re-run the screen before relying on them.`);
    }
    if (!meta.daily_change.included) {
      notices.push('The daily price change is not in this export, so the colour option is off.');
    }
    if (meta.counts.screened_before_latest_date) {
      notices.push(`${meta.counts.screened_before_latest_date} stock(s) were last screened before ${meta.screen_date}.`);
    }
    if (notices.length) {
      el.banner.innerHTML = notices.map((n) => `<p>${esc(n)}</p>`).join('');
      el.banner.hidden = false;
    }
    if (!meta.daily_change.included || !members.some((s) => s.daily_change)) {
      el.colourChange.disabled = true;
      el.colourChangeLabel.title = 'Daily price change is not in this export';
    }
    const unsized = meta.unsized || [];
    el.unsizedCount.textContent = unsized.length;
    el.unsizedList.innerHTML = unsized.length
      ? unsized.map((u) => `<tr><td>${esc(u.ticker)}</td><td>${esc(u.name)}</td><td>${badge(u.status)}</td><td>${esc(u.reason)}</td></tr>`).join('')
      : '<tr><td colspan="4" class="muted">Every stock on the list has a current market cap.</td></tr>';
    el.unsized.hidden = false;
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
    el.dataDesc.textContent = `Constituents: ${meta.constituents_as_of || 'n/a'}. Screens: ${meta.run_ids.length} run(s), latest ${meta.screen_date}. Filings and XBRL: SEC EDGAR. Prices: yfinance (unofficial), daily change only, from the two closes before each screen date. Override reviewers are shown as neutral labels.`;
  }

  /* ---------- wiring ---------- */

  function wire() {
    const controls = document.getElementById('controls');
    controls.addEventListener('input', refresh);
    controls.addEventListener('change', refresh);
    el.reset.addEventListener('click', () => {
      el.q.value = '';
      el.nearOnly.checked = false;
      document.querySelectorAll('input[name="basis"]').forEach((i) => { i.checked = true; });
      document.querySelectorAll('input[name="status"]').forEach((i) => { i.checked = true; });
      document.querySelectorAll('input[name="flag"]').forEach((i) => { i.checked = false; });
      refresh();
    });
    el.changesPassOnly.addEventListener('change', drawChanges);
    el.changesMore.addEventListener('click', () => { changesLimit = Infinity; drawChanges(); });
    document.addEventListener('click', (ev) => {
      const button = ev.target.closest('[data-ticker]');
      if (button) openDetail(button.dataset.ticker);
    });
    SMALL.addEventListener('change', () => { el.listWrap.open = SMALL.matches; });
    el.listWrap.open = SMALL.matches;
  }

  async function getJSON(path) {
    const response = await fetch(path, { cache: 'no-cache' });
    if (!response.ok) throw new Error(`${path} returned ${response.status}`);
    return response.json();
  }

  async function start() {
    try {
      const [screenData, changeData, metaData] = await Promise.all([
        getJSON('data/screens.json'), getJSON('data/changes.json'), getJSON('data/meta.json'),
      ]);
      meta = metaData;
      screens = screenData.screens;
      changes = changeData.changes;
      indexEvents = changeData.index_events;
    } catch (error) {
      el.loadError.textContent = `Could not load the data (${error.message}). Run .venv/bin/halal-heatmap export, then serve this folder over HTTP: python3 -m http.server 8000 --directory web, and open http://localhost:8000.`;
      el.loadError.hidden = false;
      el.subtitle.textContent = 'No data loaded.';
      return;
    }
    byTicker = new Map(screens.map((s) => [s.ticker, s]));
    members = screens.filter((s) => s.in_index);
    fillChrome();
    wire();
    el.app.hidden = false;
    refresh();
    drawChanges();
    openFromHash();
    window.addEventListener('hashchange', openFromHash);
  }

  function openFromHash() {
    const ticker = decodeURIComponent(location.hash.slice(1));
    if (byTicker.has(ticker) && ticker !== selected) openDetail(ticker);
  }

  start();
})();
