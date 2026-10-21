/* Shopwatch UI: inline editing, dialogs, price watch, import.
   Deliberately small and framework-free. */

const MONEY = new Intl.NumberFormat('en-AU', { style: 'currency', currency: 'AUD',
  minimumFractionDigits: 0, maximumFractionDigits: 2 });
const money = v => (v === null || v === undefined || v === '') ? '-' : MONEY.format(v);

const CLASS_STYLE = {
  HISTORICAL_LOW: 'good', EXCELLENT: 'good', TRIGGER_MET: 'warn',
  ABOVE_TARGET: 'bad', UNRESOLVED: 'none'
};
const CLASS_LABEL = {
  HISTORICAL_LOW: 'HISTORICAL LOW TERRITORY', EXCELLENT: 'EXCELLENT',
  TRIGGER_MET: 'TRIGGER MET', ABOVE_TARGET: 'ABOVE TARGET', UNRESOLVED: 'UNRESOLVED'
};
const CONDITIONS = ['NEW', 'FACTORY_SECOND', 'CARTON_DAMAGED', 'EX_DISPLAY', 'REFURBISHED', 'USED'];

/* ------------------------------------------------------------------ utilities */

function toast(message, kind = '') {
  const box = document.getElementById('toast');
  const el = document.createElement('div');
  el.className = kind;
  el.textContent = message;
  box.appendChild(el);
  setTimeout(() => el.remove(), kind === 'bad' ? 9000 : 4000);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined
  });
  const text = await response.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch (e) { data = { detail: text }; }
  if (!response.ok) {
    throw new Error((data && (data.detail || data.error)) || `HTTP ${response.status}`);
  }
  return data;
}

/* Unsaved work must never disappear silently. */
const dirtyCells = new Set();
window.addEventListener('beforeunload', event => {
  if (dirtyCells.size === 0) return;
  event.preventDefault();
  event.returnValue = '';
});

/* ------------------------------------------------------------ inline editing */

function renderValue(span, value) {
  const kind = span.dataset.kind;
  span.dataset.raw = (value === null || value === undefined) ? '' : value;
  if (value === null || value === undefined || value === '') {
    span.replaceChildren(el('span', '-', 'unresolved'));
  } else if (kind === 'money') {
    span.textContent = money(value);
  } else {
    span.textContent = value;
  }
}

function applyListing(listing) {
  const row = document.querySelector(`[data-listing-row="${listing.id}"]`);
  if (!row) return;

  const price = row.querySelector('[data-cell="delivered"]');
  if (price) {
    price.replaceChildren(...(listing.delivered_price === null
      ? [el('span', 'no price', 'unresolved')]
      : [document.createTextNode(money(listing.delivered_price)),
         el('small', listing.delivered_resolved ? 'delivered' : 'before freight')]));
  }

  const tag = row.querySelector('[data-cell="classification"]');
  if (tag) {
    const node = tagFor(listing);
    if (node) tag.replaceChildren(node); else tag.replaceChildren();
  }

  // Same precedence as the server macro: a ruled-out listing is never styled as a
  // buy, however its price classifies. Applying both left the row green and struck
  // through at once.
  const out = Boolean(listing.ruled_out);
  row.classList.toggle('is-ruled-out', out);
  row.classList.toggle('is-act', !out && ['HISTORICAL_LOW', 'EXCELLENT'].includes(listing.classification));
  row.classList.toggle('is-close', !out && listing.classification === 'TRIGGER_MET');

  for (const [field, prov] of Object.entries(listing.provenance || {})) {
    const span = row.querySelector(`[data-edit][data-field="${field}"]`);
    if (!span) continue;
    renderValue(span, listing[field]);
    const label = span.closest('.field')?.querySelector('.k');
    if (label) {
      label.querySelectorAll('.lock, .src').forEach(n => n.remove());
      const mark = provenanceMark(listing.id, field, prov);
      if (mark) label.appendChild(mark);
    }
  }
}

/* The headline sentence and the ruler are derived from every listing at once, so an
   edit to one row can change both. Without this the page contradicts itself: the row
   reads "at target" under a sentence still saying "unconfirmed". */
function applyProductSummary(product) {
  if (!product) return;
  const card = document.getElementById(`product-${product.id}`);
  if (!card) return;

  const verdict = card.querySelector('.verdict');
  if (verdict) {
    verdict.textContent = product.verdict_line;
    verdict.className = `verdict ${product.tone}`;
  }

  const track = card.querySelector('.ruler-track');
  if (!track || !product.scale) return;

  track.querySelectorAll('.ruler-mark').forEach((mark, i) => {
    const m = product.scale.marks[i];
    if (m) mark.style.left = `${m.pos}%`;
  });

  let best = track.querySelector('.ruler-best');
  if (!product.scale.best) { if (best) best.remove(); return; }
  if (!best) {
    best = document.createElement('span');
    best.className = 'ruler-best';
    track.appendChild(best);
  }
  best.className = `ruler-best ${product.tone}`;
  best.style.left = `${product.scale.best.pos}%`;
  /* Built as nodes, not markup. A retailer name arrives through POST /api/import,
     which is the documented way to paste in research produced elsewhere, so it is
     untrusted text by design and must never be interpolated into innerHTML. */
  best.replaceChildren(
    el('b', money(product.scale.best.value)),
    el('small', `${product.best_retailer || ''}${product.best_resolved ? '' : ', freight unknown'}`),
    document.createElement('i')
  );
}

/* Small helper: element with text, never markup. */
function el(tag, text, className) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function tagFor(listing) {
  // Ruled out comes first, exactly as the server macro orders it. Without this an
  // inline edit re-rendered a ruled-out listing as "excellent" or "at target", with
  // is-act and is-ruled-out both applied, until the next reload.
  if (listing.ruled_out) return el('span', 'ruled out', 'tag ruled-out');
  if (listing.verification?.model?.status === 'FLAGGED') return el('span', 'model mismatch', 'tag fault');
  if (listing.classification === 'HISTORICAL_LOW') return el('span', 'historical low', 'tag act');
  if (listing.classification === 'EXCELLENT') return el('span', 'excellent', 'tag act');
  if (listing.classification === 'TRIGGER_MET') return el('span', 'at target', 'tag close');
  if (listing.classification === 'UNRESOLVED') return el('span', 'unconfirmed', 'tag');
  return null;
}

/* Returns a node or null. `prov.source` is attacker-controllable through the import
   endpoint, so it is set as text and never as markup. */
function provenanceMark(listingId, field, prov) {
  if (prov.manual_locked) {
    const button = el('button', 'set by hand', 'lock');
    button.dataset.clearOverride = '';
    button.dataset.listing = listingId;
    button.dataset.field = field;
    button.title = 'You set this by hand. Automated runs will not change it. Click to hand it back.';
    return button;
  }
  if (prov.state === 'STALE') return el('span', 'stale', 'src');
  if (prov.source) return el('span', prov.source, 'src');
  return null;
}

function startEdit(span) {
  if (span.classList.contains('editing')) return;
  const original = span.dataset.raw;
  const kind = span.dataset.kind;
  span.classList.add('editing');

  let input;
  if (kind === 'condition') {
    input = document.createElement('select');
    for (const c of CONDITIONS) {
      const option = document.createElement('option');
      option.value = option.textContent = c;
      if (c === original) option.selected = true;
      input.appendChild(option);
    }
  } else {
    input = document.createElement('input');
    input.type = kind === 'money' ? 'number' : kind === 'date' ? 'date' : 'text';
    if (kind === 'money') input.step = '0.01';
    input.value = original;
    input.size = Math.max(6, String(original).length + 2);
  }
  input.style.width = kind === 'money' ? '85px' : kind === 'date' ? '145px' : 'auto';
  span.textContent = '';
  span.appendChild(input);
  input.focus();
  if (input.select) input.select();
  dirtyCells.add(span);
  span.classList.add('dirty');

  let settled = false;
  const cancel = () => {
    if (settled) return;
    settled = true;
    dirtyCells.delete(span);
    span.classList.remove('editing', 'dirty');
    renderValue(span, original === '' ? null : original);
  };
  const commit = async () => {
    if (settled) return;
    settled = true;
    const value = input.value.trim();
    span.classList.remove('editing');
    if (value === original) { cancel(); return; }
    span.classList.add('saving');
    try {
      const payload = {};
      payload[span.dataset.field] = value === '' ? null : value;
      const result = await api(`/api/retailers/${span.dataset.listing}`, { method: 'PATCH', body: payload });
      dirtyCells.delete(span);
      span.classList.remove('dirty', 'saving');
      applyListing(result.listing);
      applyProductSummary(result.product);
      const target = document.querySelector(
        `[data-listing-row="${result.listing.id}"] [data-edit][data-field="${span.dataset.field}"]`);
      if (target) { target.classList.add('saved'); setTimeout(() => target.classList.remove('saved'), 1200); }
    } catch (err) {
      span.classList.remove('saving');
      span.classList.add('error', 'dirty');
      renderValue(span, value === '' ? null : value);
      toast(`Save failed, value kept in the cell: ${err.message}`, 'bad');
      settled = true;
    }
  };

  input.addEventListener('keydown', event => {
    if (event.key === 'Enter') { event.preventDefault(); commit(); }
    if (event.key === 'Escape') { event.preventDefault(); cancel(); }
  });
  input.addEventListener('blur', commit);
}

/* --------------------------------------------------------- progressive disclosure */

function toggleRegion(button, region) {
  const open = region.hidden;
  region.hidden = !open;
  button.setAttribute('aria-expanded', String(open));
  const row = button.closest('.listing');
  if (row) row.classList.toggle('open', open);
  return open;
}

/* The listing row is a <div role="button"> now, not a real <button> - it has to be,
   since the retailer-open link lives inside it and an <a> nested in an actual <button>
   is invalid HTML. A native button gets Enter/Space activation for free; this is that,
   for just this one role. Delegated rather than bound per-row so it keeps working after
   applyListing() or a reload replaces the row's markup, the same reason the click
   handler below is delegated too. */
document.addEventListener('keydown', event => {
  if (event.key !== 'Enter' && event.key !== ' ') return;
  if (event.target.closest('.listing-open')) return;  // a focused link handles its own Enter
  const toggle = event.target.closest('[data-toggle-detail]');
  if (!toggle) return;
  event.preventDefault();  // Space must not also scroll the page
  toggle.click();
});

