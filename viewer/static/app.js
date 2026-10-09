'use strict';
// Receipts clip viewer. All URLs are relative so the page also works behind a path prefix.

const $ = (s) => document.querySelector(s);
const video = $('#v'), canvas = $('#overlay'), ctx = canvas.getContext('2d');
const PALETTE = ['#c4a35a', '#6fbf8a', '#6ca0d9', '#d97b6c', '#b58bd9', '#d9b36c', '#6cd0c9', '#d96ca8'];
const PINNED = [
  'run_10_seed_213384163.ceiling_04.rgb_chunk_0000_segment_001_of_002.mp4',
  'run_7_seed_900334964.eye_04.rgb_chunk_0000_segment_002_of_002.mp4',
];
const state = {clips: [], clip: null, meta: null, hidden: new Set(), activeKey: '', cite: null,
               tmin: 0, tmax: 1, questions: [], clock: {}};

function colorFor(name) {
  let h = 0;
  for (const ch of String(name)) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return PALETTE[h % PALETTE.length];
}
function fmtRel(s) {  // clip-relative seconds -> m:ss.s
  if (s == null) return '–';
  const m = Math.floor(s / 60), r = s - m * 60;
  return m + ':' + r.toFixed(1).padStart(4, '0');
}
function fmtAbs(s) {  // shared timeline seconds -> hh:mm:ss
  if (s == null) return '–';
  s = Math.round(s);
  return [Math.floor(s / 3600), Math.floor(s % 3600 / 60), s % 60].map(x => String(x).padStart(2, '0')).join(':');
}
function esc(s) { return String(s ?? '').replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c])); }
function badge(status) { return `<span class="badge ${esc(status)}">${esc(status).replace('_', ' ')}</span>`; }
async function getJSON(url) {
  const r = await fetch(url);
  const d = await r.json();
  if (!r.ok) throw new Error(d.error || (url + ': ' + r.status));
  return d;
}

