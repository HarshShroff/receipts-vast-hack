'use strict';
// Model comparison page: labeled questions x systems, from the committed eval results.
// Relative URLs only, so it also works under the VAST app's /browse prefix.

const $ = (s) => document.querySelector(s);
const video = $('#v');
const COLORS = ['#c4a35a', '#d97b6c', '#b58bd9', '#d9b36c', '#6cd0c9', '#d96ca8'];
const state = {d: null, systems: [], color: {}, stopAt: null, range: [0, 1], clipEnd: null};

function esc(s) { return String(s ?? '').replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c])); }
function clock(s) {
  if (s == null) return '–';
  const whole = Math.floor(s + 1e-6), frac = s - whole;
  const hms = [Math.floor(whole / 3600), Math.floor(whole % 3600 / 60), whole % 60].map(x => String(x).padStart(2, '0')).join(':');
  return frac > 0.004 ? hms + frac.toFixed(2).slice(1).replace(/0$/, '') : hms;
}
function parseClock(x) {
  if (typeof x === 'number') return x;
  const p = String(x).split(':').map(Number);
  while (p.length < 3) p.push(0);
  return p[0] * 3600 + p[1] * 60 + p[2];
}
function shortName(n) {
  return n.replace(/\s*\((gemini[^)]*)\)/i, '').replace('Gemini Flash, full video to as_of', 'Gemini Flash (full video to as_of)');
}
function kind(n) { return /^Receipts/.test(n) ? 'receipts' : /gemini/i.test(n) ? 'gemini' : 'baseline'; }

async function load() {
  const r = await fetch('api/compare');
  const d = await r.json();
  state.d = d;
  state.systems = Object.keys(d.summaries);
  let i = 0;
  for (const s of state.systems) {
    state.color[s] = kind(s) === 'receipts' ? '#6fbf8a' : kind(s) === 'gemini' ? '#6ca0d9' : COLORS[i++ % COLORS.length];
  }
  $('#resfile').textContent = d.results_file;
  $('#label').textContent = d.label;
  $('#notes').innerHTML = (d.notes || []).map(n => `<li>${esc(n)}</li>`).join('');
  if (d.clip_url) video.src = d.clip_url;
  const asofs = d.questions.map(q => parseClock(q.as_of));
  state.range = [d.clip_start, Math.max(...asofs, d.clip_start + 9) + 1];
  renderSummary();
  renderLegend();
  renderQuestions();
}

function renderSummary() {
  const d = state.d, rows = state.systems.map(s => [s, d.summaries[s]]);
  const n = rows.length ? rows[0][1].n : 0;
  $('#n').textContent = `n = ${n} questions · counts, no significance claims`;
  const runs = (s, key, base) => s[key] ? ` <span class="meta">(runs ${Math.min(...s[key])}–${Math.max(...s[key])})</span>` : '';
  $('#summary').innerHTML = '<tr><th>system</th><th>correct</th><th>cited inside labeled interval</th>'
    + '<th>answered from outdated state</th><th>after footage ended: flagged stale</th></tr>'
    + rows.map(([name, s]) => {
      const ran = s.answered > 0 || s.exact > 0;
      const cell = (v, extra = '') => ran ? `<td class="num">${v}/${s.n}${extra}</td>` : '<td class="meta">did not run</td>';
      return `<tr class="${kind(name)}"><td><span class="sw" style="display:inline-block;width:10px;height:10px;background:${state.color[name]};border-radius:2px"></span> ${esc(shortName(name))}`
        + (s.model_id ? ` <span class="meta">${esc(s.model_id)}${s.repeats ? ' · majority of ' + s.repeats : ''}</span>` : '') + '</td>'
        + cell(s.exact, runs(s, 'exact_runs')) + cell(s.cited_in_interval)
        + (ran ? `<td class="num">${s.stale_claim}/${s.answered}</td>` : '<td class="meta">–</td>')
        + (ran ? `<td class="num">${s.after_footage_correct_stale}/${s.after_footage_n}${runs(s, 'after_footage_runs')}</td>` : '<td class="meta">–</td>')
        + '</tr>';
    }).join('');
}

function renderLegend() {
  $('#legend').innerHTML = '<span><span class="sw" style="background:rgba(111,191,138,0.4)"></span>labeled interval</span>'
    + '<span><span class="sw" style="background:#fff"></span>question time</span>'
    + '<span><span class="sw" style="background:var(--bad)"></span>footage ends</span>'
    + state.systems.map(s => `<span><span class="sw" style="background:${state.color[s]}"></span>${esc(shortName(s))} cited</span>`).join('');
}

function pct(t) {
  const [a, b] = state.range;
  return (100 * Math.max(0, Math.min(1, (t - a) / (b - a)))).toFixed(2) + '%';
}

