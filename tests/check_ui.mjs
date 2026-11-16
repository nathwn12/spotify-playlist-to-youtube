#!/usr/bin/env node
/* Headless check of the shipped page UI: index.html's inline script is run in a
   stub DOM and driven through the same mergeRun()/render() path the poll uses.

   What it proves, and why each one is here:
   - the 1:1 toggle is gone and nothing still calls /api/one-to-one;
   - a verified count and a sent-but-unread count are shown as two different
     things (counter, counts row, pill, progress band), on the current payload
     shape and on the older one that carries no `pending`;
   - a terminal line never claims success over rows the read did not confirm;
   - the ledger's sort, tally, missing list and follow index are memoised per
     row revision: repeated paints and filter changes do not rescans a large
     playlist, and one arriving row does;
   - a verified jump (a slice read back at once) does not blank the rate/ETA,
     while a count that goes backwards does.

   No network, no browser, no dependencies. Run: node tests/check_ui.mjs
   (exit 0 only when every check passes). */
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import {fileURLToPath} from "node:url";

const ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const html = fs.readFileSync(path.join(ROOT, "index.html"), "utf8");
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
function jsonResponse(payload) {
  return {ok: true, json: async () => payload, text: async () => JSON.stringify(payload)};
}
async function stubFetch(url) {
  const at = String(url);
  requests.push(at);
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
function feed(payload, full = false) {
  run("mergeRun(" + JSON.stringify(payload) + "," + (full ? "true" : "false") + ");render();");
}

/* ---------- 1. the 1:1 toggle is gone ---------- */
ok("no 1:1 control in the markup", !html.includes('id="oneone"'), "#oneone button still in index.html");
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
ok("counter title names the waiting rows", el("bignum").title.includes("100 sent, awaiting the playlist read"),
  "counter title: " + el("bignum").title);
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

/* ---------- 3. the older payload shape, with no `pending` field ---------- */
run("clearLocal();");
feed({
  run_id: "r2", seq: 3, phase: "append", state: "running", total: 508, declared: 508,
  counts: {source: 508, unique: 480, added: 120, skipped: 4, duplicate: 0,
    unverified: 0, refused: 0},
});
ok("legacy counts derive the waiting rows instead of dropping them",
  chip("pending") === "pending 384" && el("breakdown").innerHTML.includes("<b>384</b> pending"),
  "chip " + chip("pending") + " · counts " + el("breakdown").innerHTML);
ok("legacy counts still show the verified adds", el("bignum").textContent === "120",
  "counter read " + el("bignum").textContent);

feed({
  seq: 4,
  counts: {source: 100, unique: 100, added: 120, skipped: 0, duplicate: 0,
    unverified: 0, refused: 0},
});
ok("a count that implies nothing waiting shows no pending",
  chip("pending") === "pending 0" && !el("breakdown").innerHTML.includes("pending"),
  "chip " + chip("pending") + " · counts " + el("breakdown").innerHTML);

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
console.log("ui: " + count + " checks green - no 1:1 toggle, verified and pending told apart, ledger memoised, rate survives a slice");