// ------------------------------------------------------------- clips --
function shortName(id) {
  const m = String(id).match(/(ceiling_\d+|eye_\d+).*?segment_(\d+)/);
  if (m) return m[1] + ' · seg ' + m[2];
  return id.length > 56 ? id.slice(0, 54) + '…' : id;
}
function sameClip(a, b) {
  if (!a || !b) return false;
  return a === b || a.endsWith(b) || b.endsWith(a);
}
function hms(s) {
  if (s == null || s === '') return null;
  const parts = String(s).trim().split(':').map(Number);
  if (!parts.length || parts.some(n => Number.isNaN(n))) return null;
  if (parts.length === 3) return parts[0] * 3600 + parts[1] * 60 + parts[2];
  if (parts.length === 2) return parts[0] * 60 + parts[1];
  return parts[0];
}
function questionOffset(q) {
  const origin = hms(q.clip_origin || state.clock[q.expected_clip_id] || '0:00:00');
  const at = hms(q.as_of);
  if (origin == null || at == null) return 0;
  return Math.max(0, at - origin);
}
function addOption(group, clip) {
  const o = document.createElement('option');
  o.value = clip.clip_id;
  const when = clip.t_start != null ? ` · ${fmtAbs(clip.t_start)}–${fmtAbs(clip.t_end)}` : '';
  o.textContent = clip.source === 'vast'
    ? `${shortName(clip.clip_id)}${when}`
    : `${clip.clip_id} · ${clip.scene}${when}`;
  o.title = clip.clip_id;
  group.appendChild(o);
}
async function loadVastSegments() {
  try {
    return await getJSON('../api/segments');
  } catch (e) {
    return {segments: [], error: e.message};
  }
}
async function loadClips() {
  const d = await getJSON('api/clips');
  let qdoc = {questions: [], clip_clock: {}};
  try { qdoc = await getJSON('api/questions'); } catch (e) { /* questions file optional */ }
  state.questions = qdoc.questions || [];
  state.clock = qdoc.clip_clock || {};
  const vast = await loadVastSegments();
  const segments = vast.segments || [];
  const used = new Set();
  const pinned = PINNED.map(name => {
    const seg = segments.find(s => sameClip(s.clip_id, name));
    if (seg) used.add(seg.clip_id);
    const id = seg ? seg.clip_id : name;
    return {clip_id: id, url: '../api/clip?clip_id=' + encodeURIComponent(id),
            scene: 'warehouse', description: (seg && seg.caption) || name,
            t_start: seg && seg.t_start, t_end: seg && seg.t_end,
            source: 'vast', group: 'pinned', in_archive: !!seg};
  });
  const archive = segments.filter(s => !used.has(s.clip_id)).map(s => ({
    clip_id: s.clip_id, url: '../api/clip?clip_id=' + encodeURIComponent(s.clip_id),
    scene: 'warehouse', description: s.caption || '', t_start: s.t_start, t_end: s.t_end,
    source: 'vast', group: 'archive', in_archive: true,
  }));
  const local = d.clips.filter(c => !String(c.clip_id).endsWith('_noaudio')).map(c => Object.assign(c, {source: 'local', group: 'local'}));
  state.clips = pinned.concat(local, archive);
  state.tmin = d.t_min; state.tmax = d.t_max;
  const sel = $('#clip');
  sel.innerHTML = '';
  const groups = [['Warehouse — start here', pinned], ['Local clips', local], ['Rest of the archive', archive]];
  for (const [label, items] of groups) {
    if (!items.length) continue;
    const g = document.createElement('optgroup');
    g.label = label;
    for (const c of items) addOption(g, c);
    sel.appendChild(g);
  }
  const asof = $('#asof');
  asof.min = d.t_min; asof.max = d.t_max; asof.value = d.t_max;
  $('#asofval').textContent = fmtAbs(d.t_max);
  $('#maxage').value = d.max_age_default;
  const notes = [];
  if (!d.db) notes.push('local receipts.db not built yet');
  if (vast.error) notes.push('archive: ' + vast.error);
  const missing = pinned.filter(c => !c.in_archive).map(c => shortName(c.clip_id));
  if (segments.length && missing.length) notes.push('not in the index yet: ' + missing.join(', '));
  $('#dbnote').textContent = notes.join(' · ');
  fillKeys(d.db ? await getJSON('api/keys') : []);
  const hash = decodeURIComponent(location.hash.slice(1));
  const pinnedReady = pinned.find(c => c.in_archive);
  const first = (hash && state.clips.some(c => c.clip_id === hash) && hash)
    || (pinnedReady && pinnedReady.clip_id)
    || (local[0] && local[0].clip_id)
    || (pinned[0] && pinned[0].clip_id);
  if (first) { sel.value = first; await selectClip(first); }
}

function fillKeys(keys) {
  const sel = $('#keysel');
  const uniq = (f) => [...new Set(keys.map(f))];
  for (const [id, f] of [['dl-entity', k => k.entity], ['dl-attribute', k => k.attribute], ['dl-location', k => k.location]]) {
    $('#' + id).innerHTML = uniq(f).map(v => `<option value="${esc(v)}">`).join('');
  }
  for (const k of keys) {
    const o = document.createElement('option');
    o.value = JSON.stringify([k.entity, k.attribute, k.location]);
    o.textContent = `${k.entity} / ${k.attribute} @ ${k.location || '-'}  (${k.n})`;
    sel.appendChild(o);
  }
  sel.addEventListener('change', () => {
    if (!sel.value) return;
    const [e, a, l] = JSON.parse(sel.value);
    $('#entity').value = e; $('#attribute').value = a; $('#location').value = l;
  });
}

function seekVideo(t) {
  const seek = () => { video.currentTime = t; };
  if (video.readyState >= 1) seek(); else video.addEventListener('loadedmetadata', seek, {once: true});
}

