/* TV Tracker - a small hand-rolled SPA, no build step. */

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
  showFilter: { filter: 'active', sort: 'name', q: '' },
  selecting: false,
  selected: new Set(),
};

const TITLES = {
  home: 'Up Next',
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

function go(view, showId = null) {
  if (view !== 'shows') {
    state.selecting = false;
    state.selected = new Set();
  }
  state.view = view;
  state.showId = showId;
  titleEl.textContent = TITLES[view] ?? '';
  document.querySelectorAll('.tab').forEach((tab) => {
    tab.classList.toggle('active', tab.dataset.view === view);
  });
  window.scrollTo(0, 0);
  render();
}

async function render() {
  document.querySelectorAll('.bulkbar').forEach((bar) => bar.remove());
  document.body.classList.remove('selecting');
  main.innerHTML = '<div class="empty">Loading…</div>';
  try {
    const views = {
      home: viewHome,
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

function section(title, cards, note = '', options = {}) {
  if (!cards.length) return '';
  return `
    <section class="section">
      <div class="section-head"><h2>${esc(title)}</h2><span class="muted">${esc(note)}</span></div>
      ${cards.map((card) => showCard(card, options)).join('')}
    </section>`;
}

async function viewHome() {
  const data = await api('/home');
  updateBadge(data.new_since_last_visit);

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
    section('Priority watch', data.priority, '', { inPriority: true }),
    section('Ready to watch', data.ready, data.counts.episodes_ready ? `${data.counts.episodes_ready} episodes` : ''),
    section('Coming up', data.scheduled),
    section('Not started yet', data.not_started, `${data.counts.not_started} shows`),
    section('Waiting for more', data.waiting),
    section('Finished', data.complete),
  ].join('') || '<div class="empty">Nothing to show yet.</div>';
}

/* -------------------------------------------------------------- new view */

async function viewNew() {
  const data = await api('/new');
  await api('/seen', { method: 'POST' });
  updateBadge(0);

  if (!data.episodes.length) {
    main.innerHTML = `
      <div class="empty">
        <strong>Nothing new right now</strong>
        Episodes that air after your last visit land here.
      </div>`;
    return;
  }

  main.innerHTML = `
    <p class="muted" style="margin:0 2px 14px">
      ${data.episodes.length} unwatched episode${data.episodes.length === 1 ? '' : 's'} aired recently.
    </p>
    <div class="list">${data.episodes.map(newRow).join('')}</div>`;
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

async function viewCalendar() {
  const episodes = await api('/upcoming?days=35');
  if (!episodes.length) {
    main.innerHTML = `
      <div class="empty">
        <strong>No airings scheduled</strong>
        Nothing you follow has a confirmed air date in the next five weeks.
      </div>`;
    return;
  }

  const groups = new Map();
  episodes.forEach((episode) => {
    const key = (episode.airstamp || '').slice(0, 10);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(episode);
  });

  main.innerHTML = [...groups.entries()].map(([day, items]) => `
    <div class="date-head">${esc(dayHeading(day))}</div>
    <div class="list">
      ${items.map((episode) => `
        <div class="row" data-open="${episode.show_id}">
          ${poster(episode.show_image, 'thumb', episode.show_name)}
          <div class="row-body">
            <div class="row-title">${esc(episode.show_name)}</div>
            <div class="row-sub">${esc(episode.code)} · ${esc(episode.name || 'TBA')}</div>
            <div class="row-sub">${esc(timeOf(new Date(episode.airstamp)))}${episode.network ? ' · ' + esc(episode.network) : ''}</div>
          </div>
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
      <input type="search" id="show-search" placeholder="Filter your shows" value="${esc(q)}">
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
      <input type="search" id="search-input" placeholder="Search for a show" value="${esc(state.searchQuery)}" autocomplete="off">
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
  if (!state.searchResults) input.focus();
}

function renderSearchResults(results) {
  if (!results.length) return '<div class="empty">No matches on TVmaze.</div>';
  return `<div class="list">${results.map((show) => `
    <div class="row">
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
    </div>

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

async function viewSettings() {
  const [status, imports, backups] = await Promise.all([
    api('/status'), api('/imports?limit=5'), api('/backups'),
  ]);
  const last = status.last_refresh || {};

  main.innerHTML = `
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
      <div class="section-head"><h2>Backups</h2></div>
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
  const target = event.target.closest('[data-watch], [data-toggle], [data-open], [data-add], [data-through], [data-archive], [data-favorite], [data-priority], [data-remove], [data-resync], [data-season-toggle], [data-season-mark], [data-back], [data-back-settings], [data-go], [data-stats], [data-pick], [data-bulk], [data-backup-now], .card-title, .poster');
  if (!target) return;

  const data = target.dataset;

  if (data.pick) {
    const id = Number(data.pick);
    if (state.selected.has(id)) state.selected.delete(id);
    else state.selected.add(id);
    return render();
  }

  if (data.bulk) return runBulk(data.bulk);

  if (data.go) return go(data.go);
  if (data.stats) return viewStats();
  if (data.back) return go('home');
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
    go('home');
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
  go('home');
  pollBadge();
  setInterval(pollBadge, 10 * 60 * 1000);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) pollBadge();
  });
}

if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').catch(() => { /* not fatal */ });
}

boot();