document.addEventListener('click', async event => {
  // .listing-open now lives inside the listing row's own [data-toggle-detail] element
  // (it has to, to sit right after the retailer's name rather than floating at the
  // row's far edge), so a click on it would otherwise ALSO match the toggle-detail
  // check below and expand/collapse the row on top of navigating. Let it navigate.
  if (event.target.closest('.listing-open')) return;

  const ruleOutOpen = event.target.closest('[data-rule-out-open]');
  if (ruleOutOpen) {
    const id = ruleOutOpen.dataset.ruleOutOpen;
    const form = document.querySelector(`[data-rule-out-form="${id}"]`);
    if (form) {
      form.hidden = false;
      ruleOutOpen.hidden = true;
      const input = form.querySelector('[data-rule-out-reason]');
      if (input) input.focus();
    }
    return;
  }
  const ruleOut = event.target.closest('[data-rule-out]');
  if (ruleOut) {
    const id = ruleOut.dataset.ruleOut;
    const input = document.querySelector(`[data-rule-out-reason="${id}"]`);
    const reason = input ? input.value.trim() : '';
    try {
      await api(`/api/retailers/${id}/ruled-out`,
        { method: 'POST', body: { ruled_out: true, reason } });
      location.reload();
    } catch (err) { toast(`Could not rule it out: ${err.message}`, 'bad'); }
    return;
  }
  const ruleIn = event.target.closest('[data-rule-in]');
  if (ruleIn) {
    try {
      await api(`/api/retailers/${ruleIn.dataset.ruleIn}/ruled-out`,
        { method: 'POST', body: { ruled_out: false } });
      location.reload();
    } catch (err) { toast(`Could not put it back: ${err.message}`, 'bad'); }
    return;
  }

  const retailerToggle = event.target.closest('[data-retailer-toggle]');
  if (retailerToggle) {
    const id = retailerToggle.dataset.retailerToggle;
    const currentlyExcluded = retailerToggle.dataset.excluded === '1';
    const next = !currentlyExcluded;
    if (next && !confirm(
      'Exclude this retailer? It stops being offered anywhere shopwatch suggests a '
      + 'retailer, and every listing already tracked under it is switched off (across '
      + 'every product). Un-excluding later does not turn those back on by itself.'
    )) return;
    retailerToggle.disabled = true;
    try {
      await api(`/api/retailer/${id}`, { method: 'PATCH', body: { excluded: next } });
      location.reload();
    } catch (err) {
      toast(`Could not update retailer: ${err.message}`, 'bad');
      retailerToggle.disabled = false;
    }
    return;
  }

  const detailBtn = event.target.closest('[data-toggle-detail]');
  if (detailBtn) {
    const region = document.getElementById(`detail-${detailBtn.dataset.toggleDetail}`);
    if (region) toggleRegion(detailBtn, region);
    return;
  }

  const alsoRan = event.target.closest('[data-toggle-alsoran]');
  if (alsoRan) {
    const region = document.getElementById(`alsoran-${alsoRan.dataset.toggleAlsoran}`);
    if (region) {
      const open = toggleRegion(alsoRan, region);
      const n = region.querySelectorAll('.listing').length;
      alsoRan.textContent = open ? 'Hide the ones above target'
                                 : `Show ${n} more, all above target`;
    }
    return;
  }

  const cell = event.target.closest('[data-edit]');
  if (cell && !event.target.closest('input, select')) { startEdit(cell); return; }

  const lock = event.target.closest('[data-clear-override]');
  if (lock) {
    const { listing, field } = lock.dataset;
    if (!confirm(`Clear the manual override on "${field}"? Automated runs will be able to change it again.`)) return;
    try {
      const result = await api(`/api/retailers/${listing}/clear-override`,
        { method: 'POST', body: { field } });
      lock.remove();
      toast(`${field} handed back to automated updates.`, 'good');
      if (result.listing) applyListing(result.listing);
      applyProductSummary(result.product);
    } catch (err) { toast(`Could not clear override: ${err.message}`, 'bad'); }
    return;
  }

  if (event.target.matches('[data-close]')) {
    event.target.closest('dialog').close();
  }
});

/* ------------------------------------------------------------------ toolbar */

function wire(id, handler) {
  const el = document.getElementById(id);
  if (el) el.addEventListener('click', handler);
}

wire('btn-watch', async event => {
  await runWatch(event.currentTarget, {});
});
wire('btn-watch-product', async event => {
  await runWatch(event.currentTarget, { product_id: Number(event.currentTarget.dataset.product) });
});

async function runWatch(button, body) {
  button.disabled = true;
  const label = button.textContent;
  button.textContent = 'Running…';
  try {
    const summary = await api('/api/price-watch/run', { method: 'POST', body });
    const parts = [`${summary.status}`, `checked ${summary.checked}`, `ok ${summary.ok}`,
      `unresolved ${summary.unresolved}`, `skipped ${summary.skipped}`, `errors ${summary.errors}`,
      `alerts ${summary.alerts.length}`];
    toast(`Price watch: ${parts.join(', ')}`, summary.status === 'failed' ? 'bad' : 'good');
    if (summary.delivery_failures.length) {
      toast(`ALERT DELIVERY FAILED: ${summary.delivery_failures.join('; ')}`, 'bad');
    }
    setTimeout(() => location.reload(), 1400);
  } catch (err) {
    toast(`Price watch failed: ${err.message}`, 'bad');
  } finally {
    button.disabled = false;
    button.textContent = label;
  }
}

wire('btn-backup', async () => {
  try {
    const result = await api('/api/backup', { method: 'POST' });
    toast(`Backup written to ${result.backup}`, 'good');
  } catch (err) { toast(`Backup failed: ${err.message}`, 'bad'); }
});

wire('btn-import', () => document.getElementById('import-dialog').showModal());

wire('btn-llm-setup', () => {
  document.getElementById('llm-setup-dialog').showModal();
});
wire('import-go', async () => {
  const out = document.getElementById('import-result');
  let payload;
  try {
    payload = JSON.parse(document.getElementById('import-text').value);
  } catch (err) {
    out.textContent = `Not valid JSON: ${err.message}`;
    return;
  }
  out.textContent = 'Importing…';
  try {
    const result = await api('/api/import', { method: 'POST', body: payload });
    out.textContent = JSON.stringify(result, null, 2);
    toast('Import complete. Manual overrides were preserved.', 'good');
    setTimeout(() => location.reload(), 2500);
  } catch (err) {
    out.textContent = `Import failed: ${err.message}`;
    toast(`Import failed: ${err.message}`, 'bad');
  }
});

wire('btn-dry-alerts', async () => {
  try {
    const result = await api('/api/alerts/evaluate', { method: 'POST', body: {} });
    if (!result.alerts.length) { toast('No alert rule would fire right now.'); return; }
    for (const alert of result.alerts) {
      toast(`${alert.rule}: ${alert.retailer} ${money(alert.delivered)} (${alert.classification})`, 'good');
    }
  } catch (err) { toast(`Evaluate failed: ${err.message}`, 'bad'); }
});

/* ------------------------------------------------------------------ dialogs */

/* --------------------------------------------------------------- product wizard
 *
 * A genuine multi-step wizard, not a grouped single form: the tiers (required,
 * optional, price target, retailers, the costed research step) are each their own
 * screen. One <dialog> whose body swaps between named .wizard-step blocks, since this
 * app has no separate stepper component and one dialog element is the existing idiom.
 */

const WIZ_STEPS = ['basics', 'optional', 'price', 'retailers', 'go', 'running', 'done'];
let wiz = { index: 0, productId: null, retailers: [], selected: new Set(), pollTimer: null };

function wizVal(id) { return document.getElementById(id).value.trim(); }
function wizNum(id) { const v = wizVal(id); return v === '' ? null : Number(v); }

function wizRender() {
  const step = WIZ_STEPS[wiz.index];
  for (const node of document.querySelectorAll('.wizard-step')) {
    node.hidden = node.dataset.step !== step;
  }
  document.getElementById('wiz-step-count').textContent =
    ['running', 'done'].includes(step) ? '' : `Step ${wiz.index + 1} of 5`;
  document.getElementById('wiz-back').hidden = wiz.index === 0 || ['running', 'done'].includes(step);
  document.getElementById('wiz-next').hidden = ['running', 'done'].includes(step);
  document.getElementById('wiz-next').textContent = step === 'go' ? "Let's go" : 'Next';
  document.getElementById('wiz-finish').hidden = step !== 'done';
  document.getElementById('wiz-cancel').textContent = step === 'done' ? 'Close' : 'Cancel';
}

async function wizLoadGroups() {
  const select = document.getElementById('wiz-group');
  const none = document.createElement('option');
  none.value = '';
  none.textContent = '- none -';
  select.replaceChildren(none);
  const groups = await api('/api/groups');
  for (const g of groups) {
    const opt = document.createElement('option');
    opt.value = g.id;
    opt.textContent = `${g.name} (${g.members.length})`;
    select.appendChild(opt);
  }
}

/* A reusable "pick retailers, with dedup-safe adding and discovery approval" component.
 * Two screens need exactly this: the wizard's retailers step, and the product page's
 * "Research retailers again" dialog - the first version of this only existed in the
 * wizard, which is why "research retailers again" on an existing product silently had
 * no discovery at all until this generalisation.
 *
 * `retailers` and `selected` are the CALLER's own array/Set, mutated in place (pushed
 * to / added to), not copies - so the caller's existing state (wiz.retailers, or
 * researchRetry.retailers) stays the single source of truth. */
function makeRetailerPicker({ list, retailers, selected }) {
  function rowScaffold(nameText, capText) {
    const row = el('label', '', 'wizard-retailer-row');
    const box = document.createElement('input');
    box.type = 'checkbox';
    row.append(box, el('span', nameText, 'name'), el('span', capText, 'cap'));
    return row;
  }

  function retailerRow(r, { checked = false } = {}) {
    const caps = [];
    if (r.adapter_available) caps.push('has an automated price check');
    if (r.mail_alerts_parsed) caps.push('mailwatch reads its price alerts');
    const row = rowScaffold(r.name, caps.join(' · '));
    const box = row.querySelector('input');
    box.value = r.id;
    box.checked = checked;
    if (checked) selected.add(r.id);
    box.addEventListener('change', () => {
      if (box.checked) selected.add(r.id); else selected.delete(r.id);
    });
    return row;
  }

  /* The one place that turns a name (typed, or approved from a discovery search) into
   * a real, checked retailer row. Always POSTs - ensure_retailer is idempotent by slug
   * on the server, so this is the authoritative dedup, not a client-side name guess (a
   * client-side check missed "JB HiFi" vs "JB Hi-Fi" resolving to the same slug, and
   * separately skipped selecting an existing-but-unticked retailer entirely instead of
   * ticking it). If a row for the returned id already exists, this ticks it rather
   * than adding a second, unsynchronised checkbox for the same retailer. */
  async function ensureRetailerRow(name, { homepage = null, checked = true } = {}) {
    const body = homepage ? { name, homepage } : { name };
    const retailer = await api('/api/retailers', { method: 'POST', body });
    const existing = [...list.querySelectorAll('input[type=checkbox]')]
      .find(b => Number(b.value) === retailer.id);
    if (existing) {
      if (checked && !existing.checked) {
        existing.checked = true;
        existing.dispatchEvent(new Event('change'));
      }
      return retailer;
    }
    retailers.push(retailer);
    list.appendChild(retailerRow(retailer, { checked }));
    return retailer;
  }

  /* Splits on commas so "JB Hi-Fi, Bing Lee, Officeworks" adds all three in one go,
   * same as typing one, clicking Add, typing the next. Each name is isolated: one
   * failing (a network blip, a transient 500) must not stop the rest, and must not be
   * reported as if it succeeded - callers get back which names failed so the input can
   * be left with just those, rather than clearing text only partly acted on. */
  async function addRetailerNames(text) {
    const names = text.split(',').map(n => n.trim()).filter(Boolean);
    const failed = [];
    for (const name of names) {
      try {
        await ensureRetailerRow(name, { checked: true });
      } catch {
        failed.push(name);
      }
    }
    return failed;
  }

  /* Renders a discovered retailer as an unchecked row: naming it is not the same as
   * wanting it researched, so ticking the box is the approval step. Approving promotes
   * it into the real retailer list via ensureRetailerRow (same dedup as typed names)
   * and removes this row - it is now represented by the permanent, checked row
   * instead, so a later re-search can safely clear the discovered box without losing
   * anything approved. */
  function discoveredRow(candidate) {
    const row = rowScaffold(candidate.name, candidate.homepage || 'homepage unknown');
    const box = row.querySelector('input');
    box.addEventListener('change', async () => {
      if (!box.checked) return;
      box.disabled = true;
      try {
        await ensureRetailerRow(candidate.name, { homepage: candidate.homepage, checked: true });
        row.remove();
      } catch (err) {
        toast(`Could not add ${candidate.name}: ${err.message}`, 'bad');
        box.checked = false;
        box.disabled = false;
      }
    });
    return row;
  }

  // Only the retailers actually selected here, not every retailer in the system - an
  // unrelated product's retailer must never suppress a genuine suggestion.
  function knownRetailers() {
    return retailers.filter(r => selected.has(r.id));
  }

  function renderDiscovered(result, box) {
    if (!result.retailers.length) {
      box.appendChild(el('p', result.note || 'No confident finds beyond the ones already listed.', 'muted'));
    } else {
      for (const candidate of result.retailers) box.appendChild(discoveredRow(candidate));
      if (result.note) box.appendChild(el('p', result.note, 'muted'));
    }
  }

  return { retailerRow, ensureRetailerRow, addRetailerNames, discoveredRow, knownRetailers, renderDiscovered };
}

