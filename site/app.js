// The talk list (index.html): filters, the list grouped by day, the selection of talks
// to download as one calendar file (the bar above the list, and the dock that keeps the
// count and the download in reach while the bar is scrolled away) and the subscription
// panel.

import { calendarFor, fetchCalendar, parseCalendar, saveFile } from "./calendar.js";
import { h, link, replaceChildren } from "./dom.js";
import {
  ANY_CHOICES,
  NO_FILTERS,
  RANGES,
  filtersFromQuery,
  groupByDay,
  isUnfiltered,
  isUpcoming,
  matches,
  prepareEvents,
  queryFromFilters,
  toggled,
  withinChoices,
} from "./filters.js";
import { children, currentLang, formatSigned, parts, plural, t, withLang } from "./i18n.js";
import {
  byId,
  debounced,
  fact,
  fetchJson,
  notice,
  replaceQuery,
  reportError,
  startPage,
} from "./page.js";
import { knownFeeds, renderSubscribe } from "./subscribe.js";
import { parseTimestamp, zone } from "./time.js";

const ui = {
  updated: byId("updated"),
  timezone: byId("timezone"),
  search: byId("search"),
  fields: byId("field-chips"),
  institutions: byId("institution-chips"),
  host: byId("host"),
  range: byId("range"),
  picks: byId("picks"),
  clear: byId("clear"),
  count: byId("count"),
  selection: byId("selection"),
  selectionStatus: byId("selection-status"),
  selectShown: byId("select-shown"),
  selectClear: byId("select-clear"),
  download: byId("select-download"),
  note: byId("selection-note"),
  events: byId("events"),
  dock: byId("selection-dock"),
  dockCount: byId("selection-dock-count"),
  dockDownload: byId("selection-dock-download"),
  dockNote: byId("selection-dock-note"),
  subscribeLink: byId("subscribe-link"),
  subscribe: byId("subscribe"),
  subscribeHeading: byId("subscribe-heading"),
  steps: byId("apple-steps"),
  feeds: byId("feeds"),
};

// The message of each kind of picks reason and of each place a keyword is found in
// (REASON_KINDS and REASON_PLACES of filters.js).
const REASON_MESSAGES = Object.freeze({
  keyword: "site.event.reasons.keyword",
  series: "site.event.reasons.series",
  speaker: "site.event.reasons.speaker",
  penalty: "site.event.reasons.penalty",
});
const PLACE_MESSAGES = Object.freeze({
  title: "site.event.reasons.places.title",
  series: "site.event.reasons.places.series",
  abstract: "site.event.reasons.places.abstract",
});

// Typing in the search box updates the list and the address once the typing pauses
// for this long, so a long list is not rebuilt on every key.
const SEARCH_DELAY_MS = 200;

// The selected talks are saved under this name, cut out of the feed with this id: the
// feed of every talk on the site.
const SELECTED_FILE = "seminars-selected.ics";
const EVERY_TALK_FEED = "all";
// The hidden label (index.html) that each checkbox of the list reads before the title of
// its talk.
const SELECT_LABEL_ID = "select-label";
// The class of the dock while it is shown, and the room kept between an element scrolled
// into view and the dock, for its focus ring (style.css).
const DOCK_SHOWN = "is-shown";
const DOCK_GAP_PX = 8;
// The state of the history entry that the link of the bar to the subscription panel adds.
const SUBSCRIBE_ENTRY = Object.freeze({ subscribe: true });
// The bar counts as on screen until it leaves the viewport through the top: the area the
// bar is watched in reaches this far below the viewport, more than the page above the bar
// is ever tall. A bar below the screen (the top of the page on a phone) is not passed.
const BELOW_THE_SCREEN = "0px 0px 100000px 0px";

// The download of the selected talks: whether it is being prepared, and the note on its
// outcome (null: none).
const IDLE = Object.freeze({ busy: false, note: null });
const PREPARING = Object.freeze({ kind: "preparing" });
const FAILED = Object.freeze({ kind: "failed" });

