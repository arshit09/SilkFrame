/* Front end for gui.py.
 *
 * Python owns the queue and the job options; this file owns the pixels. State
 * changes go out through window.pywebview.api.*, and everything coming back
 * arrives as one call to app.push({kind, ...}).
 */

const $ = (id) => document.getElementById(id);

const ui = {
  drop: $("drop"),
  queue: $("queue"),
  count: $("count"),
  choose: $("choose"),
  chooseMore: $("choose-more"),
  note: $("note"),
  suffix: $("suffix"),
  audioCaption: $("audio-caption"),
  audioSeg: $("audio"),
  audioNote: $("audio-note"),
  scopeAll: $("scope-all"),
  scopeOne: $("scope-one"),
  scopeName: $("scope-name"),
  scopeMenu: $("scope-menu"),
  status: $("status"),
  percent: $("percent"),
  fill: $("fill"),
  log: $("log"),
  start: $("start"),
  stop: $("stop"),
  close: $("close"),
  toolbar: $("toolbar"),
  winControls: $("wincontrols"),
  winMin: $("win-min"),
  winMax: $("win-max"),
  winClose: $("win-close"),
};

const state = {
  mode: "fps", factor: 2, device: "auto", suffix: "", audio: "mute", busy: false,
  items: [], running: false, revision: 0,
  // The waiting file the sidebar is set to, or 0 for all of them - and, so
  // they survive a file being chosen and let go of again, a copy of the
  // settings that stand for all.
  target: 0,
  defaults: { mode: "fps", factor: 2, device: "auto", suffix: "", audio: "mute" },
};

// ----------------------------------------------------------------- listboxes

// A native <select> opens a menu Windows draws itself, which no stylesheet can
// reach, so the list is part of the page instead. Only one is ever open.
const selects = [];

function makeSelect(id, onPick) {
  const root = $(id);
  const trigger = root.querySelector(".select__trigger");
  const shown = root.querySelector(".select__value");
  const menu = root.querySelector(".select__menu");
  const select = { value: "" };

  select.close = () => {
    menu.hidden = true;
    root.dataset.open = "false";
    trigger.setAttribute("aria-expanded", "false");
  };

  select.open = () => {
    selects.forEach((other) => { if (other !== select) other.close(); });
    menu.hidden = false;
    root.dataset.open = "true";
    trigger.setAttribute("aria-expanded", "true");
    // Downwards, unless the window ends before the menu would.
    const room = window.innerHeight - trigger.getBoundingClientRect().bottom;
    menu.dataset.drop = room < menu.offsetHeight + 12 ? "up" : "down";
  };

  // Showing a value is not the same as picking one: filling the list has to
  // leave the options where they were, without telling Python anything.
  select.show = (spec) => {
    select.value = spec;
    const items = [...menu.children];
    items.forEach((item) => {
      item.setAttribute("aria-selected", String(item.dataset.value === spec));
    });
    const found = items.find((item) => item.dataset.value === spec);
    shown.textContent = found ? found.textContent : "";
  };

  select.fill = (options, chosen) => {
    menu.innerHTML = "";
    options.forEach(([spec, text]) => {
      const item = document.createElement("button");
      item.type = "button";
      item.className = "select__item";
      item.setAttribute("role", "option");
      item.dataset.value = spec;
      item.textContent = text;
      item.addEventListener("click", () => {
        select.close();
        select.show(spec);
        onPick(spec);
      });
      menu.append(item);
    });
    select.show(options.some(([spec]) => spec === chosen) ? chosen : "");
  };

  trigger.addEventListener("click", (event) => {
    event.stopPropagation();
    if (menu.hidden) select.open();
    else select.close();
  });
  selects.push(select);
  return select;
}

const closeSelects = () => selects.forEach((select) => select.close());

document.addEventListener("click", closeSelects);
window.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeSelects();
});

const deviceSelect = makeSelect("device", (spec) => change({ device: spec }));

// Naming the GPUs means loading torch, so the real list lands a moment after
// the window does and this stands in until it gets here.
deviceSelect.fill([["auto", "Auto"]], "auto");

// ------------------------------------------------------------------ tooltips

// A GPU name, or the file being worked on, is regularly longer than the strip
// it is given and is cut off with an ellipsis. Hovering one shows the whole of
// it - and only when it really was cut, so nothing ever pops up over text that
// can already be read in full.
const CLIPPED = ".select__trigger, .select__item, .statusbar__text, .queue__name, "
              + ".scope__name";

const tip = document.createElement("div");
tip.className = "tip";
tip.hidden = true;
document.body.append(tip);

const hideTip = () => { tip.hidden = true; };

// A trigger is hovered anywhere across its width, but the value inside it is
// the part that was cut.
const inner = (target) => target.querySelector(".select__value") || target;
const cut = (target) => inner(target).scrollWidth > inner(target).clientWidth + 1;

