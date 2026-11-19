#!/usr/bin/env node
/* Headless check of the shipped page UI: index.html's inline script is run in a
   stub DOM and driven through the same mergeRun()/render() path the poll uses.

   What it proves, and why each one is here:
   - the 1:1 toggle is gone and nothing still calls /api/one-to-one;
   - a verified count and a not-yet-verified count are shown as two different
     things (counter, counts row, pill, progress band), on the current payload
     shape and on the older one that carries no `pending` - where the number is
     taken from the rows' own states, never from source-minus-outcomes, and no
     such row reads as an explicit 0; nothing calls those rows "sent", because
     the server's `pending` also holds queued, in-flight and failed adds;
   - a terminal line never claims success over rows the read did not confirm;
   - the entry count a playlist read reported (`playlist_count`) is shown as its own
     fact - on both terminal lines, in the counts row and in the counter title - beside
     the matched plan and the verified adds, and never folded into either; it is
     worded as what the read observed, never as the playlist's total, because the
     backend fills it from any finished read and the payload carries no completeness
     flag; a count the payload did not serve is left unsaid rather than printed as
     zero; a reported 401 is repeated as that one fact - never as a cause, never with a
     reconnect or a retry - and prose that only sounds like a login problem never becomes
     a 401; the absence of a status is named as unreported instead of guessed;
   - an interrupted record the server marks not resumable (`resumable:false` or a
     `blocked` reason) never reads as an offer to resume: the pin names the refusal,
     repeats the reason served, points at checking the linked playlist by hand, and
     recommends neither deleting the playlist nor resetting the run;
   - the ledger's sort, tally, missing list and follow index are memoised per
     row revision: repeated paints and filter changes do not rescans a large
     playlist, and one arriving row does;
   - a verified jump (a slice read back at once) does not blank the rate/ETA,
     while a count that goes backwards does.
   - the playlist preview - the page's one full-track read - is not given the
     transfer's 25 s budget, says so when it cannot answer instead of dropping
     the failure, still points at Ctrl+Enter, and does not read the same link
     twice (remembered, in flight, or answering late over a live run);
   - the running version is the server's own: /health's `version` is the one
     painted, no copy of backend.py's APP_VERSION lives in the page (the stub
     serves a version no shipped file contains, so a hardcoded one cannot pass),
     and a read that fails - no answer, a non-2xx, a body with no version - says
     the version is unread instead of leaving a plausible number on screen;
   - the two sheets behave as the modals they declare: opening one moves focus
     into its card, the page behind it is made inert (not merely covered), Tab and
     Shift+Tab stay inside it, a Tab arriving while focus sits behind the overlay is
     pulled back, closing returns focus to the control that opened it (or the card's
     own control when that one is now hidden), and the ledger sits inside the page's
     one main landmark. No progressbar role is claimed anywhere: the run's progress
     is several phases, not one percentage;
   - Clear and New transfer report what the server actually did. /api/reset clears
     the run and the created-playlist record in two steps, so 200 (both gone), 409
     (nothing cleared) and 500 (run cleared, record kept) are three different
     outcomes: a confirmed reset clears the screen, a refused one clears nothing and
     re-attaches, a partial one clears the run and states the record fact rather than
     showing a fresh-looking idle page, and a reset that never got an answer is never
     reported as done, with the server's own next read deciding what is still there.
     Any reset that ends unresolved then locks the session - no reset and no start, for
     any source, because an empty-id reset clears every record and a per-source guard
     could be replaced - and the lock says it is in memory for this session only: a
     reload clears it and settles nothing, so it neither restores nor clears the record;

   No network, no browser, no dependencies. Run: node tests/check_ui.mjs
   (exit 0 only when every check passes). */
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import {fileURLToPath} from "node:url";

const ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
/* Optional argument: check a specific copy of the page (used to prove a check
   goes red against an older file). Default: the shipped index.html. */
const TARGET = process.argv[2] ? path.resolve(process.argv[2]) : path.join(ROOT, "index.html");
const html = fs.readFileSync(TARGET, "utf8");
const found = /<script>([\s\S]*?)<\/script>/.exec(html);
if (!found) {
  console.error("UI RED - no inline script in index.html");
  process.exit(1);
}
const script = found[1];

let fails = 0;
let count = 0;
function ok(name, cond, detail = "") {
  count += 1;
  if (cond) {
    console.log("pass " + name);
    return;
  }
  fails += 1;
  console.log("FAIL " + name + (detail ? "  <- " + detail : ""));
}


const stripTags = (s) => s.replace(/<[^>]*>/g, "");

function makeStyle() {
  const props = new Map();
  return {
    setProperty(k, v) { props.set(k, String(v)); },
    getPropertyValue(k) { return props.has(k) ? props.get(k) : ""; },
    removeProperty(k) { props.delete(k); },
  };
}

function makeClassList(el) {
  return {
    add(...names) { for (const n of names) el._classes.add(n); },
    remove(...names) { for (const n of names) el._classes.delete(n); },
    contains(name) { return el._classes.has(name); },
    toggle(name, on) {
      const want = on === undefined ? !el._classes.has(name) : !!on;
      if (want) el._classes.add(name); else el._classes.delete(name);
      return want;
    },
  };
}

class StubEl {
  constructor(doc, tag = "div", id = "") {
    this.ownerDocument = doc;
    this.tagName = String(tag).toUpperCase();
    this.id = id;
    this.children = [];
    this.parent = null;
    this._text = "";
    this._html = null;
    this._classes = new Set();
    this.dataset = {};
    this.style = makeStyle();
    this._attrs = new Map();
    this._listeners = new Map();
    this._fragment = false;
    this.hidden = false;
    this.disabled = false;
    this.title = "";
    this.value = "";
    this.href = "";
    this.files = null;
    this.scrollTop = 0;
    this.scrollHeight = 0;
    this.clientHeight = 392;
    this.offsetWidth = 0;
    this.classList = makeClassList(this);
  }
  get className() { return [...this._classes].join(" "); }
  set className(v) {
    this._classes = new Set(String(v || "").split(/\s+/).filter(Boolean));
    this.classList = makeClassList(this);
  }
  get textContent() {
    if (this.children.length) return this.children.map((c) => c.textContent).join("");
    if (this._html != null) return stripTags(this._html);
    return this._text;
  }
  set textContent(v) {
    this._text = String(v);
    this._html = null;
    this.children = [];
  }
  get innerHTML() { return this._html == null ? "" : this._html; }
  set innerHTML(v) {
    this._html = String(v);
    this._text = "";
    this.children = [];
  }
  append(...kids) {
    for (const kid of kids) {
      if (!kid) continue;
      if (kid._fragment) {
        for (const c of kid.children.slice()) this.append(c);
        continue;
      }
      this.children.push(kid);
      kid.parent = this;
    }
    if (this.children.length) this._text = "";
  }
  appendChild(kid) { this.append(kid); return kid; }
  remove() {
    if (this.parent) {
      const at = this.parent.children.indexOf(this);
      if (at >= 0) this.parent.children.splice(at, 1);
    }
    this.parent = null;
  }
  setAttribute(k, v) {
    this._attrs.set(k, String(v));
    if (k === "id") this.id = String(v);
    if (k === "hidden") this.hidden = true;
    if (k === "disabled") this.disabled = true;
    if (k === "href") this.href = String(v);
  }
  getAttribute(k) {
    return this._attrs.has(k) ? this._attrs.get(k) : null;
  }
  removeAttribute(k) {
    this._attrs.delete(k);
    if (k === "hidden") this.hidden = false;
    if (k === "disabled") this.disabled = false;
    if (k === "href") this.href = "";
  }
  addEventListener(type, fn) {
    if (!this._listeners.has(type)) this._listeners.set(type, []);
    this._listeners.get(type).push(fn);
  }
  removeEventListener(type, fn) {
    const list = this._listeners.get(type) || [];
    const at = list.indexOf(fn);
    if (at >= 0) list.splice(at, 1);
  }
  /* A browser moves focus for real, and the sheet's whole job is where focus
     lands, so the stub tracks it: focus() is observable through
     document.activeElement, as it is on the page. */
  focus() { this.ownerDocument.focusEl(this); }
  /* Node.contains: true for itself and for anything under it. */
  contains(node) {
    if (!node) return false;
    if (node === this) return true;
    return this.children.some((child) => child.contains(node));
  }
  /* Nothing in the stub is ever detached from a document, so every node it makes
     is connected - which is what the restore path asks before it moves focus. */
  get isConnected() { return true; }
  /* Dispatch one event to this element's own listeners. Bubbling is not modelled,
     so a case that drives a handler attached to an ancestor fires it there. */
  fire(type, ev) {
    for (const fn of this._listeners.get(type) || []) fn(ev);
  }
  click() {
    for (const fn of this._listeners.get("click") || []) {
      fn({preventDefault() {}, stopPropagation() {}});
    }
  }
  querySelector(sel) { return this._query(sel)[0] || null; }
  querySelectorAll(sel) { return this._query(sel); }
  _query(sel) {
    const last = String(sel).trim().split(/\s+/).pop();
    const match = (el) => {
      if (last.startsWith(".")) return el._classes.has(last.slice(1));
      if (last.startsWith("#")) return el.id === last.slice(1);
      return el.tagName === last.toUpperCase();
    };
    const out = [];
    const walk = (node) => {
      for (const child of node.children) {
        if (match(child)) out.push(child);
        walk(child);
      }
    };
    walk(this);
    return out;
  }
}

class StubDoc {
  constructor() {
    this.byId = new Map();
    this.body = new StubEl(this, "body", "body");
    this.listeners = new Map();
    this._active = null;
    /* A browser also exposes every element with an id as a global. The page
       leans on that once (palcard), so the stub does the same instead of
       missing a surface the real page can reach. */
    this.named = () => {};
    this._fbts = ["all", "added", "skipped", "duplicate", "refused", "notverified", "pending"]
      .map((f) => {
        const b = new StubEl(this, "button", "");
        b.className = "fbtn";
        b.dataset.f = f;
        return b;
      });
    this._privs = ["PUBLIC", "UNLISTED", "PRIVATE"].map((p) => {
      const b = new StubEl(this, "button", "");
      b.dataset.priv = p;
      return b;
    });
  }
  getElementById(id) {
    let el = this.byId.get(id);
    if (!el) {
      el = new StubEl(this, "div", id);
      this.byId.set(id, el);
      this.named(id, el);
    }
    return el;
  }
  createElement(tag) { return new StubEl(this, tag, ""); }
  createDocumentFragment() {
    const frag = new StubEl(this, "#fragment", "");
    frag._fragment = true;
    return frag;
  }
  querySelectorAll(sel) {
    if (sel === ".fbtn") return this._fbts;
    if (sel === ".privseg button") return this._privs;
    return [];
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  addEventListener(type, fn) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(fn);
  }
  removeEventListener() {}
  fire(type, ev) {
    for (const fn of this.listeners.get(type) || []) fn(ev);
  }
  focusEl(el) { this._active = el; }
  get activeElement() { return this._active || null; }
}

class StubObserver {
  constructor(cb) { this.cb = cb; }
  observe() {}
  disconnect() {}
}

class StubBlob {
  constructor(parts = []) { this.parts = parts; }
}

const requests = [];
/* /health is the page's running-version read, decided per case. The default answer
   is a version no shipped file contains, so a page that hardcodes its own number
   cannot pass by accident. */
const healthOk = () => jsonResponse({ok: true, version: "9.9.9-test"});
let healthReply = healthOk;
let healthHits = 0;
/* The playlist preview is the one route whose answer each case decides for
   itself: it is the page's only full-track read, and its timing is the point. */
let spotifyReply = null;
let spotifyHits = 0;
/* The transfer start, decided per case: a fresh run must not inherit counts. */
let transferReply = null;
/* /api/run is read after a reset the page could not confirm, so its answer is decided
   per case too; the default is an idle, empty run. */
let runReply = () => jsonResponse({ok: true, seq: 0});
/* Retry now is a request followed by a read, so its answer is decided per case: the race case
   holds it open across a detach. */
