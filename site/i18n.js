// Message catalogs of the site: loads locales/<lang>.json and fills messages into the page.
//
// This module holds no interface text. Every visible string comes from the locale
// files, whose format is described in the Python module seminars.i18n: nested
// objects with string leaves, addressed by dotted keys, with {name} placeholders.
// Pages name keys in data-i18n (text) and data-i18n-attr ("attr:key;attr:key");
// scripts call t(), parts() or plural().

export const LANGS = Object.freeze(["en", "zh-CN"]);
export const DEFAULT_LANG = "en";

const URL_PARAM = "lang";
const URL_VALUES = Object.freeze({ en: "en", "zh-CN": "zh" });
const STORAGE_KEY = "seminars.lang";
const PLACEHOLDER = /\{([a-z][a-z0-9_]*)\}/g;
const ATTRIBUTES = Object.freeze(["aria-label", "placeholder", "title"]);

const catalogs = new Map();
const reported = new Set();
let active = DEFAULT_LANG;

/** Nested locale objects as a Map of dotted key -> message (non-string leaves are dropped). */
export function flatten(tree, prefix = "", into = new Map()) {
  for (const [name, value] of Object.entries(tree)) {
    const key = prefix + name;
    if (typeof value === "string") {
      into.set(key, value);
    } else if (value !== null && typeof value === "object" && !Array.isArray(value)) {
      flatten(value, `${key}.`, into);
    }
  }
  return into;
}

async function fetchCatalog(lang) {
  const response = await fetch(`locales/${lang}.json`, { cache: "no-cache" });
  if (!response.ok) {
    throw new Error(`locales/${lang}.json: HTTP status ${response.status}`);
  }
  const tree = await response.json();
  if (tree === null || typeof tree !== "object" || Array.isArray(tree)) {
    throw new Error(`locales/${lang}.json: not a JSON object`);
  }
  return flatten(tree);
}

/** Load every language; only the default language is required. */
export async function loadCatalogs() {
  const results = await Promise.allSettled(LANGS.map((lang) => fetchCatalog(lang)));
  results.forEach((result, index) => {
    if (result.status === "fulfilled") {
      catalogs.set(LANGS[index], result.value);
    } else {
      console.error(result.reason);
    }
  });
  if (!catalogs.has(DEFAULT_LANG)) {
    throw new Error(`the ${DEFAULT_LANG} catalog could not be loaded`);
  }
}

export function availableLangs() {
  return LANGS.filter((lang) => catalogs.has(lang));
}

export function currentLang() {
  return active;
}

/** True when the active language has a message for ``key``. */
export function has(key) {
  return catalogs.get(active)?.has(key) ?? false;
}

function report(lang, key, problem) {
  const id = `${lang} ${key} ${problem}`;
  if (!reported.has(id)) {
    reported.add(id);
    console.error(`locale ${lang}: ${problem}: ${key}`);
  }
}

function lookup(lang, key) {
  const message = catalogs.get(lang)?.get(key);
  if (message === undefined) {
    report(lang, key, "missing message");
  }
  return message;
}

/**
 * The message for ``key`` as a list of strings and nodes. A placeholder whose value
 * is a DOM node keeps the node, so markup such as <time> can sit inside a message
 * without any HTML parsing. A missing message gives the key itself, which makes the
 * gap visible (and it is reported on the console).
 */
export function partsIn(lang, key, params = {}) {
  const message = lookup(lang, key);
  if (message === undefined) {
    return [key];
  }
  const pieces = [];
  let last = 0;
  for (const match of message.matchAll(PLACEHOLDER)) {
    if (match.index > last) {
      pieces.push(message.slice(last, match.index));
    }
    const name = match[1];
    if (Object.hasOwn(params, name)) {
      const value = params[name];
      pieces.push(value instanceof Node ? value : String(value));
    } else {
      report(lang, key, `no value for {${name}}`);
      pieces.push(match[0]);
    }
    last = match.index + match[0].length;
  }
  if (last < message.length) {
    pieces.push(message.slice(last));
  }
  return pieces;
}

