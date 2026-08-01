/* TV Tracker - a small hand-rolled SPA, no build step. */

// Bump this whenever app.js changes. The server reads the same constant out of
// the file it would serve, so a mismatch means the browser is running a cached
// copy of an older build — the one failure that makes a deploy look broken when
// it is not.
const APP_VERSION = '2026.08.02-9';

const main = document.getElementById('main');
const titleEl = document.getElementById('view-title');
const toastEl = document.getElementById('toast');
const badgeEl = document.getElementById('new-badge');

const state = {
  view: 'home',
  showId: null,
  openSeasons: new Set(),
  searchResults: null,
  searchQuery: '',
  returnTo: 'home',
  calendarJump: true,
  newJump: true,
  showFilter: { filter: 'active', sort: 'name', q: '' },
  selecting: false,
  selected: new Set(),
  expanded: new Set(),
};

const TITLES = {
  home: 'Up Next',
  priority: 'Priority Watch',
  new: 'New Episodes',
  calendar: 'Calendar',
  shows: 'Your Shows',
  search: 'Add a Show',
  settings: 'More',
  show: '',
};

/* ------------------------------------------------------------------ utils */

async function api(path, options = {}) {
  const response = await fetch(`/api${path}`, {
    headers: options.body instanceof FormData ? {} : { 'Content-Type': 'application/json' },
    ...options,
  });
  if (response.status === 401) {
    showLogin();
    throw new Error('Authentication required');
  }
  if (!response.ok) {
    let detail = `Request failed (${response.status})`;
    try {
      detail = (await response.json()).detail || detail;
    } catch (_) { /* keep the generic message */ }
    throw new Error(detail);
  }
  return response.status === 204 ? null : response.json();
}

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

let toastTimer;
function toast(message) {
  toastEl.textContent = message;
  toastEl.classList.remove('hidden');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toastEl.classList.add('hidden'), 2600);
}

// A text field with an ✕ to empty it. iOS does not offer one reliably, and
// backspacing through a long query is miserable on a phone.
function clearableInput({ id, placeholder, value = '', type = 'search' }) {
  return `
    <div class="clearable">
      <input type="${type}" id="${esc(id)}" placeholder="${esc(placeholder)}"
             value="${esc(value)}" autocomplete="off">
      <button type="button" class="clear-btn ${value ? '' : 'hidden'}"
              data-clearfor="${esc(id)}" aria-label="Clear">&times;</button>
    </div>`;
}

function wireClearable(id, onClear) {
  const input = document.getElementById(id);
  const button = document.querySelector(`[data-clearfor="${id}"]`);
  if (!input || !button) return;
  input.addEventListener('input', () => button.classList.toggle('hidden', !input.value));
  button.addEventListener('click', () => {
    input.value = '';
    button.classList.add('hidden');
    input.focus();
    if (onClear) onClear();
  });
}

function poster(url, className, alt) {
  if (!url) return `<div class="${className}" aria-hidden="true"></div>`;
  return `<img class="${className}" loading="lazy" src="${esc(url)}" alt="${esc(alt || '')}">`;
}

const DAY = 86400000;

function airLabel(stamp) {
  if (!stamp) return 'Date to be announced';
  const when = new Date(stamp);
  const now = new Date();
  const days = Math.round((startOfDay(when) - startOfDay(now)) / DAY);
  if (days === 0) return `Today, ${timeOf(when)}`;
  if (days === 1) return `Tomorrow, ${timeOf(when)}`;
  if (days === -1) return 'Yesterday';
  if (days > 1 && days <= 6) return `${when.toLocaleDateString(undefined, { weekday: 'long' })}, ${timeOf(when)}`;
  if (days < 0 && days >= -6) return `${-days} days ago`;
  if (days < 0) return when.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
  return when.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
}