function timeline(q) {
  const [a, b] = state.range;
  const asof = parseClock(q.as_of);
  const ticks = [];
  for (let t = Math.ceil(a); t <= b; t += 2) ticks.push(`<span style="left:${pct(t)}">${clock(t).slice(3)}</span>`);
  let html = `<div class="tl" data-q="${esc(q.id)}"><div class="axis">${ticks.join('')}</div>`;
  if (q.expected_interval) {
    const [s, e] = q.expected_interval.map(parseClock);
    html += `<div class="truth" style="left:${pct(s)};width:calc(${pct(e)} - ${pct(s)})"></div>`;
  }
  if (state.clipEnd != null && state.clipEnd < b) {
    html += `<div class="gap" style="left:${pct(state.clipEnd)};right:0"></div><div class="mark end" style="left:${pct(state.clipEnd)}" title="footage ends ${clock(state.clipEnd)}"></div>`;
  }
  html += `<div class="mark asof" style="left:${pct(asof)}" title="question time ${clock(asof)}"></div><div class="mark now"></div>`;
  for (const s of state.systems) {
    const row = (state.d.per_question[s] || []).find(r => r.id === q.id) || {};
    const iv = row.cited_interval || (row.t_start != null ? [row.t_start, row.t_end] : null);
    html += `<div class="trk"><span class="lbl">${esc(shortName(s))}</span>`;
    if (iv) html += `<div class="iv" style="left:${pct(iv[0])};width:calc(${pct(iv[1])} - ${pct(iv[0])});background:${state.color[s]}" title="${esc(shortName(s))}: ${clock(iv[0])}–${clock(iv[1])}"></div>`;
    html += '</div>';
  }
  return html + '</div>';
}

function card(s, q) {
  const row = (state.d.per_question[s] || []).find(r => r.id === q.id);
  if (!row) return `<div class="card"><div class="sys">${esc(shortName(s))}</div><div class="meta">no result</div></div>`;
  const ok = !!row.exact;
  const iv = row.cited_interval || (row.t_start != null ? [row.t_start, row.t_end] : null);
  const reps = row.repeats ? `<div class="meta">${esc(row.agreement)} runs agree: ${row.repeats.map(x => esc(x.answer ?? 'unknown') + (x.stale ? ' (stale)' : '')).join(' · ')}</div>` : '';
  return `<div class="card ${ok ? 'ok' : 'bad'}">`
    + `<div class="sys"><span class="sw" style="display:inline-block;width:9px;height:9px;background:${state.color[s]};border-radius:2px"></span>${esc(shortName(s))}</div>`
    + `<div class="ans">${row.answer == null ? '<span class="meta">no answer</span>' : esc(row.answer)} `
    + `<span class="${ok ? 'mark-ok' : 'mark-bad'}">${ok ? '✓' : '✗'}</span></div>`
    + `<div><span class="badge ${row.stale ? 'STALE' : 'FRESH'}">${row.stale ? 'STALE' : 'FRESH'}</span>`
    + (iv ? ` <span class="meta">cites ${clock(iv[0])}–${clock(iv[1])}${row.cited_in_interval ? '' : ' (outside label)'}</span>` : ' <span class="meta">no citation</span>') + '</div>'
    + reps + (row.note ? `<div class="why">${esc(row.note)}</div>` : '') + '</div>';
}

function renderQuestions() {
  const d = state.d;
  $('#questions').innerHTML = d.questions.map(q => {
    const exp = q.category === 'after-footage'
      ? `Correct behaviour: flag the answer <span class="badge STALE">STALE</span> — nothing after the footage ends was seen.`
      : `Expected <b>${esc(q.expected_answer)}</b> · labeled ${esc(q.expected_interval ? q.expected_interval.join('–') : '–')}`;
    return `<div class="q" id="q-${esc(q.id)}"><div class="head"><code>${esc(q.id)}</code><span class="meta">${esc(q.category || '')}</span>`
      + `<span class="text">${esc(q.question)}</span><span class="meta">asked at ${esc(q.as_of)}</span>`
      + `<button class="play small" data-asof="${parseClock(q.as_of)}">▶ play to question time</button></div>`
      + `<div class="expected">${exp}</div>${timeline(q)}`
      + `<div class="cards">${state.systems.map(s => card(s, q)).join('')}</div></div>`;
  }).join('');
  for (const b of document.querySelectorAll('button.play')) {
    b.addEventListener('click', () => {
      const rel = Number(b.dataset.asof) - state.d.clip_start;
      state.stopAt = Math.min(rel, video.duration || rel);
      $('#playing').textContent = rel > (video.duration || rel)
        ? `question time is ${(rel - video.duration).toFixed(1)} s after the footage ends — playing to the end`
        : `playing to ${clock(Number(b.dataset.asof))}`;
      video.currentTime = 0;
      video.play();
      $('.player').scrollIntoView({behavior: 'smooth', block: 'start'});
    });
  }
}

video.addEventListener('loadedmetadata', () => {
  state.clipEnd = state.d.clip_start + video.duration;
  renderQuestions();
});
video.addEventListener('timeupdate', () => {
  const t = video.currentTime;
  $('#clock').textContent = clock(state.d ? state.d.clip_start + t : t);
  for (const m of document.querySelectorAll('.tl .mark.now')) m.style.left = pct(state.d.clip_start + t);
  if (state.stopAt != null && t >= state.stopAt - 0.03) {
    video.pause();
    state.stopAt = null;
  }
});
load().catch(e => { $('#summary').innerHTML = `<tr><td class="err">${esc(e.message)}</td></tr>`; });