async function wizLoadRetailers() {
  // Issue #112: an excluded retailer is never offered as a choice here - filtered
  // client-side because GET /api/retailers itself has to keep returning every
  // retailer (the Retailers page needs the excluded ones too, to un-exclude them).
  wiz.retailers = (await api('/api/retailers')).filter(r => !r.excluded);
  const list = document.getElementById('wiz-retailer-list');
  list.replaceChildren();
  wiz.picker = makeRetailerPicker({ list, retailers: wiz.retailers, selected: wiz.selected });
  for (const r of wiz.retailers) list.appendChild(wiz.picker.retailerRow(r));
}

function wizProgressRow(result) {
  const li = el('li', '');
  li.dataset.outcome = result.status.toLowerCase();
  const ICONS = { pending: '…', found: '✓', needs_manual_check: '!', blocked: '✕', timed_out: '⏱' };
  const LABELS = {
    pending: 'waiting', found: 'found a price', needs_manual_check: 'needs manual check',
    blocked: 'blocked - check by hand', timed_out: 'timed out - check by hand',
  };
  li.append(
    el('span', ICONS[result.status.toLowerCase()] || '?', 'icon'),
    el('span', result.retailer_name),
    el('span', LABELS[result.status.toLowerCase()] || result.status),
  );
  if (result.note) li.appendChild(el('span', result.note, 'note'));
  return li;
}

async function wizPoll() {
  const job = await api(`/api/research-jobs/${wiz.jobId}`);
  document.getElementById('wiz-progress').replaceChildren(...job.results.map(wizProgressRow));
  if (job.status === 'DONE' || job.status === 'FAILED') {
    clearInterval(wiz.pollTimer);
    await wizShowDone(job);
  }
}

async function wizShowDone(job) {
  wiz.index = WIZ_STEPS.indexOf('done');
  wizRender();
  const summary = document.getElementById('wiz-done-summary');
  const found = job.results.filter(r => r.status === 'FOUND').length;
  const needsCheck = job.results.length - found;
  summary.textContent = job.status === 'FAILED'
    ? `Research failed: ${job.error || 'unknown error'}`
    : `Found real prices at ${found} of ${job.results.length} retailers.`;
  document.getElementById('wiz-results').replaceChildren(...job.results.map(wizProgressRow));

  // Not FOUND covers NEEDS_MANUAL_CHECK, BLOCKED, TIMED_OUT and any retailer still
  // PENDING because the job itself failed outright - all of them are worth another
  // attempt without the person having to re-tick the retailers step from scratch.
  const retryBtn = document.getElementById('wiz-retry');
  const stillToCheck = job.results.filter(r => r.status !== 'FOUND');
  retryBtn.hidden = stillToCheck.length === 0;
  retryBtn.textContent =
    `Retry ${stillToCheck.length} failed retailer${stillToCheck.length === 1 ? '' : 's'}`;
  retryBtn.onclick = async () => {
    retryBtn.disabled = true;
    try {
      const retried = await api('/api/research-jobs', { method: 'POST', body: {
        product_id: wiz.productId, retailer_ids: stillToCheck.map(r => r.retailer_id),
      }});
      wiz.jobId = retried.id;
      wiz.index = WIZ_STEPS.indexOf('running');
      wizRender();
      document.getElementById('wiz-progress').replaceChildren(...retried.results.map(wizProgressRow));
      wiz.pollTimer = setInterval(wizPoll, 2000);
    } catch (err) {
      toast(`Could not retry: ${err.message}`, 'bad');
    } finally {
      retryBtn.disabled = false;
    }
  };

  if (wiz.wantsSuggestion) {
    const box = document.getElementById('wiz-suggestion');
    try {
      const suggestion = await api(`/api/products/${wiz.productId}/price-suggestion`);
      box.hidden = false;
      if (suggestion) {
        box.replaceChildren(
          el('p', `Suggested trigger: ${money(suggestion.suggested_trigger)} `
            + `(${suggestion.note}).`),
        );
        const applyBtn = el('button', 'Apply as trigger price', 'primary');
        applyBtn.addEventListener('click', async () => {
          await api(`/api/products/${wiz.productId}`, { method: 'PATCH',
            body: { trigger_price: suggestion.suggested_trigger } });
          toast('Trigger price set.', 'good');
          applyBtn.disabled = true;
        });
        box.appendChild(applyBtn);
      } else {
        box.textContent = 'Not enough data yet to suggest a price. Add one later once '
          + 'more listings are on the board.';
      }
    } catch (err) { toast(`Could not fetch a suggestion: ${err.message}`, 'bad'); }
  }
}

wire('btn-new-product', async () => {
  wiz = { index: 0, productId: null, retailers: [], selected: new Set(), pollTimer: null };
  for (const id of ['wiz-name', 'wiz-model', 'wiz-brand', 'wiz-notes', 'wiz-trigger',
    'wiz-excellent', 'wiz-histlow', 'wiz-new-retailer', 'wiz-new-group']) {
    document.getElementById(id).value = '';
  }
  document.getElementById('wiz-category').value = 'general';
  document.getElementById('wiz-model-warning').hidden = true;
  document.getElementById('wiz-model-suggestions').hidden = true;
  document.getElementById('wiz-model-suggestions').replaceChildren();
  document.getElementById('wiz-retailer-discovered').hidden = true;
  document.getElementById('wiz-retailer-discovered').replaceChildren();
  const discoverBtn = document.getElementById('wiz-discover-retailers');
  discoverBtn.disabled = false;
  discoverBtn.textContent = 'Find other retailers';
  const discoverWebBtn = document.getElementById('wiz-discover-retailers-web');
  discoverWebBtn.disabled = false;
  discoverWebBtn.textContent = 'Search the web';
  await wizLoadGroups();
  await wizLoadRetailers();
  wizRender();
  document.getElementById('wizard-dialog').showModal();
});

// The empty-board onboarding CTA opens the exact same wizard, not a second one.
wire('btn-empty-cta', () => document.getElementById('btn-new-product').click());

document.getElementById('wiz-model')?.addEventListener('blur', async event => {
  const model = event.target.value.trim();
  const warning = document.getElementById('wiz-model-warning');
  if (!model) { warning.hidden = true; return; }
  try {
    const result = await api(`/api/products/check-model?model=${encodeURIComponent(model)}`);
    if (result.duplicate_of) {
      warning.hidden = false;
      warning.textContent = `This looks like the same model as an existing product: `
        + `"${result.duplicate_of.name}" (${result.duplicate_of.model}).`;
    } else {
      warning.hidden = true;
    }
  } catch { /* the deterministic check is a courtesy, never a blocker */ }
});

/* Polls a queued job row (llm_jobs or retailer_search_jobs - same {status, result,
 * error} shape) until it lands, same idea as wizPoll() for research jobs below.
 * notRunningHint is job-kind-specific because GUI queues can be serviced by different
 * host processes and need different troubleshooting advice. */
async function pollJob(getUrl, { intervalMs = 1000, timeoutMs = 30000, notRunningHint, onStatus } = {}) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const job = await api(getUrl);
    if (onStatus) onStatus(job.status);
    if (job.status === 'DONE') return JSON.parse(job.result);
    if (job.status === 'FAILED') throw new Error(job.error || 'the research runner failed to answer');
    await new Promise(resolve => setTimeout(resolve, intervalMs));
  }
  throw new Error(`no answer in time - ${notRunningHint}`);
}

/* Shared busy/poll/error control flow for every "queue a job, wait, render the
 * result" wizard action (model suggestions, and both retailer-discovery buttons) -
 * three near-identical copies of this is exactly the kind of drift that caused a real
 * bug (see the "already checking" list scoping) in an earlier pass of this feature. */
async function wizRunJob({ btn, siblingBtn, box, busyText, idleText, waitingText, create, pollOpts, onResult, onError }) {
  btn.disabled = true;
  // Both retailer-discovery buttons render into the same #wiz-retailer-discovered box -
  // disabling the sibling too means only one search can ever be in flight, so whichever
  // finishes later can never silently wipe out the other's still-unapproved results.
  if (siblingBtn) siblingBtn.disabled = true;
  btn.textContent = busyText;
  box.hidden = false;
  box.replaceChildren(el('p', waitingText, 'muted'));
  try {
    const { createUrl, body, pollUrl } = await create();
    const job = await api(createUrl, { method: 'POST', body });
    const result = await pollJob(pollUrl(job.id), {
      ...pollOpts,
      onStatus: status => {
        const text = status === 'RUNNING' ? pollOpts.runningText : pollOpts.queuedText;
        if (text) box.replaceChildren(el('p', text, 'muted'));
      },
    });
    box.replaceChildren();
    onResult(result, box);
  } catch (err) {
    box.replaceChildren(el('p', onError(err), 'muted'));
  } finally {
    btn.disabled = false;
    if (siblingBtn) siblingBtn.disabled = false;
    btn.textContent = idleText;
  }
}

function renderModelSuggestions(result) {
  const box = document.getElementById('wiz-model-suggestions');
  box.replaceChildren();
  if (!result.candidates.length) {
    box.appendChild(el('p', result.note || 'No confident candidates - fill it in yourself.', 'muted'));
    return;
  }
  for (const c of result.candidates) {
    const chip = el('button', `${c.label} (${c.model})`, 'btn wizard-suggest-chip');
    chip.type = 'button';
    // A candidate is a suggestion to VERIFY, never applied silently: clicking one only
    // fills the field a person still submits themselves, and re-runs the existing
    // duplicate check exactly as if they had typed it.
    chip.addEventListener('click', () => {
      document.getElementById('wiz-model').value = c.model;
      document.getElementById('wiz-model').dispatchEvent(new Event('blur'));
    });
    box.appendChild(chip);
    const source = safeHttpUrl(c.source);
    if (source) {
      const link = el('a', 'Source');
      link.href = source;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      box.appendChild(link);
    }
  }
  if (result.note) box.appendChild(el('p', result.note, 'muted'));
}

wire('wiz-suggest-models', async () => {
  const query = wizVal('wiz-name');
  if (!query) { toast('Type what it is first.', 'bad'); return; }
  await wizRunJob({
    btn: document.getElementById('wiz-suggest-models'),
    box: document.getElementById('wiz-model-suggestions'),
    busyText: 'Asking...', idleText: 'Suggest models',
    waitingText: 'Waiting for the server research runner...',
    create: () => ({
      createUrl: '/api/llm-jobs', body: { query },
      pollUrl: id => `/api/llm-jobs/${id}`,
    }),
    pollOpts: { intervalMs: 3000, timeoutMs: 240000,
      queuedText: 'Queued for the server research runner...',
      runningText: 'Searching for sourced model suggestions...',
      notRunningHint: 'the server research runner has not picked up the job yet. Retry '
        + 'in a moment or reconnect the Claude Code session on the research host.' },
    onResult: renderModelSuggestions,
    onError: err => `Could not get suggestions: ${err.message}`,
  });
});

wire('wiz-add-retailer', async () => {
  const input = document.getElementById('wiz-new-retailer');
  const btn = document.getElementById('wiz-add-retailer');
  const text = input.value.trim();
  if (!text) return;
  input.disabled = true;
  btn.disabled = true;
  try {
    const failed = await wiz.picker.addRetailerNames(text);
    input.value = failed.join(', ');
    if (failed.length) toast(`Could not add: ${failed.join(', ')}`, 'bad');
  } finally {
    input.disabled = false;
    btn.disabled = false;
  }
});

