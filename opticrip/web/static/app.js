"use strict";

const $ = (id) => document.getElementById(id);

const state = {
  snap: null,
  discKey: "",
  selection: new Map(),   // title index -> boolean
  streams: new Map(),     // title index -> Map(stream index -> boolean)
  logSeq: -1,
  polling: false,
};

function text(value, fallback = "") {
  return value === null || value === undefined || value === "" ? fallback : String(value);
}

function setStatus(message, tone = "") {
  const element = $("status");
  element.textContent = message;
  element.className = `status ${tone}`.trim();
}

function showError(message) {
  const element = $("last");
  element.className = "last error";
  element.textContent = message;
}

async function request(path, body) {
  const options = body === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
  const response = await fetch(path, options);
  let data = {};
  try {
    data = await response.json();
  } catch (error) {
    /* a proxy or crash produced a non-JSON body */
  }
  if (!response.ok) {
    throw new Error(data.error || `HTTP ${response.status}`);
  }
  return data;
}

async function post(path, body) {
  try {
    const data = await request(path, body);
    if (data.state) {
      apply(data.state);
    }
  } catch (error) {
    showError(error.message);
  }
}

function syncSelection(disc) {
  const seen = new Set();
  for (const title of disc.titles) {
    seen.add(title.index);
    if (!state.selection.has(title.index)) {
      state.selection.set(title.index, Boolean(title.selected));
    }
    let streams = state.streams.get(title.index);
    if (!streams) {
      streams = new Map();
      state.streams.set(title.index, streams);
    }
    for (const stream of title.streams) {
      if (!streams.has(stream.index)) {
        streams.set(stream.index, Boolean(stream.selected));
      }
    }
  }
  for (const index of Array.from(state.selection.keys())) {
    if (!seen.has(index)) {
      state.selection.delete(index);
      state.streams.delete(index);
    }
  }
}

function titleCard(title) {
  const item = document.createElement("li");
  item.className = "title";

  const head = document.createElement("label");
  head.className = "title-head";

  const box = document.createElement("input");
  box.type = "checkbox";
  box.checked = Boolean(state.selection.get(title.index));
  box.addEventListener("change", () => {
    state.selection.set(title.index, box.checked);
    updateButtons();
  });

  const body = document.createElement("span");
  body.className = "title-text";
  const name = document.createElement("strong");
  name.textContent = `Title ${title.index}`;
  const meta = document.createElement("span");
  meta.className = "meta";
  const chapters = Number(title.chapters) || 0;
  meta.textContent = [
    text(title.duration_text),
    text(title.size_text),
    `${chapters} chapter${chapters === 1 ? "" : "s"}`,
    text(title.comment),
  ].filter(Boolean).join(" · ");
  body.append(name, meta);
  head.append(box, body);
  item.append(head);

  if (Array.isArray(title.streams) && title.streams.length) {
    const details = document.createElement("details");
    const summary = document.createElement("summary");
    summary.textContent = `Tracks (${title.streams.length})`;
    details.append(summary);

    const tracks = document.createElement("ul");
    tracks.className = "tracks";
    for (const stream of title.streams) {
      const row = document.createElement("li");
      const label = document.createElement("label");
      label.className = "track";
      const trackBox = document.createElement("input");
      trackBox.type = "checkbox";
      const map = state.streams.get(title.index);
      trackBox.checked = Boolean(map && map.get(stream.index));
      trackBox.addEventListener("change", () => {
        if (map) {
          map.set(stream.index, trackBox.checked);
        }
      });
      const caption = document.createElement("span");
      caption.textContent = text(stream.display,
        `${text(stream.kind_label, "Track")}: ${text(stream.codec, stream.codec_id)}`);
      label.append(trackBox, caption);
      row.append(label);
      tracks.append(row);
    }
    details.append(tracks);
    item.append(details);
  }
  return item;
}