function localDay(stamp) {
  const date = new Date(stamp);
  const pad = (n) => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

function startOfDay(date) {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
}

function timeOf(date) {
  return date.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
}

function countdown(stamp) {
  if (!stamp) return '';
  const days = Math.round((startOfDay(new Date(stamp)) - startOfDay(new Date())) / DAY);
  if (days <= 0) return '';
  return days === 1 ? 'in 1 day' : `in ${days} days`;
}

function humanMinutes(minutes) {
  if (!minutes) return '0m';
  const days = Math.floor(minutes / 1440);
  const hours = Math.floor((minutes % 1440) / 60);
  if (days) return `${days}d ${hours}h`;
  return hours ? `${hours}h ${minutes % 60}m` : `${minutes}m`;
}

/* ------------------------------------------------------------ navigation */

// The current screen lives in the URL, so a reload comes back to it rather than
// bouncing to Up Next, and the browser's back gesture moves between screens.
function hashFor(view, showId) {
  return view === 'show' ? `#/show/${showId}` : `#/${view}`;
}

function parseHash() {
  const raw = (location.hash || '').replace(/^#\/?/, '');
  if (!raw) return null;
  const [name, id] = raw.split('/');
  if (name === 'show') {
    return Number(id) ? { view: 'show', showId: Number(id) } : null;
  }
  return TITLES[name] === undefined ? null : { view: name, showId: null };
}

// Where you were in each list, so coming back from a show does not dump you at
// the top of a long Up Next again.
const scrollPositions = new Map();

function viewKey(view = state.view, showId = state.showId) {
  return view === 'show' ? `show:${showId}` : view;
}

async function go(view, showId = null, { push = true } = {}) {
  const leaving = viewKey();
  scrollPositions.set(leaving, window.scrollY);

  if (view !== 'shows') {
    state.selecting = false;
    state.selected = new Set();
  }
  // Remember where a show was opened from, so Back returns there.
  if (view === 'show' && state.view !== 'show') state.returnTo = state.view;
  // Opening Calendar from elsewhere starts at today; returning from a show
  // keeps where you were.
  if (view === 'calendar' && state.view !== 'show') state.calendarJump = true;
  if (view === 'new' && state.view !== 'show') state.newJump = true;

  const arriving = viewKey(view, showId);
  // Tapping the tab you are already on jumps to the top, as tab bars do.
  const target = arriving === leaving ? 0 : scrollPositions.get(arriving) ?? 0;

  state.view = view;
  state.showId = showId;
  const hash = hashFor(view, showId);
  if (push && location.hash !== hash) {
    history.pushState({ view, showId }, '', hash);
  }
  titleEl.textContent = TITLES[view] ?? '';
  document.querySelectorAll('.tab').forEach((tab) => {
    tab.classList.toggle('active', tab.dataset.view === view);
  });
  await render(target);
}

// Height of the sticky header, so an anchored scroll does not tuck under it.
const TOPBAR_OFFSET = 58;

// A view may set this while rendering to request a specific scroll position,
// which wins over both the restored position and the caller's request.
let pendingScroll = null;

async function render(scrollTarget = null) {
  // A re-render in place (marking an episode watched) should not move the page,
  // so hold the current position unless a navigation asked for a specific one.
  const keep = scrollTarget === null ? window.scrollY : scrollTarget;
  pendingScroll = null;

  document.querySelectorAll('.bulkbar').forEach((bar) => bar.remove());
  document.body.classList.remove('selecting');
  main.innerHTML = '<div class="empty">Loading…</div>';
  try {
    const views = {
      home: viewHome,
      priority: viewPriority,
      new: viewNew,
      calendar: viewCalendar,
      shows: viewShows,
      search: viewSearch,
      settings: viewSettings,
      show: viewShowDetail,
    };
    await (views[state.view] || viewHome)();
  } catch (error) {
    main.innerHTML = `<div class="empty"><strong>Something went wrong</strong>${esc(error.message)}</div>`;
  }
  // After layout, or the page may not yet be tall enough to scroll that far.
  requestAnimationFrame(() => {
    window.scrollTo(0, pendingScroll !== null ? pendingScroll : keep);
    pendingScroll = null;
  });
}

/* ------------------------------------------------------------- home view */

function showCard(card, options = {}) {
  const { show, progress, next, last_watched: last, status } = card;
  const lines = [];

  if (next) {
    const when = status === 'ready'
      ? `Aired ${airLabel(next.airstamp)}`
      : `${airLabel(next.airstamp)} ${countdown(next.airstamp)}`.trim();
    lines.push(`<div class="card-line"><strong>${esc(next.code)}</strong> ${esc(next.name || '')}</div>`);
    lines.push(`<div class="card-line">${esc(when)}</div>`);
  } else if (status === 'complete') {
    lines.push('<div class="card-line"><span class="pill good">Finished</span></div>');
  } else {
    lines.push('<div class="card-line"><span class="pill">All caught up</span></div>');
  }

  if (last && options.showLast !== false) {
    lines.push(`<div class="card-line muted">Last watched ${esc(last.code)} · ${esc(airLabel(last.watched_at))}</div>`);
  }

  // Explains a next-up that looks too early: you watched this show out of order.
  if (card.gaps) {
    lines.push(`<div class="card-line muted">${card.gaps} earlier episode${card.gaps === 1 ? '' : 's'} never marked watched</div>`);
  }

  const remaining = progress.remaining > 1
    ? `<span class="pill">${progress.remaining} to watch</span>`
    : '';
  const star = card.favorite ? '<span class="star" title="Favorite">&#9733;</span> ' : '';
  const pin = card.priority && !options.inPriority
    ? '<span class="pill prio">Priority</span> '
    : '';

  if (options.selectable) {
    const on = state.selected.has(show.id);
    return `
      <div class="card selectable ${on ? 'picked' : ''}" data-pick="${show.id}">
        <button class="check ${on ? 'on' : ''}" tabindex="-1">&#10003;</button>
        ${poster(show.image, 'poster', show.name)}
        <div class="card-body">
          <div class="card-title">${star}${esc(show.name)} ${pin}</div>
          <div class="card-line muted">${progress.watched}/${progress.total} watched${card.archived ? ' · archived' : ''}</div>
          <div class="progress"><i style="width:${progress.percent}%"></i></div>
        </div>
      </div>`;
  }

  return `
    <div class="card" data-show="${show.id}">
      ${poster(show.image, 'poster', show.name)}
      <div class="card-body">
        <div class="card-title">${star}${esc(show.name)} ${pin}${remaining}</div>
        ${lines.join('')}
        <div class="progress"><i style="width:${progress.percent}%"></i></div>
        <div class="card-actions">
          ${next && status === 'ready'
            ? `<button class="primary" data-watch="${next.id}">Watched ${esc(next.code)}</button>`
            : ''}
          <button class="secondary" data-open="${show.id}">Details</button>
        </div>
      </div>
    </div>`;
}

// Long sections push everything below them off the screen, so each one shows a
// first handful with the rest a tap away.
const SECTION_LIMIT = 10;

function section(title, cards, note = '', options = {}) {
  if (!cards.length) return '';
  // Namespaced by view, since the same heading appears on more than one screen.
  const key = `${state.view}:${title.toLowerCase().replace(/[^a-z]+/g, '-')}`;
  const expanded = state.expanded.has(key);
  const collapsible = cards.length > SECTION_LIMIT;
  const shown = expanded || !collapsible ? cards : cards.slice(0, SECTION_LIMIT);

  return `
    <section class="section">
      <div class="section-head"><h2>${esc(title)}</h2><span class="muted">${esc(note)}</span></div>
      ${shown.map((card) => showCard(card, options)).join('')}
      ${collapsible ? `
        <button class="secondary expander" data-expand="${key}">
          ${expanded
            ? 'Show fewer'
            : `Show all ${cards.length} &#9662;`}
        </button>` : ''}
    </section>`;
}

async function viewHome() {
  const data = await api('/home');
  updateBadge(data.new_since_last_visit);
  updatePriorityBadge(data.counts.priority_waiting);

  const total = Object.values(data.counts).reduce((sum, n) => sum + n, 0) - data.counts.episodes_ready;
  if (!total) {
    main.innerHTML = `
      <div class="empty">
        <strong>Your library is empty</strong>
        Search for a show to start tracking, or import your TV Time export from the More tab.
        <div style="margin-top:16px"><button class="primary" data-go="search">Find a show</button></div>
      </div>`;
    return;
  }

  main.innerHTML = [
    data.storage_warning ? `
      <div class="alarm">
        <strong>Your data is not being saved permanently</strong>
        ${esc(data.storage_warning)}
      </div>` : '',
    section('Ready to watch', data.ready, data.counts.episodes_ready ? `${data.counts.episodes_ready} episodes` : ''),
    section('Coming up', data.scheduled),
    section('Not started yet', data.not_started, `${data.counts.not_started} shows`),
    section('Waiting for more', data.waiting),
    section('Finished', data.complete),
  ].join('') || '<div class="empty">Nothing to show yet.</div>';
}

/* --------------------------------------------------------- priority view */

// Most useful first: things you are part-way through with an episode waiting,
// then unstarted ones that have aired, then everything still to come.
function priorityRank(card) {
  if (card.status === 'ready') return card.started ? 0 : 1;
  if (card.status === 'scheduled') return 2;
  if (card.status === 'complete') return 4;
  return 3;
}

async function viewPriority() {
  const cards = await api('/shows?filter=priority&sort=name');
  updatePriorityBadge(cards.filter((card) => card.status === 'ready').length);

  if (!cards.length) {
    main.innerHTML = `
      <div class="empty">
        <strong>Nothing pinned yet</strong>
        Open a show and tap ○ Priority to add it here. This is your shortlist of
        what to get to next — it does not change your Up Next.
      </div>`;
    return;
  }

  cards.sort((a, b) => (
    priorityRank(a) - priorityRank(b)
    || ((b.last_watched || {}).watched_at || '').localeCompare((a.last_watched || {}).watched_at || '')
  ));

  const waiting = cards.filter((card) => card.status === 'ready');
  const later = cards.filter((card) => card.status !== 'ready');

  main.innerHTML = [
    section('Ready to watch', waiting, waiting.length ? `${waiting.length} of ${cards.length}` : '', { inPriority: true }),
    section('Nothing waiting yet', later, '', { inPriority: true }),
  ].join('');
}

function updatePriorityBadge(count) {
  const badge = document.getElementById('priority-badge');
  if (!badge) return;
  if (count > 0) {
    badge.textContent = count > 99 ? '99+' : count;
    badge.classList.remove('hidden');
  } else {
    badge.classList.add('hidden');
  }
}

/* -------------------------------------------------------------- new view */

// The defaults the server last told us about. The toggle handler needs them:
// with nothing stored yet, the effective selection *is* the defaults, and
// starting from an empty set would wipe every service on the first tap.
let premiereDefaults = [];

function selectedServices(defaults) {
  const raw = localStorage.getItem('tv.services');
  if (raw === null) return new Set(defaults);
  try {
    return new Set(JSON.parse(raw));
  } catch (_) {
    return new Set(defaults);
  }
}

async function viewNew() {
  const [data, premieres] = await Promise.all([
    api('/new'),
    api('/premieres?back=14&ahead=90'),
  ]);
  await api('/seen', { method: 'POST' });
  updateBadge(0);

  premiereDefaults = premieres.defaults;
  const services = selectedServices(premiereDefaults);
  const picked = premieres.premieres.filter((p) => services.has(p.channel));
  // The API sorts newest-first so that trimming drops the oldest first. Flip it
  // back to build one rising timeline: oldest above, furthest ahead at the
  // bottom, today in the middle.
  const past = picked.filter((p) => p.aired).reverse();
  const ahead = picked.filter((p) => !p.aired).reverse();

  main.innerHTML = `
    ${data.episodes.length ? `
      <div class="section-head"><h2>From shows you follow</h2>
        <span class="muted">${data.episodes.length} unwatched</span></div>
      <div class="list">${data.episodes.map(newRow).join('')}</div>` : `
      <div class="section-head"><h2>From shows you follow</h2></div>
      <p class="muted" style="margin:0 2px 20px">Nothing new since your last visit.</p>`}

    <div class="section-head" style="margin-top:26px">
      <h2>Premieres</h2>
      <button class="ghost" id="services-toggle">Services (${services.size})</button>
    </div>
    <div id="services-panel" class="hidden"></div>

    ${picked.length ? `
      ${premiereDays(past)}
      <div class="today-line" id="premieres-today"><span>Today</span></div>
      ${premiereDays(ahead)}
    ` : premiereEmptyState(premieres)}

    <p class="muted" style="margin:16px 2px 0;font-size:12.5px">
      New shows and returning seasons, English only, from the last two weeks and
      the next three months. Already-followed shows are left out.
      ${premieres.swept_at
        ? `Last checked ${esc(airLabel(premieres.swept_at))}.`
        : 'Not checked yet.'}
      <button class="ghost" id="premieres-refresh" style="padding:2px 6px">Check now</button>
    </p>`;

  document.getElementById('premieres-refresh').addEventListener('click', async (event) => {
    event.target.disabled = true;
    event.target.textContent = 'Checking…';
    try {
      const report = await api('/premieres/refresh', { method: 'POST' });
      toast(`Found ${report.found} premieres`);
      await render();
    } catch (error) {
      toast(error.message);
      event.target.disabled = false;
      event.target.textContent = 'Check now';
    }
  });

  document.getElementById('services-toggle').addEventListener('click', () => {
    const panel = document.getElementById('services-panel');
    if (!panel.innerHTML) panel.innerHTML = servicesPanel(premieres, services);
    panel.classList.toggle('hidden');
  });

  // Open at today, so the past sits above and scrolling up walks backwards.
  // Unwatched episodes from shows you follow win, though: they sit at the top
  // and they are the reason the tab has a badge.
  if (state.newJump) {
    state.newJump = false;
    const anchor = document.getElementById('premieres-today');
    if (anchor && !data.episodes.length && past.length) {
      pendingScroll = Math.max(
        anchor.getBoundingClientRect().top + window.scrollY - TOPBAR_OFFSET, 0
      );
    }
  }
}

// One heading per day, so the timeline reads as dates rather than two piles.
// Grouped by the local day rather than the stamp's UTC date, or a late-evening
// airing would sit under tomorrow's heading with today's time beside it.
function premiereDays(items) {
  const groups = new Map();
  items.forEach((item) => {
    const key = localDay(item.airstamp);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(item);
  });

  return [...groups.entries()].map(([day, rows]) => `
    <div class="date-head">${esc(dayHeading(day))}</div>
    <div class="list">${rows.map(premiereRow).join('')}</div>`).join('');
}

// Never claim there is nothing out there when the truth is we have not looked.
// The first sweep after an update takes a couple of minutes, because it shares
// a rate limit with the episode refresh.
function premiereEmptyState(premieres) {
  if (!premieres.swept_at) {
    return `
      <p class="muted" style="margin:0 2px">
        Still checking for premieres. The first sweep after an update takes a
        minute or two — pull down to refresh, or use Check now below.
      </p>`;
  }
  if (!premieres.stored) {
    return `
      <p class="muted" style="margin:0 2px">
        The last check found nothing at all, which usually means TVmaze was
        unreachable. Try Check now below.
      </p>`;
  }
  if (!premieres.premieres.length) {
    return `
      <p class="muted" style="margin:0 2px">
        ${premieres.stored} premieres are stored, but every one in this window is
        from a show you already follow.
      </p>`;
  }
  return `
    <p class="muted" style="margin:0 2px">
      None on your selected services.
      ${premieres.premieres.length} found on others — widen your services above.
    </p>`;
}

function servicesPanel(premieres, services) {
  const counts = Object.fromEntries(premieres.channels);
  const listed = new Set();
  const group = (title, names) => {
    const rows = names.filter((name) => {
      if (listed.has(name)) return false;
      listed.add(name);
      return counts[name] || services.has(name);
    });
    if (!rows.length) return '';
    return `
      <p class="muted" style="margin:12px 2px 6px;font-size:12px;text-transform:uppercase;letter-spacing:.08em">${esc(title)}</p>
      ${rows.map((name) => `
        <label class="checkline">
          <input type="checkbox" data-service="${esc(name)}" ${services.has(name) ? 'checked' : ''}>
          ${esc(name)} ${counts[name] ? `<span class="muted">· ${counts[name]}</span>` : ''}
        </label>`).join('')}`;
  };

  const groups = Object.entries(premieres.groups).map(([title, names]) => group(title, names)).join('');
  const other = Object.keys(counts).filter((name) => !listed.has(name)).sort();

  return `
    <div class="report" style="margin-bottom:14px">
      ${groups}
      ${other.length ? group('Also showing premieres', other) : ''}
      <div class="card-actions" style="margin-top:12px">
        <button class="secondary" id="services-reset">Reset to defaults</button>
      </div>
    </div>`;
}

function premiereRow(item) {
  const badge = item.kind === 'series'
    ? '<span class="pill good">New show</span>'
    : `<span class="pill">Season ${item.season}</span>`;
  const meta = [item.channel, (item.genres || []).slice(0, 2).join(', ')].filter(Boolean).join(' · ');
  return `
    <div class="row" data-open="${item.show_id}">
      ${poster(item.image, 'thumb', item.show_name)}
      <div class="row-body">
        <div class="row-title">${esc(item.show_name)} ${badge}</div>
        <div class="row-sub">${esc(meta)}</div>
        <div class="row-sub">${esc(timeOf(new Date(item.airstamp)))}</div>
      </div>
      <button class="secondary" data-add="${item.show_id}">Add</button>
    </div>`;
}

function newRow(episode) {
  return `
    <div class="row episode-row" data-show="${episode.show_id}">
      ${poster(episode.show_image, 'thumb', episode.show_name)}
      <div class="row-body" data-open="${episode.show_id}">
        <div class="row-title">${esc(episode.show_name)}</div>
        <div class="row-sub">${esc(episode.code)} · ${esc(episode.name || '')}</div>
        <div class="row-sub">${esc(airLabel(episode.airstamp))}</div>
      </div>
      <button class="check" data-watch="${episode.id}" title="Mark watched">&#10003;</button>
    </div>`;
}

/* --------------------------------------------------------- calendar view */

const LOOKBACK_OPTIONS = [
  { days: 0, label: 'Upcoming only' },
  { days: 30, label: 'Back 1 month' },
  { days: 90, label: 'Back 3 months' },
  { days: 180, label: 'Back 6 months' },
];

function lookbackDays() {
  // Guard the raw value: Number(null) is 0, which is itself a valid choice
  // ("Upcoming only"), so testing the number alone loses the default.
  const raw = localStorage.getItem('tv.lookback');
  if (raw === null) return 90;
  const saved = Number(raw);
  return LOOKBACK_OPTIONS.some((option) => option.days === saved) ? saved : 90;
}

async function viewCalendar() {
  const back = lookbackDays();
  const data = await api(`/calendar?back=${back}&forward=35`);

  const picker = `
    <div class="field">
      <select id="lookback">
        ${LOOKBACK_OPTIONS.map((o) => `
          <option value="${o.days}" ${o.days === back ? 'selected' : ''}>${esc(o.label)}</option>`).join('')}
      </select>
    </div>`;

  if (!data.upcoming.length && !data.recent.length) {
    main.innerHTML = picker + `
      <div class="empty">
        <strong>No airings in this window</strong>
        Nothing you follow aired recently or has a confirmed date coming up.
      </div>`;
    document.getElementById('lookback').addEventListener('change', onLookbackChange);
    return;
  }

  // One continuous timeline, oldest at the top. The API returns the past
  // newest-first so that capping trims the oldest, so flip it back here.
  const past = [...data.recent].reverse();

  main.innerHTML = picker
    + (data.truncated ? `<p class="muted" style="margin:0 2px 12px">
         Showing the most recent ${data.recent.length}. Narrow the range for fewer.
       </p>` : '')
    + (past.length ? `<p class="muted" style="margin:0 2px 12px">
         ${data.unwatched_recent} unwatched in this window. Scroll up for older.
       </p>` : '')
    + groupByDay(past, true)
    + `<div class="today-line" id="calendar-today"><span>Today</span></div>`
    + groupByDay(data.upcoming, false);

  document.getElementById('lookback').addEventListener('change', onLookbackChange);

  // Open at today, so the past sits above and scrolling up walks backwards.
  // Skipped when returning from a show, where the saved position should win.
  if (state.calendarJump) {
    state.calendarJump = false;
    const anchor = document.getElementById('calendar-today');
    if (anchor) {
      pendingScroll = Math.max(
        anchor.getBoundingClientRect().top + window.scrollY - TOPBAR_OFFSET, 0
      );
    }
  }
}

function onLookbackChange(event) {
  localStorage.setItem('tv.lookback', event.target.value);
  state.calendarJump = true;
  render(0);
}

function groupByDay(episodes, past) {
  const groups = new Map();
  episodes.forEach((episode) => {
    const key = (episode.airstamp || '').slice(0, 10);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(episode);
  });

  return [...groups.entries()].map(([day, items]) => `
    <div class="date-head">${esc(dayHeading(day))}</div>
    <div class="list">
      ${items.map((episode) => `
        <div class="row ${past && episode.watched ? 'seen' : ''}" data-open="${episode.show_id}">
          ${poster(episode.show_image, 'thumb', episode.show_name)}
          <div class="row-body">
            <div class="row-title">${esc(episode.show_name)}</div>
            <div class="row-sub">${esc(episode.code)} · ${esc(episode.name || 'TBA')}</div>
            <div class="row-sub">${esc(timeOf(new Date(episode.airstamp)))}${episode.network ? ' · ' + esc(episode.network) : ''}</div>
          </div>
          ${past
            ? (episode.watched
                ? '<span class="pill good">Watched</span>'
                : `<button class="check" data-watch="${episode.id}" title="Mark watched">&#10003;</button>`)
            : ''}
        </div>`).join('')}
    </div>`).join('');
}

function dayHeading(day) {
  const date = new Date(`${day}T12:00:00`);
  const days = Math.round((startOfDay(date) - startOfDay(new Date())) / DAY);
  const label = date.toLocaleDateString(undefined, { weekday: 'long', month: 'short', day: 'numeric' });
  if (days === 0) return `Today · ${label}`;
  if (days === 1) return `Tomorrow · ${label}`;
  return label;
}

/* ------------------------------------------------------------ shows view */

const SHOW_FILTERS = {
  active: 'Following',
  favorites: 'Favorites',
  priority: 'Priority watch',
  unstarted: 'Never started',
  archived: 'Archived',
};

const EMPTY_MESSAGES = {
  favorites: 'No favorites yet. Open a show and tap ☆ Favorite.',
  priority: 'Nothing marked priority. Open a show and tap ○ Priority to pin it to the top of Up Next.',
  unstarted: 'You have watched something from every show you follow.',
  archived: 'Nothing archived.',
  active: 'No shows here yet.',
};

async function viewShows() {
  const { filter, sort, q } = state.showFilter;
  const params = new URLSearchParams({ filter, sort, q });
  const cards = await api(`/shows?${params}`);
  const visible = cards.map((card) => card.show.id);

  // Selection only ever refers to what is on screen.
  state.selected = new Set([...state.selected].filter((id) => visible.includes(id)));

  main.innerHTML = `
    <div class="field">
      ${clearableInput({ id: 'show-search', placeholder: 'Filter your shows', value: q })}
    </div>
    <div class="field" style="display:flex;gap:8px">
      <select id="show-which">
        ${Object.entries(SHOW_FILTERS).map(([value, label]) => `
          <option value="${value}" ${filter === value ? 'selected' : ''}>${esc(label)}</option>`).join('')}
      </select>
      <select id="show-sort">
        <option value="name" ${sort === 'name' ? 'selected' : ''}>A–Z</option>
        <option value="recent" ${sort === 'recent' ? 'selected' : ''}>Recently watched</option>
        <option value="remaining" ${sort === 'remaining' ? 'selected' : ''}>Most left to watch</option>
        <option value="progress" ${sort === 'progress' ? 'selected' : ''}>Furthest along</option>
      </select>
    </div>
    ${cards.length ? `
      <div class="list-head">
        <span class="muted">${cards.length} show${cards.length === 1 ? '' : 's'}</span>
        <button class="ghost" id="select-toggle">${state.selecting ? 'Done' : 'Select'}</button>
      </div>` : ''}
    ${cards.length
      ? cards.map((card) => showCard(card, { selectable: state.selecting })).join('')
      : `<div class="empty">${esc(EMPTY_MESSAGES[filter] || 'Nothing here.')}</div>`}`;

  if (state.selecting) renderBulkBar(visible);

  const search = document.getElementById('show-search');
  search.addEventListener('change', () => {
    state.showFilter.q = search.value.trim();
    render();
  });
  wireClearable('show-search', () => {
    state.showFilter.q = '';
    render();
  });
  document.getElementById('show-which').addEventListener('change', (event) => {
    state.showFilter.filter = event.target.value;
    state.selected = new Set();
    render();
  });
  document.getElementById('show-sort').addEventListener('change', (event) => {
    state.showFilter.sort = event.target.value;
    render();
  });
  const selectToggle = document.getElementById('select-toggle');
  if (selectToggle) {
    selectToggle.addEventListener('click', () => {
      state.selecting = !state.selecting;
      state.selected = new Set();
      render();
    });
  }
}

const BULK_ACTIONS = [
  { action: 'archive', label: 'Archive', filters: ['active', 'favorites', 'priority', 'unstarted'] },
  { action: 'unarchive', label: 'Unarchive', filters: ['archived'] },
  { action: 'favorite', label: '★ Favorite', filters: ['active', 'unstarted', 'archived'] },
  { action: 'unfavorite', label: 'Unfavorite', filters: ['favorites'] },
  { action: 'priority', label: '● Priority', filters: ['active', 'favorites', 'unstarted'] },
  { action: 'unpriority', label: 'Unpin', filters: ['priority'] },
  { action: 'unfollow', label: 'Remove', filters: ['active', 'favorites', 'priority', 'unstarted', 'archived'] },
];

function renderBulkBar(visible) {
  const count = state.selected.size;
  const filter = state.showFilter.filter;
  const bar = document.createElement('div');
  bar.className = 'bulkbar';
  bar.innerHTML = `
    <div class="bulkbar-row">
      <button class="ghost" id="select-all">
        ${count === visible.length ? 'Select none' : `Select all ${visible.length}`}
      </button>
      <span class="muted">${count} selected</span>
    </div>
    <div class="bulkbar-row actions">
      ${BULK_ACTIONS.filter((item) => item.filters.includes(filter)).map((item) => `
        <button class="secondary" data-bulk="${item.action}" ${count ? '' : 'disabled'}>${item.label}</button>`).join('')}
    </div>`;
  document.body.appendChild(bar);
  document.body.classList.add('selecting');

  document.getElementById('select-all').addEventListener('click', () => {
    state.selected = count === visible.length ? new Set() : new Set(visible);
    render();
  });
}

async function runBulk(action) {
  const ids = [...state.selected];
  if (!ids.length) return;
  const label = BULK_ACTIONS.find((item) => item.action === action)?.label || action;
  if (action === 'unfollow'
      && !confirm(`Remove ${ids.length} show${ids.length === 1 ? '' : 's'} from your library? Your watch history is kept.`)) {
    return;
  }
  try {
    const result = await api('/shows/bulk', {
      method: 'POST',
      body: JSON.stringify({ show_ids: ids, action }),
    });
    toast(`${result.changed} show${result.changed === 1 ? '' : 's'} ${result.verb}`);
    state.selected = new Set();
    await render();
  } catch (error) {
    toast(error.message);
  }
}

/* ----------------------------------------------------------- search view */

async function viewSearch() {
  main.innerHTML = `
    <form id="search-form" class="field" style="display:flex;gap:8px">
      ${clearableInput({ id: 'search-input', placeholder: 'Search for a show', value: state.searchQuery })}
      <button class="primary" type="submit">Go</button>
    </form>
    <div id="search-results">${
      state.searchResults ? renderSearchResults(state.searchResults) : '<div class="empty">Search TVmaze for anything you want to track.</div>'
    }</div>`;

  const form = document.getElementById('search-form');
  const input = document.getElementById('search-input');
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const query = input.value.trim();
    if (!query) return;
    state.searchQuery = query;
    document.getElementById('search-results').innerHTML = '<div class="empty">Searching…</div>';
    try {
      state.searchResults = await api(`/search?q=${encodeURIComponent(query)}`);
      document.getElementById('search-results').innerHTML = renderSearchResults(state.searchResults);
    } catch (error) {
      document.getElementById('search-results').innerHTML = `<div class="empty">${esc(error.message)}</div>`;
    }
  });
  wireClearable('search-input', () => { state.searchQuery = ''; });
  if (!state.searchResults) input.focus();
}

