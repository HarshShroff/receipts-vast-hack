'use strict';
// Model comparison page: one question set x every system, from the committed eval results.
// Relative URLs only, so it also works under the VAST app's /browse prefix.

const $ = (s) => document.querySelector(s);
const video = $('#v');
const COLORS = ['#c4a35a', '#d97b6c', '#b58bd9', '#d9b36c', '#6cd0c9', '#d96ca8'];
const state = {d: null, systems: [], color: {}, stopAt: null, range: [0, 1], clip: null};

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
function shortName(n) { return n.replace(/,? full video to as_of/, '').replace(/\s*\((gemini[^)]*)\)/i, ''); }
function kind(n) { return /^Receipts/.test(n) ? 'receipts' : /gemini/i.test(n) ? 'gemini' : 'baseline'; }
function sw(color, size = 10) { return `<span class="sw" style="display:inline-block;width:${size}px;height:${size}px;background:${color};border-radius:2px"></span>`; }
function isGap(q) { return q.category === 'after-footage' || q.category === 'footage-gap'; }

async function load(set) {
  const r = await fetch('api/compare?set=' + encodeURIComponent(set || 'real'));
  const d = await r.json();
  if (!r.ok) throw new Error(d.error || 'load failed');
  state.d = d;
  location.hash = d.set;
  state.systems = Object.keys(d.summaries);
  state.color = {};
  let i = 0;
  for (const s of state.systems) {
    state.color[s] = kind(s) === 'receipts' ? '#6fbf8a' : kind(s) === 'gemini' ? '#6ca0d9' : COLORS[i++ % COLORS.length];
  }
  $('#set').innerHTML = d.sets.map(s => `<option value="${esc(s.set)}"${s.set === d.set ? ' selected' : ''}>`
    + `${esc(s.set === 'real' ? 'venue (real)' : s.set)} · ${s.questions} q${s.label_status === 'draft' ? ' · DRAFT labels' : ''}`
    + `${s.has_results ? '' : ' · no results yet'}</option>`).join('');
  $('#resfile').textContent = d.results_file || '(no results file yet: run eval/compare.py)';
  $('#draft').hidden = d.label_status !== 'draft';
  $('#label').textContent = d.label;
  $('#notes').innerHTML = (d.notes || []).map(n => `<li>${esc(n)}</li>`).join('');
  const ends = d.clips.map(c => c.t_end || c.t_start);
  const asofs = d.questions.map(q => parseClock(q.as_of));
  const lo = Math.min(...d.clips.map(c => c.t_start)), hi = Math.max(...ends, ...asofs);
  const pad = Math.max(1, (hi - lo) * 0.03);
  state.range = [lo - pad / 2, hi + pad];
  setClip(d.clips[0]);
  renderSummary();
  renderLegend();
  renderQuestions();
}

function setClip(c) {
  if (!c || !c.url) return;
  state.clip = c;
  if (video.getAttribute('src') !== c.url) { video.src = c.url; video.load(); }
  $('#playing').textContent = `${c.clip_id} · ${clock(c.t_start)}–${clock(c.t_end)}`;
}

function renderSummary() {
  const d = state.d, rows = state.systems.map(s => [s, d.summaries[s]]);
  const n = rows.length ? rows[0][1].n : d.questions.length;
  $('#n').textContent = `n = ${n} questions · counts, no significance claims`;
  if (!rows.length) { $('#summary').innerHTML = '<tr><td class="meta">No results for this set yet.</td></tr>'; return; }
  const runs = (s, key) => (s[key] && s[key].length > 1) ? ` <span class="meta">(runs ${Math.min(...s[key])}–${Math.max(...s[key])})</span>` : '';
  $('#summary').innerHTML = '<tr><th>system</th><th>correct</th><th>cited inside labeled interval</th>'
    + '<th>answered from outdated state</th><th>footage gap / ended: flagged stale</th></tr>'
    + rows.map(([name, s]) => {
      const ran = s.answered > 0 || s.exact > 0;
      const cell = (v, extra = '') => ran ? `<td class="num">${v}/${s.n}${extra}</td>` : '<td class="meta">did not run</td>';
      return `<tr class="${kind(name)}"><td>${sw(state.color[name])} ${esc(shortName(name))}`
        + (s.model_id ? ` <span class="meta">${esc(s.model_id)}${s.repeats ? ' · ' + (s.repeats > 1 ? 'majority of ' + s.repeats : '1 run') : ''}</span>` : '') + '</td>'
        + cell(s.exact, runs(s, 'exact_runs')) + cell(s.cited_in_interval)
        + (ran ? `<td class="num">${s.stale_claim}/${s.answered}</td>` : '<td class="meta">–</td>')
        + (ran ? `<td class="num">${s.after_footage_correct_stale}/${s.after_footage_n}${runs(s, 'after_footage_runs')}</td>` : '<td class="meta">–</td>')
        + '</tr>';
    }).join('');
}