/* Wires up both discovery buttons (LLM-knowledge and Firecrawl-backed) for one picker.
 * Used by both the wizard's retailers step AND the product page's "Research retailers
 * again" dialog - the first version of this only existed in the wizard, which is why
 * researching an existing product's retailers silently had no discovery at all until
 * this generalisation. getPicker/getName/getModel are called at CLICK time, not wire
 * time, because both screens create a fresh picker (and know the product's name/model)
 * only once their dialog actually opens. */
/* getGeneration is only set for a dialog that can be reopened for a DIFFERENT context
 * (the product page's research-dialog is reused across products, unlike the wizard's
 * dialog whose close handler force-reloads the page, wiping any pending state anyway).
 * Reopening creates a brand new picker with its own retailers/selected Set - a
 * discovery poll or add still in flight from a PREVIOUS opening closes over the OLD
 * Set, so if it resolved after reopen and applied normally, ticking a candidate would
 * visibly check a box in the shared list DOM while actually mutating a Set nothing
 * reads any more. The generation check drops a stale result instead of applying it. */
function wireDiscoveryButtons({ llmBtnId, webBtnId, boxId, getPicker, getName, getModel, getGeneration }) {
  const isStale = generation => getGeneration && generation !== null && getGeneration() !== generation;

  wire(llmBtnId, async () => {
    const name = getName(), model = getModel();
    if (!name) { toast('Type what it is first.', 'bad'); return; }
    const picker = getPicker();
    if (!picker) { toast('Still loading retailers - try again in a moment.', 'bad'); return; }
    const generation = getGeneration ? getGeneration() : null;
    await wizRunJob({
      btn: document.getElementById(llmBtnId),
      siblingBtn: document.getElementById(webBtnId),
      box: document.getElementById(boxId),
      busyText: 'Asking...', idleText: 'Find other retailers',
      waitingText: 'Waiting for the server research runner...',
      create: () => {
        const known = picker.knownRetailers().map(r => r.name);
        const query = `Product: ${name}${model ? ` (${model})` : ''}. Already checking these `
          + `retailers, do not repeat them: ${known.length ? known.join(', ') : 'none yet'}.`;
        return {
          createUrl: '/api/llm-jobs', body: { query, kind: 'retailer_discovery' },
          pollUrl: id => `/api/llm-jobs/${id}`,
        };
      },
      pollOpts: { intervalMs: 3000, timeoutMs: 240000,
        queuedText: 'Queued for the server research runner...',
        runningText: 'Searching for other Australian retailers...',
        notRunningHint: 'the server research runner has not picked up the job yet. Retry '
          + 'in a moment or reconnect the Claude Code session on the research host.' },
      onResult: (result, box) => { if (!isStale(generation)) picker.renderDiscovered(result, box); },
      onError: err => `Could not ask the LLM for retailers: ${err.message}`,
    });
  });

  wire(webBtnId, async () => {
    const name = getName(), model = getModel();
    if (!name) { toast('Type what it is first.', 'bad'); return; }
    const picker = getPicker();
    if (!picker) { toast('Still loading retailers - try again in a moment.', 'bad'); return; }
    const generation = getGeneration ? getGeneration() : null;
    await wizRunJob({
      btn: document.getElementById(webBtnId),
      siblingBtn: document.getElementById(llmBtnId),
      box: document.getElementById(boxId),
      busyText: 'Searching...', idleText: 'Search the web',
      waitingText: 'Searching the web via opti - this runs on a 2-minute poll cycle, '
        + 'so it can take a minute or two...',
      create: () => {
        const known = picker.knownRetailers();
        const query = JSON.stringify({
          product_name: name, model,
          excluded_names: known.map(r => r.name),
          excluded_homepages: known.map(r => r.homepage).filter(Boolean),
        });
        return {
          createUrl: '/api/retailer-search-jobs', body: { query },
          pollUrl: id => `/api/retailer-search-jobs/${id}`,
        };
      },
      // Long timeout: the opti runner that answers this only polls every 2 minutes
      // (shopwatch-research-runner.timer), unlike llm-helper.py's own short interval.
      pollOpts: { intervalMs: 3000, timeoutMs: 240000,
        queuedText: 'Queued for the server research runner...',
        runningText: 'Searching the web for other retailers...',
        notRunningHint: 'is the opti research runner online? It only checks for a new '
          + 'job every 2 minutes, so this can genuinely take a couple of minutes.' },
      onResult: (result, box) => { if (!isStale(generation)) picker.renderDiscovered(result, box); },
      onError: err => `Could not search the web for retailers: ${err.message}`,
    });
  });
}

wireDiscoveryButtons({
  llmBtnId: 'wiz-discover-retailers', webBtnId: 'wiz-discover-retailers-web',
  boxId: 'wiz-retailer-discovered', getPicker: () => wiz.picker,
  getName: () => wizVal('wiz-name'), getModel: () => wizVal('wiz-model'),
});

wire('wiz-back', () => { wiz.index = Math.max(0, wiz.index - 1); wizRender(); });

wire('wiz-next', async () => {
  const step = WIZ_STEPS[wiz.index];
  try {
    if (step === 'basics') {
      if (!wizVal('wiz-name') || !wizVal('wiz-model')) {
        toast('Name and model are both required.', 'bad');
        return;
      }
    } else if (step === 'optional') {
      const product = await api('/api/products', { method: 'POST', body: {
        name: wizVal('wiz-name'), model: wizVal('wiz-model'), brand: wizVal('wiz-brand') || null,
        category: wizVal('wiz-category') || 'general', verdict: wizVal('wiz-verdict'),
        notes: wizVal('wiz-notes') || null,
      }});
      wiz.productId = product.id;
      const existingGroup = wizVal('wiz-group');
      const newGroupName = wizVal('wiz-new-group');
      if (existingGroup || newGroupName) {
        const body = existingGroup ? { group_id: Number(existingGroup) } : { name: newGroupName };
        await api(`/api/products/${product.id}/group`, { method: 'POST', body });
      }
    } else if (step === 'price') {
      const trigger = wizNum('wiz-trigger'), excellent = wizNum('wiz-excellent'),
        histLow = wizNum('wiz-histlow');
      wiz.wantsSuggestion = trigger === null && excellent === null && histLow === null;
      if (!wiz.wantsSuggestion) {
        await api(`/api/products/${wiz.productId}`, { method: 'PATCH', body: {
          trigger_price: trigger, excellent_price: excellent, historical_low_price: histLow,
        }});
      }
    } else if (step === 'retailers') {
      // Courtesy for typing a name (or several, comma-separated) and hitting Next
      // straight away, without clicking Add first.
      const newName = wizVal('wiz-new-retailer');
      if (newName) {
        const failed = await wiz.picker.addRetailerNames(newName);
        document.getElementById('wiz-new-retailer').value = failed.join(', ');
        if (failed.length) {
          toast(`Could not add: ${failed.join(', ')}`, 'bad');
          return; // don't advance past a name that didn't actually get added
        }
      }
      if (wiz.selected.size === 0) {
        toast('Pick at least one retailer, or add one.', 'bad');
        return;
      }
      document.getElementById('wiz-cost-notice').textContent =
        `This will research ${wiz.selected.size} retailer${wiz.selected.size === 1 ? '' : 's'}. `
        + 'It can take a few minutes and uses shared Claude quota, shared with other things '
        + 'that use the same subscription. Go ahead?';
    } else if (step === 'go') {
      const job = await api('/api/research-jobs', { method: 'POST', body: {
        product_id: wiz.productId, retailer_ids: [...wiz.selected],
      }});
      wiz.jobId = job.id;
      wiz.index = WIZ_STEPS.indexOf('running');
      wizRender();
      document.getElementById('wiz-progress').replaceChildren(...job.results.map(wizProgressRow));
      wiz.pollTimer = setInterval(wizPoll, 2000);
      return;
    }
  } catch (err) {
    toast(`Could not continue: ${err.message}`, 'bad');
    return;
  }
  wiz.index += 1;
  wizRender();
});

wire('wiz-finish', () => { location.href = `/products/${wiz.productId}`; });

document.getElementById('wizard-dialog')?.addEventListener('close', () => {
  if (wiz.pollTimer) clearInterval(wiz.pollTimer);
  // A product created earlier in the wizard (from the 'optional' step onward) is real
  // and already on the board even if the wizard is closed before "Done" - reload so it
  // shows up rather than looking like the cancel discarded it.
  if (wiz.productId) location.reload();
});

wire('btn-add-listing', () => document.getElementById('listing-dialog').showModal());
wire('al-save', async () => {
  const value = id => document.getElementById(id).value.trim();
  const number = id => value(id) === '' ? null : Number(value(id));
  const productId = Number(location.pathname.split('/').pop());
  const body = {
    retailer: value('al-retailer'), url: value('al-url') || null,
    model_on_page: value('al-model') || null, advertised_price: number('al-price'),
    freight: number('al-freight'), cashback: number('al-cashback'),
    condition: value('al-condition'), stock_status: value('al-stock') || null,
    pickup_status: value('al-pickup') || null, warranty: value('al-warranty') || null,
    included_components: value('al-contents') || null, seller_notes: value('al-notes') || null,
    price_valid_until: value('al-promo-end') || null
  };
  try {
    await api(`/api/products/${productId}/retailers`, { method: 'POST', body });
    toast('Listing added.', 'good');
    location.reload();
  } catch (err) { toast(`Could not add listing: ${err.message}`, 'bad'); }
});

/* ---------------------------------------------------- paste a listing URL (issue #94)
 *
 * The low-friction sibling of "Add retailer" above: no retailer name to type, no
 * price to know yet - just the page address. See app/main.py's
 * api_create_listing_from_url for what happens server-side (adapter scrape first,
 * a URL-scoped research job otherwise, and the listing lands with its URL either way).
 */

wire('btn-wayback-history', async event => {
  const button = event.currentTarget;
  const productId = Number(button.dataset.product);
  button.disabled = true;
  button.textContent = 'Checking archived listings...';
  try {
    const result = await api(`/api/products/${productId}/wayback-history`, { method: 'POST' });
    toast(`${result.evidence_count} sourced archive price${result.evidence_count === 1 ? '' : 's'} found.`, 'good');
    location.reload();
  } catch (err) {
    toast(`Could not check Wayback history: ${err.message}`, 'bad');
    button.disabled = false;
    button.textContent = 'Check archived listings';
  }
});

wire('btn-add-listing-url', () => {
  for (const id of ['ul-url', 'ul-seller-name', 'ul-seller-id', 'ul-offer-id']) document.getElementById(id).value = '';
  document.getElementById('url-listing-dialog').showModal();
});
wire('ul-save', async () => {
  const input = document.getElementById('ul-url');
  const url = input.value.trim();
  if (!url) { toast('Paste a URL first.', 'bad'); return; }
  const productId = Number(location.pathname.split('/').pop());
  const btn = document.getElementById('ul-save');
  btn.disabled = true;
  try {
    const result = await api(`/api/products/${productId}/retailers/from-url`, {
      method: 'POST', body: {
        url, seller_name: document.getElementById('ul-seller-name').value.trim() || null,
        seller_id: document.getElementById('ul-seller-id').value.trim() || null,
        offer_id: document.getElementById('ul-offer-id').value.trim() || null,
      },
    });
    const message = result.scraped
      ? `Listing added for ${result.retailer.name} - price found straight away.`
      : result.research_job
        ? `Listing added for ${result.retailer.name}. Checking that page for a price now.`
        : `Listing added for ${result.retailer.name}. No price yet - add one by hand, `
          + 'or try again once any research already running for this product finishes.';
    toast(message, 'good');
    location.reload();
  } catch (err) {
    toast(`Could not add listing: ${err.message}`, 'bad');
  } finally {
    btn.disabled = false;
  }
});

/* ------------------------------------------------------ retry research (product page)
 *
 * The wizard's own research step only ever runs once, against a product just created -
 * closing the wizard after a failed or partial run left no way back in at all. This is
 * that way back in: same /api/research-jobs the wizard uses, but pre-ticking whichever
 * retailers did not come back FOUND last time, so a retry never means reconstructing
 * the retailer list from memory.
 */

