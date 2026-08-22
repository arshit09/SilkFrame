/* Front end for gui.py.
 *
 * Python owns the queue and the job options; this file owns the pixels. State
 * changes go out through window.pywebview.api.*, and everything coming back
 * arrives as one call to app.push({kind, ...}).
 */

const $ = (id) => document.getElementById(id);

const ui = {
  drop: $("drop"),
  choose: $("choose"),
  note: $("note"),
  suffix: $("suffix"),
  status: $("status"),
  percent: $("percent"),
  fill: $("fill"),
  log: $("log"),
  cancel: $("cancel"),
  close: $("close"),
  toolbar: $("toolbar"),
  winControls: $("wincontrols"),
  winMin: $("win-min"),
  winMax: $("win-max"),
  winClose: $("win-close"),
};

const state = {
  mode: "fps", factor: 2, device: "auto", suffix: "", busy: false,
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

const deviceSelect = makeSelect("device", (spec) => setDevice(spec));

// Naming the GPUs means loading torch, so the real list lands a moment after
// the window does and this stands in until it gets here.
deviceSelect.fill([["auto", "Auto"]], "auto");

// ------------------------------------------------------------------ tooltips

// A GPU name, or the file being worked on, is regularly longer than the strip
// it is given and is cut off with an ellipsis. Hovering one shows the whole of
// it - and only when it really was cut, so nothing ever pops up over text that
// can already be read in full.
const CLIPPED = ".select__trigger, .select__item, .statusbar__text";

const tip = document.createElement("div");
tip.className = "tip";
tip.hidden = true;
document.body.append(tip);

const hideTip = () => { tip.hidden = true; };

function showTip(target) {
  // A trigger is hovered anywhere across its width, but the value inside it is
  // the part that was cut.
  const text = target.querySelector(".select__value") || target;
  if (text.scrollWidth <= text.clientWidth + 1) return hideTip();
  tip.textContent = text.textContent;
  tip.hidden = false;
  // Under the text it belongs to, and inside the window on every side: the
  // size is only known once it is on screen, so it is placed after showing.
  const box = target.getBoundingClientRect();
  const below = window.innerHeight - box.bottom > tip.offsetHeight + 14;
  tip.style.left = `${Math.max(8, Math.min(box.left, window.innerWidth - tip.offsetWidth - 8))}px`;
  tip.style.top = `${below ? box.bottom + 6 : box.top - tip.offsetHeight - 6}px`;
}

// One listener for the window rather than one per element, because the list
// items are built and thrown away as the pickers are filled.
document.addEventListener("mouseover", (event) => {
  const target = event.target.closest ? event.target.closest(CLIPPED) : null;
  if (target) showTip(target);
  else hideTip();
});

// Nothing sends a mouseover when the text moves out from under a still
// pointer, so a menu that scrolls, a menu that closes and a pointer that
// leaves the window all say so themselves.
document.addEventListener("scroll", hideTip, true);
document.addEventListener("click", hideTip);
document.addEventListener("mouseleave", hideTip);

// ------------------------------------------------------------------- options

function pushOptions() {
  if (!window.pywebview) return;
  window.pywebview.api.set_options(state.mode, state.factor, state.device, state.suffix);
}

// Mode and factor only mean something together - 4x is a doubled rate twice
// over in one mode and a quarter-speed clip in the other - so the one line
// that says what they add up to, and the name it would write, are rewritten
// whenever either of them changes.
function describe() {
  const n = state.factor;
  ui.note.textContent = state.mode === "slowmo"
    ? `${n}x longer, no audio`
    : `30 fps becomes ${30 * n}, audio kept`;
  ui.suffix.placeholder = state.mode === "slowmo"
    ? (n === 2 ? "slowmo" : `slowmo${n}x`)
    : `${n}x`;
}

function setMode(mode) {
  state.mode = mode;
  document.querySelectorAll("[data-mode]").forEach((cell) => {
    cell.setAttribute("aria-checked", String(cell.dataset.mode === mode));
  });
  describe();
  pushOptions();
}

function setFactor(factor) {
  state.factor = factor;
  document.querySelectorAll("[data-factor]").forEach((cell) => {
    cell.setAttribute("aria-checked", String(Number(cell.dataset.factor) === factor));
  });
  describe();
  pushOptions();
}

function setDevice(spec) {
  state.device = spec;
  deviceSelect.show(spec);
  pushOptions();
}

// Anything Windows will not take in a file name is dropped as it is typed, so
// the field always reads as what lands on the disk. An empty one means the
// automatic name, which is what the placeholder is showing.
function setSuffix(text) {
  const kept = text.replace(/[<>:"/\\|?*]/g, "");
  if (ui.suffix.value !== kept) ui.suffix.value = kept;
  state.suffix = kept;
  pushOptions();
}

// The list arrives a moment after the window does, because naming the GPUs
// means loading torch. Whatever was chosen in the meantime is kept.
function fillDevices(devices) {
  deviceSelect.fill([["auto", "Auto"], ...devices], state.device);
  setDevice(deviceSelect.value || "auto");
}

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
      case "summary":
        ui.status.textContent = message.text;
        ui.status.dataset.state = message.failed ? "failed" : "done";
        ui.close.hidden = false;
        break;
      case "busy":
        state.busy = message.value;
        ui.cancel.disabled = !message.value;
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

ui.choose.addEventListener("click", (event) => {
  event.stopPropagation();
  window.pywebview.api.browse();
});
ui.drop.addEventListener("click", () => window.pywebview.api.browse());
ui.cancel.addEventListener("click", () => window.pywebview.api.cancel());
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
  cell.addEventListener("click", () => setMode(cell.dataset.mode));
});
document.querySelectorAll("[data-factor]").forEach((cell) => {
  cell.addEventListener("click", () => setFactor(Number(cell.dataset.factor)));
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
  setFactor(boot.factor);
  setDevice(boot.device);
  setSuffix(boot.suffix);
  setMode(boot.mode);
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

// Out fast, in slow, and a little past the slot before it comes to rest.
const ease = (t) => 1 + 1.7 * (t - 1) ** 3 + 0.7 * (t - 1) ** 2;
const random = (low, high) => low + Math.random() * (high - low);

let dots = [];
let slots = [];
let formed = false;
let origin = null;   // where the file crossed the edge, in canvas coordinates
let since = 0;   // milliseconds since the dots last changed their minds
let calm = 0;    // milliseconds nothing has asked for the grid
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
    fx: 0, fy: 0, slot: 0, spark: Math.random(),
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

  const secs = step / 1000;
  context.fillStyle = "#f3f3f3";

  dots.forEach((dot) => {
    let settled = 0;
    if (formed) {
      const slot = slots[dot.slot];
      settled = Math.max(0, Math.min((since - slot.delay) / SETTLE, 1));
      const shift = ease(settled);
      // Written back, so the drift picks up wherever the grid let go.
      dot.x = dot.fx + (slot.x - dot.fx) * shift;
      dot.y = dot.fy + (slot.y - dot.fy) * shift;
    } else {
      drift(dot, secs);
    }
    // A slow band of light crosses the grid once it is standing still.
    const band = 0.5 + 0.5 * Math.sin((dot.x / Math.max(width, 1)) * Math.PI * 2 - clock * 0.0011);
    context.globalAlpha = 0.14 + 0.20 * dot.spark + settled * (0.12 + 0.20 * band);
    context.beginPath();
    context.arc(dot.x, dot.y, 1.2, 0, Math.PI * 2);
    context.fill();
  });

  requestAnimationFrame(paint);
}

requestAnimationFrame(paint);