let retryReply = () => jsonResponse({ok: false});
/* /api/reset answers with a real status, because the page must tell 200, 409 and 500
   apart, and the body it sent is kept so a case can prove which record was cleared. */
const http = (status, body) => ({ok: status >= 200 && status < 300, status,
  json: async () => body, text: async () => JSON.stringify(body)});
let resetReply = () => http(200, {ok: true});
const resets = [];
function jsonResponse(payload) {
  return {ok: true, json: async () => payload, text: async () => JSON.stringify(payload)};
}
async function stubFetch(url, opts) {
  const at = String(url);
  requests.push(at);
  if (at === "/health") {
    healthHits += 1;
    return healthReply(at);
  }
  if (at.startsWith("/api/reset")) {
    try { resets.push(opts && opts.body ? JSON.parse(opts.body) : {}); } catch (e) { resets.push({}); }
    return resetReply(at);
  }
  if (at.startsWith("/api/spotify")) {
    spotifyHits += 1;
    if (spotifyReply) return spotifyReply(at);
    return jsonResponse({ok: false, error: "no stub for this case"});
  }
  if (at.startsWith("/api/settings")) return jsonResponse({ok: true});
  if (at.startsWith("/api/cookies")) return jsonResponse({ok: true, yt: {state: "none", has: false}});
  if (at.startsWith("/api/transfer")) {
    return transferReply ? transferReply() : jsonResponse({ok: false, error: "no stub for this case"});
  }
  if (at.startsWith("/api/run")) return runReply(at);
  if (at.startsWith("/api/retry")) return retryReply(at);
  if (at.startsWith("/api/watch")) return jsonResponse({ok: false});
  return jsonResponse({ok: false});
}

const doc = new StubDoc();
/* The stub cannot parse markup, so body's own children and the two modal sheets are
   built to the same shape and order index.html ships: the bar, the deck, the action
   bar, then the palette and the sheets as siblings. That sibling set is what the page
   inerts, so the stub has to have it, and the cards carry the shipped order (field,
   then footer buttons) because focus containment is only provable against it. */
function seedSheets(d) {
  const make = (tag, id, cls) => {
    const n = d.createElement(tag);
    if (id) { n.id = id; d.byId.set(id, n); }
    if (cls) n.className = cls;
    return n;
  };
  const row = (parent, cls, kids) => {
    const box = make("div", "", cls);
    box.append(...kids);
    parent.append(box);
  };
  const paste = make("div", "pastesheet", "sheet");
  const more = make("div", "moresheet", "sheet");
  d.body.append(make("header", "", "topbar"), make("div", "", "deck"),
    make("footer", "", "actionbar"), make("div", "cmdpal", ""), paste, more);
  const pcard = make("div", "", "sheetcard");
  paste.append(pcard);
  pcard.append(make("h2", "pastetitle", "sheettitle"), make("p", "", "sheetline"),
    make("textarea", "pasteinput", ""));
  row(pcard, "sheetfoot", [make("button", "pastecancel", "acctbtn quiet"),
    make("button", "pastesave", "acctbtn pri")]);
  const mcard = make("div", "", "sheetcard");
  more.append(mcard);
  mcard.append(make("h2", "moretitle", "sheettitle"));
  row(mcard, "minirow", [make("input", "watchdir", ""),
    make("button", "watchbtn", "acctbtn quiet")]);
  mcard.append(make("div", "filelist", ""));
  row(mcard, "minirow", [make("button", "copybtn", "acctbtn quiet")]);
  row(mcard, "sheetfoot", [make("button", "moreclose", "acctbtn")]);
}
/* The nested spans the script queries by class are in the shipped markup; the
   stub cannot parse HTML, so it seeds the ones the page reaches for. */
function seed(d) {
  const pill = d.getElementById("statepill");
  for (const cls of ["pdot", "ptxt"]) {
    const s = d.createElement("span");
    s.className = cls;
    pill.append(s);
  }
  const miss = d.getElementById("missbtn");
  for (const cls of ["misslab", "missdone"]) {
    const s = d.createElement("span");
    s.className = cls;
    miss.append(s);
  }
  d.getElementById("missbtn").querySelector(".misslab").textContent = "Copy";
  seedSheets(d);
}
seed(doc);
/* Every key the page writes to localStorage, so a case can prove what it does NOT persist:
   the unresolved-reset lock must live in memory only, and nothing about a playlist may be
   stored where a reload could read it back as a fact. */
const stored = [];
const ctx = {
  document: doc,
  localStorage: {getItem: () => null, setItem(k) { stored.push(String(k)); }, removeItem() {}},
  navigator: {},
  fetch: stubFetch,
  MutationObserver: StubObserver,
  AbortController,
  setTimeout,
  clearTimeout,
  setInterval,
  clearInterval,
  requestAnimationFrame: (fn) => { fn(0); return 0; },
  URL,
  Blob: StubBlob,
  console,
};
ctx.window = ctx;
ctx.globalThis = ctx;
doc.named = (id, el) => { if (!(id in ctx)) ctx[id] = el; };
vm.createContext(ctx);
vm.runInContext(script, ctx, {filename: "index.html"});
/* the page's load-time GETs are awaited; let their microtasks settle */
await new Promise((r) => setTimeout(r, 0));
await new Promise((r) => setTimeout(r, 0));

const run = (code) => vm.runInContext(code, ctx);
const el = (id) => doc.getElementById(id);
const press = (name) => (el("statepill").querySelector(".ptxt") || {textContent: ""}).textContent;
const chip = (f) => doc.querySelectorAll(".fbtn").find((b) => b.dataset.f === f).textContent;

function tracks(n, state = "pending") {
  return Array.from({length: n}, (_, k) => ({
    i: k + 1,
    title: "Track " + (k + 1),
    artist: "Artist " + (k + 1),
    videoId: "V" + String(k + 1).padStart(10, "0"),
    state,
  }));
}
/* One row per state, indexed so the ledger keeps them apart. */
function mixed(states) {
  return states.map((state, k) => ({
    i: k + 1,
    title: "Track " + (k + 1),
    artist: "Artist " + (k + 1),
    videoId: "M" + String(k + 1).padStart(10, "0"),
    state,
  }));
}
function feed(payload, full = false) {
  run("mergeRun(" + JSON.stringify(payload) + "," + (full ? "true" : "false") + ");render();");
}

/* ---------- 1. the 1:1 toggle is gone ---------- */ok("no 1:1 control in the markup", !html.includes('id="oneone"'), "#oneone button still in index.html");
ok("no 1:1 setting wiring in the script", !/one-?to-?one|oneToOne|one_to_one/i.test(script),
  "the page still reads or writes the one-to-one setting");
ok("nothing posts /api/one-to-one", !script.includes("/api/one-to-one"), "the toggle's endpoint is still called");
ok("the copy-missing button still owns the row's right edge", /#missbtn\{margin-left:auto\}/.test(html),
  "#missbtn lost the placement #oneone used to provide");

/* ---------- 2. verified vs pending, on the current payload ---------- */
run("clearLocal();");
feed({
  run_id: "r1", seq: 1, phase: "append", state: "running", total: 508, declared: 508,
  counts: {source: 508, unique: 480, added: 120, skipped: 0, duplicate: 0,
    unverified: 0, refused: 0, pending: 100},
  rows: tracks(40, "appending"),
}, true);

ok("counter shows only the verified adds", el("bignum").textContent === "120",
  "counter read " + el("bignum").textContent);
ok("counter denominator is the declared total", el("bignum").dataset.den === "508",
  "denominator read " + el("bignum").dataset.den);
ok("counter title says the waiting rows are awaiting verification",
  el("bignum").title.includes("100 awaiting verification"),
  "counter title: " + el("bignum").title);
ok("no label calls the waiting rows sent",
  !/sent/i.test([el("bignum").title, press(), el("phaseline").textContent].join(" ")),
  "a 'sent' claim appeared: " + [el("bignum").title, press(), el("phaseline").textContent].join(" | "));
ok("counts row shows added and pending apart",
  el("breakdown").innerHTML.includes("<b>120</b> added") &&
  el("breakdown").innerHTML.includes("<b>100</b> pending"),
  "counts row: " + el("breakdown").innerHTML);
ok("pill carries the same verified count and the pending count",
  press() === "Adding · 120/508 · 100 pending", "pill read " + press());
ok("pending filter chip counts the waiting rows", chip("pending") === "pending 100",
  "chip read " + chip("pending"));
ok("the pill never wears the success word while live", !press().toLowerCase().includes("done"),
  "pill read " + press());

const fill = parseFloat(el("rule").style.getPropertyValue("--fill"));
const fill2 = parseFloat(el("rule").style.getPropertyValue("--fill2"));
ok("rule fill is the verified share only", Math.abs(fill - 120 / 508 * 100) < 0.01,
  "--fill read " + fill);
ok("rule shows the waiting band behind it, capped at the total",
  fill2 >= fill && Math.abs(fill2 - 220 / 508 * 100) < 0.01 && fill2 <= 100,
  "--fill2 read " + fill2);

/* a run that has sent 500 and proved nothing yet: still no fake success */
feed({
  seq: 2, phase: "append", state: "running", total: 500, declared: 500,
  counts: {source: 500, unique: 500, added: 0, skipped: 0, duplicate: 0,
    unverified: 0, refused: 0, pending: 500},
});
ok("nothing proven stays zero", el("bignum").textContent === "0",
  "counter read " + el("bignum").textContent);
ok("nothing proven is not called done", !press().toLowerCase().includes("done"),
  "pill read " + press());

/* ---------- 3. the older payload shape, with no `pending` field ----------
   Arithmetic over the seven numbers would say 508-120-4 = 384 rows are waiting.
   That remainder also holds rows never sent and rows the payload does not name,
   so the only evidence is the rows: exactly the two states the server folds into
   `pending` (pending, appending) may be counted - never matched, queued or
   unverified - and no such row is an explicit 0. */
run("clearLocal();");
feed({
  run_id: "r2", seq: 3, phase: "append", state: "running", total: 508, declared: 508,
  counts: {source: 508, unique: 480, added: 120, skipped: 4, duplicate: 0,
    unverified: 0, refused: 0},
  rows: mixed(["landed", "landed", "appending", "appending",
    "pending", "pending", "pending", "matched", "queued"]),
});
ok("legacy counts take the waiting number from the row states, not from source-minus-outcomes",
  chip("pending") === "pending 5" && el("breakdown").innerHTML.includes("<b>5</b> pending"),
  "chip " + chip("pending") + " · counts " + el("breakdown").innerHTML);
ok("legacy counts never revive the 384 the arithmetic would invent",
  !el("breakdown").innerHTML.includes("384") && !press().includes("384"),
  "counts " + el("breakdown").innerHTML + " · pill " + press());
ok("legacy counts still show the verified adds", el("bignum").textContent === "120",
  "counter read " + el("bignum").textContent);
ok("the waiting number is called pending on the pill and awaiting verification in the title",
  press() === "Adding · 120/508 · 5 pending" &&
  el("bignum").title.includes("5 awaiting verification"),
  "pill " + press() + " · title " + el("bignum").title);

feed({
  seq: 4,
  counts: {source: 508, unique: 480, added: 120, skipped: 4, duplicate: 0,
    unverified: 0, refused: 0},
  rows: tracks(10, "landed"),
});
ok("a payload with no waiting row reads as an explicit zero",
  chip("pending") === "pending 0" && !el("breakdown").innerHTML.includes("pending"),
  "chip " + chip("pending") + " · counts " + el("breakdown").innerHTML);

/* with no counts at all, the rows are the only ledger there is */
run("clearLocal();");
feed({
  seq: 5, phase: "append", state: "running", total: 3, declared: 3,
  rows: mixed(["landed", "appending", "appending"]),
});
ok("no counts yet: the row states still report the waiting rows",
  el("breakdown").innerHTML.includes("<b>2</b> pending") && chip("pending") === "pending 2",
  "counts " + el("breakdown").innerHTML + " · chip " + chip("pending"));
ok("no counts yet: the counter names them as awaiting verification",
  el("bignum").textContent === "1" && el("bignum").title.includes("2 awaiting verification"),
  "counter " + el("bignum").textContent + " · title " + el("bignum").title);