async function selectClip(id, seekTo) {
  const c = state.clips.find(x => x.clip_id === id);
  if (!c) return;
  state.clip = c;
  location.hash = encodeURIComponent(id);
  $('#clip').value = id;
  if (video.getAttribute('src') !== c.url) { video.src = c.url; video.load(); }
  state.hidden.clear();
  state.activeKey = '';
  if (c.source === 'vast') {
    state.meta = {has_sidecar: false, tracks: [], segments: [], zones: {}, in_db: false,
                  video: {duration: (c.t_end != null && c.t_start != null) ? (c.t_end - c.t_start) : null}};
    indexTracks(state.meta);
    renderClipInfo();
    renderTimeline();
    renderTracks();
    renderQuestions();
    await renderVast(c);
  } else {
    state.meta = await getJSON(`api/clips/${encodeURIComponent(id)}/meta`);
    indexTracks(state.meta);
    renderClipInfo();
    renderTimeline();
    renderTracks();
    renderSegments(video.currentTime || 0, true);
    renderQuestions();
  }
  if (seekTo != null) seekVideo(seekTo);
  draw(video.currentTime || 0);
}

function renderClipInfo() {
  const c = state.clip, m = state.meta, v = (m && m.video) || {};
  const dims = v.width ? ` · <code>${v.width}×${v.height} @ ${v.fps} fps, ${(v.duration || 0).toFixed(1)} s</code>` : '';
  const where = c.source === 'vast' ? 'warehouse' : 'local';
  $('#clipinfo').innerHTML = `<span class="meta">${where}</span> ${esc(c.description || '')}${dims}`
    + (c.t_start != null ? ` · timeline ${fmtAbs(c.t_start)}–${fmtAbs(c.t_end)}` : '')
    + (c.source === 'vast' ? '' : (m && m.has_sidecar ? '' : ' · <span class="err">no sidecar</span>'))
    + (c.source === 'vast' ? (c.in_archive ? '' : ' · <span class="err">not in the index yet</span>')
       : (m && m.in_db ? '' : ' · <span class="err">not in receipts.db</span>'));
}

function questionsFor(clip) {
  if (!clip) return [];
  return state.questions.filter(q => sameClip(q.expected_clip_id, clip.clip_id)
    || sameClip(q.expected_clip_id, clip.clip_id.replace(/\.mp4$/, '')));
}

function renderQuestions() {
  const qs = questionsFor(state.clip);
  $('#qcount').textContent = qs.length ? String(qs.length) : '';
  if (!qs.length) {
    const note = state.clip && state.clip.source === 'vast'
      ? 'No labeled questions for this warehouse segment. Claims from the index are under Segments.'
      : 'No labeled questions for this clip.';
    $('#questions').innerHTML = `<p class="meta">${note}</p>`;
    return;
  }
  $('#questions').innerHTML = qs.map((q, i) => {
    const stale = q.expected_stale ? badge('STALE') : '';
    return `<div class="q" data-qi="${i}"><div class="prompt">${esc(q.question)}</div>`
      + `<div class="expect">${esc(q.expected_answer)} ${stale}</div>`
      + `<div class="meta">as of ${esc(q.as_of)} · ${esc(q.id)} · ${esc((q.expected_interval || []).join(' – '))}</div></div>`;
  }).join('');
  $('#questions').querySelectorAll('.q').forEach(el => el.addEventListener('click', () => {
    const q = qs[Number(el.dataset.qi)];
    $('#questions').querySelectorAll('.q').forEach(n => n.classList.remove('on'));
    el.classList.add('on');
    $('#follow').checked = false;
    seekVideo(questionOffset(q));
  }));
}