let researchRetry = { productId: null, name: '', model: '', retailers: [], selected: new Set(), picker: null, pollTimer: null, generation: 0 };

wire('btn-research-retailers', async event => {
  if (researchRetry.pollTimer) clearInterval(researchRetry.pollTimer);
  const { product, name, model } = event.currentTarget.dataset;
  researchRetry = {
    productId: Number(product), name, model,
    retailers: [], selected: new Set(), picker: null, pollTimer: null,
    // Bumped on every open (even reopening the SAME product) so a discovery poll or an
    // in-flight "Add" from a previous opening can tell it has been superseded and drop
    // its result instead of mutating a Set this session no longer reads.
    generation: researchRetry.generation + 1,
  };
  const list = document.getElementById('research-retailer-list');
  const progress = document.getElementById('research-progress');
  const goBtn = document.getElementById('research-go');
  const intro = document.getElementById('research-dialog-intro');
  const discoverBox = document.getElementById('research-retailer-discovered');
  progress.hidden = true;
  progress.replaceChildren();
  list.replaceChildren();
  discoverBox.hidden = true;
  discoverBox.replaceChildren();
  document.getElementById('research-new-retailer').value = '';
  for (const id of ['research-discover-retailers', 'research-discover-retailers-web']) {
    const btn = document.getElementById(id);
    btn.disabled = false;
  }
  document.getElementById('research-discover-retailers').textContent = 'Find other retailers';
  document.getElementById('research-discover-retailers-web').textContent = 'Search the web';
  goBtn.hidden = false;
  goBtn.disabled = false;
  goBtn.textContent = 'Research selected';
  intro.textContent = 'Loading retailers...';
  document.getElementById('research-dialog').showModal();

  let retailers, latestJob;
  try {
    [retailers, latestJob] = await Promise.all([
      api('/api/retailers'),
      api(`/api/products/${researchRetry.productId}/research-jobs/latest`),
    ]);
  } catch (err) {
    intro.textContent = `Could not load retailers: ${err.message}`;
    return;
  }
  // Issue #112: same client-side filter as the wizard's own retailer picker.
  retailers = retailers.filter(r => !r.excluded);

  researchRetry.retailers = retailers;
  researchRetry.picker = makeRetailerPicker({
    list, retailers: researchRetry.retailers, selected: researchRetry.selected,
  });

  const stillToCheck = new Set(
    (latestJob?.results || []).filter(r => r.status !== 'FOUND').map(r => r.retailer_id)
  );
  intro.textContent = stillToCheck.size
    ? `Pre-selected the ${stillToCheck.size} retailer${stillToCheck.size === 1 ? '' : 's'} `
      + 'that did not turn up a price last time. Untick or add more as you like.'
    : 'Pick which retailers to check.';

  for (const r of retailers) list.appendChild(researchRetry.picker.retailerRow(r, { checked: stillToCheck.has(r.id) }));
});

wire('research-add-retailer', async () => {
  const input = document.getElementById('research-new-retailer');
  const btn = document.getElementById('research-add-retailer');
  const text = input.value.trim();
  if (!text) return;
  if (!researchRetry.picker) { toast('Still loading retailers - try again in a moment.', 'bad'); return; }
  const picker = researchRetry.picker;
  const generation = researchRetry.generation;
  input.disabled = true;
  btn.disabled = true;
  try {
    const failed = await picker.addRetailerNames(text);
    // The dialog was closed and reopened (same or different product) while this add
    // was in flight - the retailer it created still exists server-side (harmless,
    // idempotent), but reporting success/failure here would be about a session that
    // no longer exists, so leave the now-current dialog alone instead.
    if (researchRetry.generation !== generation) return;
    input.value = failed.join(', ');
    if (failed.length) toast(`Could not add: ${failed.join(', ')}`, 'bad');
  } finally {
    input.disabled = false;
    btn.disabled = false;
  }
});

wireDiscoveryButtons({
  llmBtnId: 'research-discover-retailers', webBtnId: 'research-discover-retailers-web',
  boxId: 'research-retailer-discovered', getPicker: () => researchRetry.picker,
  getName: () => researchRetry.name, getModel: () => researchRetry.model,
  getGeneration: () => researchRetry.generation,
});

wire('research-go', async () => {
  if (researchRetry.selected.size === 0) { toast('Pick at least one retailer.', 'bad'); return; }
  const goBtn = document.getElementById('research-go');
  const progress = document.getElementById('research-progress');
  goBtn.disabled = true;
  try {
    const job = await api('/api/research-jobs', { method: 'POST', body: {
      product_id: researchRetry.productId, retailer_ids: [...researchRetry.selected],
    }});
    goBtn.hidden = true;
    progress.hidden = false;
    progress.replaceChildren(...job.results.map(wizProgressRow));
    researchRetry.pollTimer = setInterval(async () => {
      const updated = await api(`/api/research-jobs/${job.id}`);
      progress.replaceChildren(...updated.results.map(wizProgressRow));
      if (updated.status === 'DONE' || updated.status === 'FAILED') {
        clearInterval(researchRetry.pollTimer);
        const found = updated.results.filter(r => r.status === 'FOUND').length;
        toast(`Research finished: found ${found} of ${updated.results.length}.`,
              found ? 'good' : 'bad');
        setTimeout(() => location.reload(), 1400);
      }
    }, 2000);
  } catch (err) {
    toast(`Could not start research: ${err.message}`, 'bad');
    goBtn.disabled = false;
  }
});

document.getElementById('research-dialog')?.addEventListener('close', () => {
  if (researchRetry.pollTimer) clearInterval(researchRetry.pollTimer);
});

/* ------------------------------------------------------ historical-low research
 *
 * issue #93: "has this ever been cheaper", not "what's it selling for today" - a
 * different question put to the same research-job pipeline (kind: 'historical_low'),
 * answered once for the whole product rather than once per retailer. The finding is
 * always shown as something to confirm or discard, never applied on its own: "Use
 * this" only prefills the ordinary product-edit dialog's lowest_known_* fields, the
 * same dialog a manual edit already uses, so the only way it reaches the product is
 * the person reviewing it and clicking that dialog's own Save.
 */

let histLow = { productId: null, name: '', model: '', pollTimer: null, generation: 0 };

function histLowStatusText(job) {
  if (!job) return 'Pick "Research" to look for a historical low.';
  if (job.status === 'QUEUED') return 'Queued - waiting for the opti research runner to pick it up.';
  if (job.status === 'RUNNING') return 'Researching... this can take a couple of minutes.';
  if (job.status === 'FAILED') return `Could not complete the research: ${job.error || 'unknown error'}.`;
  return '';
}

/* http(s)-only allowlist before a candidate's URL ever goes into a real href.
 * candidate.url ultimately came out of an LLM's reply to a "search the web" prompt -
 * untrusted content relayed through the model, not something safe to assign straight
 * into an anchor's href. The server (app/research.py, deploy/research-runner.py) already
 * filters to http(s) before storing it, but that must not be the only place this is
 * checked: the value is exposed as-is through the API, so a link is only ever built
 * from a URL this function itself has approved. Returns null (never linkable) for
 * anything that isn't a parseable http/https URL, including a bare `new URL()` throw. */
function safeHttpUrl(url) {
  try {
    // No base argument: a relative or protocol-relative string is not a real listing
    // URL either, so it is rejected the same as a bad scheme rather than silently
    // resolved against this page's own origin.
    const parsed = new URL(url);
    return (parsed.protocol === 'http:' || parsed.protocol === 'https:') ? parsed.href : null;
  } catch {
    return null;
  }
}

/* issue #98: the historical-low search inevitably notices other retailers currently
 * selling the product along the way - genuinely incidental, since HISTORICAL_LOW_PROMPT
 * already asks it to search broadly. Parsed client-side from a raw JSON string, same
 * convention job.result already uses elsewhere (retailer-discovery jobs). */
function otherRetailersFound(job) {
  if (!job || !job.historical_low_other_retailers) return [];
  try {
    const list = JSON.parse(job.historical_low_other_retailers);
    return Array.isArray(list) ? list.filter(c => c && c.name) : [];
  } catch {
    return [];
  }
}

/* Ticking a box only selects it - nothing is added until "Add ticked as listings" is
 * pressed, same "human confirms" discipline as the historical-low price's own "Use
 * this" step. Reuses the exact endpoints "Add retailer" and "paste a listing URL"
 * (#94) already use, rather than inventing a third listing-creation path: a candidate
 * with a URL goes through .../retailers/from-url (which also tries to scrape a price
 * straight away), a name-only candidate through the plain .../retailers endpoint. */
async function renderOtherRetailers(job, container) {
  let candidates = otherRetailersFound(job);
  if (!candidates.length) return;

  // Issue #112: server-side (_clean_other_retailers) already drops a candidate
  // matching an excluded retailer's name before this is ever stored - this is the
  // defensive second check, same "never trust only one layer" discipline the URL
  // allowlist above already follows, in case a candidate was stored before an
  // exclusion was added, or the two ever drift out of step.
  try {
    const excludedLower = new Set(
      (await api('/api/retailers')).filter(r => r.excluded).map(r => r.name.toLowerCase())
    );
    candidates = candidates.filter(c => !excludedLower.has(c.name.toLowerCase()));
  } catch {
    // If the retailer list can't be loaded, fall back to whatever the server already
    // sent - it did its own filtering, so this is a missed extra check, not an
    // unfiltered one.
  }
  if (!candidates.length) return;

  container.appendChild(el(
    'p',
    'Also noticed selling this while researching the historical low - tick any to add as a tracked listing:',
    'meta',
  ));
  const list = el('div', '', 'wizard-retailer-list');
  const ticked = [];
  for (const candidate of candidates) {
    const row = el('label', '', 'wizard-retailer-row');
    const box = document.createElement('input');
    box.type = 'checkbox';
    row.appendChild(box);
    row.appendChild(el('span', candidate.name, 'name'));
    const safeUrl = safeHttpUrl(candidate.url);
    if (safeUrl) {
      const link = document.createElement('a');
      link.href = safeUrl;
      link.target = '_blank';
      link.rel = 'noopener';
      link.className = 'cap';
      link.textContent = 'listing found';
      link.addEventListener('click', event => event.stopPropagation());
      row.appendChild(link);
    } else {
      row.appendChild(el('span', candidate.url ? 'unusable URL, not linked' : 'no URL given', 'cap'));
    }
    list.appendChild(row);
    ticked.push({ box, candidate });
  }
  container.appendChild(list);

  const actions = el('div', '', 'wizard-retailer-actions');
  const addBtn = el('button', 'Add ticked as listings', 'btn');
  addBtn.type = 'button';
  addBtn.addEventListener('click', async () => {
    const chosen = ticked.filter(t => t.box.checked);
    if (!chosen.length) { toast('Tick at least one retailer first.', 'bad'); return; }
    addBtn.disabled = true;
    let added = 0;
    const failed = [];
    for (const { candidate } of chosen) {
      try {
        // Same http(s)-only allowlist as the link rendering above, not just the raw
        // value the API returned: a candidate whose URL doesn't pass falls back to
        // the name-only path rather than forwarding an unvalidated URL onward.
        const safeUrl = safeHttpUrl(candidate.url);
        if (safeUrl) {
          await api(`/api/products/${histLow.productId}/retailers/from-url`, {
            method: 'POST', body: { url: safeUrl },
          });
        } else {
          await api(`/api/products/${histLow.productId}/retailers`, {
            method: 'POST', body: { retailer: candidate.name },
          });
        }
        added += 1;
      } catch (err) {
        failed.push(`${candidate.name}: ${err.message}`);
      }
    }
    if (added) {
      toast(`Added ${added} retailer${added === 1 ? '' : 's'} as new listing${added === 1 ? '' : 's'}.`, 'good');
    }
    if (failed.length) toast(`Could not add: ${failed.join('; ')}`, 'bad');
    if (added) location.reload(); else addBtn.disabled = false;
  });
  actions.appendChild(addBtn);
  container.appendChild(actions);
}

