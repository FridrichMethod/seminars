// What both pages share: loading the messages, the language switch, the navigation,
// reading the JSON payloads and small building blocks of the page.

import { h, replaceChildren } from "./dom.js";
import {
  availableLangs,
  chooseLang,
  currentLang,
  loadCatalogs,
  messageIn,
  t,
  translate,
  useLang,
  withLang,
} from "./i18n.js";

/** The element with ``id``; a missing element is a bug of the page, so it throws. */
export function byId(id) {
  const node = document.getElementById(id);
  if (node === null) {
    throw new Error(`the page has no element #${id}`);
  }
  return node;
}

/** The parsed JSON at ``path`` on this site. */
export async function fetchJson(path) {
  const response = await fetch(path, { cache: "no-cache" });
  if (!response.ok) {
    throw new Error(`${path}: HTTP status ${response.status}`);
  }
  const value = await response.json();
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${path}: not a JSON object`);
  }
  return value;
}

/** Show ``params`` in the address bar without adding a history entry. */
export function replaceQuery(params) {
  const query = params.toString();
  const { pathname, hash } = window.location;
  window.history.replaceState(null, "", `${pathname}${query ? "?" : ""}${query}${hash}`);
}

/** A term and its description for a <dl>, or nothing when there is no value. */
export function fact(labelKey, value) {
  if (value === null || value === undefined || value === "") {
    return null;
  }
  return [h("dt", {}, t(labelKey)), h("dd", {}, value)];
}

/** A notice in place of a list (loading, empty, failed). */
export function notice(key, { alert = false } = {}) {
  return h("p", { class: "notice", role: alert ? "alert" : null }, t(key));
}

function updateLanguageSwitch(container, choose) {
  const buttons = availableLangs().map((lang) =>
    h(
      "button",
      {
        type: "button",
        class: "lang",
        lang,
        "aria-pressed": String(lang === currentLang()),
        on: { click: () => choose(lang) },
      },
      messageIn(lang, "site.lang.name"),
    ),
  );
  replaceChildren(container, buttons);
}

function updateNavigation() {
  for (const anchor of document.querySelectorAll("a[data-keep-lang]")) {
    const target = new URL(anchor.getAttribute("href"), window.location.href);
    target.search = withLang(target.search, currentLang()).toString();
    anchor.href = target.href;
  }
}

/**
 * Start a page: load the messages, pick the language (?lang=, then the remembered
 * choice, then English), translate the markup and call ``render``. ``render`` runs
 * again after every language change and must rebuild everything it made.
 */
export async function startPage(render) {
  await loadCatalogs();
  useLang(chooseLang(window.location.search), { remember: false });
  const container = byId("lang-switch");
  const refresh = () => {
    translate();
    updateLanguageSwitch(container, choose);
    updateNavigation();
    render();
  };
  const choose = (lang) => {
    useLang(lang, { remember: true });
    replaceQuery(withLang(window.location.search, lang));
    refresh();
    container.querySelector("[aria-pressed=true]")?.focus();
  };
  refresh();
  return refresh;
}

/** Log a failure of a page script; the page shows its own notice. */
export function reportError(error) {
  console.error(error);
}