async function renderVast(c) {
  $('#segcount').textContent = '';
  $('#segments').innerHTML = '<p class="meta">Loading claims…</p>';
  try {
    const d = await getJSON('../api/clip_claims?clip_id=' + encodeURIComponent(c.clip_id));
    const claims = d.claims || [];
    $('#segcount').textContent = claims.length ? String(claims.length) : '(none)';
    const cap = d.caption ? `<div class="action">${esc(d.caption)}</div>` : '';
    const rows = claims.map(cl =>
      `<div class="claim"><span class="key">${esc(cl.entity)} / ${esc(cl.attribute)} @ ${esc(cl.location)}</span>`
      + `<span class="val">= ${esc(cl.value)}</span>${badge(String(cl.status || '').toUpperCase())}</div>`).join('');
    $('#segments').innerHTML = `<div class="seg active"><div class="title"><span>Indexed caption</span></div>${cap}`
      + (rows || '<p class="meta">No claims stored for this segment.</p>') + `</div>`;
    if (d.caption) state.clip.description = d.caption;
  } catch (e) {
    $('#segcount').textContent = '';
    $('#segments').innerHTML = `<p class="meta">${esc(e.message)}</p>`;
  }
}

// ------------------------------------------------------------ tracks --
function indexTracks(meta) {
  const fps = (meta.video && meta.video.fps) || 30;
  for (const tr of meta.tracks || []) {
    tr._ts = tr.boxes.map(b => b[0]);
    tr._dt = tr._ts.length > 1 ? (tr._ts[tr._ts.length - 1] - tr._ts[0]) / (tr._ts.length - 1) : 1 / fps;
    tr._color = colorFor(tr.label + '#' + tr.track_id);
  }
}

function boxAt(tr, t) {  // last box with t_i <= t, if it is within ~1.5 frames of t
  const ts = tr._ts;
  if (!ts.length || ts[0] > t) return null;
  let lo = 0, hi = ts.length - 1;
  while (lo < hi) { const mid = (lo + hi + 1) >> 1; if (ts[mid] <= t) lo = mid; else hi = mid - 1; }
  return (t - ts[lo] <= 1.5 * tr._dt) ? tr.boxes[lo] : null;
}

function renderTracks() {
  const m = state.meta, box = $('#tracks');
  const tracks = (m && m.tracks) || [];
  $('#trackcount').textContent = tracks.length ? `${tracks.length}` : '(none — run viewer/detect.py)';
  const byTrack = {};
  for (const tc of (m && m.track_claims) || []) (byTrack[tc.track_id] ||= []).push(tc);
  box.innerHTML = tracks.map(tr => {
    const zones = (byTrack[tr.track_id] || []).map(tc =>
      `<span title="${esc(tc.entity)} zone=${esc(tc.zone)} ${fmtRel(tc.t_start)}–${fmtRel(tc.t_end)}">`
      + `${esc(tc.zone)} ${badge(tc.db ? tc.db.status : 'NOT_INGESTED')}</span>`).join('');
    return `<div class="track"><input type="checkbox" data-tid="${tr.track_id}" checked/>`
      + `<span class="sw" style="background:${tr._color}"></span><code>${esc(tr.label)}#${tr.track_id}</code>`
      + `<span class="meta">${tr.boxes.length} boxes ${fmtRel(tr._ts[0])}–${fmtRel(tr._ts[tr._ts.length - 1])}</span>`
      + `<span class="zones">${zones}</span></div>`;
  }).join('');
  box.querySelectorAll('input[type=checkbox]').forEach(cb => cb.addEventListener('change', () => {
    const tid = Number(cb.dataset.tid);
    if (cb.checked) state.hidden.delete(tid); else state.hidden.add(tid);
    draw(video.currentTime);
  }));
}

// ------------------------------------------------------------ canvas --
function fit() {  // object-fit: contain geometry of the video picture inside the element
  const cw = video.clientWidth, ch = video.clientHeight, vw = video.videoWidth, vh = video.videoHeight;
  if (!vw || !vh || !cw || !ch) return null;
  const scale = Math.min(cw / vw, ch / vh), w = vw * scale, h = vh * scale;
  return {dx: (cw - w) / 2, dy: (ch - h) / 2, w, h, cw, ch};
}