// The page state; every change replaces the object. Until the talks are there, the
// filters are those of the address, kept as they are (a bookmarked or shared link), and
// a change made in the meantime is added to them; once the talks arrive, the filters are
// checked against what exists. The selection (uids in the order they were ticked) lives
// here only: neither the address nor the storage of the browser holds it, so a reload
// starts empty; a talk that has ended leaves it when the list is drawn again. ``listed``
// holds the uids of the talks the list shows; ``barPassed`` whether the selection bar is
// scrolled past, wholly above the screen.
let view = Object.freeze({
  model: null,
  failed: false,
  filters: filtersFromQuery(window.location.search, ANY_CHOICES),
  selected: Object.freeze([]),
  listed: Object.freeze([]),
  download: IDLE,
  barPassed: false,
});

/** The payload of events.json prepared for the page. */
function prepareModel(payload, now) {
  const tz = zone(typeof payload.timezone === "string" ? payload.timezone : "UTC");
  const knownFields = children("fields.");
  const fieldIds = (Array.isArray(payload.fields) ? payload.fields : []).filter((id) =>
    knownFields.includes(id),
  );
  const fields = fieldIds.length ? fieldIds : knownFields;
  const events = prepareEvents(payload.events, tz, fields);
  const today = tz.dayKey(now);
  const upcoming = events.filter((event) => isUpcoming(event, now, today));
  const present = (values) => new Set(upcoming.flatMap(values));
  const institutions = present((event) => [event.institution]);
  const hosts = [...present((event) => (event.host === null ? [] : [event.host]))];
  return Object.freeze({
    zone: tz,
    generatedAt: parseTimestamp(payload.generated_at),
    feeds: payload.feeds,
    events,
    choices: Object.freeze({
      fields,
      institutions: children("site.institutions.").filter((id) => institutions.has(id)),
      hosts: hosts.sort((a, b) => a.localeCompare(b)),
    }),
  });
}

/**
 * Show the current filters: the address, the controls and the list. Until the talks
 * are there, the list is a notice (loading or failed) that the filters do not change.
 */
function applyFilters() {
  replaceQuery(queryFromFilters(view.filters, window.location.search));
  syncControls();
  if (view.model !== null) {
    renderList();
  }
}

const pendingSearch = debounced(applyFilters, SEARCH_DELAY_MS);

/** Change the filters and show them at once (a pending search goes with them). */
function setFilters(filters) {
  pendingSearch.cancel();
  view = Object.freeze({ ...view, filters: Object.freeze(filters) });
  applyFilters();
}

/** Take the text of the search box; the list follows once the typing pauses. */
function typeQuery() {
  const filters = Object.freeze({ ...view.filters, query: ui.search.value });
  view = Object.freeze({ ...view, filters });
  ui.clear.disabled = isUnfiltered(filters);
  pendingSearch.schedule();
}

function chip(label, value, selected, onToggle) {
  return h(
    "button",
    {
      type: "button",
      class: "chip",
      "data-value": value,
      "aria-pressed": String(selected),
      on: { click: () => onToggle(value) },
    },
    label,
  );
}

function option(value, label) {
  return h("option", { value }, label);
}

/** What the controls offer before the talks are there: the organizer of the address. */
function choicesWhileLoading(filters) {
  return { fields: [], institutions: [], hosts: filters.host === "" ? [] : [filters.host] };
}