/* ---------- 4. a terminal line never claims more than the read proved ---------- */
run("clearLocal();");
feed({
  run_id: "r3", seq: 5, phase: "done", state: "done", total: 10, declared: 10,
  counts: {source: 10, unique: 10, added: 8, skipped: 0, duplicate: 0,
    unverified: 2, refused: 0},
  rows: tracks(10, "landed"),
});
ok("done with unverified rows says so", press() === "Done, 2 unverified", "pill read " + press());
ok("done with unverified rows is not green", el("statepill").dataset.t === "warn",
  "tone read " + el("statepill").dataset.t);

feed({
  seq: 6,
  counts: {source: 10, unique: 10, added: 8, skipped: 0, duplicate: 0,
    unverified: 2, refused: 0, pending: 3},
});
ok("rows still waiting at the end are counted as unproven too",
  press() === "Done, 5 unverified", "pill read " + press());

feed({
  seq: 7,
  counts: {source: 10, unique: 10, added: 10, skipped: 0, duplicate: 0,
    unverified: 0, refused: 0},
});
ok("a fully verified run is green", press() === "Done" && el("statepill").dataset.t === "ok",
  "pill " + press() + " · tone " + el("statepill").dataset.t);

/* ---------- 4b. the playlist read's own entry count, kept observational ----------
   The observed run: the source read named 1,037 tracks, the read-back proved 312
   copies, 725 stayed unverified, the append was refused, and the read that finished
   reported 510 entries. Three numbers, three different questions: what the run set
   out to write (the matched plan), what a read proved (added), and what a read saw on
   the playlist (`playlist_count`).

   The count is NOT a total. The backend sets `playlist_count` from whatever read
   finished - a capped or short read included, which is exactly the shape that leaves
   copies unverified - and /api/run serves no completeness flag of any kind (checked
   against the payload keys: phase, state, attempt, next_attempt_at, reason, residual,
   started_at, updated_at, age, spotify_id, title, cover_url, playlist_id, url, created,
   existing, total, declared, summary, matched, sent, landed, attempted, refused,
   skipped, missing, unverified, duplicate, pending, counts, order_ok, playlist_count,
   rows, error, hint, kind - and `order_ok` is a claim about order, not completeness).
   So the page may only report what the read observed, never "the playlist holds N":
   that sentence would claim a total the payload cannot support. This block pins the
   observational wording on both terminal lines, the counts row and the counter title,
   pins that an incomplete read's count is worded the same way, and pins the 401 words to
   the code the payload actually carries - one fact, no cause, no retry. */
run("clearLocal();");
const EMERGENCY = {
  run_id: "r401", seq: 30, phase: "done", state: "gaveup", attempt: 2,
  total: 1037, declared: 1037, matched: 1037, playlist_count: 510,
  reason: "append refused by YouTube Music", error: "", hint: "", kind: "",
  residual: ["V0000000001", "V0000000002"],
  counts: {source: 1037, unique: 1000, added: 312, skipped: 0, duplicate: 0,
    unverified: 725, refused: 0, pending: 0},
  rows: mixed(["landed", "landed", "missing", "missing"]),
};
feed(EMERGENCY, true);
const gaveLine = () => el("phaseline").textContent;
const CLAIMS_TOTAL = /playlist holds|holds 510|the playlist has|playlist total|entries total/i;
ok("a gave-up line reports the entries the read saw",
  /510 entries seen in the playlist/.test(gaveLine()), "line: " + gaveLine());
ok("the matched plan, the verified adds and the read's entries are three phrases",
  /312 added of 1037 matched/.test(gaveLine()) &&
  /510 entries seen in the playlist/.test(gaveLine()) &&
  !/510 added|1037 held/.test(gaveLine()),
  "line: " + gaveLine());
ok("the entry count is never written as a total the payload cannot support",
  !CLAIMS_TOTAL.test(gaveLine() + el("breakdown").textContent + el("bignum").title),
  "claimed a total: " + [gaveLine(), el("breakdown").textContent, el("bignum").title].join(" | "));
ok("the read's entries are compared with this run's own proof, never called extra copies",
  /198 not credited to this run/.test(gaveLine()), "line: " + gaveLine());
ok("the counts row carries the read's entry count beside the verified adds",
  el("breakdown").innerHTML.includes("<b>312</b> added") &&
  el("breakdown").innerHTML.includes("<b>510</b> entries seen") &&
  el("breakdown").innerHTML.includes("<b>725</b> unverified"),
  "counts row: " + el("breakdown").innerHTML);
ok("the counter title carries the read's entry count too",
  el("bignum").title.includes("510 entries seen in the playlist"), "title: " + el("bignum").title);
ok("a payload with no status never claims a 401 or asks for a reconnect",
  !/401|unauthori[sz]ed|reconnect/i.test([gaveLine(), el("breakdown").textContent,
    press()].join(" ")),
  "claimed auth: " + [gaveLine(), el("breakdown").textContent, press()].join(" | "));
ok("a payload with no status says so instead of guessing a cause",
  /no status/i.test(gaveLine()), "line: " + gaveLine());

/* an incomplete read: the count is a floor, and the line must still not imply a total.
   Here more copies are unverified than the read saw entries, which is only possible
   when the read stopped early - the count cannot be a total, yet the wording may not
   change, because nothing in the payload says which kind of read produced it. */
feed({
  seq: 31, playlist_count: 510, tail_unverified: 725,
  counts: {source: 1037, unique: 1000, added: 312, skipped: 0, duplicate: 0,
    unverified: 725, refused: 0, pending: 0},
});
ok("an incomplete read's count is worded as an observation, not a total",
  /510 entries seen in the playlist/.test(gaveLine()) && !CLAIMS_TOTAL.test(gaveLine()),
  "line: " + gaveLine());
ok("an incomplete read is not silently promoted by a payload field it does not carry",
  !/complete|reached the end|whole playlist/i.test(gaveLine()), "line: " + gaveLine());

/* the same refusal, this time with the status the page is allowed to repeat */
feed({
  seq: 32, kind: "auth", hint: "YTM/Spotify login rejected it — cookie expired or wrong site.",
  error: "RuntimeError: append refused (HTTP 401)", reason: "append refused by YouTube Music",
});
ok("an emitted 401 is repeated as what it is", /YouTube Music returned HTTP 401/.test(gaveLine()),
  "line: " + gaveLine());
/* A 401 does not say why. The page may not turn the hint's prose into the cause, because
   the same status is what an expired session, another account and a plain refusal all
   look like from here. */
ok("an emitted 401 names no cause",
  !/cookie|session|login|expired|rejected|account/i.test(gaveLine()),
  "line: " + gaveLine());
/* A write may already have landed, so the advice is to keep the evidence and look before
   anything else writes: no reconnect, no "start again", no delete or reset. */
ok("an emitted 401 keeps the playlist and the record and sends the user to check them",
  /keep the existing playlist and this run's record/i.test(gaveLine()) &&
  /check that playlist in YouTube Music/i.test(gaveLine()) &&
  /before another transfer writes to it/i.test(gaveLine()),
  "line: " + gaveLine());
ok("an emitted 401 offers no reconnect, no retry and no delete",
  !/reconnect|start the transfer again|try again|delete|reset the run|clear the run/i
    .test(gaveLine()),
  "line: " + gaveLine());
ok("an emitted 401 promises no resume the page cannot prove",
  !/same playlist|resumes|no second create/i.test(gaveLine()), "line: " + gaveLine());
ok("an emitted 401 still reports the entries the read saw",
  /510 entries seen in the playlist/.test(gaveLine()), "line: " + gaveLine());

/* prose that sounds like a login problem is not a 401: the backend's own hint can name an
   expired cookie, and the page may not print a status the payload never carried */
feed({
  seq: 34, kind: "auth", hint: "YTM/Spotify login rejected it — cookie expired or wrong site.",
  error: "RuntimeError: append refused", reason: "append refused by YouTube Music",
});
ok("a cause-only payload never invents a 401",
  !/401|unauthori[sz]ed|reconnect|cookie|login/i.test(gaveLine()),
  "line: " + gaveLine());

/* nothing measured is not the same as measured zero */
feed({seq: 33, playlist_count: 0, tail_unverified: 0, kind: "", hint: "", error: ""});
ok("an unmeasured entry count is never printed as zero",
  !/entries seen|playlist holds/.test(gaveLine() + el("breakdown").innerHTML),
  "line: " + gaveLine() + " · counts: " + el("breakdown").innerHTML);

/* a new transfer starts from nothing: the gave-up run's entry count is not its own */
run("clearLocal();");
feed(EMERGENCY, true);
run("S.ytState='connected';$('srcurl').value='https://open.spotify.com/playlist/PLNEXT';");
transferReply = () => jsonResponse({ok: true, run_id: "rnext"});
await run("startTransfer()");
run("unpoll();S.state='running';S.phase='spotify';render();");
ok("a new transfer does not inherit the previous read's entry count",
  run("S.playlist_count") === 0 && run("S.matched") === 0 &&
  !/entries seen|playlist holds/.test(gaveLine() + el("breakdown").innerHTML),
  "line: " + gaveLine() + " · counts: " + el("breakdown").innerHTML +
  " · playlist_count " + run("S.playlist_count") + " · matched " + run("S.matched"));
transferReply = null;
run("unpoll();");

/* a done run that the read never confirmed has the same obligation */
run("clearLocal();");
feed({
  run_id: "r5", seq: 34, phase: "done", state: "done", total: 10, declared: 10,
  matched: 10, playlist_count: 9, reason: "",
  counts: {source: 10, unique: 10, added: 8, skipped: 0, duplicate: 0,
    unverified: 2, refused: 0},
  rows: mixed(["landed", "missing"]),
}, true);
ok("a done-with-unverified line reports the entries the read saw as well",
  /9 entries seen in the playlist/.test(gaveLine()) && !CLAIMS_TOTAL.test(gaveLine()),
  "line: " + gaveLine());
ok("a done-with-unverified line never says the plan is on the playlist",
  !/10 on the playlist/.test(gaveLine()), "line: " + gaveLine());

/* ---------- 5. the large-ledger memo, and its exact invalidation ---------- */
run(["_sortCalls=0;",
  "(function(){var s=Array.prototype.sort;",
  "Array.prototype.sort=function(){_sortCalls++;return s.apply(this,arguments);};})();"].join(""));
run("clearLocal();");
feed({
  run_id: "big", seq: 8, phase: "append", state: "running", total: 5000, declared: 5000,
  counts: {source: 5000, unique: 5000, added: 0, skipped: 0, duplicate: 0,
    unverified: 0, refused: 0, pending: 5000},
  rows: tracks(5000, "pending"),
}, true);
const firstPaintSorts = run("_sortCalls");
ok("the first paint sorts the ledger", firstPaintSorts >= 1, "sorts " + firstPaintSorts);

run("_sortCalls=0;render();render();render();");
ok("repainting unchanged data sorts nothing", run("_sortCalls") === 0,
  "sorts " + run("_sortCalls"));
ok("repainting hands back the same sorted array", run("filtered()===filtered()"),
  "filtered() rebuilt an unchanged ledger");
ok("the tally is memoised with it", run("tally()===tally()"), "tally() rescanned the ledger");
ok("the missing list is memoised with it", run("missingRows()===missingRows()"),
  "missingRows() rescanned the ledger");

run("_sortCalls=0;S.filter='pending';render();");
ok("a filter change reuses the sorted array", run("_sortCalls") === 0,
  "sorts " + run("_sortCalls"));
ok("the filter still returns the right rows", run("filtered().length") === 5000,
  "filtered().length " + run("filtered().length"));

run("_sortCalls=0;");
feed({seq: 9, rows: [{i: 1, title: "Track 1", artist: "Artist 1", videoId: "V0000000001", state: "landed"}]});
ok("one arriving row invalidates the memo", run("_sortCalls") >= 1,
  "sorts " + run("_sortCalls"));
ok("the invalidated filter sees the new state", run("filtered().length") === 4999,
  "filtered().length " + run("filtered().length"));