function renderDisc(disc) {
  $("empty").hidden = true;
  $("disc-view").hidden = false;
  $("disc-label").textContent = text(disc.label || disc.volume_name, text(disc.kind, "Disc"));

  const meta = [
    text(disc.kind),
    text(disc.size_text),
    disc.protection && disc.protection !== "None" ? `protection: ${disc.protection}` : "",
    disc.encrypted ? "encrypted" : "",
    disc.uhd ? `UHD ${text(disc.aacs_generation, "AACS 2.x")} — refused` : "",
  ].filter(Boolean);
  $("disc-meta").textContent = meta.join(" · ");

  const titles = Array.isArray(disc.titles) ? disc.titles : [];
  $("title-count").textContent = `${titles.length} title${titles.length === 1 ? "" : "s"}`;
  const list = $("titles");
  list.replaceChildren();
  for (const title of titles) {
    list.append(titleCard(title));
  }
  updateButtons();
}

function renderDrives(drives) {
  const list = $("drives");
  list.replaceChildren();
  if (!Array.isArray(drives) || !drives.length) {
    return;
  }
  for (const drive of drives) {
    const item = document.createElement("li");
    const label = document.createElement("span");
    const name = text(drive.name || drive.device, "Drive");
    label.innerHTML = "";
    const strong = document.createElement("span");
    strong.className = "name";
    strong.textContent = name;
    label.append(strong, document.createTextNode(
      `  ${text(drive.label, "no disc")} · ${text(drive.device, "")}`.trimEnd()));
    const use = document.createElement("button");
    use.type = "button";
    use.className = "ghost small";
    use.textContent = "Use";
    use.addEventListener("click", () => {
      $("source").value = `disc:${drive.index}`;
    });
    item.append(label, use);
    list.append(item);
  }
}

function renderProgress(snap) {
  const progress = snap.progress || {};
  const total = Number(progress.total) || 65536;
  const current = Math.max(0, Number(progress.current) || 0);
  const percent = Math.max(0, Math.min(100, total ? (current / total) * 100 : 0));
  $("progress-bar").style.width = `${percent.toFixed(1)}%`;

  let message = "";
  if (snap.busy) {
    message = `${text(progress.phase, snap.job || "working")} · ${percent.toFixed(0)}%`;
    if (progress.title_total) {
      message += ` · title ${text(progress.title_number, "?")}/${progress.title_total}`;
    }
  } else if (snap.error) {
    message = snap.error;
  } else if (snap.last && snap.last.ok) {
    message = "Last operation completed.";
  }
  $("progress-text").textContent = message || "idle";
}

function renderLast(snap) {
  const element = $("last");
  element.className = "last";
  const last = snap.last;
  if (!last) {
    if (snap.error) {
      element.classList.add("error");
      element.textContent = snap.error;
    } else {
      element.textContent = "";
    }
    return;
  }
  if (last.ok) {
    element.classList.add("ok");
    if (last.kind === "save") {
      element.textContent = last.path
        ? `Saved ${last.path}`
        : `Saved ${text(last.count, "0")} title(s).`;
    } else if (last.kind === "open") {
      element.textContent = `Opened ${text(last.label, "source")}.`;
    } else if (last.kind === "backup") {
      element.textContent = "Backup complete.";
    } else {
      element.textContent = "Done.";
    }
  } else {
    element.classList.add("error");
    element.textContent = text(last.message, "Operation failed.");
  }
}

function renderLog(entries) {
  const log = $("log");
  if (!Array.isArray(entries) || !entries.length) {
    if (state.logSeq >= 0) {
      log.replaceChildren();
      state.logSeq = -1;
    }
    return;
  }
  for (const entry of entries) {
    const seq = Number(entry.seq);
    if (!Number.isFinite(seq) || seq <= state.logSeq) {
      continue;
    }
    const item = document.createElement("li");
    item.textContent = text(entry.text);
    if (Number(entry.flags) & 2) {
      item.className = "error";
    }
    log.append(item);
    state.logSeq = seq;
  }
  while (log.children.length > 250) {
    log.firstElementChild.remove();
  }
  log.scrollTop = log.scrollHeight;
}