function renderSearchResults(results) {
  if (!results.length) return '<div class="empty">No matches on TVmaze.</div>';
  return `<div class="list">${results.map((show) => `
    <div class="row" data-open="${show.id}">
      ${poster(show.image, 'thumb', show.name)}
      <div class="row-body">
        <div class="row-title">${esc(show.name)}</div>
        <div class="row-sub">${esc([show.premiered?.slice(0, 4), show.network, show.status].filter(Boolean).join(' · '))}</div>
      </div>
      ${show.following
        ? '<span class="pill good">Added</span>'
        : `<button class="primary" data-add="${show.id}">Add</button>`}
    </div>`).join('')}</div>`;
}

/* ------------------------------------------------------ show detail view */

async function viewShowDetail() {
  const card = await api(`/shows/${state.showId}`);
  const { show, progress, next, last_watched: last } = card;
  titleEl.textContent = show.name;

  if (state.openSeasons.size === 0 && next) state.openSeasons.add(next.season);

  const meta = [show.status, show.network, show.premiered?.slice(0, 4), show.genres?.slice(0, 2).join(', ')]
    .filter(Boolean).join(' · ');

  main.innerHTML = `
    <button class="back" data-back="1">&larr; Back</button>
    <div class="hero">
      ${poster(show.image_original || show.image, '', show.name)}
      <div>
        <h2>${esc(show.name)}</h2>
        <div class="meta">${esc(meta)}</div>
        <div class="card-line">${progress.watched}/${progress.total} watched · ${progress.percent}%</div>
        <div class="progress" style="margin-bottom:8px"><i style="width:${progress.percent}%"></i></div>
        ${last ? `<div class="card-line muted">Last watched ${esc(last.code)}, ${esc(airLabel(last.watched_at))}</div>` : ''}
        ${next ? `<div class="card-line">Next up <strong>${esc(next.code)}</strong> · ${esc(airLabel(next.airstamp))}</div>` : ''}
      </div>
    </div>

    ${card.following ? `
      <div class="card-actions" style="margin-bottom:10px">
        ${next && next.aired ? `<button class="primary" data-watch="${next.id}">Watched ${esc(next.code)}</button>` : ''}
        <button class="secondary ${card.favorite ? 'on' : ''}" data-favorite="${card.favorite ? 0 : 1}">
          ${card.favorite ? '&#9733;' : '&#9734;'} Favorite
        </button>
        <button class="secondary ${card.priority ? 'on' : ''}" data-priority="${card.priority ? 0 : 1}">
          ${card.priority ? '&#9679;' : '&#9675;'} Priority
        </button>
      </div>
      <div class="card-actions" style="margin-bottom:16px">
        ${card.archived
          ? `<button class="secondary" data-archive="0">Unarchive</button>`
          : `<button class="secondary" data-archive="1">Archive</button>`}
        <button class="secondary" data-resync="${show.id}">Re-sync</button>
        <button class="ghost" data-remove="${show.id}">Remove</button>
      </div>` : `
      <div class="card-actions" style="margin-bottom:16px">
        <button class="primary" data-add="${show.id}" data-stay="1">Add to my shows</button>
      </div>
      <p class="muted" style="margin:-8px 2px 16px;font-size:13px">
        Not in your library yet. You can look through the episodes below first.
      </p>`}

    ${show.summary ? `<p class="summary">${esc(show.summary)}</p>` : ''}

    ${card.seasons.map((season) => renderSeason(show.id, season)).join('')}`;
}