run("_sortCalls=0;S.filter='all';render();");
ok("switching filter back to all reuses the sorted array, not a re-sort",
  run("_sortCalls") === 0, "sorts " + run("_sortCalls"));
ok("the all-rows view is complete again", run("filtered().length") === 5000,
  "filtered().length " + run("filtered().length"));

run("_sortCalls=0;");
feed({seq: 10, counts: {source: 5000, unique: 5000, added: 1, skipped: 0, duplicate: 0,
  unverified: 0, refused: 0, pending: 4999}});
ok("a count-only poll does not touch the row memo", run("_sortCalls") === 0,
  "sorts " + run("_sortCalls"));

run("clearLocal();");
feed({
  run_id: "rate", seq: 11, phase: "append", state: "running", total: 508, declared: 508,
  counts: {source: 508, unique: 480, added: 20, skipped: 0, duplicate: 0,
    unverified: 0, refused: 0, pending: 80},
});
run("S.hist=[{t:Date.now()-30000,a:20}];S.localT0=Date.now()-40000;window.__t0srv=0;");
feed({
  seq: 12,
  counts: {source: 508, unique: 480, added: 120, skipped: 0, duplicate: 0,
    unverified: 0, refused: 0, pending: 80},
});
ok("a 100-track slice keeps the measured rate", /200\/min/.test(el("phaseline").textContent),
  "line: " + el("phaseline").textContent);
ok("a slice does not blank the ETA", !el("phaseline").textContent.includes("—/min"),
  "line: " + el("phaseline").textContent);
ok("the ETA is measured from the verified count",
  /~\d\d:\d\d left/.test(el("phaseline").textContent),
  "line: " + el("phaseline").textContent);

run("S.hist=[{t:Date.now()-30000,a:200}];");
feed({
  seq: 13,
  counts: {source: 508, unique: 480, added: 50, skipped: 0, duplicate: 0,
    unverified: 0, refused: 0, pending: 80},
});
ok("a count that goes backwards resets the window instead of inventing a rate",
  el("phaseline").textContent.includes("—/min"), "line: " + el("phaseline").textContent);

/* ---------- 7. the playlist preview: the page's one full-track read ----------
   GET-free POST /api/spotify returns every track of the source playlist, so the
   wait grows with the list: measured ~10.5 s at 1,102 tracks, and the partner
   query pages 100 at a time with a pause between pages, so a 2,500-track list
   lands at the 25 s the transfer's requests use. The old code gave the preview
   that same 25 s and then dropped whatever came back - failure and timeout both
   ended in `catch(e){}` - so a big playlist read as a link that never loaded.
   These checks are about the two things that fixes: a preview that cannot
   answer says so and points at the control that still works, and the read is
   not repeated for a link the page already has. */
const PREVIEW_A = "https://open.spotify.com/playlist/PLBIGLIST";
const PREVIEW_B = "https://open.spotify.com/playlist/PLINFLIGHT";
const PREVIEW_C = "https://open.spotify.com/playlist/PLBUDGET";
const PREVIEW_D = "https://open.spotify.com/playlist/PLLATE";
const deferred = () => {
  let res;
  const p = new Promise((r) => { res = r; });
  return {p, res};
};
const setUrl = (u) => run("$('srcurl').value=" + JSON.stringify(u) + ";paintSrcX();");
const readPreview = async () => { await run("autoTitle()"); run("render();"); };
const previewPayload = (name, n) => ({ok: true, playlist_name: name, declared_total: n,
  tracks: Array.from({length: n}, () => ({title: "t"})), cover_url: "https://i.scdn.co/image/" + name});

run("clearLocal();");
setUrl(PREVIEW_A);
spotifyReply = () => jsonResponse({ok: false, error: "RuntimeError: spotify full-list refused (HTTP 429)"});
await readPreview();
ok("a preview the server refused is said out loud, not swallowed",
  /preview could not be read/.test(el("phaseline").textContent),
  "line: " + el("phaseline").textContent);
ok("a refused preview names the server's own reason",
  el("phaseline").textContent.includes("429"), "line: " + el("phaseline").textContent);
ok("a refused preview keeps the route to a transfer visible",
  /Ctrl\+Enter/.test(el("phaseline").textContent), "line: " + el("phaseline").textContent);
ok("a refused preview does not leave the identity line claiming no link was pasted",
  !el("pname").textContent.includes("no playlist yet"), "pname: " + el("pname").textContent);

/* a link whose read simply ran out of time - what a 2,500-track list does */
setUrl(PREVIEW_B);
spotifyReply = () => {
  const e = new Error("The user aborted a request.");
  e.name = "AbortError";
  return Promise.reject(e);
};
await readPreview();
ok("a preview that ran out of time says so instead of failing silently",
  /preview could not be read/.test(el("phaseline").textContent) &&
  /no answer/.test(el("phaseline").textContent),
  "line: " + el("phaseline").textContent);

/* the preview must not be given the transfer's 25 s budget: that is the bug */
const previewTimers = [];
const realSetTimeout = ctx.setTimeout;
ctx.setTimeout = (fn, ms) => { previewTimers.push(ms); return realSetTimeout(fn, ms); };
setUrl(PREVIEW_C);
const budgeted = deferred();
spotifyReply = () => budgeted.p;
const pendingBudget = run("autoTitle()");
budgeted.res(jsonResponse(previewPayload("Budget List", 2500)));
await pendingBudget;
ctx.setTimeout = realSetTimeout;
run("render();");
ok("the preview waits longer than the transfer's 25 s for a big list",
  Math.max(0, ...previewTimers) > 25000, "preview timer(s): " + previewTimers.join(", "));
ok("the budgeted preview still paints what it read",
  el("pname").textContent === "Budget List — 2500/2500", "pname: " + el("pname").textContent);

/* a link the page already read is not read again on every debounce */
setUrl(PREVIEW_A);
spotifyReply = () => jsonResponse(previewPayload("Big List", 2500));
spotifyHits = 0;
await readPreview();
ok("a link with no remembered read is read once", spotifyHits === 1, "hits " + spotifyHits);
ok("the read paints the identity",
  el("pname").textContent === "Big List — 2500/2500", "pname: " + el("pname").textContent);
await readPreview();
ok("re-reading a remembered link sends nothing", spotifyHits === 1,
  "hits " + spotifyHits + " for one link");

/* a link already in flight is one request, not two */
setUrl(PREVIEW_D);
const inflight = deferred();
spotifyReply = () => inflight.p;
spotifyHits = 0;
const first = run("autoTitle()");
ok("a read in progress is named as one, with the control that skips it",
  /previewing the playlist/.test(el("phaseline").textContent) &&
  /Ctrl\+Enter starts now/.test(el("phaseline").textContent),
  "line: " + el("phaseline").textContent);
const second = run("autoTitle()");
inflight.res(jsonResponse(previewPayload("Late List", 40)));
await Promise.all([first, second]);
run("render();");
ok("the same link in flight is one request", spotifyHits === 1, "requests " + spotifyHits);

/* the answer must not land on a screen a live run owns */
setUrl(PREVIEW_D + "X");
const late = deferred();
spotifyReply = () => late.p;
const pendingLate = run("autoTitle()");
feed({
  run_id: "previewlive", seq: 20, phase: "append", state: "running", total: 40, declared: 40,
  counts: {source: 40, unique: 40, added: 3, skipped: 0, duplicate: 0,
    unverified: 0, refused: 0, pending: 2},
  rows: tracks(5, "appending"),
});
late.res(jsonResponse(previewPayload("Late List", 40)));
await pendingLate;
run("render();");
ok("a late preview does not put a preview note on the run's line",
  !/preview could not be read|previewing the playlist/.test(el("phaseline").textContent),
  "line: " + el("phaseline").textContent);
ok("a late preview does not take the identity from the run",
  !el("pname").textContent.includes("Late List"), "pname: " + el("pname").textContent);
spotifyReply = null;
run("clearLocal();");
spotifyReply = null;

/* ---------- 8. an interrupted record the server will not resume ----------
   /api/run's `interrupted` payload carries `resumable` and `blocked` (backend
   _latest_incomplete): a record from before RECORD_VERSION cannot say which copies it
   already wrote, so the backend fails closed instead of re-adding them. The page used
   to print one pin for every interrupted run - "press Ctrl+Enter to resume the same
   playlist" - which for that record is an offer the server refuses. These checks pin
   the blocked pin: it never offers that resume, it repeats the reason the payload
   carries, it names the one route that needs no guess (check the linked playlist by
   hand), and it recommends neither deleting the playlist nor resetting the run, both
   of which destroy the evidence the check needs. */
const PIN = () => el("phaseline").textContent;
const PINTITLE = () => el("phaseline").title;
const BLOCKED = "this record was written by an older version and cannot be resumed "
  + "without re-adding copies that may already be on the playlist";
const INC = (extra) => Object.assign({
  spotify_id: "SPOTBLOCK", playlist_id: "PLBLOCK",
  url: "https://music.youtube.com/playlist?list=PLBLOCK",
  title: "Blocked List", expected: 40, state: "verifying", attempt: 2, pending: ["V1"],
}, extra);
const showInc = (inc) => {
  run("clearLocal();showInterrupted(" + JSON.stringify(inc) + ");render();");
};

showInc(INC({resumable: false, blocked: BLOCKED}));
ok("a blocked record never offers Ctrl+Enter as a resume",
  !/Ctrl\+Enter/.test(PIN()) && !/resume the same playlist/.test(PIN()), "pin: " + PIN());
ok("a blocked record says the record cannot be resumed",
  /cannot be resumed/.test(PIN()), "pin: " + PIN());
ok("a blocked record names the manual check the payload supports",
  /check that playlist in YouTube Music/i.test(PIN()) && /by hand/i.test(PIN()),
  "pin: " + PIN());
ok("a blocked record recommends neither deleting the playlist nor resetting the run",
  !/delete|reset/i.test(PIN() + " " + PINTITLE()),
  "pin: " + PIN() + " · title: " + PINTITLE());
ok("a blocked record repeats the backend's own reason in the title",
  PINTITLE().includes("cannot be resumed without re-adding copies"), "title: " + PINTITLE());
ok("a blocked record still links the playlist it left behind",
  run("S.created") === true && run("S.url").includes("PLBLOCK"),
  "chip url: " + run("S.url") + " · created: " + run("S.created"));
ok("a blocked record leaves the source link it was started from",
  el("srcurl").value === "https://open.spotify.com/playlist/SPOTBLOCK",
  "source: " + el("srcurl").value);

/* a reason with no resumable flag is a block too: the page must not read the refusal
   away just because one field is missing */
showInc(INC({blocked: BLOCKED}));
ok("a blocked reason with no resumable flag is still treated as blocked",
  /cannot be resumed/.test(PIN()) && !/Ctrl\+Enter/.test(PIN()), "pin: " + PIN());

/* refused, but told no reason: refuse without inventing one */
showInc(INC({resumable: false}));
ok("a blocked record with no reason refuses to invent a cause",
  /cannot be resumed/.test(PIN()) && !/older version/i.test(PIN() + " " + PINTITLE()),
  "pin: " + PIN() + " · title: " + PINTITLE());

/* the resume it can honour is still offered, because the payload says it can */
showInc(INC({resumable: true, blocked: ""}));
ok("a resumable record still offers the resume it can honour",
  /press Ctrl\+Enter to resume the same playlist/.test(PIN()) && !/cannot be resumed/.test(PIN()),
  "pin: " + PIN());

/* a payload with neither field predates the block: it keeps the older page's offer,
   and it never claims a refusal it was not told about */
showInc(INC({}));
ok("a payload with no resumable field keeps the older offer and claims no refusal",
  /Ctrl\+Enter/.test(PIN()) && !/cannot be resumed|blocked/i.test(PIN()), "pin: " + PIN());

/* ---------- 9. the running version ----------
   /health answers {ok, version} straight from backend.py's APP_VERSION, so the
   label must be that answer and nothing else: a copy of the number in the page
   would report the version the page was written against, which is the defect this
   pins. The stub serves a version no shipped file contains, so a hardcoded number
   cannot pass by accident. Every way the read can fail - no answer, a non-2xx, a
   body with no version - must leave an honest note instead of a plausible-looking
   number, and the label is chrome (one dim token beside the brand), never a second
   status beside the pill. */