/* JS-side twin of app/main.py's _notes_html Jinja filter - same "- " prefixed line
 * convention (deploy/research-runner.py's _format_reason), same order-preserving
 * <ul> grouping, same "fewer than 2 bullet lines renders as plain text" fallback for
 * old single-paragraph notes. Needed here too because this dialog shows a job's
 * result before it is ever saved to the product (and the Jinja filter only ever
 * sees saved product data) - a fresh research result must read the same way the
 * saved note will once "Use this" is clicked. */
function notesElement(text, fallbackText) {
  const wrap = el('div', '', 'reason');
  if (!text) {
    if (fallbackText) wrap.textContent = fallbackText;
    return wrap;
  }
  const lines = text.split('\n').map(l => l.trim()).filter(Boolean);
  if (lines.filter(l => l.startsWith('- ')).length < 2) {
    wrap.textContent = lines.join(' ');
    return wrap;
  }
  let list = null;
  for (const line of lines) {
    if (line.startsWith('- ')) {
      if (!list) {
        list = document.createElement('ul');
        list.style.cssText = 'margin:0 0 6px;padding-left:20px';
        wrap.appendChild(list);
      }
      list.appendChild(el('li', line.slice(2).trim()));
    } else {
      list = null;
      const p = el('p', line);
      p.style.margin = '0 0 6px';
      wrap.appendChild(p);
    }
  }
  return wrap;
}

function renderHistLowResult(job) {
  const box = document.getElementById('historical-low-result');
  box.replaceChildren();
  if (!job || job.status !== 'DONE') { box.hidden = true; return; }
  box.hidden = false;

  if (job.historical_low_price === null || job.historical_low_price === undefined) {
    box.append(
      el('span', 'No confident historical low found.', 'meta'),
      notesElement(job.historical_low_notes, 'The research pass could not find a source it trusted.'),
    );
    renderOtherRetailers(job, box);
    return;
  }

  const meta = [];
  if (job.historical_low_retailer) meta.push(job.historical_low_retailer);
  if (job.historical_low_date) meta.push(job.historical_low_date);
  if (job.historical_low_confidence) meta.push(`${job.historical_low_confidence.toLowerCase()} confidence`);

  const useBtn = el('button', 'Use this', 'primary');
  const discardBtn = el('button', 'Discard');
  useBtn.type = 'button';
  discardBtn.type = 'button';
  useBtn.addEventListener('click', () => {
    const set = (id, value) => { const field = document.getElementById(id); if (field) field.value = value ?? ''; };
    set('ep-lowest_known_price', job.historical_low_price);
    set('ep-lowest_known_date', job.historical_low_date);
    set('ep-lowest_known_retailer', job.historical_low_retailer);
    // The meta line stays on its own line rather than glued onto the reason text with
    // " - " and a trailing ". Check before saving." - historical_low_notes can now be
    // several "- " prefixed bullet lines (deploy/research-runner.py's _format_reason),
    // and gluing free text onto the end of a bullet line reads as broken, not joined.
    const metaLine = 'AI-estimated historical low, unconfirmed'
      + (job.historical_low_confidence ? ` (${job.historical_low_confidence.toLowerCase()} confidence).` : '.')
      + ' Check before saving.';
    const note = job.historical_low_notes ? `${metaLine}\n${job.historical_low_notes}` : metaLine;
    set('ep-lowest_known_notes', note);
    document.getElementById('historical-low-dialog').close();
    document.getElementById('product-dialog').showModal();
    toast('Filled into the edit form below - check it, then Save to apply.', 'good');
  });
  discardBtn.addEventListener('click', () => document.getElementById('historical-low-dialog').close());

  box.append(
    el('span', 'UNCONFIRMED ESTIMATE - not a real listing', 'tag'),
    el('div', money(job.historical_low_price), 'num'),
    el('div', meta.join(' · '), 'meta'),
    notesElement(job.historical_low_notes, ''),
    (() => { const actions = el('div', '', 'actions'); actions.append(useBtn, discardBtn); return actions; })(),
  );
  renderOtherRetailers(job, box);
}

wire('btn-research-historical-low', async event => {
  if (histLow.pollTimer) clearInterval(histLow.pollTimer);
  const { product, name, model } = event.currentTarget.dataset;
  histLow = { productId: Number(product), name, model, pollTimer: null, generation: histLow.generation + 1 };
  const generation = histLow.generation;

  const status = document.getElementById('historical-low-status');
  const progress = document.getElementById('historical-low-progress');
  const goBtn = document.getElementById('historical-low-go');
  progress.hidden = true;
  goBtn.hidden = false;
  goBtn.disabled = false;
  status.textContent = 'Checking for a previous run...';
  renderHistLowResult(null);
  document.getElementById('historical-low-dialog').showModal();

  let latest;
  try {
    latest = await api(`/api/products/${histLow.productId}/research-jobs/latest`);
  } catch (err) {
    status.textContent = `Could not load research history: ${err.message}`;
    return;
  }
  if (histLow.generation !== generation) return;
  const relevant = latest && latest.kind === 'historical_low' ? latest : null;
  status.textContent = histLowStatusText(relevant);
  if (relevant && (relevant.status === 'QUEUED' || relevant.status === 'RUNNING')) {
    goBtn.hidden = true;
    pollHistLow(relevant.id, generation);
  } else {
    renderHistLowResult(relevant);
  }
});

function pollHistLow(jobId, generation) {
  const status = document.getElementById('historical-low-status');
  histLow.pollTimer = setInterval(async () => {
    if (histLow.generation !== generation) { clearInterval(histLow.pollTimer); return; }
    let job;
    try {
      job = await api(`/api/research-jobs/${jobId}`);
    } catch (err) {
      clearInterval(histLow.pollTimer);
      status.textContent = `Lost track of the job: ${err.message}`;
      return;
    }
    status.textContent = histLowStatusText(job);
    if (job.status === 'DONE' || job.status === 'FAILED') {
      clearInterval(histLow.pollTimer);
      document.getElementById('historical-low-go').hidden = false;
      renderHistLowResult(job);
    }
  }, 2000);
}

wire('historical-low-go', async () => {
  const goBtn = document.getElementById('historical-low-go');
  const status = document.getElementById('historical-low-status');
  const generation = histLow.generation;
  goBtn.disabled = true;
  renderHistLowResult(null);
  try {
    const job = await api('/api/research-jobs', { method: 'POST', body: {
      product_id: histLow.productId, kind: 'historical_low',
    }});
    goBtn.hidden = true;
    status.textContent = histLowStatusText(job);
    pollHistLow(job.id, generation);
  } catch (err) {
    toast(`Could not start research: ${err.message}`, 'bad');
    goBtn.disabled = false;
  }
});

document.getElementById('historical-low-dialog')?.addEventListener('close', () => {
  if (histLow.pollTimer) clearInterval(histLow.pollTimer);
});

/* ----------------------------------------------------------------- purchases */

wire('btn-mark-purchased', () => {
  const today = new Date().toISOString().slice(0, 10);
  const date = document.getElementById('pu-date');
  if (date && !date.value) date.value = today;
  prefillFromListing();
  document.getElementById('purchase-dialog').showModal();
});

/* Picking a listing fills the numbers from what the board already knows, so the common
   case is two clicks. Every field stays editable: what you paid is frequently not what
   the page said. */
function prefillFromListing() {
  const select = document.getElementById('pu-listing');
  if (!select) return;
  const option = select.selectedOptions[0];
  if (!option || !option.value) return;
  const set = (id, value) => {
    const el = document.getElementById(id);
    if (el && value !== undefined && value !== '') el.value = value;
  };
  set('pu-price', option.dataset.delivered);
  set('pu-advertised', option.dataset.advertised);
  set('pu-freight', option.dataset.freight);
  set('pu-condition', option.dataset.condition);
}
document.getElementById('pu-listing')?.addEventListener('change', prefillFromListing);

wire('pu-save', async () => {
  const productId = Number(location.pathname.split('/').pop());
  const value = id => (document.getElementById(id)?.value || '').trim();
  const number = id => value(id) === '' ? null : Number(value(id));
  if (value('pu-price') === '') {
    toast('Delivered price paid is required: it is the number the whole board ranks on.', 'bad');
    return;
  }
  const body = {
    listing_id: value('pu-listing') ? Number(value('pu-listing')) : null,
    retailer_name: value('pu-retailer') || null,
    price_paid: number('pu-price'),
    advertised_paid: number('pu-advertised'),
    freight_paid: number('pu-freight'),
    purchased_at: value('pu-date') || null,
    condition: value('pu-condition'),
    order_reference: value('pu-order') || null,
    warranty_months: number('pu-warranty'),
    price_protection_until: value('pu-protection') || null,
    notes: value('pu-notes') || null
  };
  try {
    await api(`/api/products/${productId}/purchase`, { method: 'POST', body });
    toast('Purchase recorded. The card stays watched.', 'good');
    location.reload();
  } catch (err) { toast(`Could not record the purchase: ${err.message}`, 'bad'); }
});

wire('btn-undo-purchase', async event => {
  if (!confirm('Undo the purchase record and return this product to ACTIVE? The price history row is kept.')) return;
  const productId = Number(event.currentTarget.dataset.product);
  try {
    await api(`/api/products/${productId}/purchase`, { method: 'DELETE' });
    toast('Purchase record removed. Product is ACTIVE again.', 'good');
    location.reload();
  } catch (err) { toast(`Could not undo: ${err.message}`, 'bad'); }
});

wire('btn-give-up-product', async event => {
  if (!confirm('Remove this card? It leaves the board, but its listings and history are kept and can be restored.')) return;
  const productId = Number(event.currentTarget.dataset.product);
  try {
    await api(`/api/products/${productId}`, { method: 'DELETE' });
    toast('Archived. It has come off the board.', 'good');
    location.href = '/';
  } catch (err) { toast(`Could not archive: ${err.message}`, 'bad'); }
});

document.querySelectorAll('[data-archive-product]').forEach(button => button.addEventListener('click', async () => {
  const id = button.dataset.archiveProduct;
  if (!confirm('Remove this card? It leaves the board, but its listings and history are kept and can be restored.')) return;
  try {
    await api(`/api/products/${id}`, { method: 'DELETE' });
    location.href = '/';
  } catch (err) { toast(`Could not remove card: ${err.message}`, 'bad'); }
}));

for (const [attribute, status, archived, message] of [
  ['data-pause-product', 'PARKED', undefined, 'Watch paused.'],
  ['data-resume-product', 'ACTIVE', undefined, 'Watch resumed.'],
  ['data-restore-product', undefined, 0, 'Card restored.']
]) {
  document.querySelectorAll(`[${attribute}]`).forEach(button => button.addEventListener('click', async () => {
    const id = button.getAttribute(attribute);
    try {
      await api(`/api/products/${id}`, { method: 'PATCH', body: {
        ...(status ? { status: button.dataset.resumeStatus || status } : {}),
        ...(archived !== undefined ? { archived } : {})
      }});
      location.href = `/products/${id}`;
      toast(message, 'good');
    } catch (err) { toast(`Could not update watch: ${err.message}`, 'bad'); }
  }));
}

wire('btn-delete-product', () => {
  document.getElementById('del-confirm-name').value = '';
  document.getElementById('del-confirm-go').disabled = true;
  document.getElementById('delete-dialog').showModal();
});

// Typing the product name back is the whole guard here - there is no second prompt,
// so the button stays disabled until the text matches exactly.
document.getElementById('del-confirm-name')?.addEventListener('input', event => {
  document.getElementById('del-confirm-go').disabled = event.target.value !== event.target.dataset.expected;
});

