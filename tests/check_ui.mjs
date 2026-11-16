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
   - the ledger's sort, tally, missing list and follow index are memoised per
     row revision: repeated paints and filter changes do not rescans a large
     playlist, and one arriving row does;
   - a verified jump (a slice read back at once) does not blank the rate/ETA,
     while a count that goes backwards does.
   - the playlist preview - the page's one full-track read - is not given the
     transfer's 25 s budget, says so when it cannot answer instead of dropping
     the failure, still points at Ctrl+Enter, and does not read the same link
     twice (remembered, in flight, or answering late over a live run).

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

/* ---------- the smallest DOM the page actually touches ---------- */

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
  focus() {}
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
  get activeElement() { return null; }
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
/* The playlist preview is the one route whose answer each case decides for
   itself: it is the page's only full-track read, and its timing is the point. */
let spotifyReply = null;
let spotifyHits = 0;
function jsonResponse(payload) {
  return {ok: true, json: async () => payload, text: async () => JSON.stringify(payload)};
}
async function stubFetch(url) {
  const at = String(url);
  requests.push(at);
  if (at.startsWith("/api/spotify")) {
    spotifyHits += 1;
    if (spotifyReply) return spotifyReply(at);
    return jsonResponse({ok: false, error: "no stub for this case"});
  }
  if (at.startsWith("/api/settings")) return jsonResponse({ok: true});
  if (at.startsWith("/api/cookies")) return jsonResponse({ok: true, yt: {state: "none", has: false}});
  if (at.startsWith("/api/run")) return jsonResponse({ok: true, seq: 0});
  if (at.startsWith("/api/watch")) return jsonResponse({ok: false});
  return jsonResponse({ok: false});
}

const doc = new StubDoc();
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
}
seed(doc);
const ctx = {
  document: doc,
  localStorage: {getItem: () => null, setItem() {}, removeItem() {}},
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

/* ---------- 6. rate and ETA across a verified slice ---------- */
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

/* ---------- the load-time surface is still intact ---------- */
ok("the page read its settings and cookie record on load",
  requests.some((u) => u.startsWith("/api/settings")) &&
  requests.some((u) => u.startsWith("/api/cookies")),
  "requests: " + requests.join(", "));
ok("no third-party request is made", requests.every((u) => u.startsWith("/api/")),
  "requests: " + requests.join(", "));

console.log();
if (fails) {
  console.error("UI RED - " + fails + " of " + count + " checks failed");
  process.exit(1);
}
console.log("ui: " + count + " checks green - no 1:1 toggle, verified and pending told apart, ledger memoised, rate survives a slice, the preview says what it could not read");