function buildControls() {
  const { model, filters } = view;
  const choices = model?.choices ?? choicesWhileLoading(filters);
  replaceChildren(
    ui.fields,
    choices.fields.map((id) =>
      chip(t("fields." + id), id, filters.fields.includes(id), (value) =>
        setFilters({ ...view.filters, fields: toggled(view.filters.fields, value, choices.fields) }),
      ),
    ),
  );
  replaceChildren(
    ui.institutions,
    choices.institutions.map((id) =>
      chip(t("site.institutions." + id), id, filters.institutions.includes(id), (value) =>
        setFilters({
          ...view.filters,
          institutions: toggled(view.filters.institutions, value, choices.institutions),
        }),
      ),
    ),
  );
  replaceChildren(
    ui.host,
    option("", t("site.filters.host_all")),
    choices.hosts.map((host) => option(host, host)),
  );
  replaceChildren(
    ui.range,
    Object.keys(RANGES).map((id) => option(id, t("site.filters.range_options." + id))),
  );
}

function syncControls() {
  const { filters } = view;
  if (ui.search.value !== filters.query) {
    ui.search.value = filters.query;
  }
  for (const [container, selected] of [
    [ui.fields, filters.fields],
    [ui.institutions, filters.institutions],
  ]) {
    for (const button of container.querySelectorAll("button.chip")) {
      button.setAttribute("aria-pressed", String(selected.includes(button.dataset.value)));
    }
  }
  ui.host.value = filters.host;
  ui.range.value = filters.range;
  ui.picks.checked = filters.picksOnly;
  ui.clear.disabled = isUnfiltered(filters);
}

function connectControls() {
  ui.search.addEventListener("input", typeQuery);
  // Enter or leaving the box: no need to wait for the pause.
  ui.search.addEventListener("change", () => pendingSearch.flush());
  ui.host.addEventListener("change", () => setFilters({ ...view.filters, host: ui.host.value }));
  ui.range.addEventListener("change", () => setFilters({ ...view.filters, range: ui.range.value }));
  ui.picks.addEventListener("change", () =>
    setFilters({ ...view.filters, picksOnly: ui.picks.checked }),
  );
  ui.clear.addEventListener("click", () => {
    setFilters(NO_FILTERS);
    ui.search.focus();
  });
  ui.selectShown.addEventListener("click", selectShown);
  ui.selectClear.addEventListener("click", clearSelection);
  for (const button of downloadButtons()) {
    button.addEventListener("keydown", ignoreRepeatedKey);
    button.addEventListener("click", pressDownload);
  }
  ui.subscribeLink.addEventListener("click", showSubscribe);
  window.addEventListener("popstate", followHistory);
}

/**
 * Back and Forward between the entries of the page (the link to the subscription panel
 * adds one) show the filters of the address of the entry, so the address and the list
 * agree; the browser puts back the scroll position. The language stays the one shown, and
 * the address is made to say so. The list is drawn again only when the filters change.
 */
function followHistory() {
  const search = window.location.search;
  const lang = withLang(search, currentLang());
  if (lang.toString() !== new URLSearchParams(search).toString()) {
    replaceQuery(lang);
  }
  const address = filtersFromQuery(search, ANY_CHOICES);
  const filters = view.model === null ? address : withinChoices(address, view.model.choices);
  if (queryFromFilters(filters, "").toString() !== queryFromFilters(view.filters, "").toString()) {
    setFilters(filters);
  }
}

/**
 * Follow the link of the bar to the subscription panel in place: bring the heading of the
 * panel into view and move the focus to it, as the jump to #subscribe would. The panel
 * goes back to its top first: on a wide screen it sticks beside the list and scrolls
 * within itself, and the heading is its first line. The page then scrolls to the panel,
 * unless the heading is on screen already (the panel beside the list). Like the jump, the
 * link adds an entry to the history, so that Back returns to the list where it was, but
 * for the same address, without a fragment; pressed again from that entry, it adds none.
 * A click that opens the link elsewhere (with a modifier key) is left to the browser.
 */