function resizeCanvas() {
  const dpr = window.devicePixelRatio || 1, cw = video.clientWidth, ch = video.clientHeight;
  canvas.width = Math.round(cw * dpr); canvas.height = Math.round(ch * dpr);
  canvas.style.width = cw + 'px'; canvas.style.height = ch + 'px';
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  draw(video.currentTime);
}

function draw(t) {
  ctx.clearRect(0, 0, video.clientWidth, video.clientHeight);
  updatePlayhead(t);
  renderSegments(t);
  syncAsOf(t);
  const f = fit(), m = state.meta;
  if (!f || !m) return;
  const X = x => f.dx + x * f.w, Y = y => f.dy + y * f.h;
  const showLabels = $('#labels').checked;
  if ($('#zones').checked) {
    for (const [name, poly] of Object.entries(m.zones || {})) {
      ctx.beginPath();
      poly.forEach(([x, y], i) => i ? ctx.lineTo(X(x), Y(y)) : ctx.moveTo(X(x), Y(y)));
      ctx.closePath();
      ctx.fillStyle = 'rgba(108,160,217,0.12)'; ctx.fill();
      ctx.setLineDash([6, 4]); ctx.lineWidth = 1.5; ctx.strokeStyle = 'rgba(108,160,217,0.9)'; ctx.stroke();
      ctx.setLineDash([]);
      if (showLabels) label(X(poly[0][0]), Y(poly[0][1]), name, 'rgba(108,160,217,0.9)', '#111');
    }
  }
  for (const tr of m.tracks || []) {
    if (state.hidden.has(tr.track_id)) continue;
    const b = boxAt(tr, t);
    if (!b) continue;
    const x = X(b[1]), y = Y(b[2]), w = b[3] * f.w, h = b[4] * f.h;
    ctx.lineWidth = 2; ctx.strokeStyle = tr._color; ctx.strokeRect(x, y, w, h);
    if (showLabels) label(x, y, `${tr.label}#${tr.track_id} ${b[5].toFixed(2)}`, tr._color, '#111');
  }
}

function label(x, y, text, bg, fg) {
  ctx.font = '12px ui-monospace, Menlo, monospace';
  const w = ctx.measureText(text).width + 8, h = 16;
  const ly = y - h < 0 ? y : y - h;
  ctx.fillStyle = bg; ctx.fillRect(x, ly, w, h);
  ctx.fillStyle = fg; ctx.fillText(text, x + 4, ly + 12);
}

function startLoop() {
  if ('requestVideoFrameCallback' in HTMLVideoElement.prototype) {
    const cb = (_now, md) => { draw(md.mediaTime); video.requestVideoFrameCallback(cb); };
    video.requestVideoFrameCallback(cb);
  } else {
    const tick = () => { draw(video.currentTime); requestAnimationFrame(tick); };
    requestAnimationFrame(tick);
  }
}

// ---------------------------------------------------------- timeline --
function renderTimeline() {
  const m = state.meta, dur = (m && m.video && m.video.duration) || video.duration || 1;
  const pct = s => (100 * Math.max(0, Math.min(1, s / dur))).toFixed(2) + '%';
  const segLane = $('#lane-segments'), trkLane = $('#lane-tracks');
  segLane.innerHTML = ((m && m.segments) || []).map(s =>
    `<span data-seg="${esc(s.id)}" data-t="${s.t_start}" style="left:${pct(s.t_start)};width:${pct(s.t_end - s.t_start)};background:${colorFor(s.entity)}" title="${esc(s.id)} ${fmtRel(s.t_start)}–${fmtRel(s.t_end)}">${esc(s.id)}</span>`).join('');
  trkLane.innerHTML = ((m && m.track_claims) || []).map(tc =>
    `<span data-t="${tc.t_start}" style="left:${pct(tc.t_start)};width:${pct(tc.t_end - tc.t_start)};background:${colorFor('zone:' + tc.zone)}" title="${esc(tc.entity)} in ${esc(tc.zone)} ${fmtRel(tc.t_start)}–${fmtRel(tc.t_end)}">${esc(tc.entity)}→${esc(tc.zone)}</span>`).join('');
  if (state.cite && state.cite.clip_id === state.clip.clip_id) {
    const c = state.cite;
    segLane.insertAdjacentHTML('beforeend', `<div class="cite" style="left:${pct(c.t0)};width:${pct(c.t1 - c.t0)}"></div>`);
  }
  for (const el of document.querySelectorAll('.lane span')) {
    el.addEventListener('click', () => { video.currentTime = Number(el.dataset.t) + 0.01; });
  }
}