export function parts(key, params = {}) {
  return partsIn(active, key, params);
}

/** The message for ``key`` in ``lang`` as text, placeholders filled in. */
export function messageIn(lang, key, params = {}) {
  return partsIn(lang, key, params)
    .map((piece) => (typeof piece === "string" ? piece : piece.textContent))
    .join("");
}

export function t(key, params = {}) {
  return messageIn(active, key, params);
}

export function formatNumber(value) {
  return new Intl.NumberFormat(active).format(value);
}

/** ``value`` with its sign, such as "+3" or "-1.5" (never "-0"). */
export function formatSigned(value) {
  const options = { signDisplay: "always", maximumSignificantDigits: 6 };
  return new Intl.NumberFormat(active, options).format(value + 0);
}

/** ``<base>_one`` or ``<base>_other``, chosen by the plural rules of the language. */
export function plural(base, count, params = {}) {
  const category = new Intl.PluralRules(active).select(count);
  const key = has(`${base}_${category}`) ? `${base}_${category}` : `${base}_other`;
  return t(key, { ...params, count: formatNumber(count) });
}

/** Names directly below ``prefix`` (which ends with "."), in the order of the file. */
export function children(prefix) {
  const names = [];
  for (const key of catalogs.get(DEFAULT_LANG)?.keys() ?? []) {
    if (key.startsWith(prefix)) {
      const name = key.slice(prefix.length).split(".")[0];
      if (name !== "" && !names.includes(name)) {
        names.push(name);
      }
    }
  }
  return names;
}

/** Fill in every data-i18n and data-i18n-attr below ``root`` in the active language. */
export function translate(root = document) {
  for (const node of root.querySelectorAll("[data-i18n]")) {
    node.textContent = t(node.dataset.i18n);
  }
  for (const node of root.querySelectorAll("[data-i18n-attr]")) {
    for (const pair of node.dataset.i18nAttr.split(";")) {
      const [name, key] = pair.split(":").map((part) => part.trim());
      if (ATTRIBUTES.includes(name) && key) {
        node.setAttribute(name, t(key));
      }
    }
  }
}

/** The language named by ?lang= ("en", "zh", "zh-CN"; any case), or null. */
export function langFromQuery(search) {
  const value = (new URLSearchParams(search).get(URL_PARAM) ?? "").trim().toLowerCase();
  return LANGS.find((lang) => lang.toLowerCase() === value || URL_VALUES[lang] === value) ?? null;
}

function storedLang() {
  try {
    return window.localStorage.getItem(STORAGE_KEY);
  } catch {
    return null; // storage blocked or unavailable: fall back to the default
  }
}

function storeLang(lang) {
  try {
    window.localStorage.setItem(STORAGE_KEY, lang);
  } catch {
    // storage blocked or unavailable: the choice lasts until the page is left
  }
}

/** The language to start with: ?lang=, then the remembered choice, then English. */
export function chooseLang(search) {
  const wanted = langFromQuery(search) ?? storedLang();
  return catalogs.has(wanted) ? wanted : DEFAULT_LANG;
}

/** Make ``lang`` the active language and set <html lang>; remember it when asked to. */
export function useLang(lang, { remember }) {
  active = catalogs.has(lang) ? lang : DEFAULT_LANG;
  document.documentElement.lang = active;
  if (remember) {
    storeLang(active);
  }
  return active;
}

/** ``search`` with the lang parameter for ``lang`` (none for the default language). */
export function withLang(search, lang) {
  const params = new URLSearchParams(search);
  if (lang === DEFAULT_LANG) {
    params.delete(URL_PARAM);
  } else {
    params.set(URL_PARAM, URL_VALUES[lang]);
  }
  return params;
}