function showSubscribe(event) {
  if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) {
    return;
  }
  event.preventDefault();
  if (window.history.state?.subscribe !== true) {
    try {
      window.history.pushState(SUBSCRIBE_ENTRY, "");
    } catch (error) {
      // A browser may refuse (Safari allows only so many entries in a short time).
      console.warn(error);
    }
  }
  ui.subscribe.scrollTop = 0;
  const heading = ui.subscribeHeading.getBoundingClientRect();
  if (heading.top < 0 || heading.bottom > window.innerHeight) {
    ui.subscribe.scrollIntoView({ block: "start" });
  }
  ui.subscribeHeading.focus({ preventScroll: true });
}

/** The Download buttons of the bar and of the dock: one state, one handler. */
function downloadButtons() {
  return [ui.download, ui.dockDownload];
}

/**
 * A held Enter repeats its keydown, and every repeat clicks the button again: once the
 * file of the first press is saved, each would save it once more. Only the first keydown
 * of a held Enter or Space is a press.
 */
function ignoreRepeatedKey(event) {
  if (event.repeat && (event.key === "Enter" || event.key === " ")) {
    event.preventDefault();
  }
}

function pressDownload(event) {
  // The second click of a double click is not a press of its own: the file of the first
  // one may be saved already when a fast feed has arrived in between. A key press has
  // detail 0, a single click 1.
  if (event.detail > 1) {
    return;
  }
  downloadSelected().catch(reportError);
}

function canSelectShown() {
  return view.listed.some((uid) => !view.selected.includes(uid));
}

function canClear() {
  return view.selected.length > 0;
}

function canDownload() {
  return view.model !== null && view.selected.length > 0 && !view.download.busy;
}

/**
 * Mark a button of the bar or the dock as unavailable. aria-disabled instead of disabled
 * keeps the focus on a button that its own press makes unavailable (Clear selection, a
 * download being prepared); its handler checks the state and does nothing then.
 */
function setUnavailable(button, unavailable) {
  button.setAttribute("aria-disabled", String(unavailable));
}

/** What the bar says about the selection: how many, and how many the filters hide. */
function selectionStatus({ selected, listed }) {
  if (selected.length === 0) {
    return t("site.select.none");
  }
  const count = plural("site.select.count", selected.length);
  const hidden = selected.filter((uid) => !listed.includes(uid)).length;
  if (hidden === 0) {
    return count;
  }
  return t("site.select.with_hidden", {
    selected: count,
    hidden: plural("site.select.hidden", hidden),
  });
}

/** Replace the text of ``node`` when it differs; unchanged text is left alone. */
function writeText(node, text) {
  if (node.textContent !== text) {
    replaceChildren(node, text);
  }
}

/**
 * The checkboxes of the list, the status and the buttons of the bar and the dock, updated
 * in place: nothing is rebuilt and the bar keeps its height (style.css reserves the
 * status two lines on a phone), so the focus, the scroll position and the list stay where
 * they are.
 */
function syncSelection() {
  const { selected, download } = view;
  for (const box of ui.events.querySelectorAll("input.select-talk")) {
    box.checked = selected.includes(box.dataset.uid);
  }
  // The status is a live region: unchanged text is left alone, so it is not said again.
  writeText(ui.selectionStatus, selectionStatus(view));
  // The dock says the same while the bar is off screen, the talks the filters hide
  // included, as plain text: the status of the bar is the one announcer.
  writeText(ui.dockCount, selected.length ? selectionStatus(view) : "");
  setUnavailable(ui.selectShown, !canSelectShown());
  setUnavailable(ui.selectClear, !canClear());
  for (const button of downloadButtons()) {
    setUnavailable(button, !canDownload());
    button.setAttribute("aria-busy", String(download.busy));
  }
  syncDock();
}

/** Whether the dock is shown (style.css hides it, keeping its place, otherwise). */
function dockShown() {
  return ui.dock.classList.contains(DOCK_SHOWN);
}