function updateButtons() {
  const snap = state.snap || {};
  const busy = Boolean(snap.busy);
  const hasDisc = Boolean(snap.disc);
  const anySelected = Array.from(state.selection.values()).some(Boolean);
  $("open-btn").disabled = busy;
  $("drives-btn").disabled = busy;
  $("close-btn").disabled = busy || !hasDisc;
  $("save-btn").disabled = busy || !hasDisc || !anySelected;
  $("cancel-btn").disabled = !busy;
  $("backup-btn").disabled = busy;
}

function apply(snap) {
  state.snap = snap;
  $("version").textContent = `v${text(snap.version, "?")}`;

  if (snap.busy) {
    setStatus(`${text(snap.job, "working")}…`, "busy");
  } else if (snap.error) {
    setStatus("error", "error");
  } else if (snap.disc) {
    setStatus("disc loaded", "ready");
  } else {
    setStatus("idle");
  }

  if (!$("dest").value && snap.output_dir) {
    $("dest").value = snap.output_dir;
  }
  if (!$("backup-spec").value && snap.source) {
    $("backup-spec").value = snap.source;
  }

  renderDrives(snap.drives || []);

  if (snap.disc) {
    const key = JSON.stringify(snap.disc);
    if (key !== state.discKey) {
      state.discKey = key;
      syncSelection(snap.disc);
      renderDisc(snap.disc);
    }
  } else {
    state.discKey = "";
    $("disc-view").hidden = true;
    $("empty").hidden = false;
    $("titles").replaceChildren();
  }

  renderProgress(snap);
  renderLast(snap);
  renderLog(snap.log || []);
  updateButtons();
}

async function refresh() {
  if (state.polling) {
    return;
  }
  state.polling = true;
  try {
    apply(await request("/api/state"));
  } catch (error) {
    setStatus("offline", "error");
  } finally {
    state.polling = false;
  }
}

function collectSelection() {
  const titles = [];
  const streams = {};
  for (const [index, checked] of state.selection.entries()) {
    if (!checked) {
      continue;
    }
    titles.push(index);
    const map = state.streams.get(index) || new Map();
    streams[String(index)] = Array.from(map.entries())
      .filter(([, selected]) => selected)
      .map(([stream]) => stream);
  }
  return { titles, streams };
}

$("open-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const spec = $("source").value.trim();
  if (!spec) {
    showError("Enter a source first.");
    return;
  }
  post("/api/open", { spec });
});

$("drives-btn").addEventListener("click", () => post("/api/drives", {}));

$("close-btn").addEventListener("click", () => {
  state.selection.clear();
  state.streams.clear();
  state.discKey = "";
  post("/api/close", {});
});

$("save-btn").addEventListener("click", () => {
  const { titles, streams } = collectSelection();
  if (!titles.length) {
    showError("Select at least one title.");
    return;
  }
  const dest = $("dest").value.trim();
  if (!dest) {
    showError("Choose an output folder.");
    return;
  }
  post("/api/save_all", { titles, streams, dest });
});

$("cancel-btn").addEventListener("click", () => post("/api/cancel", {}));

$("backup-btn").addEventListener("click", () => {
  const spec = $("backup-spec").value.trim() || $("source").value.trim();
  if (!spec) {
    showError("Enter a drive source for the backup.");
    return;
  }
  const dest = $("dest").value.trim();
  if (!dest) {
    showError("Choose a destination folder.");
    return;
  }
  post("/api/backup", {
    spec,
    dest,
    decrypt: $("backup-decrypt").checked,
  });
});

$("select-all").addEventListener("click", () => {
  if (!state.snap || !state.snap.disc) {
    return;
  }
  for (const title of state.snap.disc.titles) {
    state.selection.set(title.index, true);
  }
  renderDisc(state.snap.disc);
});

$("select-none").addEventListener("click", () => {
  if (!state.snap || !state.snap.disc) {
    return;
  }
  for (const title of state.snap.disc.titles) {
    state.selection.set(title.index, false);
  }
  renderDisc(state.snap.disc);
});

refresh();
setInterval(refresh, 900);