function showTip(target, words) {
  tip.textContent = words;
  tip.hidden = false;
  // Under the text it belongs to, and inside the window on every side: the
  // size is only known once it is on screen, so it is placed after showing.
  const box = target.getBoundingClientRect();
  const below = window.innerHeight - box.bottom > tip.offsetHeight + 14;
  tip.style.left = `${Math.max(8, Math.min(box.left, window.innerWidth - tip.offsetWidth - 8))}px`;
  tip.style.top = `${below ? box.bottom + 6 : box.top - tip.offsetHeight - 6}px`;
}

// One listener for the window rather than one per element, because the list
// items are built and thrown away as the pickers are filled. A name that was
// cut short is worth more than any explanation standing behind it, so the two
// kinds are tried in that order.
document.addEventListener("mouseover", (event) => {
  if (!event.target.closest) return hideTip();
  const clipped = event.target.closest(CLIPPED);
  if (clipped && cut(clipped)) return showTip(clipped, inner(clipped).textContent);
  const told = event.target.closest("[data-tip]");
  if (told) return showTip(told, told.dataset.tip);
  hideTip();
});

// Nothing sends a mouseover when the text moves out from under a still
// pointer, so a menu that scrolls, a menu that closes and a pointer that
// leaves the window all say so themselves.
document.addEventListener("scroll", hideTip, true);
document.addEventListener("click", hideTip);
document.addEventListener("mouseleave", hideTip);

// ------------------------------------------------------------------- options

/* The sidebar is set either to every video in the list or to the one whose row
 * was clicked, and holds that side's settings. Only what actually changed is
 * sent, along with the file it belongs to - 0 meaning all of them - so setting
 * the factor for everything leaves each file's own suffix where it was. */

const current = () => ({ mode: state.mode, factor: state.factor,
                         device: state.device, suffix: state.suffix,
                         audio: state.audio });

// Stretched sound is not the sound that was there, and each way of stretching
// it is wrong in its own way, so the line under the choice says what will be
// heard. How each one does it is on the cells themselves, where a line of text
// costs the column nothing.
const AUDIO_NOTES = {
  "keep-pitch": "Same pitch, but it echoes and warbles as it slows.",
  "drop-pitch": "Deeper: an octave down at 2x, a rumble by 8x.",
  mute: "No sound track at all.",
};

// Mode and factor only mean something together - 4x is a doubled rate twice
// over in one mode and a quarter-speed clip in the other - so the one line
// that says what they add up to, and the name it would write, are rewritten
// whenever either of them changes. The sound is only a question in slow motion,
// where the clip outruns it; a raised frame rate keeps the length and the audio
// with it, so the whole choice stays out of the way there.
function describe() {
  const n = state.factor;
  const slowmo = state.mode === "slowmo";
  ui.note.textContent = slowmo ? `${n}x longer` : `30 fps becomes ${30 * n}`;
  ui.suffix.placeholder = slowmo ? (n === 2 ? "slowmo" : `slowmo${n}x`) : `${n}x`;
  ui.audioCaption.hidden = !slowmo;
  ui.audioSeg.hidden = !slowmo;
  ui.audioNote.hidden = !slowmo;
  ui.audioNote.textContent = AUDIO_NOTES[state.audio];
}

// Every control put back to what the settings on screen say, telling Python
// nothing: this is how a file that has just been clicked is shown.
function draw() {
  document.querySelectorAll("[data-mode]").forEach((cell) => {
    cell.setAttribute("aria-checked", String(cell.dataset.mode === state.mode));
  });
  document.querySelectorAll("[data-factor]").forEach((cell) => {
    cell.setAttribute("aria-checked", String(Number(cell.dataset.factor) === state.factor));
  });
  document.querySelectorAll("[data-audio]").forEach((cell) => {
    cell.setAttribute("aria-checked", String(cell.dataset.audio === state.audio));
  });
  deviceSelect.show(state.device);
  if (ui.suffix.value !== state.suffix) ui.suffix.value = state.suffix;
  describe();
}

function change(patch) {
  Object.assign(state, patch);
  // What stands for all is only ever what the sidebar holds while no one file
  // is chosen, so it is kept up to date here and put back when one is let go of.
  if (!state.target) state.defaults = current();
  draw();
  if (window.pywebview) window.pywebview.api.set_options(state.target, patch);
}