function updatePlayhead(t) {
  const m = state.meta, dur = (m && m.video && m.video.duration) || video.duration || 1;
  $('#playhead').style.left = (100 * Math.max(0, Math.min(1, t / dur))).toFixed(2) + '%';
}

// ---------------------------------------------------------- segments --
function renderSegments(t, force) {
  const m = state.meta, segs = (m && m.segments) || [];
  const active = segs.filter(s => s.t_start <= t && t <= s.t_end).map(s => s.id);
  const key = active.join('|');
  if (!force && key === state.activeKey) return;
  state.activeKey = key;
  $('#segcount').textContent = segs.length ? `${active.length} of ${segs.length} active` : '(none)';
  $('#segments').innerHTML = segs.map(s => {
    const on = active.includes(s.id);
    const claims = (s.claims || []).map(c => {
      const db = c.db;
      let status = db ? badge(db.status) : badge('NOT_INGESTED');
      let extra = '';
      if (db) {
        extra += `<span class="meta">#${db.claim_id} · ${esc(db.created_rule || db.rule_fired || '')} · ${db.sighting} · conf ${db.confidence.toFixed(2)}</span>`;
        if (db.superseded_by) {
          extra += ` <button class="small" data-jump="${esc(db.superseded_by_clip)}">superseded by #${db.superseded_by} = ${esc(db.superseded_by_value)} (${esc(db.superseded_by_clip)}) ↗</button>`;
        }
      }
      return `<div class="claim"><span class="key">${esc(c.entity)} / ${esc(c.attribute)} @ ${esc(c.location || m.scene)}</span>`
        + `<span class="val">= ${esc(c.value)}</span>${status}${extra}</div>`;
    }).join('');
    return `<div class="seg ${on ? 'active' : ''}" data-t="${s.t_start}">`
      + `<div class="title"><span><code>${esc(s.id)}</code> · ${esc(s.entity)} · <span class="meta">${esc(s.source || '')}</span></span>`
      + `<span class="meta">${fmtRel(s.t_start)}–${fmtRel(s.t_end)}</span></div>`
      + `<div class="action">${esc(s.action || '')}</div>`
      + (s.notes ? `<div class="notes">${esc(s.notes)}</div>` : '')
      + claims + `</div>`;
  }).join('');
  for (const el of document.querySelectorAll('.seg .title')) {
    el.addEventListener('click', () => { video.currentTime = Number(el.parentElement.dataset.t) + 0.01; });
  }
  for (const b of document.querySelectorAll('[data-jump]')) {
    b.addEventListener('click', () => selectClip(b.dataset.jump, 0));
  }
  for (const el of document.querySelectorAll('.lane span[data-seg]')) {
    el.classList.toggle('active', active.includes(el.dataset.seg));
  }
}

// ------------------------------------------------------------- as of --
function syncAsOf(t) {
  if (!$('#follow').checked || !state.clip) return;
  const v = Math.min(state.tmax, state.clip.t_start + t);
  $('#asof').value = v;
  $('#asofval').textContent = fmtAbs(v);
}

