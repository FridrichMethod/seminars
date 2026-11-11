// Building the page from untrusted data.
//
// Every value in events.json and status.json comes from scraped web pages. It reaches
// the page only as text nodes and attribute values through the helpers below; nothing
// here parses HTML. Links are made by link() only, which accepts http(s) addresses and
// nothing else (no script, data or other schemes), and marks them rel="noopener
// noreferrer" because they lead to other sites.

const EXTERNAL_REL = "noopener noreferrer";
const WEB_PROTOCOLS = Object.freeze(["http:", "https:"]);
// Attributes that load or run something (all src* included); h() refuses them, so that
// only link() and siteLink() set an address.
const UNSAFE_ATTRIBUTE = /^(on|href$|src|style$|action$|formaction$|xlink:)/i;

function appendChildren(node, children) {
  for (const child of children) {
    if (child === null || child === undefined || child === false) {
      continue;
    }
    if (Array.isArray(child)) {
      appendChildren(node, child);
    } else if (child instanceof Node) {
      node.appendChild(child);
    } else {
      node.appendChild(document.createTextNode(String(child)));
    }
  }
}

/**
 * A new element. ``props`` holds attributes (``class``, ``id``, ``aria-*``, ``data-*``,
 * ...), ``on`` (an object of event listeners) and ``hidden``; children are nodes or text.
 * Text always becomes a text node, never markup.
 */
export function h(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(props)) {
    if (value === null || value === undefined || value === false) {
      continue;
    }
    if (name === "on") {
      for (const [type, listener] of Object.entries(value)) {
        node.addEventListener(type, listener);
      }
    } else if (UNSAFE_ATTRIBUTE.test(name)) {
      throw new Error(`h() does not set the attribute ${name}; use link()`);
    } else {
      node.setAttribute(name, value === true ? "" : String(value));
    }
  }
  appendChildren(node, children);
  return node;
}

/** Remove every child of ``node`` and append ``children`` instead. */
export function replaceChildren(node, ...children) {
  while (node.firstChild) {
    node.removeChild(node.firstChild);
  }
  appendChildren(node, children);
  return node;
}

/**
 * ``value`` as an absolute http(s) address, or null for anything else: other schemes,
 * relative addresses, user names or passwords in the address, non-strings.
 */
export function webUrl(value) {
  if (typeof value !== "string" || value.trim() !== value || value === "") {
    return null;
  }
  let url;
  try {
    url = new URL(value);
  } catch {
    return null;
  }
  if (!WEB_PROTOCOLS.includes(url.protocol) || url.username || url.password || !url.hostname) {
    return null;
  }
  return url.href;
}

/**
 * A link to another site when ``value`` is an http(s) address, otherwise the same
 * children as plain text in a span: a bad address never becomes a link.
 */
export function link(value, props = {}, ...children) {
  const href = webUrl(value);
  if (href === null) {
    return h("span", props, ...children);
  }
  const anchor = h("a", { ...props, rel: EXTERNAL_REL }, ...children);
  anchor.href = href;
  return anchor;
}

/** A link to a page or file of this site (``path`` is relative to the current page). */
export function siteLink(path, props = {}, ...children) {
  const anchor = h("a", props, ...children);
  anchor.href = siteUrl(path);
  return anchor;
}

/** Absolute address of ``path`` on this site; anything that leaves the site is refused. */
export function siteUrl(path) {
  const url = new URL(String(path), document.baseURI);
  if (url.origin !== window.location.origin) {
    throw new Error("a site path must stay on this site");
  }
  return url.href;
}

/** Text of an untrusted value: strings as they are, anything else as nothing. */
export function text(value) {
  return typeof value === "string" ? value : "";
}