wire('del-confirm-go', async event => {
  const button = event.currentTarget;
  if (button.disabled) return;  // belt and braces: the listener fires on any click, typed or not
  const productId = Number(button.dataset.product);
  button.disabled = true;
  try {
    await api(`/api/products/${productId}/permanently`, { method: 'DELETE' });
    toast('Deleted permanently.', 'good');
    location.href = '/';
  } catch (err) {
    toast(`Could not delete: ${err.message}`, 'bad');
    button.disabled = false;
  }
});

wire('btn-edit-product', () => document.getElementById('product-dialog').showModal());
wire('ep-save', async () => {
  const productId = Number(location.pathname.split('/').pop());
  const text = ['name', 'model', 'brand', 'category', 'generation', 'verdict', 'status', 'vesa',
    'fit_notes', 'notes', 'lowest_known_notes', 'lowest_known_date', 'lowest_known_retailer', 'quantity_unit'];
  const numeric = ['trigger_price', 'excellent_price', 'historical_low_price',
    'lowest_known_price', 'width_mm', 'height_mm', 'depth_mm', 'weight_kg', 'quantity'];
  const body = {};
  for (const key of text) {
    const el = document.getElementById(`ep-${key}`);
    if (el) body[key] = el.value.trim() || null;
  }
  for (const key of numeric) {
    const el = document.getElementById(`ep-${key}`);
    if (el) body[key] = el.value.trim() === '' ? null : Number(el.value);
  }
  try {
    body.specs = JSON.parse(document.getElementById('ep-specs').value || '{}');
    body.components = JSON.parse(document.getElementById('ep-components').value || '{}');
  } catch (err) {
    toast(`Specifications JSON is invalid: ${err.message}`, 'bad');
    return;
  }
  try {
    await api(`/api/products/${productId}`, { method: 'PATCH', body });
    toast('Product saved.', 'good');
    location.reload();
  } catch (err) { toast(`Could not save product: ${err.message}`, 'bad'); }
});

/* ----------------------------------------------------------------- price axis
 *
 * The axis carries every contender, so its domain is set by the dearest one and the
 * three targets end up crushed together at the cheap end. Zoom is therefore not a
 * nicety: at full extent the interesting part of the axis is a few percent wide.
 *
 * Two invariants:
 *   - the visible window is always stated in the readout. A zoomed axis that does not
 *     say where it is looking reads as the full picture and is worse than no axis.
 *   - labels are assigned to lanes by measured width, so a label never covers the line
 *     or another label. Server-rendered positions are the no-JS fallback, which is
 *     honest but unzoomed.
 */

const AXIS_MIN_SPAN = 20;        // never zoom past a $20 window: the dots would separate
                                 // into meaninglessness and the readout would imply
                                 // precision the prices do not have.
const LANE_HEIGHT = 30;
const LANE_COUNT = 3;
const CLUSTER_GAP = 26;          // px between dot centres below which they are one marker
const LABEL_PAD = 12;            // px of clear air each side of a label before it is a collision

function axisMoney(value) {
  return '$' + Math.round(value).toLocaleString('en-AU');
}

function setupAxis(root) {
  const plot = root.querySelector('[data-axis-plot]');
  const readout = root.querySelector('[data-axis-window]');
  const controls = root.querySelector('[data-axis-controls]');
  if (!plot) return;

  // Captured before layoutThresholdLabels() ever grows the plot's own inline height
  // to make room for a stacked threshold lane, so growing it is always relative to
  // the real base rather than compounding on a previous draw()'s already-grown value.
  const basePlotHeight = plot.clientHeight;

  const full = { lo: Number(root.dataset.lo), hi: Number(root.dataset.hi) };
  if (!isFinite(full.lo) || !isFinite(full.hi) || full.hi <= full.lo) return;

  const points = Array.from(plot.querySelectorAll('[data-point]'));
  const thresholds = Array.from(plot.querySelectorAll('[data-threshold]'));
  const bands = Array.from(plot.querySelectorAll('[data-band]'));

  // Bands were positioned server-side against the full domain. Recover each band's
  // price range once, so zooming re-projects from prices rather than compounding
  // percentages, which would drift.
  const span = full.hi - full.lo;
  for (const band of bands) {
    band._from = full.lo + (Number(band.dataset.from) / 100) * span;
    band._to = full.lo + (Number(band.dataset.to) / 100) * span;
  }

  let view = { ...full };
  if (controls) controls.hidden = false;

  const project = value => ((value - view.lo) / (view.hi - view.lo)) * 100;

  function place(el, value) {
    const pos = project(value);
    const visible = pos >= -12 && pos <= 112;
    el.style.left = pos + '%';
    el.style.visibility = visible ? '' : 'hidden';
    return visible;
  }

  function layoutThresholdLabels() {
    // Same lane-avoidance promise the block comment above makes for points - "a label
    // never covers the line or another label" - extended to threshold marks, which
    // never got it: draw() only ever called clusterAndLabel() below, which walks
    // `points`, not `thresholds`. pricing.threshold_scale() already merges two marks
    // that land on the exact same value into one ("hist low + excellent"), but that
    // combined label is often wider than either label alone, so a *near* miss in
    // position - two genuinely different values close together, e.g. a merged mark at
    // $69.30 next to a lone "trigger" at $76.23 - collides even though the values
    // themselves are correctly distinct. At most 3 threshold marks ever exist, so
    // unlike points there is no "hide it" fallback: every mark always gets a lane.
    const width = plot.clientWidth || 1;
    const ordered = thresholds
      .map(el => ({ el, centre: (project(Number(el.dataset.value)) / 100) * width }))
      .filter(item => item.el.style.visibility !== 'hidden')
      .sort((a, b) => a.centre - b.centre);

    const laneEnds = [];
    for (const item of ordered) {
      const half = item.el.offsetWidth / 2 + LABEL_PAD;
      let lane = laneEnds.findIndex(end => item.centre - half > end);
      if (lane === -1) { lane = laneEnds.length; laneEnds.push(-Infinity); }
      laneEnds[lane] = item.centre + half;
      item.el.style.top = (2 + lane * LANE_HEIGHT) + 'px';
    }

    // Only lane 0 (top:2px) fits above the bands strip's default top:34px - a second
    // lane needs the bands/line/contenders pushed down by the same amount (see
    // --band-shift in style.css) or it would either overlap the bands strip or get
    // clipped by .axis-plot's own overflow:hidden. Always set both explicitly (not
    // only when non-zero) so a product that stops colliding after a zoom/pan
    // shrinks back to its normal size rather than staying grown from an earlier draw().
    const extraLanes = Math.max(0, laneEnds.length - 1);
    plot.style.setProperty('--band-shift', (extraLanes * LANE_HEIGHT) + 'px');
    plot.style.height = (basePlotHeight + extraLanes * LANE_HEIGHT) + 'px';
  }

  function clusterAndLabel() {
    // Five of these retailers sit within $32 of each other. Drawn individually at full
    // extent they are one illegible smudge of overlapping dots and text, which is worse
    // than not plotting them: it looks like a rendering fault rather than like a tie.
    //
    // So anything closer together than CLUSTER_GAP collapses into one marker that says
    // how many and over what range. Zooming in separates them again, which is the
    // honest relationship: they ARE nearly the same price, and the axis should say so.
    const width = plot.clientWidth || 1;
    const ordered = points
      .map(el => ({ el, value: Number(el.dataset.value), centre: (project(Number(el.dataset.value)) / 100) * width }))
      .filter(item => item.el.style.visibility !== 'hidden')
      .sort((a, b) => a.value - b.value);

    for (const { el } of ordered) {
      el.classList.remove('is-cluster', 'is-clustered');
      const label = el.querySelector('.axis-label');
      if (label && el._label !== undefined) label.innerHTML = el._label;
    }

    const groups = [];
    for (const item of ordered) {
      const last = groups[groups.length - 1];
      if (last && item.centre - last[last.length - 1].centre < CLUSTER_GAP) last.push(item);
      else groups.push([item]);
    }

    const laneEnds = new Array(LANE_COUNT).fill(-Infinity);
    for (const group of groups) {
      const lead = group[0];
      const label = lead.el.querySelector('.axis-label');

      if (group.length > 1) {
        for (const item of group.slice(1)) item.el.classList.add('is-clustered');
        lead.el.classList.add('is-cluster');
        lead.el.dataset.count = group.length;
        const lo = group[0].value;
        const hi = group[group.length - 1].value;
        if (label) {
          if (lead.el._label === undefined) lead.el._label = label.innerHTML;
          label.innerHTML = `<b>${axisMoney(lo)}${hi > lo ? ' to ' + axisMoney(hi) : ''}</b>${group.length} retailers`;
        }
      } else if (label && lead.el._label === undefined) {
        lead.el._label = label.innerHTML;
      }

      const half = (label ? label.offsetWidth : 60) / 2 + LABEL_PAD;
      // No lane has room. Hide the label rather than stacking it on the one already
      // in the bottom lane: an unreadable overlap is worse than an absent label, and
      // the dot still carries the figure in its title. The block comment above
      // promises labels never cover each other; this is what keeps that true.
      const lane = laneEnds.findIndex(end => lead.centre - half > end);
      if (lane === -1) {
        if (label) label.style.visibility = 'hidden';
        continue;
      }
      if (label) label.style.visibility = '';
      laneEnds[lane] = lead.centre + half;
      lead.el.style.setProperty('--leader', lane * LANE_HEIGHT + 'px');
      if (label) label.style.marginTop = (5 + lane * LANE_HEIGHT) + 'px';
    }
  }

  function draw() {
    for (const el of points) place(el, Number(el.dataset.value));
    for (const el of thresholds) place(el, Number(el.dataset.value));

    for (const band of bands) {
      const from = project(band._from);
      const to = project(band._to);
      const left = Math.max(from, -5);
      const right = Math.min(to, 105);
      const visible = right > left;
      band.style.visibility = visible ? '' : 'hidden';
      band.style.left = left + '%';
      band.style.width = Math.max(right - left, 0) + '%';
      // Name the band only when there is room for the words.
      const px = ((right - left) / 100) * (plot.clientWidth || 1);
      band.classList.toggle('is-roomy', px > 78);
    }

    clusterAndLabel();
    layoutThresholdLabels();
    if (readout) {
      readout.textContent = view.lo <= full.lo && view.hi >= full.hi
        ? `${axisMoney(full.lo)} to ${axisMoney(full.hi)}, everything`
        : `${axisMoney(view.lo)} to ${axisMoney(view.hi)}`;
    }
  }

  function clamp(next) {
    let { lo, hi } = next;
    if (hi - lo < AXIS_MIN_SPAN) {
      const mid = (lo + hi) / 2;
      lo = mid - AXIS_MIN_SPAN / 2;
      hi = mid + AXIS_MIN_SPAN / 2;
    }
    // Allow a little overscroll so an edge dot is not pinned to the frame, but never
    // let the window wander off the data entirely.
    const pad = (full.hi - full.lo) * 0.25;
    if (lo < full.lo - pad) { hi += (full.lo - pad) - lo; lo = full.lo - pad; }
    if (hi > full.hi + pad) { lo -= hi - (full.hi + pad); hi = full.hi + pad; }
    view = { lo, hi };
  }

  function zoomAt(factor, anchorRatio) {
    const width = view.hi - view.lo;
    const anchor = view.lo + width * anchorRatio;
    const next = width * factor;
    clamp({ lo: anchor - next * anchorRatio, hi: anchor + next * (1 - anchorRatio) });
    draw();
  }

  // Zoom on ctrl/cmd + wheel only. Swallowing every wheel event made the axis a
  // scroll trap: with the pointer anywhere over a 150px plot the page stopped
  // moving and the axis zoomed instead, with no modifier to escape it and no way
  // to scroll past. With several products stacked that is a wall, not a quirk.
  // A plain wheel now scrolls the page, which is what a wheel is for.
  plot.addEventListener('wheel', event => {
    if (!event.ctrlKey && !event.metaKey) return;   // let the page have it
    event.preventDefault();
    const rect = plot.getBoundingClientRect();
    const ratio = rect.width ? (event.clientX - rect.left) / rect.width : 0.5;
    zoomAt(event.deltaY > 0 ? 1.18 : 0.85, Math.min(Math.max(ratio, 0), 1));
  }, { passive: false });

  let dragging = null;
  plot.addEventListener('pointerdown', event => {
    dragging = { x: event.clientX, lo: view.lo, hi: view.hi };
    plot.setPointerCapture(event.pointerId);
  });
  plot.addEventListener('pointermove', event => {
    if (!dragging) return;
    const rect = plot.getBoundingClientRect();
    if (!rect.width) return;
    const moved = ((event.clientX - dragging.x) / rect.width) * (dragging.hi - dragging.lo);
    clamp({ lo: dragging.lo - moved, hi: dragging.hi - moved });
    draw();
  });
  const endDrag = () => { dragging = null; };
  plot.addEventListener('pointerup', endDrag);
  plot.addEventListener('pointercancel', endDrag);

  plot.addEventListener('keydown', event => {
    const step = (view.hi - view.lo) * 0.15;
    if (event.key === 'ArrowRight') { clamp({ lo: view.lo + step, hi: view.hi + step }); }
    else if (event.key === 'ArrowLeft') { clamp({ lo: view.lo - step, hi: view.hi - step }); }
    else if (event.key === '+' || event.key === '=') { zoomAt(0.8, 0.5); return; }
    else if (event.key === '-') { zoomAt(1.25, 0.5); return; }
    else if (event.key === '0') { view = { ...full }; }
    else return;
    event.preventDefault();
    draw();
  });

  if (controls) {
    controls.addEventListener('click', event => {
      const button = event.target.closest('[data-axis-zoom]');
      if (!button) return;
      const mode = button.dataset.axisZoom;
      if (mode === 'in') zoomAt(0.7, 0.5);
      else if (mode === 'out') zoomAt(1.4, 0.5);
      else if (mode === 'fit') { view = { ...full }; draw(); }
      else if (mode === 'targets') {
        // The three targets plus a little air. This is the view that answers "how far
        // off are we", which is the question the product exists to answer.
        const values = thresholds.map(el => Number(el.dataset.value)).filter(isFinite);
        if (!values.length) return;
        const lo = Math.min(...values);
        const hi = Math.max(...values);
        const pad = Math.max((hi - lo) * 0.35, 40);
        clamp({ lo: lo - pad, hi: hi + pad });
        draw();
      }
    });
  }

  // Tone each dot by the territory it actually lands in, so the colour on the axis and
  // the colour in the list below come from the same fact rather than being set twice.
  const toneBands = bands
    .filter(b => b.classList.contains('tone-act') || b.classList.contains('tone-close'))
    .map(b => ({ to: b._to, tone: b.classList.contains('tone-act') ? 'at-act' : 'at-close' }))
    .sort((a, b) => a.to - b.to);
  for (const el of points) {
    const value = Number(el.dataset.value);
    const hit = toneBands.find(b => value <= b.to);
    if (hit) el.classList.add(hit.tone);
  }

  const observer = new ResizeObserver(() => draw());
  observer.observe(plot);
  draw();
}