const verEl = el("appver");
const backendSrc = fs.readFileSync(path.join(ROOT, "backend.py"), "utf8");
const appVersion = (/^APP_VERSION\s*=\s*["']([^"']+)["']/m.exec(backendSrc) || [])[1] || "";
const unread = (t) => /unread/i.test(t) && !/^v?\d/.test(t);
/* A page that never shipped the read fails these checks instead of throwing:
   the missing function is the same fact as a label that never painted. */
const readVersionNow = async () => {
  if (run("typeof readVersion") !== "function") return false;
  await run("readVersion()");
  return true;
};

ok("the version label shows the version /health reported",
  verEl.textContent === "v9.9.9-test", "label: " + JSON.stringify(verEl.textContent));
ok("the version was read from the server, never copied from the backend",
  requests.includes("/health") && healthHits >= 1 && !!appVersion && !script.includes(appVersion),
  "health hits " + healthHits + " · APP_VERSION " + JSON.stringify(appVersion) +
  " · literal in the page script: " + (!!appVersion && script.includes(appVersion)));
ok("the label is chrome, not a second status",
  /#appver\{[^}]*--dim/.test(html) &&
  !/<span id="appver"[^>]*\b(role|aria-live)=/.test(html) &&
  html.indexOf('id="appver"') < html.indexOf('id="statepill"'),
  "label markup: " + (html.match(/<span id="appver"[^>]*>/) || [""])[0]);
ok("the markup ships the label empty, with no version of its own",
  /<span id="appver" hidden><\/span>/.test(html),
  "label markup: " + (html.match(/<span id="appver"[^>]*>/) || [""])[0]);

healthReply = () => Promise.reject(new TypeError("Failed to fetch"));
ok("a /health that never answers has a read to report", await readVersionNow(), "readVersion missing");
ok("a /health that never answers says the version is unread",
  unread(verEl.textContent), "label: " + JSON.stringify(verEl.textContent));
ok("an unread version says what failed and calls the version unknown",
  /\/health did not answer/.test(verEl.title) && /unknown/.test(verEl.title),
  "title: " + verEl.title);

healthReply = () => ({ok: false, status: 503, json: async () => ({ok: false})});
await readVersionNow();
ok("a /health answering with a status is not read as a version",
  unread(verEl.textContent) && /HTTP 503/.test(verEl.title),
  "label: " + JSON.stringify(verEl.textContent) + " · title: " + verEl.title);

healthReply = () => jsonResponse({ok: true});
await readVersionNow();
ok("a /health with no version field is named as such, never filled in",
  unread(verEl.textContent) && /without a version/.test(verEl.title),
  "label: " + JSON.stringify(verEl.textContent) + " · title: " + verEl.title);

healthReply = () => jsonResponse({ok: true, version: "<img src=x onerror=alert(1)>1.2.3"});
await readVersionNow();
ok("a version string is painted as text, never as markup",
  verEl.children.length === 0 && verEl.innerHTML === "" && verEl.textContent.startsWith("v<img"),
  "label: " + JSON.stringify(verEl.textContent) + " · html: " + JSON.stringify(verEl.innerHTML));

healthReply = healthOk;
await readVersionNow();
ok("the label returns to the served version when a later read answers",
  verEl.textContent === "v9.9.9-test", "label: " + JSON.stringify(verEl.textContent));

/* ---------- 10. the sheets are the modals they declare ----------
   Each sheet is role="dialog" aria-modal="true", a promise that the page behind the
   overlay is inert. Two things were missing: a sheet opened from "More options" left
   focus on the button under the overlay, and nothing kept Tab inside the card, so
   the keyboard walked into the ledger the sheet covers. Closing also left focus on a
   control that is now display:none, which drops it to the body. These cases drive the
   page's own handlers; the seeded cards carry the shipped order (field, then footer
   buttons), and the stub tracks focus the way a browser does. */
const pasteSheetEl = run("pasteSheet");
const moreSheetEl = run("moreSheet");
const active = () => doc.activeElement;
const named = () => (active() ? active().id || active().tagName : "none");
const inertOf = (what) => {
  const hit = [...doc.body.children].find((c) => c.id === what || c.className === what);
  return hit ? !!hit.inert : "missing:" + what;
};
const tab = (target, shift) => {
  let prevented = false;
  target.fire("keydown", {key: "Tab", shiftKey: !!shift, preventDefault() { prevented = true; }});
  return prevented;
};
const esc = () => doc.fire("keydown", {key: "Escape", preventDefault() {}, stopPropagation() {}});

el("morebtn").focus();
run("openSheet(moreSheet);");
ok("opening a sheet moves focus into its card",
  active() === el("watchdir"), "focus: " + named());
/* aria-modal is only honest when the page behind it is inert, and a backdrop image is
   not inert: this is the native flag doing the work. */
ok("opening a sheet makes the page behind it inert",
  inertOf("topbar") === true && inertOf("deck") === true && inertOf("actionbar") === true &&
  inertOf("cmdpal") === true && inertOf("pastesheet") === true && inertOf("moresheet") === false,
  JSON.stringify(["topbar", "deck", "actionbar", "cmdpal", "pastesheet", "moresheet"].map(inertOf)));
el("moreclose").focus();
ok("Tab off the last control stays inside the sheet",
  tab(moreSheetEl, false) === true && active() === el("watchdir"), "focus: " + named());
ok("Shift+Tab off the first control stays inside the sheet",
  tab(moreSheetEl, true) === true && active() === el("moreclose"), "focus: " + named());
el("startbtn").focus();
ok("a Tab arriving while focus sits behind the overlay is pulled back",
  tab(moreSheetEl, false) === true && active() === el("watchdir"), "focus: " + named());
el("watchbtn").focus();
ok("Tab between two controls inside the sheet is left to the browser",
  tab(moreSheetEl, false) === false && active() === el("watchbtn"), "focus: " + named());

el("morebtn").focus();
run("openSheet(moreSheet);");
esc();
ok("Escape closes the sheet", !moreSheetEl.classList.contains("show"), "sheet still open");
ok("closing returns focus to the control that opened it",
  active() === el("morebtn"), "focus: " + named());
ok("closing gives the page back: no inert flag survives it",
  inertOf("deck") === false && inertOf("topbar") === false && inertOf("cmdpal") === false,
  JSON.stringify([inertOf("topbar"), inertOf("deck"), inertOf("cmdpal")]));

/* the palette is the same custom overlay, so it gets the same treatment */
el("clearbtn").focus();
run("openPal();");
ok("opening the palette inerts the page behind it",
  inertOf("deck") === true && inertOf("cmdpal") === false &&
  inertOf("pastesheet") === true && inertOf("moresheet") === true,
  JSON.stringify(["deck", "cmdpal", "pastesheet", "moresheet"].map(inertOf)));
esc();
ok("closing the palette restores the page and the focus",
  inertOf("deck") === false && inertOf("cmdpal") === false &&
  active() === el("clearbtn"),
  JSON.stringify([inertOf("deck"), inertOf("cmdpal")]) + " · focus: " + named());
ok("the palette and the sheet overlays are body children, which is what inert covers",
  doc.body.children.filter((c) => c.id === "cmdpal" || c.id === "pastesheet" ||
    c.id === "moresheet").length === 3,
  "overlay parent chain is not body");

el("pastecookie").focus();
run("openSheet(pasteSheet);");
ok("the paste sheet opens on its field", active() === el("pasteinput"), "focus: " + named());
el("pastesave").focus();
ok("a Tab at the sheet's last control wraps back to its field",
  tab(pasteSheetEl, false) === true && active() === el("pasteinput"), "focus: " + named());
/* a saved cookie hides the control that opened Paste, so the restore cannot land
   there: the card's own primary control takes it instead of the body */
el("pastecookie").hidden = true;
run("closeSheets();");
ok("a hidden opener hands focus to the card's own control, not the body",
  active() === el("pickcookie") && !active().hidden, "focus: " + named());
el("pastecookie").hidden = false;

/* ---------- 11. one main landmark ----------
   The page shipped a banner and a contentinfo but no main, so a screen reader had
   no way to jump past the top bar to the deck. The ledger is the main content, and
   exactly one element may claim it. */
const mains = (html.match(/<main\b/g) || []).length;
ok("the page has exactly one main landmark", mains === 1 && /<\/main>/.test(html),
  "main elements: " + mains);