// Anything Windows will not take in a file name is dropped as it is typed, so
// the field always reads as what lands on the disk. An empty one means the
// automatic name, which is what the placeholder is showing.
const setSuffix = (text) => change({ suffix: text.replace(/[<>:"/\\|?*]/g, "") });

// The list arrives a moment after the window does, because naming the GPUs
// means loading torch. Whatever was chosen in the meantime is kept.
function fillDevices(devices) {
  deviceSelect.fill([["auto", "Auto"], ...devices], state.device);
  change({ device: deviceSelect.value || "auto" });
}

// --------------------------------------------------------------- the queue

/* Python holds the real line of files; this draws it, and lets it be dragged
 * into a different order. Every change - one arriving, one starting, one being
 * dragged past another - moves rows that were already on screen, so each redraw
 * measures where they were, puts them where they now belong, and hands the
 * browser the difference to animate away. */

const rows = new Map();   // id -> the row drawn for it
const LIFT = 4;           // pixels of movement before a press counts as a drag
const LEAVING = 260;      // milliseconds a row takes to fade out of the list
const EDGE = 30;          // how near the end of the list a drag starts scrolling it
const CREEP = 11;         // pixels a frame it scrolls by while held right at the end

let held = null;          // the row under the pointer, and where it started
let waiting = null;       // a snapshot that arrived while a drag was in hand

function build(item) {
  const row = document.createElement("div");
  row.className = "queue__row";
  row.dataset.id = String(item.id);
  row.dataset.entering = "true";   // its first painted state, faded and above
  row.innerHTML = `
    <div class="queue__fill"></div>
    <svg class="queue__grip" viewBox="0 0 10 16" aria-hidden="true">
      <circle cx="3.5" cy="4.5" r="1"></circle><circle cx="6.5" cy="4.5" r="1"></circle>
      <circle cx="3.5" cy="8" r="1"></circle><circle cx="6.5" cy="8" r="1"></circle>
      <circle cx="3.5" cy="11.5" r="1"></circle><circle cx="6.5" cy="11.5" r="1"></circle>
    </svg>
    <span class="queue__place"></span>
    <span class="queue__name"></span>
    <span class="queue__tag meta"></span>
    <button class="queue__kill" type="button" aria-label="Remove from the queue">
      <svg viewBox="0 0 10 10" aria-hidden="true"><path d="M1 1l8 8M9 1l-8 8"></path></svg>
    </button>`;
  row.querySelector(".queue__kill").addEventListener("click", (event) => {
    event.stopPropagation();
    forget(item.id);
  });
  return row;
}

function dress(row, item, place) {
  // A file that has just started over: its bar begins empty again.
  if (row.dataset.state !== item.state) row.querySelector(".queue__fill").style.width = "0";
  row.dataset.state = item.state;
  row.querySelector(".queue__place").textContent = place ? String(place) : "";
  row.querySelector(".queue__name").textContent = item.name;
  row.querySelector(".queue__tag").textContent = item.tag;
}

// A row on its way out is lifted out of the flow first, so the rows below it
// can close the gap while it is still fading.
function leave(row) {
  const box = row.getBoundingClientRect();
  const frame = ui.queue.getBoundingClientRect();
  rows.delete(Number(row.dataset.id));
  row.dataset.leaving = "true";
  row.style.transition = "";
  row.style.position = "absolute";
  row.style.top = `${box.top - frame.top + ui.queue.scrollTop}px`;
  row.style.left = `${box.left - frame.left}px`;
  row.style.width = `${box.width}px`;
  requestAnimationFrame(() => { row.dataset.gone = "true"; });
  setTimeout(() => row.remove(), LEAVING);
}

const standing = () => [...ui.queue.children].filter((row) => !row.dataset.leaving);

function render(items, running) {
  state.items = items;
  state.running = running;

  const before = new Map();
  standing().forEach((row) => {
    before.set(row, row.getBoundingClientRect().top);
    row.style.transition = "none";   // whatever a drag left behind is not the answer
    row.style.transform = "";
  });

  const kept = new Set(items.map((item) => String(item.id)));
  standing().forEach((row) => { if (!kept.has(row.dataset.id)) leave(row); });

  const fresh = [];
  let place = 0;
  items.forEach((item) => {
    let row = rows.get(item.id);
    if (!row) {
      row = build(item);
      rows.set(item.id, row);
      fresh.push(row);
    }
    dress(row, item, item.state === "queued" ? (place += 1) : 0);
    ui.queue.append(row);            // already a child: this only moves it
  });

  // Every row that has moved is put back where it was, the browser is made to
  // look at it there, and then it is let go of - which is the animation.
  standing().forEach((row) => {
    const was = before.get(row);
    if (was === undefined) return;
    const gap = was - row.getBoundingClientRect().top;
    if (Math.abs(gap) > 0.5) row.style.transform = `translateY(${gap}px)`;
  });
  void ui.queue.offsetHeight;
  standing().forEach((row) => { row.style.transition = ""; row.style.transform = ""; });
  fresh.forEach((row) => { delete row.dataset.entering; });

  // The file the sidebar is set to may have started, or been taken out of the
  // line, since the last snapshot; either way it is no longer the one on screen.
  if (state.target && !items.some((item) => item.id === state.target
                                            && item.state === "queued")) choose(0);
  else mark();

  // A file that starts, or is taken out of the line, while the list is open has
  // to leave it as well; a line with nothing left waiting has nothing to show.
  if (scopeMenu.open) { if (firstQueued()) scopeMenu.fill(); else scopeMenu.close(); }

  ui.drop.dataset.empty = String(items.length === 0);
  ui.count.textContent = items.length
    ? `${items.length} ${items.length === 1 ? "video" : "videos"}`
    : "";
  ui.start.hidden = running;
  ui.stop.hidden = !running;
  ui.start.disabled = items.length === 0;
}

// Python is the one that knows the order; a snapshot that predates a move made
// here would put it back, so anything older than the last change is ignored.
function accept(message) {
  if (held) { waiting = message; return; }
  if (message.revision < state.revision) return;
  state.revision = message.revision;
  render(message.items, message.running);
}

function settle() {
  const message = waiting;
  waiting = null;
  if (message) accept(message);
}

function forget(id) {
  state.revision += 1;
  render(state.items.filter((item) => item.id !== id), state.running);
  window.pywebview.api.remove(id);
}

// ---------------------------------------------------------- choosing a file

/* Every file carries its own settings, taken from the sidebar as it arrived.
 * Bringing one up in the sidebar - by clicking its row, or by taking its name
 * out of the list the Selected cell drops down - edits that file alone until it
 * is let go of, by clicking the row again, by clicking past the end of the
 * list, or by All. */

const firstQueued = () => state.items.find((one) => one.state === "queued");

// The Selected cell is a way into the choice as well as a label for it: it
// drops down every video still waiting, by name, so the one to be set is picked
// from the sidebar rather than hunted for in the list. Drawn like the device
// menu and closed by the same click on the page, because only one menu is ever
// open - which is why it is registered among them.
const scopeMenu = {
  open: false,

  close() {
    if (!scopeMenu.open) return;
    scopeMenu.open = false;
    ui.scopeMenu.hidden = true;
    ui.scopeOne.setAttribute("aria-expanded", "false");
  },

  // Only the files still waiting: one that has started is past the point where
  // its settings mean anything.
  fill() {
    ui.scopeMenu.innerHTML = "";
    state.items.filter((one) => one.state === "queued").forEach((item) => {
      const entry = document.createElement("button");
      entry.type = "button";
      entry.className = "select__item";
      entry.setAttribute("role", "option");
      entry.setAttribute("aria-selected", String(item.id === state.target));
      entry.textContent = item.name;
      entry.addEventListener("click", () => { scopeMenu.close(); choose(item.id); });
      ui.scopeMenu.append(entry);
    });
  },

  show() {
    selects.forEach((other) => { if (other !== scopeMenu) other.close(); });
    scopeMenu.fill();
    scopeMenu.open = true;
    ui.scopeMenu.hidden = false;
    ui.scopeOne.setAttribute("aria-expanded", "true");
    // Downwards, unless the window ends before the menu would.
    const room = window.innerHeight - ui.scopeOne.getBoundingClientRect().bottom;
    ui.scopeMenu.dataset.drop = room < ui.scopeMenu.offsetHeight + 12 ? "up" : "down";
  },
};

selects.push(scopeMenu);

function mark() {
  const item = state.items.find((one) => one.id === state.target);
  rows.forEach((row, id) => { row.dataset.chosen = String(id === state.target); });
  // The cell carries the file's name once one has been taken, and stands
  // empty-handed under its own name until then - greyed out while the line has
  // nothing to offer, and saying as much to a pointer left resting on it.
  const waiting = firstQueued();
  ui.scopeName.textContent = item ? item.name : "Selected";
  ui.scopeOne.setAttribute("aria-disabled", String(!waiting));
  ui.scopeOne.dataset.tip = waiting
    ? "Settings for one video, picked from the list"
    : "No videos in the list to pick from";
  ui.scopeAll.setAttribute("aria-checked", String(!item));
  ui.scopeOne.setAttribute("aria-checked", String(Boolean(item)));
}

function choose(id) {
  const item = state.items.find((one) => one.id === id && one.state === "queued");
  const target = item ? item.id : 0;
  if (target === state.target) return;
  state.target = target;
  Object.assign(state, target ? item.options : state.defaults);
  draw();
  mark();
}

// Past the end of the list is still the list, and means none of it - but where
// the pointer is, rather than what the event calls its target. Pressing a row
// captures the pointer, so the row keeps it if the press turns into a drag, and
// a captured pointer hands the click that follows to the queue itself; going by
// the target alone, every click on a row would read as a click past the end and
// let go of the file it had just taken.
ui.queue.addEventListener("click", (event) => {
  const under = document.elementFromPoint(event.clientX, event.clientY);
  if (!under || !under.closest(".queue__row")) choose(0);
});

ui.scopeAll.addEventListener("click", () => choose(0));

// Opening the list is what the cell does, whether or not a video is already
// taken - so the one being set can be swapped for another without letting go of
// it first. Grey and doing nothing while there is nothing waiting to pick.
ui.scopeOne.addEventListener("click", (event) => {
  event.stopPropagation();   // the click on the page is what closes the menus
  hideTip();                 // and what would otherwise have taken this one down
  if (!firstQueued()) return;
  if (scopeMenu.open) scopeMenu.close();
  else scopeMenu.show();
});

// ------------------------------------------------------------ dragging a row

// The row under the pointer follows it, and the rows it passes step aside by
// exactly one place each. Pointer events rather than HTML drag and drop,
// because a drag started in here must not read as a file arriving from outside.
function slide() {
  held.line.forEach((row, index) => {
    if (row === held.row) return;
    let shift = 0;
    if (held.to > held.from && index > held.from && index <= held.to) shift = -held.step;
    if (held.to < held.from && index >= held.to && index < held.from) shift = held.step;
    row.style.transform = shift ? `translateY(${shift}px)` : "";
  });
}

// How far the row has come since it was picked up, counted in the list's own
// coordinates rather than the window's: a list that scrolls under a pointer
// that has not moved has still carried the row somewhere new.
function track() {
  const dy = (held.at - held.y) + (ui.queue.scrollTop - held.scroll);
  const to = Math.max(0, Math.min(held.line.length - 1,
                                  held.from + Math.round(dy / held.step)));
  if (to !== held.to) { held.to = to; slide(); }
  held.row.style.transform = `translateY(${dy}px)`;
}

// A queue longer than the panel would otherwise be impossible to drag across,
// so holding a row near either end scrolls the rest of the list past it.
function creep() {
  if (!held || !held.live) return;
  const box = ui.queue.getBoundingClientRect();
  const down = held.at - box.top;
  let by = 0;
  if (down < EDGE) by = -CREEP * Math.min(1, (EDGE - down) / EDGE);
  else if (down > box.height - EDGE) by = CREEP * Math.min(1, (down - box.height + EDGE) / EDGE);
  if (by) {
    const was = ui.queue.scrollTop;
    ui.queue.scrollTop += by;
    if (ui.queue.scrollTop !== was) track();   // it moved, so the row is elsewhere
  }
  requestAnimationFrame(creep);
}

ui.queue.addEventListener("pointerdown", (event) => {
  if (event.button !== 0 || event.target.closest(".queue__kill")) return;
  const row = event.target.closest(".queue__row");
  if (!row || row.dataset.state !== "queued") return;
  const line = [...ui.queue.querySelectorAll('.queue__row[data-state="queued"]')];
  const at = line.indexOf(row);
  held = { row, line, from: at, to: at, pointer: event.pointerId,
           y: event.clientY, at: event.clientY, scroll: ui.queue.scrollTop,
           step: 0, live: false };
  ui.queue.setPointerCapture(event.pointerId);
});

ui.queue.addEventListener("pointermove", (event) => {
  if (!held || event.pointerId !== held.pointer) return;
  held.at = event.clientY;
  if (!held.live) {
    if (held.line.length < 2) return;                // nothing to reorder it past
    if (Math.abs(held.at - held.y) < LIFT) return;   // a click is not a drag
    held.live = true;
    held.row.dataset.held = "true";
    const tops = held.line.map((row) => row.getBoundingClientRect().top);
    held.step = tops[1] - tops[0];          // one row and the gap under it
    requestAnimationFrame(creep);
  }
  track();
});

function release() {
  if (!held) return;
  const { row, from, to, live } = held;
  const id = Number(row.dataset.id);
  held = null;
  delete row.dataset.held;
  if (!live) {                 // a press that went nowhere: the file is chosen,
    choose(id === state.target ? 0 : id);   // or, if it already was, let go of
    return settle();
  }
  // Redrawn from the order it was dropped into, which animates the row out of
  // the pointer's hand and into its slot, and then Python is told.
  const queued = state.items.filter((item) => item.state === "queued");
  const rest = state.items.filter((item) => item.state !== "queued");
  queued.splice(to, 0, ...queued.splice(from, 1));
  state.revision += 1;
  render([...rest, ...queued], state.running);
  if (to !== from) window.pywebview.api.move(id, to);
  settle();
}

ui.queue.addEventListener("pointerup", release);
ui.queue.addEventListener("pointercancel", release);

// -------------------------------------------------------------------- output

function write(text) {
  const line = document.createElement("span");
  line.className = "log__line";
  line.textContent = text;
  ui.log.append(line);
  ui.log.scrollTop = ui.log.scrollHeight;
}

function setProgress(done, total) {
  const fraction = total > 0 ? Math.min(done / total, 1) : 0;
  ui.fill.style.width = `${fraction * 100}%`;
  ui.percent.textContent = total > 0 ? `${done} / ${total} frames` : "";
  // The same figure again, as a wash filling the row it belongs to.
  const item = state.items.find((one) => one.state === "running");
  const row = item && rows.get(item.id);
  if (row) row.querySelector(".queue__fill").style.width = `${fraction * 100}%`;
}

const app = {
  push(message) {
    switch (message.kind) {
      case "log":
        write(message.text);
        break;
      case "status":
        ui.status.textContent = message.text;
        ui.status.dataset.state = "running";
        break;
      case "progress":
        setProgress(message.done, message.total);
        break;
      case "queue":
        accept(message);
        break;
      case "summary":
        ui.status.textContent = message.text;
        ui.status.dataset.state = message.failed ? "failed" : "done";
        ui.close.hidden = state.items.length > 0;   // still work waiting: not an ending
        break;
      case "busy":
        state.busy = message.value;
        if (message.value) {
          ui.close.hidden = true;
          ui.status.dataset.state = "running";
        } else {
          setProgress(0, 0);
        }
        break;
      case "devices":
        fillDevices(message.devices);
        break;
      default:
        break;
    }
  },
};

window.app = app;

// ------------------------------------------------------------------- wiring

const browse = (event) => {
  event.stopPropagation();
  window.pywebview.api.browse();
};

ui.choose.addEventListener("click", browse);
ui.chooseMore.addEventListener("click", browse);
// The panel itself opens the picker too, except over the list, where a click
// belongs to the row under it.
ui.drop.addEventListener("click", (event) => {
  if (event.target.closest(".queue")) return;
  window.pywebview.api.browse();
});
ui.start.addEventListener("click", () => window.pywebview.api.start());
ui.stop.addEventListener("click", () => window.pywebview.api.stop());
ui.close.addEventListener("click", () => window.pywebview.api.close());

// The toolbar is the title bar. pywebview starts a window drag from any
// mousedown that bubbles up to it, so the buttons standing on it have to keep
// their own clicks to themselves - and a double click anywhere else maximises,
// the way the real title bar did.
ui.winMin.addEventListener("click", () => window.pywebview.api.minimize());
ui.winMax.addEventListener("click", () => window.pywebview.api.toggle_maximize());
ui.winClose.addEventListener("click", () => window.pywebview.api.close());
ui.winControls.addEventListener("mousedown", (event) => event.stopPropagation());
ui.toolbar.addEventListener("dblclick", (event) => {
  if (!ui.winControls.contains(event.target)) window.pywebview.api.toggle_maximize();
});

// And the window has no border either, so the grips are the resize handles.
// Each edge says how far a pointer delta moves the window's top left corner
// and how far it grows the window: west drags the left edge out, east only
// widens, and the corners do both at once.
const EDGES = {
  n: [0, 1, 0, -1], s: [0, 0, 0, 1], w: [1, 0, -1, 0], e: [0, 0, 1, 0],
  nw: [1, 1, -1, -1], ne: [0, 1, 1, -1], sw: [1, 0, -1, 1], se: [0, 0, 1, 1],
};
let grip = null;

document.querySelectorAll(".grip").forEach((strip) => {
  strip.addEventListener("mousedown", (event) => {
    event.stopPropagation();           // an edge is not the title bar
    // Screen coordinates arrive in CSS pixels; the window lives in real ones.
    const scale = window.devicePixelRatio;
    grip = {
      edge: EDGES[strip.dataset.edge], scale,
      x: event.screenX, y: event.screenY,
      box: [window.screenX, window.screenY, window.outerWidth, window.outerHeight],
    };
  });
});

window.addEventListener("mousemove", (event) => {
  if (!grip) return;
  const dx = event.screenX - grip.x;
  const dy = event.screenY - grip.y;
  const [moveX, moveY, growW, growH] = grip.edge;
  const [x, y, width, height] = grip.box;
  window.pywebview.api.resize_window(
    Math.round((x + moveX * dx) * grip.scale), Math.round((y + moveY * dy) * grip.scale),
    Math.round((width + growW * dx) * grip.scale),
    Math.round((height + growH * dy) * grip.scale));
});

window.addEventListener("mouseup", () => { grip = null; });

document.querySelectorAll("[data-mode]").forEach((cell) => {
  cell.addEventListener("click", () => change({ mode: cell.dataset.mode }));
});
document.querySelectorAll("[data-factor]").forEach((cell) => {
  cell.addEventListener("click", () => change({ factor: Number(cell.dataset.factor) }));
});
document.querySelectorAll("[data-audio]").forEach((cell) => {
  cell.addEventListener("click", () => change({ audio: cell.dataset.audio }));
});
ui.suffix.addEventListener("input", () => setSuffix(ui.suffix.value));

// The drop itself is handled in Python, which is the only side that can see the
// real path of a dropped file; these listeners only paint the hover state and
// let the drop through by cancelling the default handling of a dragover.
["dragenter", "dragover"].forEach((name) => {
  ui.drop.addEventListener(name, (event) => {
    event.preventDefault();
    // Where it came in over the edge, not where it has wandered to since: the
    // grid starts under that point, and the panel's own children raise a
    // second dragenter that must not move it.
    if (ui.drop.dataset.dragging !== "true") enter(event.clientX, event.clientY);
    ui.drop.dataset.dragging = "true";
  });
});
["dragleave", "drop"].forEach((name) => {
  ui.drop.addEventListener(name, (event) => {
    event.preventDefault();
    ui.drop.dataset.dragging = "false";
  });
});

window.addEventListener("pywebviewready", async () => {
  const boot = await window.pywebview.api.boot();
  change({ mode: boot.mode, factor: boot.factor,
           device: boot.device, suffix: boot.suffix, audio: boot.audio });
  write(boot.greeting);
});

// ------------------------------------------------------------ dot-map canvas

const canvas = $("dots");
const context = canvas.getContext("2d");

// The dots wander the panel at random until a file is held over it, and then
// sort themselves into a grid and hold it while the job runs - the background
// doing what the tool does, turning something loose into something even.
const PITCH = 24;      // roughly the spacing of the formed grid
const MARGIN = 20;     // keeps the outer rows off the rounded corners
const SLOWEST = 90;    // pixels a second, adrift
const FASTEST = 220;
const TURN = 5;        // radians a second of aimless steering
const SETTLE = 620;    // milliseconds one dot takes to reach its slot
const STAGGER = 380;   // milliseconds between the first slot filling and the last
const RELEASE = 300;   // milliseconds the grid is held once nothing asks for it
const SWELL = 520;     // milliseconds the wave takes to rise once the work starts
const WAVE = 150;      // pixels from one crest of it to the next
const PERIOD = 2400;   // milliseconds a crest takes to travel one wavelength
const CREST = 5;       // pixels a dot rides either side of its slot
const LOOSEN = 420;    // milliseconds the grid takes to come apart into drift

// Out fast, in slow, and a little past the slot before it comes to rest.
const ease = (t) => 1 + 1.7 * (t - 1) ** 3 + 0.7 * (t - 1) ** 2;
const random = (low, high) => low + Math.random() * (high - low);

let dots = [];
let slots = [];
let formed = false;
let origin = null;   // where the file crossed the edge, in canvas coordinates
let since = 0;   // milliseconds since the dots last changed their minds
let calm = 0;    // milliseconds nothing has asked for the grid
let swell = 0;   // how much of the wave is showing, nothing to all of it
let clock = 0;
let last = null;
let width = 0;
let height = 0;

function spawn() {
  const angle = random(0, Math.PI * 2);
  const pace = random(SLOWEST, FASTEST);
  return {
    x: random(0, width), y: random(0, height),
    vx: Math.cos(angle) * pace, vy: Math.sin(angle) * pace,
    fx: 0, fy: 0, slot: 0, settled: 0, spark: Math.random(),
  };
}

// The grid is rebuilt whenever the panel changes size: even spacing across
// whatever is left inside the margin, and one dot for each slot, no more.
function rebuild() {
  const across = Math.max(width - MARGIN * 2, 0);
  const down = Math.max(height - MARGIN * 2, 0);
  const cols = Math.max(2, Math.round(across / PITCH) + 1);
  const rows = Math.max(2, Math.round(down / PITCH) + 1);

  slots = [];
  for (let row = 0; row < rows; row += 1) {
    for (let col = 0; col < cols; col += 1) {
      slots.push({
        x: MARGIN + (across * col) / (cols - 1),
        y: MARGIN + (down * row) / (rows - 1),
        delay: 0,   // set when the grid forms: it depends where the file came in
      });
    }
  }
  while (dots.length > slots.length) dots.pop();
  while (dots.length < slots.length) dots.push(spawn());
  if (formed) form();
}

// Every slot takes the nearest dot still going spare, so the grid comes
// together out of short moves rather than a mass crossing of the panel.
function assign() {
  const taken = dots.map(() => false);
  slots.forEach((slot, index) => {
    let best = 0;
    let least = Infinity;
    for (let d = 0; d < dots.length; d += 1) {
      if (taken[d]) continue;
      const gap = (dots[d].x - slot.x) ** 2 + (dots[d].y - slot.y) ** 2;
      if (gap < least) { least = gap; best = d; }
    }
    taken[best] = true;
    dots[best].slot = index;
  });
}

// The order spreads out from the point the file crossed the edge at, one ring
// at a time, reaching the far corner of the panel a full stagger later. With no
// pointer to go by - a job started from the file picker - it opens out of the
// middle instead.
function form() {
  assign();
  const from = origin || { x: width / 2, y: height / 2 };
  let far = 1;
  slots.forEach((slot) => {
    slot.delay = Math.hypot(slot.x - from.x, slot.y - from.y);
    far = Math.max(far, slot.delay);
  });
  slots.forEach((slot) => { slot.delay = (STAGGER * slot.delay) / far; });
  dots.forEach((dot) => { dot.fx = dot.x; dot.fy = dot.y; });
  since = 0;
}

// The pointer is somewhere in the window; the ripple starts somewhere on the
// canvas.
function enter(clientX, clientY) {
  const box = canvas.getBoundingClientRect();
  origin = { x: clientX - box.left, y: clientY - box.top };
}

// Order breaks back into drift from wherever each dot had got to, every one of
// them heading somewhere new.
function scatter() {
  origin = null;
  dots.forEach((dot) => {
    const angle = random(0, Math.PI * 2);
    const pace = random(SLOWEST, FASTEST);
    dot.vx = Math.cos(angle) * pace;
    dot.vy = Math.sin(angle) * pace;
  });
  since = 0;
}

function drift(dot, secs) {
  // A small random turn each frame, so the paths curve about the panel rather
  // than ruling straight lines across it.
  const turn = random(-TURN, TURN) * secs;
  const cos = Math.cos(turn);
  const sin = Math.sin(turn);
  const vx = dot.vx * cos - dot.vy * sin;
  const vy = dot.vx * sin + dot.vy * cos;
  dot.vx = vx;
  dot.vy = vy;
  dot.x += vx * secs;
  dot.y += vy * secs;
  if (dot.x < 0 || dot.x > width) {          // the panel has edges to bounce off
    dot.vx = -dot.vx;
    dot.x = Math.min(Math.max(dot.x, 0), width);
  }
  if (dot.y < 0 || dot.y > height) {
    dot.vy = -dot.vy;
    dot.y = Math.min(Math.max(dot.y, 0), height);
  }
}

function paint(now) {
  const step = last === null ? 0 : Math.min(now - last, 100);
  last = now;
  clock += step;
  since += step;

  if (canvas.clientWidth !== width || canvas.clientHeight !== height) {
    width = canvas.clientWidth;
    height = canvas.clientHeight;
    rebuild();
  }
  const ratio = window.devicePixelRatio || 1;
  if (canvas.width !== width * ratio || canvas.height !== height * ratio) {
    canvas.width = width * ratio;
    canvas.height = height * ratio;
  }
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  context.clearRect(0, 0, width, height);

  const wanted = state.busy || ui.drop.dataset.dragging === "true";
  calm = wanted ? 0 : calm + step;
  // Between the file landing and Python answering that it is busy there is a
  // frame or two where nothing is asking for the grid; holding the release
  // back that long keeps one drop from reading as two.
  if (wanted && !formed) {
    formed = true;
    form();
  } else if (!wanted && formed && calm > RELEASE) {
    formed = false;
    scatter();
  }

  // A file held over the panel gets the grid standing still; a job actually
  // running gets a wave crossing it. The wave falls away faster than it rises,
  // so there is next to none of it left by the time the grid breaks up.
  const swelling = formed && state.busy;
  swell += ((swelling ? 1 : 0) - swell) * (1 - Math.exp(-step / (swelling ? SWELL : SWELL / 3)));

  const secs = step / 1000;
  context.fillStyle = "#f3f3f3";

  // A dot leaving the grid picks its drift up over the loosen instead of in one
  // frame, so a stopped job lets the formation come apart rather than flinging
  // it open.
  const loose = Math.min(since / LOOSEN, 1);

  dots.forEach((dot) => {
    let lift = 0;
    if (formed) {
      const slot = slots[dot.slot];
      dot.settled = Math.max(0, Math.min((since - slot.delay) / SETTLE, 1));
      const shift = ease(dot.settled);
      // Written back, so the drift picks up wherever the grid let go.
      dot.x = dot.fx + (slot.x - dot.fx) * shift;
      dot.y = dot.fy + (slot.y - dot.fy) * shift;
      // Crests run down and across the grid, and each dot rides the one it is
      // standing under. Off the slot rather than the dot, so a dot still on its
      // way in is not made to swim there.
      const phase = (slot.x + slot.y * 0.5) / WAVE - clock / PERIOD;
      lift = Math.sin(phase * Math.PI * 2) * CREST * swell * dot.settled;
    } else {
      drift(dot, secs * loose);
      // The light the grid was holding goes out over the same stretch.
      dot.settled *= Math.exp(-step / (LOOSEN / 3));
    }
    // A slow band of light crosses the grid once it is standing still.
    const band = 0.5 + 0.5 * Math.sin((dot.x / Math.max(width, 1)) * Math.PI * 2 - clock * 0.0011);
    context.globalAlpha = 0.14 + 0.20 * dot.spark + dot.settled * (0.12 + 0.20 * band);
    context.beginPath();
    context.arc(dot.x, dot.y + lift, 1.2, 0, Math.PI * 2);
    context.fill();
  });

  requestAnimationFrame(paint);
}

requestAnimationFrame(paint);