/**
 * Show the dock while talks are selected and the bar is scrolled past, above the screen,
 * so the two are never on screen together and the dock never covers the filters above the
 * bar. A dock that hides while it holds the focus hands it to the Download button of the
 * bar first, so the focus is not lost. The page stays where it is (the bar is only coming
 * onto the screen, its Download may still be above it), except that a focus ring (the
 * keyboard) is scrolled onto the screen the least way.
 */
function syncDock() {
  const shown = view.selected.length > 0 && view.barPassed;
  if (shown === dockShown()) {
    return;
  }
  const handOver = !shown && ui.dock.contains(document.activeElement);
  const ring = handOver && document.activeElement.matches(":focus-visible");
  if (handOver) {
    ui.download.focus({ preventScroll: true });
  }
  ui.dock.classList.toggle(DOCK_SHOWN, shown);
  fitDockSpace();
  if (ring) {
    ui.download.scrollIntoView({ block: "nearest" });
  }
  keepFocusClear();
}

/**
 * Make the room a control of the list column keeps below it when it is scrolled into view
 * (--dock-space, its scroll margin in style.css) the height of the dock while it is shown,
 * 0 while it is hidden: the control stops above the dock.
 */
function fitDockSpace() {
  const space = dockShown() ? ui.dock.offsetHeight + DOCK_GAP_PX : 0;
  document.documentElement.style.setProperty("--dock-space", `${space}px`);
}

/**
 * Keep the keyboard focus out from under the dock. The scroll margin of the controls of
 * the list column makes the browser stop one reached with Tab above the dock, but the dock
 * may appear only after that (when the same move scrolls the bar away), and a browser may
 * scroll only elements that are off screen: a focused element under the dock is then
 * scrolled into view above it. An element beside the dock (the subscription panel on a
 * wide screen) is left where it is, and a tap or a click (no focus ring) never scrolls the
 * page.
 */
function keepFocusClear() {
  const focused = document.activeElement;
  if (!dockShown() || focused === null || ui.dock.contains(focused)) {
    return;
  }
  if (!focused.matches(":focus-visible")) {
    return;
  }
  const box = focused.getBoundingClientRect();
  const dock = ui.dock.getBoundingClientRect();
  const under = box.bottom > dock.top && box.top < dock.bottom;
  if (under && box.right > dock.left && box.left < dock.right) {
    focused.scrollIntoView({ block: "nearest" });
  }
}

/**
 * Follow whether the bar is scrolled past: the dock stands in for it then. Any scroll from
 * below the bar to above it crosses the top of the viewport, so a jump past the whole bar
 * in one step is seen as well.
 */
function watchBar() {
  const observer = new IntersectionObserver(
    (entries) => {
      view = Object.freeze({ ...view, barPassed: !entries[entries.length - 1].isIntersecting });
      syncDock();
    },
    { rootMargin: BELOW_THE_SCREEN },
  );
  observer.observe(ui.selection);
}

/** Keep the focus clear of the dock as the focus moves and as the dock changes size. */
function watchDock() {
  new ResizeObserver(() => {
    fitDockSpace();
    keepFocusClear();
  }).observe(ui.dock);
  document.addEventListener("focusin", keepFocusClear);
}

/**
 * The note on the last download, in the active language. A failure is an alert in the
 * bar; ``alert`` false gives the same note as plain text (the copy in the dock).
 */
function noteContent(note, alert = true) {
  switch (note.kind) {
    case "preparing":
      return h("p", { class: "note" }, t("site.select.preparing"));
    case "saved":
      return h("p", { class: "note note-saved" }, plural("site.select.saved", note.saved));
    case "partial":
      return h(
        "p",
        { class: "note note-partial" },
        t("site.select.partial", {
          saved: plural("site.select.saved", note.saved),
          missing: plural("site.select.missing", note.missing),
        }),
      );
    default: // "failed"
      return h(
        "p",
        { class: "note note-failed", role: alert ? "alert" : null },
        t("site.select.failed"),
      );
  }
}