ok("nothing else claims a main landmark", !/role=["']main["']/.test(html),
  "a role=main duplicates the element");
ok("the ledger sits inside the main landmark",
  html.indexOf("<main") < html.indexOf('id="ledger"') &&
  html.indexOf('id="ledger"') < html.indexOf("</main>"),
  "ledger outside <main>");
ok("the top bar and the action bar keep their landmarks",
  /<header class="topbar">/.test(html) && /<footer class="actionbar">/.test(html),
  "banner/contentinfo missing");
ok("no progressbar role is invented: the run's progress is several phases",
  !/role=["']progressbar["']/.test(html), "a progressbar role is in the markup");

/* ---------- 12. Clear and New transfer report what the server did ----------
   /api/reset clears the run and the created-playlist record in two steps and answers
   with the status that says how far it got: 200 both, 409 nothing, 500 the run but not
   the record. The page used to clear the ledger before the request and swallow the
   answer, so a 500 left a clean idle screen over a record that still resumes, and a
   lost reply looked exactly like a success. These cases drive the real doClear with
   each answer, and prove the screen claims only what the server said. */
const settle = async () => { await new Promise((r) => setTimeout(r, 0)); await new Promise((r) => setTimeout(r, 0)); };
const feedDone = () => {
  /* a fresh screen: the ledger, the link, any lock and any in-flight flags are gone */
  run("S.lock=null;S.startBusy=false;S.startUnknown=null;S.resetBusy=false;clearLocal();");
  feed({
    run_id: "rdone", seq: 40, phase: "done", state: "done", total: 6, declared: 6,
    matched: 6, playlist_count: 6, created: true,
    url: "https://music.youtube.com/playlist?list=PLKEEP", title: "Kept List",
    counts: {source: 6, unique: 6, added: 6, skipped: 0, duplicate: 0, unverified: 0, refused: 0},
    rows: tracks(6, "landed"),
  }, true);
  run("$('srcurl').value='https://open.spotify.com/playlist/SPOTKEEP';paintSrcX();render();");
};
const resetPin = () => el("phaseline").textContent;
const resetTitle = () => el("phaseline").title;
/* The structured verdict /api/reset sends when it cleared the run but not the record.
   The page keys on `reset_partial`, never on the human sentence, so the text below is
   deliberately unlike anything the old matcher looked for: a page that still parsed prose
   would fall through to "not established" and the partial cases here would fail. The real
   contract (status + field, through the real handler on a throwaway port) is asserted in
   tests/test_http_guard.py. */
const PARTIAL_BODY = {ok: false, reset_partial: true,
  error: "the playlist record was left behind (wording the page must not read)"};
const REFUSED_BODY = {ok: false, error: "a transfer is already running"};

ok("the verdict fixture carries the field and no sentence the page could parse",
  PARTIAL_BODY.reset_partial === true && !/^the run was cleared/.test(PARTIAL_BODY.error),
  JSON.stringify(PARTIAL_BODY));

/* a confirmed reset: the one case where the screen may go blank */
feedDone();
resetReply = () => http(200, {ok: true});
resets.length = 0;
await run("doClear()");
ok("a confirmed reset clears the run and the link the user asked for",
  el("bignum").textContent === "0" && run("S.rows.size") === 0 && el("srcurl").value === "",
  "counter " + el("bignum").textContent + " · rows " + run("S.rows.size") +
  " · link " + JSON.stringify(el("srcurl").value));
ok("a confirmed reset leaves no failure sentence behind",
  !/reset|cleared|could not/i.test(resetPin()), "line: " + resetPin());
ok("the reset names the playlist whose record it clears",
  resets.length === 1 && resets[0].spotify_id === "SPOTKEEP", JSON.stringify(resets.slice(-1)));

/* 500: the run IS cleared on the server and the record was NOT - the partial case the
   page used to hide behind a fresh-looking idle screen */
feedDone();
resetReply = () => http(500, PARTIAL_BODY);
runReply = () => jsonResponse({ok: true, seq: 41, interrupted: {
  spotify_id: "SPOTKEEP", playlist_id: "PLKEEP", title: "Kept List", expected: 6,
  url: "https://music.youtube.com/playlist?list=PLKEEP", state: "verifying", attempt: 1,
  resumable: true, blocked: "", pending: []}});
spotifyReply = () => jsonResponse({ok: true, playlist_name: "Kept List",
  declared_total: 6, tracks: [{title: "t"}]});
await run("doClear()");
await settle();
ok("a 500 reset clears the run half, because the server cleared it",
  el("bignum").textContent === "0" && run("S.rows.size") === 0,
  "counter " + el("bignum").textContent + " · rows " + run("S.rows.size"));
ok("a 500 reset never reads as a fresh start",
  /reset incomplete/.test(resetPin()) && !/^paste a Spotify link/.test(resetPin()),
  "line: " + resetPin());
ok("a 500 reset says the record was not cleared and quotes the answer it was given",
  /not its playlist record/.test(resetPin()) && resetTitle().startsWith(PARTIAL_BODY.error),
  "line: " + resetPin() + " · title: " + resetTitle());
ok("a 500 reset never claims the record is gone or that it is unchanged by the page",
  !/record (is|was) gone|no record|record is unchanged/i.test(resetPin()),
  "line: " + resetPin());
ok("a 500 reset keeps the playlist the surviving record names reachable",
  run("S.created") === true && run("S.url").includes("PLKEEP") && !el("ytchip").hidden &&
  run("S.interrupted") !== null,
  "url " + run("S.url") + " · created " + run("S.created") + " · chip hidden " + el("ytchip").hidden);
ok("a 500 reset restores the source link the record still binds",
  el("srcurl").value === "https://open.spotify.com/playlist/SPOTKEEP",
  "link: " + JSON.stringify(el("srcurl").value));

/* An answered error that is NOT the backend's two-step verdict establishes neither half:
   the refusal sentence under another status, or the verdict sentence under a status that
   is not its own, says nothing about which step ran. The page may report the run it can
   no longer see and nothing more. */
feedDone();
resetReply = () => http(403, REFUSED_BODY);
runReply = () => jsonResponse({ok: true, seq: 43});
await run("doClear()");
ok("an answered error outside the two-step verdict proves neither half",
  !/cleared the run/.test(resetPin()) && !/record is still on disk/.test(resetPin()) &&
  /does not establish/.test(resetPin()),
  "line: " + resetPin());
ok("an answer that proves neither half still keeps the run's playlist reachable",
  run("S.url").includes("PLKEEP") && !el("ytchip").hidden,
  "url " + run("S.url") + " · chip hidden " + el("ytchip").hidden);
ok("an answer outside the verdict is quoted, not adopted",
  resetTitle().includes(REFUSED_BODY.error) &&
  /only the backend's own two-step verdict/.test(resetTitle()),
  "title: " + resetTitle());

/* the same 500 status carrying the refusal sentence is still not the verdict: the page
   needs both the status and the backend's own claim, never one without the other */
feedDone();
resetReply = () => http(500, REFUSED_BODY);
runReply = () => jsonResponse({ok: true, seq: 44});
await run("doClear()");
ok("a 500 whose body is not the verdict does not become one",
  !/not its playlist record/.test(resetPin()) && !/cleared the run/.test(resetPin()) &&
  /does not establish/.test(resetPin()) && resetTitle().includes(REFUSED_BODY.error),
  "line: " + resetPin() + " · title: " + resetTitle());

/* an explicit false is not the verdict either: the field has to say it happened */
feedDone();
resetReply = () => http(500, {ok: false, reset_partial: false, error: "the record is still there"});
runReply = () => jsonResponse({ok: true, seq: 46});
await run("doClear()");
ok("a 500 with reset_partial:false is not a partial reset",
  !/not its playlist record/.test(resetPin()) && /does not establish/.test(resetPin()),
  "line: " + resetPin());

/* an older backend sends no field: the page must fall back, never read the prose. The
   sentence here is word for word the one the old matcher looked for, and it must still
   NOT be treated as the verdict. */
feedDone();
resetReply = () => http(500, {ok: false,
  error: "the run was cleared, but the playlist record could not be cleared - it is unchanged"});
runReply = () => jsonResponse({ok: true, seq: 47});
await run("doClear()");
ok("a 500 with no field falls back even when its sentence reads like the verdict",
  /does not establish/.test(resetPin()) && !/reset incomplete/.test(resetPin()),
  "line: " + resetPin());

/* the refusal branch owns the answer: a field on it cannot turn it into a partial reset */
feedDone();
resetReply = () => http(409, {ok: false, reset_partial: true, error: REFUSED_BODY.error});
runReply = () => jsonResponse({ok: true, seq: 48});
await run("doClear()");
await settle();
run("unpoll();");
ok("a 409 stays a refusal even if it carries the field",
  /nothing was cleared/.test(resetPin()) && !/reset incomplete/.test(resetPin()),
  "line: " + resetPin());

/* Even the verdict sentence cannot make the page clear a run the server still reports:
   the read outranks the answer, and the record's fate stays unclaimed. */
feedDone();
resetReply = () => http(500, PARTIAL_BODY);
runReply = () => jsonResponse({ok: true, seq: 45, run_id: "rdone"});
await run("doClear()");
await settle();
run("unpoll();");
ok("an answered error with the run still there clears nothing",
  el("bignum").textContent === "6" && run("S.rows.size") === 6,
  "counter " + el("bignum").textContent + " · rows " + run("S.rows.size"));
ok("that answer claims the run survived and nothing about the record",
  /did not go through/.test(resetPin()) && /run was.*not cleared/.test(resetPin()) &&
  !/nothing was cleared/i.test(resetPin()) &&
  !/record (is|was) (cleared|kept|unchanged)/i.test(resetPin()),
  "line: " + resetPin());

/* no answer at all: the run and the record may both be half-done, and nothing may be
   claimed - in either direction */
feedDone();
resetReply = () => Promise.reject(new TypeError("Failed to fetch"));
runReply = () => Promise.reject(new TypeError("Failed to fetch"));
await run("doClear()");
ok("a reset with no answer does not empty the screen",
  el("bignum").textContent === "6" && run("S.rows.size") === 6,
  "counter " + el("bignum").textContent + " · rows " + run("S.rows.size"));
ok("a reset with no answer is called unconfirmed, never done",
  /not confirmed/.test(resetPin()) && /unknown/.test(resetPin()),
  "line: " + resetPin());
/* A reset is local: it clears this PC's run and record and never calls YouTube Music.
   So the doubt may name only the local half - saying the playlist may have been cleared
   would be a claim the request cannot even make. */
ok("a reset with no answer keeps the doubt on this PC's run and record",
  /local run and playlist record were cleared is unknown/.test(resetPin()) &&
  /never touches the playlist/.test(resetPin()) &&
  !/playlist[^.]*(?:may|might|could)[^.]*clear/i.test(resetPin()),
  "line: " + resetPin());
ok("a reset with no answer claims nothing about the record",
  !/record (was|is) cleared|record is gone|record is unchanged/i.test(resetPin() + resetTitle()),
  "line: " + resetPin() + " · title: " + resetTitle());
ok("a reset with no answer sends the user to the playlist, not to a retry",
  /unknown/i.test(resetPin()) &&
  /check the playlist in YouTube Music by hand/i.test(resetPin()),
  "line: " + resetPin());
/* A lost reply may have cleared the record that is the only account of which copies
   were written. Repeating the reset, or starting another transfer of a link whose
   record is in an unknown state, is how a recovery record and a playlist get
   destroyed - so the pin may not offer either. */
ok("a reset with no answer never invites the destructive action again",
  !/(?:press|click) Clear|Clear again|Start to re-attach|re-attached|press Ctrl\+Enter/i
    .test(resetPin()) && /do not (?:retry|start)/i.test(resetPin()),
  "line: " + resetPin());
ok("a reset with no answer keeps the reason it failed for the title",
  /Failed to fetch/.test(resetTitle()) && /outcome is unknown/.test(resetTitle()),
  "title: " + resetTitle());
ok("the playlist chip survives a reset the page could not confirm",
  run("S.url").includes("PLKEEP"), "url: " + run("S.url"));

/* 409: nothing was cleared, so the page must clear nothing either */
feedDone();
resetReply = () => http(409, REFUSED_BODY);
runReply = () => jsonResponse({ok: true, seq: 42});
await run("doClear()");
await settle();
run("unpoll();");
ok("a refused reset clears nothing",
  el("bignum").textContent === "6" && run("S.rows.size") === 6,
  "counter " + el("bignum").textContent + " · rows " + run("S.rows.size"));
ok("a refused reset says nothing was cleared and names the run that owns the server",
  /nothing was cleared/.test(resetPin()) && /already running|re-attached/.test(resetPin()),
  "line: " + resetPin());

/* New transfer is the same request, so it must take the same path */
feedDone();
resetReply = () => http(200, {ok: true});
runReply = () => jsonResponse({ok: true, seq: 0});
spotifyReply = null;
await run("doNewTransfer()");
ok("New transfer goes through the same reset and clears on a confirmed answer",
  run("S.rows.size") === 0 && el("srcurl").value === "" && el("bignum").textContent === "0",
  "rows " + run("S.rows.size") + " · link " + JSON.stringify(el("srcurl").value));

/* ---------- 12b. an unresolved reset locks the session ----------
   A reset the page could not confirm leaves a record nothing on this page can see: /api/run
   serves only an INCOMPLETE record, so "no run and no record" is not proof the record is
   gone, and a complete one still names the playlist a new transfer would top up. The guard is
   deliberately blunt - after ANY unresolved reset, nothing is reset and nothing is started,
   for any source - because an empty-id reset clears every record and a per-source guard let a
   second failure replace the first. Nothing unlocks itself, nothing claims the record is gone,
   and the copy says the guard is in memory only: a reload clears it without settling anything. */
const isLocked = () => run("!!S.lock");

feedDone();
resetReply = () => http(500, PARTIAL_BODY);
runReply = () => jsonResponse({ok: true, seq: 50});
spotifyReply = null;
await run("doClear()");
ok("an unresolved reset locks the session", isLocked(), "lock " + JSON.stringify(run("S.lock")));
ok("the session-only consequence is on the first failure screen, not only after a blocked retry",
  /this guard lasts this session/i.test(resetPin()) &&
  /a reload clears this guard/i.test(resetPin()),
  "line: " + resetPin());
/* the status line is one ellipsised line, so the warning it cannot hold lives in its own
   region: full sentences, wrapping, focusable and announced */
ok("the lock warning is its own announced, focusable region in the shipped markup",
  /<div id="lockwarn" role="alert" tabindex="0" hidden><\/div>/.test(html),
  "markup: " + (html.match(/<div id="lockwarn"[^>]*>/) || [""])[0]);
ok("the warning region wraps instead of clipping and takes a full row",
  /#lockwarn\{[^}]*white-space:normal/.test(html) &&
  /#lockwarn\{[^}]*flex:1 1 100%/.test(html) &&
  !/#lockwarn\{[^}]*(?:nowrap|text-overflow|overflow:hidden)/.test(html),
  "css: " + (html.match(/#lockwarn\{[^}]*\}/) || [""])[0]);
ok("the warning is visible while locked and carries the whole sentence",
  el("lockwarn").hidden === false &&
  /this session/i.test(el("lockwarn").textContent) &&
  /reloading the page clears it/i.test(el("lockwarn").textContent) &&
  /without restoring or clearing the record/i.test(el("lockwarn").textContent),
  "band: " + JSON.stringify(el("lockwarn").textContent));
ok("the held run keeps its link, which is the way back to the record",
  el("srcurl").value === "https://open.spotify.com/playlist/SPOTKEEP",
  "link: " + JSON.stringify(el("srcurl").value));

const blockedAt = {req: requests.length, res: resets.length};
await run("startTransfer()");
ok("start is refused while the session is locked",
  !requests.slice(blockedAt.req).some((u) => u.startsWith("/api/transfer")) &&
  /reset unresolved/.test(resetPin()),
  "line: " + resetPin());
await run("doClear()");
ok("a second reset is refused while the session is locked",
  resets.length === blockedAt.res && /reset unresolved/.test(resetPin()),
  "resets " + (resets.length - blockedAt.res) + " · line: " + resetPin());
ok("the refusal keeps the link and the playlist reachable",
  el("srcurl").value.includes("SPOTKEEP") && !el("ytchip").hidden,
  "link " + JSON.stringify(el("srcurl").value) + " · chip hidden " + el("ytchip").hidden);
ok("the refusal claims no unlock and never says the record is gone",
  !/record (is|was) (gone|restored|cleared)|no record|unlock|ignore/i.test(resetPin()) &&
  /not proof that the record is gone/.test(resetTitle()),
  "line: " + resetPin() + " · title: " + resetTitle());
/* the honest limit, in the page's own words and in its storage */
ok("the lock says it lasts this session only and warns that a reload clears it",
  /this session/.test(resetPin()) &&
  /in memory for this session only/i.test(resetTitle()) &&
  /reloading the page clears it/i.test(resetTitle()) &&
  /neither restores nor clears the record/i.test(resetTitle()),
  "line: " + resetPin() + " · title: " + resetTitle());
ok("nothing about the lock is persisted, so no reload can claim it back",
  stored.every((k) => k === "sp2yt.privacy") &&
  !stored.some((k) => /lock|hold|reset|playlist|spotkin/i.test(k)),
  "localStorage keys: " + stored.join(", "));

/* the poll owns the status line and clears S.pin on a terminal payload: the warning may not be
   painted from it */
runReply = () => jsonResponse({ok: true, seq: 60, phase: "done", state: "done",
  total: 6, declared: 6, playlist_count: 6,
  counts: {source: 6, unique: 6, added: 4, skipped: 0, duplicate: 0, unverified: 2, refused: 0}});
await run("pollOnce()");
ok("a poll that clears the status line cannot hide the lock warning",
  run("S.pin") === null && el("lockwarn").hidden === false &&
  /this session/i.test(el("lockwarn").textContent),
  "pin " + JSON.stringify(run("S.pin")) + " · band hidden " + el("lockwarn").hidden +
  " · band: " + JSON.stringify(el("lockwarn").textContent));
run("unpoll();");

/* every route to the two functions is the same guard: a shortcut is not a way past it */
const byKeys = {req: requests.length, res: resets.length};
doc.fire("keydown", {key: "Enter", ctrlKey: true, preventDefault() {}, stopPropagation() {}});
await settle();
ok("Ctrl+Enter is not a way past the lock",
  !requests.slice(byKeys.req).some((u) => u.startsWith("/api/transfer")),
  "requests: " + requests.slice(byKeys.req).join(", "));
doc.fire("keydown", {key: "n", ctrlKey: true, shiftKey: true, preventDefault() {}, stopPropagation() {}});
await settle();
ok("Ctrl+Shift+N is not a way past the lock", resets.length === byKeys.res,
  "resets " + (resets.length - byKeys.res));
await run("runCmd('start')");
await settle();
await run("runCmd('new')");
await settle();
ok("the palette's start and new are not a way past the lock",
  !requests.slice(byKeys.req).some((u) => u.startsWith("/api/transfer")) &&
  resets.length === byKeys.res,
  "requests: " + requests.slice(byKeys.req).join(", ") + " · resets " + (resets.length - byKeys.res));

/* a different, identified link is refused too: after one unresolved reset no source can be
   shown safe, so the lock is not per-source */
run("$('srcurl').value='https://open.spotify.com/playlist/SPOTOTHER';paintSrcX();render();");
resetReply = () => http(200, {ok: true});
const otherAt = resets.length;
await run("doClear()");
ok("a different source is refused while the session is locked",
  resets.length === otherAt && /reset unresolved/.test(resetPin()),
  "resets " + (resets.length - otherAt) + " · line: " + resetPin());
await run("startTransfer()");
ok("and it cannot be started either",
  !requests.slice(otherAt).some((u) => u.startsWith("/api/transfer")), "line: " + resetPin());

/* the empty-link case: a reset there would clear every record, so it is the sharpest form of
   the same guard */
run("$('srcurl').value='';paintSrcX();render();");
const emptyAt = resets.length;
await run("doClear()");
ok("an unscoped reset is refused while the session is locked",
  resets.length === emptyAt && /reset unresolved/.test(resetPin()),
  "line: " + resetPin());

/* an unresolved reset with NO identifiable source locks every source, which is the case a
   per-source guard could not express: the request itself could have touched any record */
feedDone();
run("$('srcurl').value='';paintSrcX();render();");
resetReply = () => http(500, PARTIAL_BODY);
runReply = () => jsonResponse({ok: true, seq: 51});
await run("doClear()");
ok("an unresolved reset with no source id still locks the session", isLocked(),
  "lock " + JSON.stringify(run("S.lock")));
run("$('srcurl').value='https://open.spotify.com/playlist/SPOTOTHER';paintSrcX();render();");
const unknownAt = resets.length;
await run("doClear()");
await run("startTransfer()");
ok("an unknown-id lock refuses an identified source too",
  resets.length === unknownAt &&
  !requests.slice(unknownAt).some((u) => u.startsWith("/api/transfer")) &&
  /reset unresolved/.test(resetPin()),
  "resets " + (resets.length - unknownAt) + " · line: " + resetPin());

/* a lost reply reaches the same lock */
feedDone();
resetReply = () => Promise.reject(new TypeError("Failed to fetch"));
runReply = () => Promise.reject(new TypeError("Failed to fetch"));
await run("doClear()");
ok("a lost reply locks the session too",
  isLocked() && el("srcurl").value.includes("SPOTKEEP"),
  "lock " + JSON.stringify(run("S.lock")) + " · link " + JSON.stringify(el("srcurl").value));
ok("the lost-reply screen says the guard is session-only in its own words",
  /this guard lasts this session/i.test(resetPin()) &&
  /a reload clears this guard/i.test(resetPin()),
  "line: " + resetPin());

/* a confirmed reset proves the record gone, so nothing is locked and work goes on */
feedDone();
resetReply = () => http(200, {ok: true});
runReply = () => jsonResponse({ok: true, seq: 0});
await run("doClear()");
ok("a confirmed reset leaves no lock and an empty link",
  !isLocked() && el("srcurl").value === "",
  "lock " + JSON.stringify(run("S.lock")) + " · link " + JSON.stringify(el("srcurl").value));
ok("a confirmed reset takes the warning region away",
  el("lockwarn").hidden === true && el("lockwarn").textContent === "",
  "band hidden " + el("lockwarn").hidden + " · band: " + JSON.stringify(el("lockwarn").textContent));
run("S.ytState='connected';$('srcurl').value='https://open.spotify.com/playlist/SPOTNEXT';"
  + "paintSrcX();render();");
transferReply = () => jsonResponse({ok: true, run_id: "rnext"});
const afterOk = requests.length;
await run("startTransfer()");
ok("a confirmed reset does not block the next transfer",
  requests.slice(afterOk).some((u) => u.startsWith("/api/transfer")),
  "requests: " + requests.slice(afterOk).join(", "));
transferReply = null;
/* the run this started is left live on screen, so stop the poll and the facts tick:
   nothing after this block drives the page again */
run("unpoll();tickStop();");

/* ---------- 12c. one reset at a time, initiation to terminal decision ----------
   /api/reset writes twice on the server (the run, then the record), so a second reset or a
   start racing the first could erase the record whose outcome the first has not reported -
   and a success landing on top of a failure would wipe the lock that failure earned. So the
   page marks itself busy before its first await and refuses everything else until its own
   answer decides. These cases hold that answer open to prove the window is closed. */
feedDone();
resetReply = () => inflightReset.p;
runReply = () => jsonResponse({ok: true, seq: 52});
const inflightReset = deferred();
const inFlight = run("doClear()");
ok("a reset is busy the moment it is sent, before any answer",
  run("S.resetBusy") === true, "busy " + run("S.resetBusy"));
const raceAt = {req: requests.length, res: resets.length};
await run("doClear()");
ok("a second Clear during a reset is refused, not sent",
  resets.length === raceAt.res && /already in progress/.test(resetPin()),
  "resets " + (resets.length - raceAt.res) + " · line: " + resetPin());
await run("startTransfer()");
ok("Start during a reset is refused, not sent",
  !requests.slice(raceAt.req).some((u) => u.startsWith("/api/transfer")) &&
  /reset is in progress/.test(resetPin()),
  "requests: " + requests.slice(raceAt.req).join(", "));
doc.fire("keydown", {key: "Enter", ctrlKey: true, preventDefault() {}, stopPropagation() {}});
await settle();
doc.fire("keydown", {key: "n", ctrlKey: true, shiftKey: true, preventDefault() {}, stopPropagation() {}});
await settle();
await run("runCmd('start')");
await settle();
await run("runCmd('new')");
await settle();
ok("no shortcut or palette command races the reset",
  resets.length === raceAt.res &&
  !requests.slice(raceAt.req).some((u) => u.startsWith("/api/transfer")),
  "resets " + (resets.length - raceAt.res) + " · requests: " + requests.slice(raceAt.req).join(", "));
/* the held answer decides, and only then is the page free again */
inflightReset.res(http(500, PARTIAL_BODY));
await inFlight;
ok("the reset's own answer decides, then the page shows the lock and clears busy",
  isLocked() && run("S.resetBusy") === false,
  "lock " + isLocked() + " · busy " + run("S.resetBusy"));
/* and no success may erase a lock that failure earned */
resetReply = () => http(200, {ok: true});
const lockAt = resets.length;
await run("doClear()");
ok("a success cannot clear an unresolved lock",
  resets.length === lockAt && isLocked(),
  "resets " + (resets.length - lockAt) + " · lock " + isLocked());
run("unpoll();tickStop();");

/* ---------- 12d. a transfer request in flight owns the page ----------
   startTransfer marks the phase live before its POST, so a detach could set "aborted" and let
   a reset run while the request was still unanswered - the run may be created after the reset,
   and the reply would then paint over a screen that was already cleared. The in-flight flag is
   set before the POST and held until a definitive answer. A lost reply leaves the outcome
   unknown for the session: no /api/run read settles it - a delayed request can arrive after
   any read, and a run for the same playlist may predate it - so detach, reset and start stay
   refused while the server's payloads are still displayed. */
feedDone();
run("S.ytState='connected';$('srcurl').value='https://open.spotify.com/playlist/SPOTSTART';"
  + "paintSrcX();render();");
runReply = () => jsonResponse({ok: true, seq: 0});
const transferGate = deferred();
transferReply = () => transferGate.p;
const startCall = run("startTransfer()");
ok("a transfer request is in flight before it answers",
  run("S.startBusy") === true, "busy " + run("S.startBusy") + " · phase " + run("S.phase"));
const startAt = {req: requests.length, res: resets.length};
await run("abortRun()");
ok("detach is refused while the transfer request is unanswered",
  run("S.phase") === "spotify" && /cannot be detached/.test(resetPin()),
  "phase " + run("S.phase") + " · line: " + resetPin());
await run("startTransfer()");
ok("a second start does not race the first",
  !requests.slice(startAt.req).some((u) => u.startsWith("/api/transfer")),
  "requests: " + requests.slice(startAt.req).join(", "));
await run("doClear()");
ok("a reset cannot race an unanswered transfer",
  resets.length === startAt.res && /still in flight/.test(resetPin()),
  "resets " + (resets.length - startAt.res) + " · line: " + resetPin());
transferGate.res(jsonResponse({ok: true, run_id: "rstart"}));
await startCall;
ok("the answer decides: the run is attached and the flag clears",
  run("S.runId") === "rstart" && run("S.startBusy") === false,
  "run " + JSON.stringify(run("S.runId")) + " · busy " + run("S.startBusy"));
run("unpoll();tickStop();");

/* the reply that never arrives: the run may exist, so nothing may pretend otherwise */
feedDone();
run("S.ytState='connected';$('srcurl').value='https://open.spotify.com/playlist/SPOTLOST';"
  + "paintSrcX();render();");
transferReply = () => Promise.reject(new TypeError("Failed to fetch"));
runReply = () => Promise.reject(new TypeError("Failed to fetch"));
await run("startTransfer()");
await settle();
ok("a lost transfer reply leaves the outcome unknown, not tidied away",
  !!run("S.startUnknown") && run("S.startBusy") === false && run("S.phase") !== "aborted",
  "unknown " + JSON.stringify(run("S.startUnknown")) + " · busy " + run("S.startBusy") +
  " · phase " + run("S.phase"));
ok("a lost transfer reply keeps reading the server instead of giving up",
  requests.slice(-6).some((u) => u.startsWith("/api/run?")),
  "requests: " + requests.slice(-6).join(", "));
const lostAt = {res: resets.length};
await run("abortRun()");
ok("detach is refused while that outcome is unknown",
  run("S.phase") !== "aborted" && /re-attaching/.test(resetPin()),
  "phase " + run("S.phase") + " · line: " + resetPin());
await run("doClear()");
ok("a reset is refused while that outcome is unknown",
  resets.length === lostAt.res && /never answered/.test(resetPin()),
  "resets " + (resets.length - lostAt.res) + " · line: " + resetPin());
/* No read settles it. Not an idle one - the request can still arrive after it - and not one
   that shows a run for the same playlist, which may predate the request. The page may show
   that run; it may not re-open a reset or a start. */
runReply = () => jsonResponse({ok: true, seq: 62});
await run("pollOnce()");
ok("a read that reports no run does not clear the doubt",
  !!run("S.startUnknown") && /never answered/.test(resetPin()),
  "unknown " + JSON.stringify(run("S.startUnknown")) + " · line: " + resetPin());
const idleReadAt = resets.length;
await run("doClear()");
ok("a no-run read does not let a reset through",
  resets.length === idleReadAt, "resets " + (resets.length - idleReadAt));

/* the deferred acceptance: the server takes the request after our read said nothing */
runReply = () => jsonResponse({ok: true, seq: 63, run_id: "rlate", phase: "append",
  state: "running", total: 6, declared: 6, spotify_id: "SPOTLOST", title: "Late List",
  counts: {source: 6, unique: 6, added: 1, skipped: 0, duplicate: 0, unverified: 0,
    refused: 0, pending: 1}});
await run("pollOnce()");
ok("a run that appears after the read is shown, never taken as proof",
  run("S.runId") === "rlate" && !!run("S.startUnknown"),
  "run " + JSON.stringify(run("S.runId")) + " · unknown " + JSON.stringify(run("S.startUnknown")));
const lateAt = {req: requests.length, res: resets.length};
await run("doClear()");
ok("a reset is still refused after that run appears",
  resets.length === lateAt.res &&
  !requests.slice(lateAt.req).some((u) => u.startsWith("/api/reset")),
  "requests: " + requests.slice(lateAt.req).join(", "));
await run("startTransfer()");
ok("start is refused too, so the late run cannot be raced",
  !requests.slice(lateAt.req).some((u) => u.startsWith("/api/transfer")),
  "requests: " + requests.slice(lateAt.req).join(", "));
ok("the unanswered-transfer warning stays up through every read",
  el("lockwarn").hidden === false && /Transfer unanswered/.test(el("lockwarn").textContent) &&
  /reloading the page clears it without resolving anything/i.test(el("lockwarn").textContent),
  "band: " + JSON.stringify(el("lockwarn").textContent));
run("unpoll();tickStop();");

/* ---------- 12e. a response belongs to the screen that asked for it ----------
   unpoll() clears the interval, not a pollOnce already awaiting its answer. Without a
   generation, an earlier run's payload lands on a newer screen: it would overwrite the new
   run's rows and counts, or - on a done payload - halt the new run's polling. The generation
   moves when the screen is replaced (start, detach, reset) and not on an ordinary poll, so a
   re-attach and overlapping polls inside one run still apply. */
feedDone();
const staleDone = deferred();
runReply = () => staleDone.p;
const stalePoll = run("pollOnce()");          /* run A's poll, waiting */
transferReply = () => jsonResponse({ok: true, run_id: "rB"});
runReply = () => jsonResponse({ok: true, seq: 5, run_id: "rB", phase: "append",
  state: "running", total: 3, declared: 3,
  counts: {source: 3, unique: 3, added: 1, skipped: 0, duplicate: 0, unverified: 0,
    refused: 0, pending: 2},
  rows: [{i: 1, title: "B one", artist: "B", videoId: "B1", state: "landed"}]});
run("S.ytState='connected';$('srcurl').value='https://open.spotify.com/playlist/SPOTB';"
  + "paintSrcX();render();");
await run("startTransfer()");
await settle();
ok("the new run's own answer paints the new screen",
  run("S.runId") === "rB" && run("S.rows.size") === 1 && el("bignum").textContent === "1",
  "run " + JSON.stringify(run("S.runId")) + " · rows " + run("S.rows.size") +
  " · counter " + el("bignum").textContent);
/* the earlier run's response arrives late */
staleDone.res(jsonResponse({ok: true, seq: 9, run_id: "rA", phase: "done", state: "done",
  total: 99, declared: 99, playlist_count: 99,
  counts: {source: 99, unique: 99, added: 99, skipped: 0, duplicate: 0, unverified: 0, refused: 0},
  rows: [{i: 1, title: "A one", artist: "A", videoId: "A1", state: "landed"}]}));
await stalePoll;
await settle();
ok("a stale done response cannot overwrite the new run's rows or counts",
  run("S.rows.size") === 1 && run("S.rows.get(1).videoId") === "B1" &&
  el("bignum").textContent === "1" && run("S.counts.added") === 1,
  "rows " + run("S.rows.size") + " · first " + JSON.stringify(run("S.rows.get(1).videoId")) +
  " · counter " + el("bignum").textContent + " · added " + run("S.counts.added"));
ok("a stale done response cannot halt the new run's polling",
  run("S.timer") !== null && run("S.phase") === "append" && run("S.state") === "running",
  "timer " + (run("S.timer") ? "set" : "cleared") + " · phase " + run("S.phase") +
  " · state " + run("S.state"));
run("unpoll();tickStop();");

/* detach retires the poll in flight: its live payload may not repaint or restart */
feedDone();
const staleDetach = deferred();
runReply = () => staleDetach.p;
const detachPoll = run("pollOnce()");
run("setPhase('append');S.state='running';render();");
await run("abortRun()");
ok("detach retires the poll still in flight",
  run("S.phase") === "aborted" && run("S.timer") === null,
  "phase " + run("S.phase") + " · timer " + (run("S.timer") ? "set" : "cleared"));
staleDetach.res(jsonResponse({ok: true, seq: 9, run_id: "rA", phase: "append", state: "running",
  total: 5, declared: 5,
  counts: {source: 5, unique: 5, added: 5, skipped: 0, duplicate: 0, unverified: 0,
    refused: 0, pending: 0}}));
await detachPoll;
await settle();
ok("the detached screen is not repainted and polling is not restarted",
  run("S.phase") === "aborted" && run("S.timer") === null &&
  run("S.counts.added") === 6 && el("bignum").textContent === "6",
  "phase " + run("S.phase") + " · timer " + (run("S.timer") ? "set" : "cleared") +
  " · counter " + el("bignum").textContent);

/* a reset retires it too: the run it cleared may not paint over the idle page */
feedDone();
const staleReset = deferred();
runReply = () => staleReset.p;
const resetPoll = run("pollOnce()");
resetReply = () => http(200, {ok: true});
await run("doClear()");
staleReset.res(jsonResponse({ok: true, seq: 9, run_id: "rA", phase: "done", state: "done",
  total: 7, declared: 7,
  counts: {source: 7, unique: 7, added: 7, skipped: 0, duplicate: 0, unverified: 0, refused: 0}}));
await resetPoll;
await settle();
ok("a cleared screen is not repainted by the run it cleared",
  run("S.rows.size") === 0 && el("bignum").textContent === "0" && run("S.phase") === "idle",
  "rows " + run("S.rows.size") + " · counter " + el("bignum").textContent +
  " · phase " + run("S.phase"));

/* the same generation still applies: an ordinary poll and a re-attach are not retired */
feedDone();
runReply = () => jsonResponse({ok: true, seq: 12, run_id: "rSame", phase: "append",
  state: "running", total: 2, declared: 2,
  counts: {source: 2, unique: 2, added: 1, skipped: 0, duplicate: 0, unverified: 0,
    refused: 0, pending: 1},
  rows: [{i: 1, title: "Same one", artist: "S", videoId: "S1", state: "landed"}]});
const sameA = run("pollOnce()");
const sameB = run("pollOnce()");
await Promise.all([sameA, sameB]);
ok("overlapping polls inside one run are not retired",
  run("S.runId") === "rSame" && run("S.rows.get(1).videoId") === "S1" &&
  run("S.counts.added") === 1,
  "run " + JSON.stringify(run("S.runId")) + " · rows " + run("S.rows.size"));
run("unpoll();tickStop();");

/* a retry's read is part of its request's continuation, not a fresh look at a screen it did
   not start on: a detach during the request retires it */
feedDone();
run("S.phase='verify';S.state='standby';S.total=6;S.declared=6;"
  + "S.counts={source:6,unique:6,added:6,skipped:0,duplicate:0,unverified:0,refused:0,pending:0};"
  + "setPhase('verify');render();");
const retryGate = deferred();
retryReply = () => retryGate.p;
const retrySent = requests.length;
el("retrybtn").click();
await settle();
ok("the retry control asks the server",
  requests.slice(retrySent).some((u) => u.startsWith("/api/retry")),
  "requests: " + requests.slice(retrySent).join(", "));
await run("abortRun()");
ok("a detach during the retry leaves the page detached",
  run("S.phase") === "aborted", "phase " + run("S.phase"));
const retryPoll = requests.length;
retryGate.res(jsonResponse({ok: true}));
await settle();
ok("the retry's answer does not poll or re-attach the detached run",
  run("S.phase") === "aborted" && run("S.timer") === null &&
  !requests.slice(retryPoll).some((u) => u.startsWith("/api/run?")),
  "phase " + run("S.phase") + " · timer " + (run("S.timer") ? "set" : "cleared") +
  " · requests: " + requests.slice(retryPoll).join(", "));
ok("the retry control is left usable, without repainting the detached screen",
  el("retrybtn").disabled === false, "disabled " + el("retrybtn").disabled);

/* and with no detach it still re-reads the run */
feedDone();
run("S.phase='verify';S.state='standby';setPhase('verify');render();");
retryReply = () => jsonResponse({ok: true});
const retryOk = requests.length;
el("retrybtn").click();
await settle();
ok("a retry with no detach still re-reads the run",
  requests.slice(retryOk).some((u) => u.startsWith("/api/run?")),
  "requests: " + requests.slice(retryOk).join(", "));
retryReply = () => jsonResponse({ok: false});
run("unpoll();tickStop();");

ok("the page read its settings, its cookie record and its version on load",
  requests.some((u) => u.startsWith("/api/settings")) &&
  requests.some((u) => u.startsWith("/api/cookies")) &&
  requests.includes("/health"),
  "requests: " + requests.join(", "));
/* Same origin only: /health is the server's own route beside the /api/ surface. */
ok("no third-party request is made",
  requests.every((u) => u.startsWith("/api/") || u === "/health"),
  "requests: " + requests.join(", "));

console.log();
if (fails) {
  console.error("UI RED - " + fails + " of " + count + " checks failed");
  process.exit(1);
}
console.log("ui: " + count + " checks green - no 1:1 toggle, verified and pending told apart, the read's entry count told apart from the plan and never called a total, a blocked interrupted record offers no resume, ledger memoised, rate survives a slice, the preview says what it could not read, the version is the server's own and honest when unread, the overlays inert the page and contain and return focus, one main landmark, a reset is cleared, partial, refused or unconfirmed exactly as the server says");