function renderSeason(showId, season) {
  const open = state.openSeasons.has(season.season);
  const label = season.season === 0 ? 'Specials' : `Season ${season.season}`;
  const complete = season.total > 0 && season.watched === season.total;
  return `
    <div class="list">
      <div class="season-head" data-season-toggle="${season.season}">
        <span>${esc(label)} <span class="count">${season.watched}/${season.total}</span></span>
        <span>
          ${season.total
            ? `<button class="ghost" data-season-mark="${season.season}" data-season-value="${complete ? 0 : 1}">
                 ${complete ? 'Unwatch all' : 'Watch all'}
               </button>`
            : ''}
          ${open ? '&#9662;' : '&#9656;'}
        </span>
      </div>
      ${open ? season.episodes.map((episode) => renderEpisode(showId, episode)).join('') : ''}
    </div>`;
}

function renderEpisode(showId, episode) {
  const sub = [episode.aired ? airLabel(episode.airstamp) : `Airs ${airLabel(episode.airstamp)}`]
    .filter(Boolean).join(' · ');
  return `
    <div class="row episode-row ${episode.aired ? '' : 'unaired'}">
      <button class="check ${episode.watched ? 'on' : ''}"
              data-toggle="${episode.id}" data-on="${episode.watched ? 1 : 0}">&#10003;</button>
      <div class="row-body">
        <div class="row-title">${esc(episode.code)} · ${esc(episode.name || 'TBA')}</div>
        <div class="row-sub">${esc(sub)}</div>
      </div>
      ${episode.aired && !episode.watched
        ? `<button class="ghost" data-through="${episode.id}" data-through-show="${showId}" title="Mark this and everything before it">&#8676;</button>`
        : ''}
    </div>`;
}

/* --------------------------------------------------------- settings view */

// State the verdict either way. A checker that only speaks up when unhappy is
// indistinguishable from one that is broken and silent.
function storageKind(store) {
  if (!store.in_container) return 'Local disk';
  if (store.on_container_disk === true) return 'Container disk — NOT persistent';
  if (store.on_container_disk === false) return 'Mounted volume ✓';
  return 'Unknown';
}

async function viewSettings() {
  const [status, imports, backups] = await Promise.all([
    api('/status'), api('/imports?limit=5'), api('/backups'),
  ]);
  const last = status.last_refresh || {};

  main.innerHTML = `
    ${status.storage && status.storage.warning ? `
      <div class="alarm">
        <strong>Your data is not being saved permanently</strong>
        ${esc(status.storage.warning)}
        <div class="muted" style="margin-top:8px">Currently writing to ${esc(status.storage.database)}</div>
      </div>` : ''}

    ${status.storage && status.storage.app_version !== APP_VERSION ? `
      <div class="alarm">
        <strong>This page is out of date</strong>
        Your browser is running app version ${esc(APP_VERSION)} but the server has
        ${esc(status.storage.app_version)}. Tap below to drop the cached copy and reload.
        <div style="margin-top:10px"><button class="primary" id="force-reload">Reload the app</button></div>
      </div>` : ''}

    <section class="section">
      <div class="section-head"><h2>Library</h2></div>
      <div class="stat-grid">
        <div class="stat"><div class="value">${status.following}</div><div class="label">Shows followed</div></div>
        <div class="stat"><div class="value">${status.episodes_watched}</div><div class="label">Episodes watched</div></div>
      </div>
      <button class="secondary" data-stats="1">View watch stats</button>
    </section>

    <section class="section">
      <div class="section-head"><h2>New episode checks</h2></div>
      <p class="muted" style="margin:0 2px 10px">
        Runs automatically every ${status.refresh_interval_hours} hours.
        ${last.at ? `Last check ${esc(airLabel(last.at))}, ${last.new_episodes || 0} new episode(s) found.` : 'No check has run yet.'}
      </p>
      <button class="primary" id="refresh-now">Check now</button>
    </section>

    <section class="section">
      <div class="section-head"><h2>Import from TV Time</h2></div>
      <p class="muted" style="margin:0 2px 10px">
        Upload the zip (or a single CSV) from your TV Time data export. Preview first —
        nothing is written until you confirm.
      </p>
      <div class="field"><input type="file" id="import-file" accept=".zip,.csv,.json"></div>
      <div class="checkline">
        <input type="checkbox" id="import-follow" checked>
        <label for="import-follow">Follow every show found in the export</label>
      </div>
      <div class="card-actions">
        <button class="secondary" id="import-preview">Preview</button>
        <button class="primary" id="import-commit">Import for real</button>
      </div>
      <div id="import-report"></div>
      ${imports.length ? `
        <div class="report">
          <strong>Recent imports</strong>
          <ul>${imports.map((job) => `
            <li>${esc(job.created_at.slice(0, 16).replace('T', ' '))} —
                ${esc(job.filename || 'upload')} (${esc(job.status)}),
                ${(job.report || {}).episodes_marked ?? 0} episodes</li>`).join('')}
          </ul>
        </div>` : ''}
    </section>

    <section class="section">
      <div class="section-head"><h2>App</h2></div>
      <p class="muted" style="margin:0 2px 10px;font-size:12.5px">
        Version ${esc(APP_VERSION)}${status.storage ? ` · server has ${esc(status.storage.app_version)}` : ''}.
        If a change I made does not seem to have arrived, reload rather than assuming it failed.
      </p>
      <button class="secondary" id="reload-app">Reload the app</button>
    </section>

    <section class="section">
      <div class="section-head"><h2>Backups</h2></div>
      ${status.storage ? `
        <div class="report" style="margin:0 0 12px">
          <div class="kv"><span>Database</span><span><code>${esc(status.storage.database)}</code></span></div>
          <div class="kv"><span>Size</span><span>${Math.max(Math.round(status.storage.size_bytes / 1024), 1)} KB</span></div>
          <div class="kv"><span>Created</span><span>${esc(airLabel(status.storage.created_at))}</span></div>
          <div class="kv"><span>Restarts survived</span><span>${Math.max(status.storage.starts - 1, 0)}</span></div>
          <div class="kv"><span>Storage</span><span>${esc(storageKind(status.storage))}</span></div>
          <p class="muted" style="margin:8px 0 0;font-size:12.5px">
            ${status.storage.starts > 1
              ? 'This database has survived a restart, so your data is being kept between deploys.'
              : 'This database was created on the most recent start. Restart the app once and check that these numbers go up rather than resetting.'}
          </p>
        </div>` : ''}
      <p class="muted" style="margin:0 2px 10px">
        ${backups.enabled
          ? `Saved automatically every ${backups.interval_hours} hours, keeping the last ${backups.keep}.
             ${backups.last_backup_at
               ? `Last backup ${esc(airLabel(backups.last_backup_at))}.`
               : 'No backup written yet.'}`
          : 'Automatic backups are switched off.'}
        Your watch history is the one thing here that cannot be fetched again.
      </p>
      <div class="card-actions" style="margin-bottom:12px">
        <button class="primary" id="backup-now">Back up now</button>
        <button class="secondary" id="export-btn">Download a copy</button>
      </div>
      <details style="margin-bottom:12px">
        <summary class="muted" style="cursor:pointer;font-size:13.5px">
          Moving to another machine? Restore a backup here
        </summary>
        <p class="muted" style="margin:10px 0">
          Pick a backup file to rebuild this library from it — shows, watch dates,
          favorites, priority and archived state. Existing entries are left alone,
          so restoring twice is harmless.
        </p>
        <div class="field"><input type="file" id="restore-file" accept=".json"></div>
        <button class="secondary" id="restore-btn">Restore from file</button>
        <div id="restore-report"></div>
      </details>
      ${backups.files.length ? `
        <div class="list">
          ${backups.files.slice(0, 8).map((file) => `
            <div class="row">
              <div class="row-body">
                <div class="row-title">${esc(airLabel(file.modified))}</div>
                <div class="row-sub">${Math.round(file.bytes / 1024)} KB · ${esc(file.name)}</div>
              </div>
              <a class="ghost" href="/api/backups/${encodeURIComponent(file.name)}" download>Download</a>
            </div>`).join('')}
        </div>
        <p class="muted" style="margin:8px 2px 0;font-size:12.5px">
          Stored in ${esc(backups.directory)} on the server. Download one now and then
          so a copy lives somewhere else too.
        </p>` : ''}
    </section>`;

  document.getElementById('refresh-now').addEventListener('click', runRefresh);
  document.getElementById('import-preview').addEventListener('click', () => runImport(true));
  document.getElementById('import-commit').addEventListener('click', () => runImport(false));
  document.getElementById('export-btn').addEventListener('click', downloadBackup);
  document.querySelectorAll('#force-reload, #reload-app').forEach((button) => {
    button.addEventListener('click', hardReload);
  });
  document.getElementById('restore-btn').addEventListener('click', runRestore);
  document.getElementById('backup-now').addEventListener('click', async (event) => {
    event.target.disabled = true;
    try {
      const result = await api('/backups?force=true', { method: 'POST' });
      toast(result.written ? `Saved ${result.watches} watched episodes` : result.reason);
      await render();
    } catch (error) {
      toast(error.message);
      event.target.disabled = false;
    }
  });
}

async function runImport(dryRun) {
  const input = document.getElementById('import-file');
  const target = document.getElementById('import-report');
  if (!input.files.length) {
    toast('Choose your export file first');
    return;
  }
  const body = new FormData();
  body.append('file', input.files[0]);
  body.append('dry_run', dryRun ? 'true' : 'false');
  body.append('follow_shows', document.getElementById('import-follow').checked ? 'true' : 'false');

  target.innerHTML = '<div class="report">Uploading…</div>';
  try {
    const { job_id: jobId } = await api('/import', { method: 'POST', body });
    await pollImport(jobId, target, dryRun);
  } catch (error) {
    target.innerHTML = `<div class="report">${esc(error.message)}</div>`;
  }
}

// A full library is several hundred shows against a rate-limited API, so the
// import runs server-side and we watch it.
async function pollImport(jobId, target, dryRun) {
  for (;;) {
    const job = await api(`/import/${jobId}`);
    if (job.status === 'failed') {
      target.innerHTML = `<div class="report"><strong>Import failed</strong><br>${esc(job.error || '')}</div>`;
      return;
    }
    if (job.report) {
      target.innerHTML = renderImportReport(job.report);
      if (!dryRun) {
        toast(`Imported ${job.report.episodes_marked} episodes`);
        updateBadge(0);
      }
      return;
    }
    const percent = job.total ? Math.round((100 * job.done) / job.total) : 0;
    target.innerHTML = `
      <div class="report">
        <strong>${esc(job.stage || 'Working')}</strong>
        ${job.total ? `<div class="kv"><span>${job.done} of ${job.total}</span><span>${percent}%</span></div>
          <div class="progress"><i style="width:${percent}%"></i></div>` : ''}
        <p class="muted" style="margin:8px 0 0">
          Matching every show against TVmaze takes a few minutes for a large library.
          You can leave this page open, or come back later — it keeps running.
        </p>
      </div>`;
    await new Promise((resolve) => setTimeout(resolve, 1500));
  }
}

function renderImportReport(report) {
  const rows = [
    ['Detected format', report.format],
    ['Shows matched', report.shows_found],
    [report.dry_run ? 'Shows to follow' : 'Shows followed', report.shows_to_follow],
    [report.dry_run ? 'Shows to archive' : 'Shows archived', report.shows_to_archive],
    ['Watch rows read', report.watch_rows_read],
    [report.dry_run ? 'Episodes that would be marked' : 'Episodes marked', report.episodes_marked],
    ['Already in your library', report.episodes_already_known],
    ['Episodes not found on TVmaze', report.episodes_unmatched],
    ['Unnumbered specials skipped', report.specials_skipped],
  ];
  const list = (title, items) => (items && items.length
    ? `<p style="margin:10px 0 0"><strong>${esc(title)}</strong></p>
       <ul>${items.slice(0, 25).map((item) => `<li>${esc(item)}</li>`).join('')}</ul>`
    : '');

  return `
    <div class="report">
      <strong>${report.dry_run ? 'Preview — nothing was saved' : 'Import complete'}</strong>
      ${rows.map(([label, value]) => `<div class="kv"><span>${esc(label)}</span><span>${value}</span></div>`).join('')}
      ${list('Files read', report.files.map((f) => `${f.file}: ${f.used}`))}
      ${list('Could not identify these shows', report.shows_unmatched)}
      ${list('Matched by title only — check these', report.shows_guessed)}
      ${list('Episodes with no TVmaze counterpart', report.episodes_unmatched_sample)}
      ${list('Specials TV Time filed without a number', report.specials_skipped_sample)}
    </div>`;
}

async function runRestore() {
  const input = document.getElementById('restore-file');
  const target = document.getElementById('restore-report');
  if (!input.files.length) {
    toast('Choose a backup file first');
    return;
  }
  target.innerHTML = '<div class="report">Reading the file…</div>';
  let payload;
  try {
    payload = JSON.parse(await input.files[0].text());
  } catch (_) {
    target.innerHTML = '<div class="report">That file is not readable JSON.</div>';
    return;
  }
  try {
    const { job_id: jobId } = await api('/restore', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    await pollImport(jobId, target, false);
  } catch (error) {
    target.innerHTML = `<div class="report">${esc(error.message)}</div>`;
  }
}

// iOS keeps a home-screen web app's files aggressively, and there is no "clear
// cache" a normal person can reach. This throws away the service worker and its
// caches, then reloads from the network.
async function hardReload() {
  toast('Fetching the latest version…');
  try {
    if ('caches' in window) {
      const names = await caches.keys();
      await Promise.all(names.map((name) => caches.delete(name)));
    }
    if ('serviceWorker' in navigator) {
      const registrations = await navigator.serviceWorker.getRegistrations();
      await Promise.all(registrations.map((registration) => registration.unregister()));
    }
  } catch (_) { /* reload anyway */ }
  // Cache-busting query so the shell itself cannot come from a stale store.
  window.location.replace(`/?fresh=${Date.now()}`);
}

async function downloadBackup() {
  const data = await api('/export');
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
  const link = document.createElement('a');
  link.href = URL.createObjectURL(blob);
  link.download = `tv-tracker-${new Date().toISOString().slice(0, 10)}.json`;
  link.click();
  URL.revokeObjectURL(link.href);
}

async function viewStats() {
  const stats = await api('/stats');
  titleEl.textContent = 'Watch Stats';
  main.innerHTML = `
    <button class="back" data-back-settings="1">&larr; Back</button>
    <div class="stat-grid">
      <div class="stat"><div class="value">${stats.episodes}</div><div class="label">Episodes watched</div></div>
      <div class="stat"><div class="value">${humanMinutes(stats.minutes)}</div><div class="label">Time watched</div></div>
      <div class="stat"><div class="value">${stats.shows}</div><div class="label">Shows started</div></div>
      <div class="stat"><div class="value">${stats.following}</div><div class="label">Currently following</div></div>
    </div>
    <section class="section">
      <div class="section-head"><h2>Most watched</h2></div>
      <div class="list">
        ${stats.top_shows.map((show) => `
          <div class="row" data-open="${show.id}">
            ${poster(show.image, 'thumb', show.name)}
            <div class="row-body">
              <div class="row-title">${esc(show.name)}</div>
              <div class="row-sub">${show.episodes} episodes · ${humanMinutes(show.minutes)}</div>
            </div>
          </div>`).join('') || '<div class="empty">Nothing watched yet.</div>'}
      </div>
    </section>
    <section class="section">
      <div class="section-head"><h2>By month</h2></div>
      <div class="list">
        ${stats.by_month.map((row) => `
          <div class="row">
            <div class="row-body"><div class="row-title">${esc(row.month)}</div></div>
            <span class="pill">${row.episodes}</span>
          </div>`).join('') || '<div class="empty">No history yet.</div>'}
      </div>
    </section>`;
}

/* ------------------------------------------------------------- refreshing */

async function runRefresh() {
  const button = document.getElementById('refresh-btn');
  button.classList.add('spinning');
  try {
    const report = await api('/refresh', { method: 'POST' });
    toast(report.new_episodes
      ? `${report.new_episodes} new episode(s) found`
      : `Checked ${report.checked} shows, nothing new`);
    await render();
  } catch (error) {
    toast(error.message);
  } finally {
    button.classList.remove('spinning');
  }
}

function updateBadge(count) {
  if (count > 0) {
    badgeEl.textContent = count > 99 ? '99+' : count;
    badgeEl.classList.remove('hidden');
  } else {
    badgeEl.classList.add('hidden');
  }
}

async function pollBadge() {
  try {
    const data = await api('/new');
    if (state.view !== 'new') updateBadge(data.episodes.length);
  } catch (_) { /* offline is fine */ }
}

/* ---------------------------------------------------------- interactions */

document.querySelectorAll('.tab').forEach((tab) => {
  tab.addEventListener('click', () => go(tab.dataset.view));
});

document.getElementById('refresh-btn').addEventListener('click', runRefresh);

document.addEventListener('click', async (event) => {
  const target = event.target.closest('[data-watch], [data-toggle], [data-open], [data-add], [data-through], [data-archive], [data-favorite], [data-priority], [data-remove], [data-resync], [data-season-toggle], [data-season-mark], [data-back], [data-back-settings], [data-go], [data-stats], [data-pick], [data-bulk], [data-expand], [data-service], #services-reset, [data-backup-now], .card-title, .poster');
  if (!target) return;

  const data = target.dataset;

  if (data.pick) {
    const id = Number(data.pick);
    if (state.selected.has(id)) state.selected.delete(id);
    else state.selected.add(id);
    return render();
  }

  if (data.bulk) return runBulk(data.bulk);

  if (data.service !== undefined) {
    const services = selectedServices(premiereDefaults);
    if (target.checked) services.add(data.service);
    else services.delete(data.service);
    localStorage.setItem('tv.services', JSON.stringify([...services]));
    return render();
  }

  if (target.id === 'services-reset') {
    localStorage.removeItem('tv.services');
    return render();
  }

  if (data.expand) {
    if (state.expanded.has(data.expand)) state.expanded.delete(data.expand);
    else state.expanded.add(data.expand);
    return render();  // no scroll target, so the page holds its place
  }

  if (data.go) return go(data.go);
  if (data.stats) return viewStats();
  if (data.back) return go(state.returnTo || 'home');
  if (data.backSettings) return go('settings');

  if (data.open) {
    event.stopPropagation();
    state.openSeasons = new Set();
    return go('show', Number(data.open));
  }

  if (target.classList.contains('card-title') || target.classList.contains('poster')) {
    const card = target.closest('[data-show]');
    if (card) {
      state.openSeasons = new Set();
      return go('show', Number(card.dataset.show));
    }
  }

  if (data.watch) {
    target.disabled = true;
    try {
      await api(`/episodes/${data.watch}/watch`, { method: 'POST', body: JSON.stringify({}) });
      toast('Marked as watched');
      await render();
    } catch (error) {
      toast(error.message);
      target.disabled = false;
    }
    return;
  }

  if (data.toggle) {
    const on = data.on === '1';
    target.classList.toggle('on', !on);
    try {
      await api(`/episodes/${data.toggle}/watch`, {
        method: on ? 'DELETE' : 'POST',
        body: on ? undefined : JSON.stringify({}),
      });
      await render();
    } catch (error) {
      toast(error.message);
      target.classList.toggle('on', on);
    }
    return;
  }

  if (data.through) {
    const result = await api(`/shows/${data.throughShow}/watch-through`, {
      method: 'POST',
      body: JSON.stringify({ episode_id: Number(data.through) }),
    });
    toast(`Marked ${result.marked} episodes as watched`);
    return render();
  }

  if (data.seasonMark) {
    event.stopPropagation();
    const value = data.seasonValue === '1';
    await api(`/shows/${state.showId}/seasons/${data.seasonMark}/watch`, {
      method: 'POST',
      body: JSON.stringify({ value }),
    });
    return render();
  }

  if (data.seasonToggle !== undefined) {
    const season = Number(data.seasonToggle);
    if (state.openSeasons.has(season)) state.openSeasons.delete(season);
    else state.openSeasons.add(season);
    return render();
  }

  if (data.add) {
    target.disabled = true;
    target.textContent = 'Adding…';
    try {
      const card = await api('/shows', {
        method: 'POST',
        body: JSON.stringify({ tvmaze_id: Number(data.add) }),
      });
      toast(`Added ${card.show.name}`);
      if (data.stay) return render();
      if (state.searchResults) {
        state.searchResults = state.searchResults.map((show) => (
          show.id === Number(data.add) ? { ...show, following: true } : show
        ));
      }
      await render();
    } catch (error) {
      toast(error.message);
      target.disabled = false;
      target.textContent = 'Add';
    }
    return;
  }

  if (data.favorite !== undefined) {
    const on = data.favorite === '1';
    await api(`/shows/${state.showId}/favorite`, {
      method: 'POST',
      body: JSON.stringify({ value: on }),
    });
    toast(on ? 'Added to favorites' : 'Removed from favorites');
    return render();
  }

  if (data.priority !== undefined) {
    const on = data.priority === '1';
    await api(`/shows/${state.showId}/priority`, {
      method: 'POST',
      body: JSON.stringify({ value: on }),
    });
    toast(on ? 'Pinned to the top of Up Next' : 'No longer a priority');
    return render();
  }

  if (data.archive !== undefined) {
    await api(`/shows/${state.showId}/archive`, {
      method: 'POST',
      body: JSON.stringify({ value: data.archive === '1' }),
    });
    toast(data.archive === '1' ? 'Archived' : 'Back in your list');
    return render();
  }

  if (data.resync) {
    target.disabled = true;
    await api(`/shows/${data.resync}/refresh`, { method: 'POST' });
    toast('Re-synced from TVmaze');
    return render();
  }

  if (data.remove) {
    if (!confirm('Remove this show? Your watch history is kept unless you add it again.')) return;
    await api(`/shows/${data.remove}`, { method: 'DELETE' });
    toast('Removed');
    return go('shows');
  }
});

/* --------------------------------------------------------------- startup */

function showLogin() {
  document.getElementById('login').classList.remove('hidden');
  document.getElementById('app').classList.add('hidden');
}

function showApp() {
  document.getElementById('login').classList.add('hidden');
  document.getElementById('app').classList.remove('hidden');
}

document.getElementById('login-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const errorEl = document.getElementById('login-error');
  errorEl.textContent = '';
  try {
    await api('/auth/login', {
      method: 'POST',
      body: JSON.stringify({ password: document.getElementById('password').value }),
    });
    showApp();
    const start = parseHash() || { view: 'home', showId: null };
    go(start.view, start.showId, { push: false });
  } catch (error) {
    errorEl.textContent = 'That password did not work.';
  }
});

async function boot() {
  const status = await fetch('/api/auth/status').then((r) => r.json());
  if (status.required && !status.authenticated) {
    showLogin();
    return;
  }
  showApp();
  const start = parseHash() || { view: 'home', showId: null };
  // Replace rather than push, so the first entry in history is where we landed.
  history.replaceState({ ...start }, '', hashFor(start.view, start.showId));
  go(start.view, start.showId, { push: false });
  pollBadge();
  setInterval(pollBadge, 10 * 60 * 1000);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) pollBadge();
  });
}

window.addEventListener('popstate', () => {
  const target = parseHash() || { view: 'home', showId: null };
  go(target.view, target.showId, { push: false });
});

if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').catch(() => { /* not fatal */ });
}

boot();