/**
 * Show the note of ``view.download``. Called when the note changes and when the whole
 * page is drawn again (a language switch), never for a filter or a tick, so a note that
 * is an alert is not announced again. The dock, shown while the bar is off screen, shows
 * a copy as plain text (the note of the bar is the one announced), except for a saved
 * file, which the browser shows.
 */
function renderNote() {
  const { note } = view.download;
  replaceChildren(ui.note, note === null ? null : noteContent(note));
  const copied = note !== null && note.kind !== "saved";
  replaceChildren(ui.dockNote, copied ? noteContent(note, false) : null);
}

/**
 * Make ``uids`` the selection. The note on the last download stays until the next one:
 * a tick never changes the height of the bar, so the list below it does not move.
 */
function setSelection(uids) {
  view = Object.freeze({ ...view, selected: Object.freeze([...new Set(uids)]) });
  syncSelection();
}

function toggleTalk(uid, checked) {
  const others = view.selected.filter((selected) => selected !== uid);
  setSelection(checked ? [...others, uid] : others);
}

function selectShown() {
  if (canSelectShown()) {
    setSelection([...view.selected, ...view.listed]);
  }
}

function clearSelection() {
  if (canClear()) {
    setSelection([]);
  }
}

function setDownload(busy, note) {
  view = Object.freeze({ ...view, download: Object.freeze({ busy, note }) });
  syncSelection();
  renderNote();
}

/** The path of the feed with every talk, as the payload lists it, or null. */
function everyTalkFeed(feeds) {
  return knownFeeds(feeds).find((feed) => feed.id === EVERY_TALK_FEED)?.path ?? null;
}

/**
 * Save the selected talks as one calendar file, cut out of the feed of every talk (see
 * calendar.js): the selection at the press of the button, talks hidden by the filters
 * included. A talk the feed lacks is left out and counted in the note; when the feed
 * cannot be read or holds none of them, no file is saved and the note says so. Every
 * press fetches the feed again, so a failed request is retried.
 */
async function downloadSelected() {
  if (!canDownload()) {
    return;
  }
  const uids = view.selected;
  const path = everyTalkFeed(view.model.feeds);
  setDownload(true, PREPARING);
  let note = FAILED;
  try {
    if (path === null) {
      throw new Error("events.json lists no feed of every talk");
    }
    const result = calendarFor(parseCalendar(await fetchCalendar(path)), uids);
    if (result.text === null) {
      throw new Error(`${path} holds none of the ${uids.length} selected talks`);
    }
    saveFile(result.text, SELECTED_FILE);
    const saved = result.included.length;
    const missing = result.missing.length;
    note = Object.freeze(
      missing === 0 ? { kind: "saved", saved } : { kind: "partial", saved, missing },
    );
  } catch (error) {
    reportError(error);
  }
  setDownload(false, note);
}

function selectBox(event, ids) {
  return h(
    "label",
    { class: "event-select" },
    h("input", {
      type: "checkbox",
      class: "select-talk",
      "data-uid": event.uid,
      "aria-labelledby": `${SELECT_LABEL_ID} ${ids.title} ${ids.day} ${ids.when}`,
      checked: view.selected.includes(event.uid),
      on: { change: (change) => toggleTalk(event.uid, change.target.checked) },
    }),
  );
}

function when(event, tz, lang) {
  if (event.allDay) {
    return t("site.event.all_day");
  }
  const start = tz.formatTime(event.start, lang);
  if (event.end === null) {
    return start;
  }
  const sameDay = tz.dayKey(event.end) === event.startDay;
  const end = sameDay ? tz.formatTime(event.end, lang) : tz.formatMoment(event.end, lang);
  return t("site.event.time_range", { start, end });
}