async function ask() {
  const q = new URLSearchParams({entity: $('#entity').value, attribute: $('#attribute').value,
    location: $('#location').value, as_of: $('#asof').value, max_age: $('#maxage').value});
  const el = $('#result');
  el.hidden = false;
  try {
    const d = await getJSON('api/answer?' + q);
    const freshness = d.claim_id ? badge(d.stale ? 'STALE' : 'FRESH') : '';
    const later = d.current_status === 'SUPERSEDED' ? ' <span class="meta">(superseded by later footage)</span>' : '';
    const cite = d.clip_id ? `<p class="meta">Cited clip <code>${esc(d.clip_id)}</code> ${fmtRel(d.rel_t_start)}–${fmtRel(d.rel_t_end)}`
      + ` (timeline ${fmtAbs(d.t_start)}–${fmtAbs(d.t_end)}) · created by <code>${esc(d.created_rule || '')}</code>`
      + ` <button class="small" data-cite="${esc(d.clip_id)}" data-t0="${d.rel_t_start}" data-t1="${d.rel_t_end}">jump to citation ↗</button></p>` : '';
    const hist = d.history.length ? `<table><tr><th>earlier value</th><th>clip</th><th>when</th><th>rule</th><th></th></tr>`
      + d.history.map(h => `<tr><td>${esc(h.value)}</td><td><code>${esc(h.clip_id)}</code></td>`
        + `<td>${fmtAbs(h.t_start)}–${fmtAbs(h.t_end)}</td><td><code>${esc(h.created_rule || '')}</code></td>`
        + `<td><button class="small" data-cite="${esc(h.clip_id)}" data-t0="${h.rel_t_start}" data-t1="${h.rel_t_end}">↗</button></td></tr>`).join('')
      + `</table>` : '';
    el.innerHTML = `<p>${badge(d.claim_status)} ${freshness}${later}</p>`
      + `<div class="answer">${d.answer == null ? '— no evidence —' : esc(d.answer)}</div>`
      + `<p class="meta"><code>${esc(d.key.entity)} / ${esc(d.key.attribute)} @ ${esc(d.key.location || '-')}</code> · as_of ${fmtAbs(d.as_of)} · max_age ${d.max_age}s</p>`
      + cite
      + (d.stale_reason ? `<p class="err">${esc(d.stale_reason)}</p>` : '')
      + (d.data_gap_note ? `<p class="meta">${esc(d.data_gap_note)}</p>` : '')
      + hist;
    for (const b of el.querySelectorAll('[data-cite]')) {
      b.addEventListener('click', async () => {
        state.cite = {clip_id: b.dataset.cite, t0: Number(b.dataset.t0), t1: Number(b.dataset.t1)};
        $('#follow').checked = false;
        await selectClip(b.dataset.cite, Number(b.dataset.t0));
      });
    }
  } catch (e) {
    el.innerHTML = `<p class="err">${esc(e.message)}</p>`;
  }
}

// -------------------------------------------------------------- wiring --
$('#clip').addEventListener('change', e => selectClip(e.target.value));
$('#zones').addEventListener('change', () => draw(video.currentTime));
$('#labels').addEventListener('change', () => draw(video.currentTime));
$('#asof').addEventListener('input', e => { $('#follow').checked = false; $('#asofval').textContent = fmtAbs(Number(e.target.value)); });
$('#ask').addEventListener('click', ask);
video.addEventListener('loadedmetadata', () => { resizeCanvas(); renderTimeline(); });
video.addEventListener('seeked', () => draw(video.currentTime));
video.addEventListener('timeupdate', () => draw(video.currentTime));
video.addEventListener('click', e => {  // shift-click: print a normalized point for zone authoring
  if (!e.shiftKey) return;
  e.preventDefault();
  const f = fit();
  if (!f) return;
  const r = video.getBoundingClientRect();
  const x = (e.clientX - r.left - f.dx) / f.w, y = (e.clientY - r.top - f.dy) / f.h;
  console.log(`[${x.toFixed(3)}, ${y.toFixed(3)}]`);
});
new ResizeObserver(resizeCanvas).observe(video);
startLoop();
loadClips().catch(e => { $('#dbnote').textContent = e.message; });
