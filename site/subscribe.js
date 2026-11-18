// The subscription panel: every calendar feed with a calendar app (webcal) link and a
// web (http or https) link, each with a copy button, and the Apple Calendar steps.
// Below the event feeds of the payload comes the feed of the deadlines, which the
// backend writes next to them on every run and every build of the site
// (seminars.outputs.ics); it is no feed id, so the payload does not list it.

import { h, link, replaceChildren, siteUrl } from "./dom.js";
import { children, parts, t } from "./i18n.js";

const FEED_PATH = /^feeds\/[a-z0-9]+(?:-[a-z0-9]+)*\.ics$/;
const NOTE_MS = 4000;
const DEADLINES_FEED = Object.freeze({ id: "deadlines", path: "feeds/deadlines.ics" });

/** The two addresses of the feed at ``path``: calendar app (webcal) and web. */
export function feedAddresses(path) {
  const web = new URL(siteUrl(path));
  return Object.freeze({ calendar: `webcal://${web.host}${web.pathname}`, web: web.href });
}

/** The feeds of the payload that the site knows and can link to, in payload order. */
export function knownFeeds(feeds) {
  const ids = children("feeds.");
  return (Array.isArray(feeds) ? feeds : []).filter(
    (feed) =>
      feed !== null &&
      typeof feed === "object" &&
      ids.includes(feed.id) &&
      typeof feed.path === "string" &&
      FEED_PATH.test(feed.path),
  );
}

async function copy(value, note) {
  try {
    await navigator.clipboard.writeText(value);
    replaceChildren(note, t("site.subscribe.copied"));
  } catch {
    // no clipboard (insecure page, permission refused): the address can be selected
    replaceChildren(note, t("site.subscribe.copy_failed"));
  }
  window.setTimeout(() => replaceChildren(note), NOTE_MS);
}

function addressRow(kindKey, address, feedName) {
  const kind = t(kindKey);
  const anchor = h("a", { class: "feed-address" }, address);
  anchor.href = address;
  const note = h("span", { class: "copy-note", role: "status" });
  const button = h(
    "button",
    {
      type: "button",
      class: "copy",
      "aria-label": t("site.subscribe.copy_label", { kind, feed: feedName }),
      on: { click: () => copy(address, note) },
    },
    t("site.subscribe.copy"),
  );
  return h("div", { class: "feed-row" }, h("span", { class: "feed-kind" }, kind), anchor, button, note);
}

function feedItem(feed, { name, description, notes = [] }) {
  const addresses = feedAddresses(feed.path);
  return h(
    "li",
    { class: "feed", "data-feed": feed.id },
    h("p", { class: "feed-name" }, name),
    h("p", { class: "feed-description" }, description),
    notes.map((note) => h("p", { class: "feed-description feed-note" }, note)),
    addressRow("site.subscribe.calendar_link", addresses.calendar, name),
    addressRow("site.subscribe.web_link", addresses.web, name),
  );
}

function eventFeedItem(feed) {
  return feedItem(feed, {
    name: t("feeds." + feed.id + ".name"),
    description: t("feeds." + feed.id + ".description"),
  });
}

/**
 * The feed of the deadlines, with the sentence that it is kept by hand and that the
 * deadlines of the main tracks of machine learning conferences are in ccfddl's
 * calendar. The address of that calendar is a message of the locale files, since no
 * file of the site holds an address of another host, and becomes a link only when it
 * is an http(s) address (link()).
 */
function deadlineFeedItem() {
  const ccfddl = link(t("deadlines.ccfddl.url"), {}, t("deadlines.ccfddl.name"));
  return feedItem(DEADLINES_FEED, {
    name: t("deadlines.feed.name"),
    description: t("deadlines.feed.description"),
    notes: [parts("site.subscribe.deadlines_note", { ccfddl })],
  });
}

/**
 * Fill the Apple Calendar steps into ``steps``, the event feeds into ``list`` and the
 * feed of the deadlines into ``deadlines``.
 */
export function renderSubscribe(feeds, { steps, list, deadlines }) {
  replaceChildren(
    steps,
    children("site.subscribe.apple.steps.").map((step) =>
      h("li", {}, t("site.subscribe.apple.steps." + step)),
    ),
  );
  replaceChildren(list, knownFeeds(feeds).map(eventFeedItem));
  replaceChildren(deadlines, deadlineFeedItem());
}