for (const root of document.querySelectorAll('[data-axis]')) setupAxis(root);

/* ------------------------------------------------------------------- watch groups */

/* Hovering a candidate row highlights every axis point for that product, and vice
 * versa - only present on the group page, so every lookup is guarded rather than
 * assumed. Several of a candidate's own listings routinely cluster into one dot (see
 * .is-clustered): the callout is what makes hovering answer "which seller, at what
 * price" without needing every one of them to have its own permanently visible dot. */
(function wireGroupHoverLinks() {
  const rows = document.querySelectorAll('[data-candidate-product]');
  const points = document.querySelectorAll('[data-point][data-product-id]');
  if (!rows.length || !points.length) return;

  const plot = document.querySelector('[data-axis-plot]');

  const pointsFor = id => [...points].filter(p => p.dataset.productId === id);
  const rowFor = id => document.querySelector(`[data-candidate-product="${id}"]`);

  let callout = null;
  function ensureCallout() {
    if (!callout) {
      callout = document.createElement('div');
      callout.className = 'axis-callout';
      document.body.appendChild(callout);
    }
    return callout;
  }

  function showCandidate(id) {
    const pts = pointsFor(id);
    if (!pts.length) return;
    for (const p of pts) p.classList.add('is-linked-hover', 'is-hover-revealed');
    rowFor(id)?.classList.add('is-linked-hover');

    // Anchored to the cheapest listing, same convention the built-in clustering
    // uses for which point leads a group.
    const sorted = [...pts].sort((a, b) => Number(a.dataset.value) - Number(b.dataset.value));
    const anchor = sorted[0];
    const box = ensureCallout();
    box.replaceChildren(...sorted.map(p => {
      const line = document.createElement('span');
      const price = document.createElement('b');
      price.textContent = axisMoney(Number(p.dataset.value));
      line.append(price, ' ' + (p.dataset.retailer || ''));
      return line;
    }));

    const rect = anchor.getBoundingClientRect();
    box.style.left = (rect.left + rect.width / 2) + 'px';
    box.style.top = (rect.bottom + 10) + 'px';
    box.classList.add('is-visible');
  }

  function hideCandidate(id) {
    for (const p of pointsFor(id)) p.classList.remove('is-linked-hover', 'is-hover-revealed');
    rowFor(id)?.classList.remove('is-linked-hover');
    callout?.classList.remove('is-visible');
  }

  for (const row of rows) {
    const id = row.dataset.candidateProduct;
    row.addEventListener('mouseenter', () => showCandidate(id));
    row.addEventListener('mouseleave', () => hideCandidate(id));
  }
  for (const point of points) {
    const id = point.dataset.productId;
    point.addEventListener('mouseenter', () => showCandidate(id));
    point.addEventListener('mouseleave', () => hideCandidate(id));
  }
  // The callout is viewport-fixed, so panning/zooming the plot while it is open
  // would leave it pointing at stale coordinates. Closing it on any redraw is
  // simpler than re-tracking a moving anchor for a transient hover state.
  if (plot) new ResizeObserver(() => callout?.classList.remove('is-visible')).observe(plot);
})();

/* Product page: hovering a retailer row highlights that row's own axis point, using
 * the --close fallback colour .axis-point.is-linked-hover already carries for this
 * exact case - one product's own points, not several models to tell apart, so none
 * of the group page's candidate-color/callout machinery is needed here. Only present
 * on the product page, so guarded the same way wireGroupHoverLinks guards itself.
 * A clustered point's label only comes back into view when it was hidden by
 * clustering in the first place - an already-visible point's own label is left
 * alone rather than hidden with nothing to replace it. */
(function wireListingHoverLinks() {
  const rows = document.querySelectorAll('[data-listing-row]');
  const points = document.querySelectorAll('[data-point][data-listing-id]');
  if (!rows.length || !points.length) return;

  const pointFor = id => document.querySelector(`[data-point][data-listing-id="${id}"]`);
  const rowFor = id => document.querySelector(`[data-listing-row="${id}"]`);

  function show(id) {
    const point = pointFor(id);
    if (!point) return;
    point.classList.add('is-linked-hover');
    if (point.classList.contains('is-clustered')) point.classList.add('is-hover-revealed');
    rowFor(id)?.classList.add('is-linked-hover');
  }

  function hide(id) {
    pointFor(id)?.classList.remove('is-linked-hover', 'is-hover-revealed');
    rowFor(id)?.classList.remove('is-linked-hover');
  }

  for (const row of rows) {
    const id = row.dataset.listingRow;
    row.addEventListener('mouseenter', () => show(id));
    row.addEventListener('mouseleave', () => hide(id));
  }
  for (const point of points) {
    const id = point.dataset.listingId;
    point.addEventListener('mouseenter', () => show(id));
    point.addEventListener('mouseleave', () => hide(id));
  }
})();

wire('btn-join-group', async () => {
  const select = document.getElementById('jg-existing');
  const none = document.createElement('option');
  none.value = '';
  none.textContent = '- none -';
  select.replaceChildren(none);
  try {
    const groups = await api('/api/groups');
    for (const g of groups) {
      const opt = document.createElement('option');
      opt.value = g.id;
      opt.textContent = `${g.name} (${g.members.length})`;
      select.appendChild(opt);
    }
  } catch (err) { toast(`Could not load groups: ${err.message}`, 'bad'); }
  document.getElementById('join-group-dialog').showModal();
});
wire('jg-save', async () => {
  const productId = Number(location.pathname.split('/').pop());
  const existing = document.getElementById('jg-existing').value;
  const newName = document.getElementById('jg-new-name').value.trim();
  if (!existing && !newName) { toast('Pick a group or name a new one.', 'bad'); return; }
  const body = existing ? { group_id: Number(existing) } : { name: newName };
  try {
    await api(`/api/products/${productId}/group`, { method: 'POST', body });
    toast('Added to group.', 'good');
    location.reload();
  } catch (err) { toast(`Could not join group: ${err.message}`, 'bad'); }
});
wire('btn-leave-group', async event => {
  if (!confirm('Leave this group? It stops being compared with the other candidates.')) return;
  const productId = Number(event.currentTarget.dataset.product);
  try {
    await api(`/api/products/${productId}/group`, { method: 'DELETE' });
    toast('Left the group.', 'good');
    location.reload();
  } catch (err) { toast(`Could not leave group: ${err.message}`, 'bad'); }
});

wire('btn-edit-group', () => {
  document.getElementById('edit-group-dialog').showModal();
});
wire('eg-save', async () => {
  const groupId = Number(location.pathname.split('/').pop());
  try {
    await api(`/api/groups/${groupId}`, { method: 'PATCH', body: {
      name: document.getElementById('eg-name').value.trim(),
      notes: document.getElementById('eg-notes').value.trim() || null,
    }});
    toast('Group saved.', 'good');
    location.reload();
  } catch (err) { toast(`Could not save group: ${err.message}`, 'bad'); }
});

wire('btn-delete-group', async event => {
  if (!confirm('Delete this group? Its candidates are not deleted - they just stop being compared together and return to the board on their own.')) return;
  const groupId = Number(event.currentTarget.dataset.group);
  try {
    await api(`/api/groups/${groupId}`, { method: 'DELETE' });
    toast('Group deleted.', 'good');
    location.href = '/';
  } catch (err) { toast(`Could not delete group: ${err.message}`, 'bad'); }
});

wire('btn-add-candidate', async () => {
  const select = document.getElementById('cd-existing');
  const none = document.createElement('option');
  none.value = '';
  none.textContent = '- pick one -';
  select.replaceChildren(none);
  try {
    const products = await api('/api/products?status=ALL');
    for (const p of products.filter(p => !p.group_id)) {
      const opt = document.createElement('option');
      opt.value = p.id;
      opt.textContent = `${p.name} (${p.model})`;
      select.appendChild(opt);
    }
  } catch (err) { toast(`Could not load products: ${err.message}`, 'bad'); }
  document.getElementById('candidate-dialog').showModal();
});
wire('cd-save', async () => {
  const groupId = Number(location.pathname.split('/').pop());
  const productId = Number(document.getElementById('cd-existing').value);
  if (!productId) { toast('Pick a product first.', 'bad'); return; }
  try {
    await api(`/api/products/${productId}/group`, { method: 'POST', body: { group_id: groupId } });
    toast('Added to the group.', 'good');
    location.reload();
  } catch (err) { toast(`Could not add candidate: ${err.message}`, 'bad'); }
});

wire('household-settings-form', async event => {
  event.preventDefault();
  const postcode = document.getElementById('household-postcode').value.trim();
  const pickup_radius_km = Number(document.getElementById('household-pickup-radius').value);
  try {
    await api('/api/household-settings', { method: 'PATCH', body: {
      postcode: postcode || null, pickup_radius_km
    }});
    toast('Local area saved.', 'good');
    location.reload();
  } catch (err) { toast(`Could not save local area: ${err.message}`, 'bad'); }
});