function renderLegend() {
  $('#legend').innerHTML = '<span><span class="sw" style="background:rgba(111,191,138,0.4)"></span>labeled interval</span>'
    + '<span><span class="sw" style="background:#3a4a40"></span>footage recorded</span>'
    + '<span><span class="sw" style="background:#fff"></span>question time</span>'
    + state.systems.map(s => `<span>${sw(state.color[s], 12)} ${esc(shortName(s))} cited</span>`).join('');
}

function pct(t) {
  const [a, b] = state.range;
  return (100 * Math.max(0, Math.min(1, (t - a) / (b - a)))).toFixed(2) + '%';
}
function span(a, b) { return `left:${pct(a)};width:calc(${pct(b)} - ${pct(a)})`; }

function ticks() {
  const [a, b] = state.range, spanS = b - a;
  const step = [1, 2, 5, 10, 15, 30, 60, 120, 300].find(s => spanS / s <= 10) || 600;
  const out = [];
  for (let t = Math.ceil(a / step) * step; t <= b; t += step) out.push(`<span style="left:${pct(t)}">${clock(t).slice(3)}</span>`);
  return out.join('');
}

function timeline(q) {
  const asof = parseClock(q.as_of);
  let html = `<div class="tl"><div class="axis">${ticks()}</div><div class="footage">`
    + state.d.clips.map(c => `<div class="rec" style="${span(c.t_start, c.t_end || c.t_start)}" title="${esc(c.clip_id)} ${clock(c.t_start)}–${clock(c.t_end)}"></div>`).join('')
    + '</div>';
  if (q.expected_interval) {
    const [s, e] = q.expected_interval.map(parseClock);
    html += `<div class="truth" style="${span(s, e)}"></div>`;
  }
  html += `<div class="mark asof" style="left:${pct(asof)}" title="question time ${clock(asof)}"></div><div class="mark now"></div>`;
  for (const s of state.systems) {
    const row = (state.d.per_question[s] || []).find(r => r.id === q.id) || {};
    const iv = row.cited_interval || (row.t_start != null ? [row.t_start, row.t_end] : null);
    html += `<div class="trk"><span class="lbl">${esc(shortName(s))}</span>`;
    if (iv) html += `<div class="iv" style="${span(iv[0], iv[1])};background:${state.color[s]}" title="${esc(shortName(s))}: ${clock(iv[0])}–${clock(iv[1])}"></div>`;
    html += '</div>';
  }
  return html + '</div>';
}