function badges(event) {
  const items = [
    event.pick && ["pick", t("site.event.pick")],
    event.status === "cancelled" && ["cancelled", t("site.event.cancelled")],
    event.status === "tentative" && ["tentative", t("site.event.tentative")],
    event.online && ["online", t("site.event.online")],
    children("site.event_types.").includes(event.type) && [
      "type",
      t("site.event_types." + event.type),
    ],
    children("site.institutions.").includes(event.institution) && [
      "institution",
      t("site.institutions." + event.institution),
    ],
    ...event.fields.map((id) => ["field", t("fields." + id)]),
  ].filter(Boolean);
  return h(
    "ul",
    { class: "badges", "aria-label": t("site.event.labels") },
    items.map(([kind, label]) => h("li", { class: `badge badge-${kind}` }, label)),
  );
}

function speakers(event) {
  if (event.speakers.length === 0) {
    return null;
  }
  return h(
    "ul",
    { class: "speakers", "aria-label": t("site.event.speakers") },
    event.speakers.map((speaker) => {
      const name = link(speaker.url, { class: "speaker-name" }, speaker.name);
      return h(
        "li",
        {},
        speaker.affiliation === null
          ? name
          : parts("site.event.speaker_affiliation", { name, affiliation: speaker.affiliation }),
      );
    }),
  );
}

/** A picks reason in the active language; the terms stay as the configuration has them. */
function reasonText(reason) {
  if (!Object.hasOwn(REASON_MESSAGES, reason.kind)) {
    return reason.term;
  }
  return t(REASON_MESSAGES[reason.kind], {
    term: reason.term,
    place: reason.place === null ? "" : t(PLACE_MESSAGES[reason.place]),
    points: formatSigned(reason.points),
  });
}

function reasons(event) {
  if (!event.pick || event.reasons.length === 0) {
    return null;
  }
  return h(
    "div",
    { class: "reasons" },
    h("p", { class: "reasons-label" }, t("site.event.picks_reasons")),
    h("ul", {}, event.reasons.map((reason) => h("li", {}, reasonText(reason)))),
  );
}

/**
 * One talk of the list. ``ids`` holds the ids of its title, of the heading of its day and
 * of its time, which name its checkbox in that order after "Select": talks of the same
 * title (many are TBA) are told apart by when they are. The ids are made from positions
 * in the list, as a uid may hold anything (a space would split the reference).
 */
function renderEvent(event, tz, lang, today, ids) {
  const classes = ["event", event.status === "cancelled" && "is-cancelled", event.pick && "is-pick"];
  const lastDay =
    event.allDay && event.lastDay !== event.startDay
      ? h(
          "p",
          { class: "event-until" },
          t("site.event.until", { date: tz.formatDay(event.lastDay, lang, today) }),
        )
      : null;
  const stream = link(event.streamUrl, {}, t("site.event.livestream"));
  return h(
    "article",
    { class: classes.filter(Boolean).join(" "), "data-uid": event.uid },
    selectBox(event, ids),
    h(
      "p",
      { class: "event-when", id: ids.when },
      h("time", { datetime: event.start.toISOString() }, when(event, tz, lang)),
    ),
    h(
      "div",
      { class: "event-main" },
      h(
        "h3",
        { class: "event-title", id: ids.title },
        link(event.url, {}, event.tba ? t("site.event.tba") : event.title),
      ),
      lastDay,
      badges(event),
      speakers(event),
      h(
        "dl",
        { class: "facts" },
        fact("site.event.series", event.series),
        fact("site.event.host", event.host),
        fact("site.event.location", event.location),
      ),
      stream.tagName === "A" ? h("p", { class: "stream" }, stream) : null,
      event.abstract === null
        ? null
        : h(
            "details",
            { class: "abstract" },
            h("summary", {}, t("site.event.abstract")),
            h("p", {}, event.abstract),
          ),
      reasons(event),
    ),
  );
}

/**
 * Note the talks the list shows and update the selection to match. ``upcoming``, the uids
 * of the talks that have not ended (null: the talks are not there), prunes the selection:
 * a selected talk that has ended since it was ticked leaves it, as no filter can show it
 * again, so "hidden by the filters" counts only talks that a filter hides.
 */
