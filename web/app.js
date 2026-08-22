/* Front end for gui.py.
 *
 * Python owns the queue and the job options; this file owns the pixels. State
 * changes go out through window.pywebview.api.*, and everything coming back
 * arrives as one call to app.push({kind, ...}).
 */

const $ = (id) => document.getElementById(id);

const ui = {
  drop: $("drop"),
  dropHint: $("drop-hint"),
  choose: $("choose"),
  device: $("device"),
  hintFps: $("hint-fps"),
  hintSlowmo: $("hint-slowmo"),
  hintFactor: $("hint-factor"),
  stage: $("stage"),
  driveCell: $("drive-cell"),
  drive: $("drive"),
  free: $("free"),
  badge: $("badge"),
  badgeText: $("badge-text"),
  status: $("status"),
  percent: $("percent"),
  fill: $("fill"),
  log: $("log"),
  cancel: $("cancel"),
  close: $("close"),
};

const state = { mode: "fps", factor: 2, device: "auto", stage: false, drives: [], busy: false };

// ------------------------------------------------------------------- options

function pushOptions() {
  if (!window.pywebview) return;
  window.pywebview.api.set_options(
    state.mode, state.factor, state.device, state.stage, ui.drive.value || "");
}

// Mode and factor only mean something together - 4x is a doubled rate twice
// over in one mode and a quarter-speed clip in the other - so every label that
// carries a number is rewritten whenever either of them changes.
function describe() {
  const n = state.factor;
  ui.hintFps.textContent = `30 fps becomes ${30 * n}. Same length, audio copied.`;
  ui.hintSlowmo.textContent = `${n}x as long at the same rate. No audio.`;
  ui.hintFactor.textContent = n === 2
    ? "One new frame between every pair."
    : `${n - 1} new frames between every pair.`;
  const tag = state.mode === "slowmo" ? (n === 2 ? "slowmo" : `slowmo${n}x`) : `${n}x`;
  ui.dropHint.textContent = `each one is written beside its source as name.${tag}.mp4`;
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
  ui.device.value = spec;
  pushOptions();
}

// The list arrives a moment after the window does, because naming the GPUs
// means loading torch. Whatever was chosen in the meantime is kept.
function fillDevices(devices) {
  const chosen = state.device;
  ui.device.innerHTML = "";
  [["auto", "Auto"], ...devices].forEach(([spec, label]) => {
    const option = document.createElement("option");
    option.value = spec;
    option.textContent = label;
    ui.device.append(option);
  });
  ui.device.value = chosen;
  setDevice(ui.device.value || "auto");
}

function setStaging(on) {
  state.stage = on;
  ui.stage.setAttribute("aria-pressed", String(on));
  ui.driveCell.setAttribute("aria-disabled", String(!on));
  ui.drive.disabled = !on;
  pushOptions();
}

function showFree() {
  const found = state.drives.find((entry) => entry[0] === ui.drive.value);
  ui.free.textContent = found ? `${Math.round(found[1] / 1073741824)} GB free` : "";
}

function fillDrives(drives, selected) {
  state.drives = drives;
  ui.drive.innerHTML = "";
  drives.forEach(([letter]) => {
    const option = document.createElement("option");
    option.value = letter;
    option.textContent = letter;
    ui.drive.append(option);
  });
  if (selected) ui.drive.value = selected;
  showFree();
}

// -------------------------------------------------------------------- output

function write(text) {
  const line = document.createElement("span");
  line.className = "log__line";
  line.textContent = text;
  ui.log.append(line);
  ui.log.scrollTop = ui.log.scrollHeight;
}

function setBadge(text, live) {
  ui.badgeText.textContent = text;
  ui.badge.dataset.live = String(live);
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
        setBadge(message.failed ? "Finished with errors" : "Done", false);
        break;
      case "busy":
        state.busy = message.value;
        ui.cancel.disabled = !message.value;
        if (message.value) {
          ui.close.hidden = true;
          ui.status.dataset.state = "running";
          setBadge("Running", true);
        } else {
          setProgress(0, 0);
        }
        break;
      case "drives":
        fillDrives(message.drives, message.drive);
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

document.querySelectorAll("[data-mode]").forEach((cell) => {
  cell.addEventListener("click", () => setMode(cell.dataset.mode));
});
document.querySelectorAll("[data-factor]").forEach((cell) => {
  cell.addEventListener("click", () => setFactor(Number(cell.dataset.factor)));
});
ui.device.addEventListener("change", () => setDevice(ui.device.value));
ui.stage.addEventListener("click", () => setStaging(!state.stage));
ui.drive.addEventListener("change", () => {
  showFree();
  pushOptions();
});
ui.drive.addEventListener("click", (event) => event.stopPropagation());

// The drop itself is handled in Python, which is the only side that can see the
// real path of a dropped file; these listeners only paint the hover state and
// let the drop through by cancelling the default handling of a dragover.
["dragenter", "dragover"].forEach((name) => {
  ui.drop.addEventListener(name, (event) => {
    event.preventDefault();
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
  fillDrives(boot.drives, boot.drive);
  setFactor(boot.factor);
  setDevice(boot.device);
  setMode(boot.mode);
  setStaging(boot.stage);
  write(boot.greeting);
});

// ------------------------------------------------------------ dot-map canvas

const canvas = $("dots");
const context = canvas.getContext("2d");
const PITCH = 15;

// The wave runs fast until the app has a video in hand, then eases down into
// slow motion — the thing this tool does, done to its own background. The
// phase is accumulated rather than derived from the clock, so changing speed
// bends the motion instead of jumping it.
const FAST = 0.0055; // ~0.9 wave cycles a second
const SLOW = 0.0007; // ~8x slower: unmistakably crawling, but never frozen
const EASE = 320; // milliseconds to fall into slow motion, and to come back out

let phase = 0;
let speed = FAST;
let last = null;

function paint(now) {
  const step = last === null ? 0 : Math.min(now - last, 100);
  last = now;

  const slowing = state.busy || ui.drop.dataset.dragging === "true";
  speed += ((slowing ? SLOW : FAST) - speed) * (1 - Math.exp(-step / EASE));
  phase += speed * step;

  const ratio = window.devicePixelRatio || 1;
  const width = canvas.clientWidth;
  const height = canvas.clientHeight;
  if (canvas.width !== width * ratio || canvas.height !== height * ratio) {
    canvas.width = width * ratio;
    canvas.height = height * ratio;
  }
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  context.clearRect(0, 0, width, height);

  const reach = height * 0.34;

  for (let x = PITCH / 2; x < width; x += PITCH) {
    const across = x / width;
    // The wave gets denser to the right: one frame becomes two.
    const wave = Math.sin(across * (1.1 + 4.4 * across) * Math.PI * 2 + phase);
    const crest = height / 2 + wave * height * 0.2;
    for (let y = PITCH / 2; y < height; y += PITCH) {
      const near = Math.max(0, 1 - Math.abs(y - crest) / reach);
      const alpha = 0.04 + 0.30 * near * near * (slowing ? 1 : 0.75);
      context.globalAlpha = alpha;
      context.fillStyle = "#f3f3f3";
      context.beginPath();
      context.arc(x, y, 1.1, 0, Math.PI * 2);
      context.fill();
    }
  }
  requestAnimationFrame(paint);
}

requestAnimationFrame(paint);