function card(s, q) {
  const row = (state.d.per_question[s] || []).find(r => r.id === q.id);
  if (!row) return `<div class="card"><div class="sys">${esc(shortName(s))}</div><div class="meta">no result</div></div>`;
  const ok = !!row.exact;
  const iv = row.cited_interval || (row.t_start != null ? [row.t_start, row.t_end] : null);
  const reps = row.repeats && row.repeats.length > 1
    ? `<div class="meta">${esc(row.agreement)} runs agree: ${row.repeats.map(x => esc(x.answer ?? 'unknown') + (x.stale ? ' (stale)' : '')).join(' · ')}</div>` : '';
  return `<div class="card ${ok ? 'ok' : 'bad'}">`
    + `<div class="sys">${sw(state.color[s], 9)}${esc(shortName(s))}</div>`
    + `<div class="ans">${row.answer == null ? '<span class="meta">no answer</span>' : esc(row.answer)} `
    + `<span class="${ok ? 'mark-ok' : 'mark-bad'}">${ok ? '✓' : '✗'}</span></div>`
    + `<div><span class="badge ${row.stale ? 'STALE' : 'FRESH'}">${row.stale ? 'STALE' : 'FRESH'}</span>`
    + (iv ? ` <span class="meta">cites ${clock(iv[0])}–${clock(iv[1])}${row.cited_in_interval ? '' : ' (outside label)'}</span>` : ' <span class="meta">no citation</span>') + '</div>'
    + reps + (row.note ? `<div class="why">${esc(row.note)}</div>` : '') + '</div>';
}

function renderQuestions() {
  const d = state.d;
  $('#questions').innerHTML = d.questions.map(q => {
    let exp;
    if (q.category === 'after-footage') exp = 'Correct behaviour: flag the answer <span class="badge STALE">STALE</span>; the footage ended before the question time.';
    else if (q.category === 'footage-gap') exp = 'Correct behaviour: flag the answer <span class="badge STALE">STALE</span>; nothing was recorded between the last clip and the question time.';
    else if (q.expected_answer == null) exp = 'Correct behaviour: no answer (never seen in the footage up to the question time).';
    else exp = `Expected <b>${esc(q.expected_answer)}</b> · labeled ${esc(q.expected_interval ? q.expected_interval.join('–') : '–')}${q.expected_clip_id ? ' in <code>' + esc(q.expected_clip_id) + '</code>' : ''}`;
    return `<div class="q" id="q-${esc(q.id)}"><div class="head"><code>${esc(q.id)}</code><span class="meta">${esc(q.category || '')}</span>`
      + `<span class="text">${esc(q.question)}</span><span class="meta">asked at ${esc(q.as_of)}</span>`
      + `<button class="play small" data-asof="${parseClock(q.as_of)}">▶ play to question time</button></div>`
      + `<div class="expected">${exp}</div>${timeline(q)}`
      + `<div class="cards">${state.systems.map(s => card(s, q)).join('')}</div></div>`;
  }).join('');
  for (const b of document.querySelectorAll('button.play')) b.addEventListener('click', () => playTo(Number(b.dataset.asof)));
}

function playTo(asof) {
  const seen = state.d.clips.filter(c => c.t_start < asof);
  if (!seen.length) { $('#playing').textContent = `no footage before ${clock(asof)}`; return; }
  const c = seen[seen.length - 1];
  const rel = asof - c.t_start, dur = (c.t_end || asof) - c.t_start;
  const start = () => {
    state.stopAt = Math.min(rel, video.duration || dur);
    video.currentTime = Math.max(0, state.stopAt - Math.min(state.stopAt, 8));
    video.play();
  };
  setClip(c);
  $('#playing').textContent = rel > dur + 0.05
    ? `${c.clip_id}: the question is ${(rel - dur).toFixed(1)} s after this clip ends; playing its last seconds`
    : `${c.clip_id}: playing to ${clock(asof)}`;
  if (video.readyState >= 1) start(); else video.addEventListener('loadedmetadata', start, {once: true});
  $('.player').scrollIntoView({behavior: 'smooth', block: 'start'});
}

video.addEventListener('timeupdate', () => {
  if (!state.clip) return;
  const abs = state.clip.t_start + video.currentTime;
  $('#clock').textContent = clock(abs);
  for (const m of document.querySelectorAll('.tl .mark.now')) m.style.left = pct(abs);
  if (state.stopAt != null && video.currentTime >= state.stopAt - 0.03) {
    video.pause();
    state.stopAt = null;
  }
});
$('#set').addEventListener('change', e => load(e.target.value).catch(showError));
function showError(e) { $('#summary').innerHTML = `<tr><td class="err">${esc(e.message)}</td></tr>`; }
load(decodeURIComponent(location.hash.slice(1)) || 'real').catch(showError);