function setListed(events, upcoming = null) {
  const { selected } = view;
  const kept = upcoming === null ? selected : selected.filter((uid) => upcoming.has(uid));
  view = Object.freeze({
    ...view,
    listed: Object.freeze(events.map((event) => event.uid)),
    selected: Object.freeze(kept),
  });
  syncSelection();
}

function renderList() {
  const { model, failed, filters } = view;
  if (failed) {
    replaceChildren(ui.count);
    replaceChildren(ui.events, notice("site.results.load_error", { alert: true }));
    setListed([]);
    return;
  }
  if (model === null) {
    replaceChildren(ui.events, notice("site.meta.loading"));
    setListed([]);
    return;
  }
  const now = new Date();
  const lang = currentLang();
  const tz = model.zone;
  const today = tz.dayKey(now);
  const upcoming = model.events.filter((event) => isUpcoming(event, now, today));
  const shown = upcoming.filter((event) => matches(event, filters, today));
  const notEnded = new Set(upcoming.map((event) => event.uid));
  replaceChildren(ui.count, plural("site.results.count", shown.length));
  if (shown.length === 0) {
    replaceChildren(ui.events, notice("site.results.empty"));
    setListed(shown, notEnded);
    return;
  }
  const positions = new Map(shown.map((event, index) => [event, index + 1]));
  const days = [...groupByDay(shown, today)].map(([day, events], index) => {
    const dayId = `day-${index + 1}`;
    return h(
      "section",
      { class: "day", "data-day": day },
      h("h2", { class: "day-heading", id: dayId }, tz.formatDay(day, lang, today)),
      events.map((event) => {
        const position = positions.get(event);
        const ids = { title: `event-title-${position}`, when: `event-when-${position}`, day: dayId };
        return renderEvent(event, tz, lang, today, ids);
      }),
    );
  });
  replaceChildren(ui.events, days);
  setListed(shown, notEnded);
}

function renderMeta() {
  const { model } = view;
  if (model === null) {
    replaceChildren(ui.updated);
    replaceChildren(ui.timezone);
    return;
  }
  const lang = currentLang();
  replaceChildren(
    ui.updated,
    model.generatedAt === null
      ? null
      : parts("site.meta.updated", {
          time: h(
            "time",
            { datetime: model.generatedAt.toISOString() },
            model.zone.formatMoment(model.generatedAt, lang),
          ),
        }),
  );
  const zoneName = model.zone.label(lang, model.generatedAt ?? new Date());
  replaceChildren(ui.timezone, t("site.meta.timezone", { timezone: zoneName }));
}

function render() {
  renderMeta();
  buildControls();
  syncControls();
  renderList();
  renderNote();
  renderSubscribe(view.model?.feeds ?? [], { steps: ui.steps, list: ui.feeds });
}

async function main() {
  // Handled at once, so a failed request is never an unhandled rejection.
  const loading = fetchJson("events.json").then(
    (payload) => ({ payload, error: null }),
    (error) => ({ payload: null, error }),
  );
  // Connected and showing the filters of the address before the messages arrive, so
  // nothing typed or ticked meanwhile is lost.
  connectControls();
  syncControls();
  watchBar();
  watchDock();
  const refresh = await startPage(render);
  try {
    const { payload, error } = await loading;
    if (error !== null) {
      throw error;
    }
    // Text typed while loading goes to the address now, as the list below shows it.
    pendingSearch.flush();
    const model = prepareModel(payload, new Date());
    view = Object.freeze({
      ...view,
      model,
      failed: false,
      filters: withinChoices(view.filters, model.choices),
    });
  } catch (error) {
    reportError(error);
    view = Object.freeze({ ...view, failed: true });
  }
  refresh();
}

main().catch(reportError);
