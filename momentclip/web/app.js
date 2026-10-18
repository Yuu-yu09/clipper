/* Clipper front end. Plain JS, no build step. */
(() => {
  "use strict";

  const $ = (s) => document.querySelector(s);
  const $$ = (s) => Array.from(document.querySelectorAll(s));
  const el = (tag, cls, html) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (html != null) n.innerHTML = html;
    return n;
  };
  const esc = (s) =>
    String(s ?? "").replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  let pollTimer = null;
  let currentJob = null;

  // ---------------------------------------------------------------- tabs
  $$(".tab").forEach((t) =>
    t.addEventListener("click", () => {
      $$(".tab").forEach((x) => x.classList.remove("active"));
      $$(".tabpane").forEach((x) => x.classList.remove("active"));
      t.classList.add("active");
      $(`.tabpane[data-pane="${t.dataset.tab}"]`).classList.add("active");
    })
  );

  // --------------------------------------------------------- segmented
  const wire = (sel, fn) =>
    $$(`${sel} button`).forEach((b) =>
      b.addEventListener("click", () => {
        $$(`${sel} button`).forEach((x) => x.classList.remove("on"));
        b.classList.add("on");
        fn(b.dataset.v);
      })
    );
  const pick = (sel) => {
    const on = $(`${sel} button.on`);
    return on ? on.dataset.v : null;
  };
  wire("#persona", () => {});
  wire("#clen", () => {});
  wire("#aspect", () => {});
  wire("#cstyle", () => {});
  wire("#mood", () => {});

  // -------------------------------------------------------------- drop
  const drop = $("#drop");
  const fileInput = $("#file");
  // NOTE: no click handler here on purpose. #drop is a <label> wrapping the
  // input, so the browser already forwards clicks to it -- adding
  // fileInput.click() opened the file dialog a SECOND time, which looked
  // like a dead upload button.
  ["dragenter", "dragover"].forEach((e) =>
    drop.addEventListener(e, (ev) => { ev.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach((e) =>
    drop.addEventListener(e, (ev) => { ev.preventDefault(); drop.classList.remove("over"); }));
  drop.addEventListener("drop", (ev) => {
    if (ev.dataTransfer.files?.length) {
      fileInput.files = ev.dataTransfer.files;
      showPick();
    }
  });
  fileInput.addEventListener("change", showPick);
  function showPick() {
    const f = fileInput.files?.[0];
    $("#pickname").textContent = f ? `✓ ${f.name}  (${(f.size / 1e6).toFixed(1)} MB)` : "";
  }

  // ---------------------------------------------------------- caps bar
  async function loadCaps() {
    try {
      const c = await (await fetch("/api/health")).json();
      const cap = (label, ok, extra = "") =>
        `<span class="cap ${ok ? "on" : "off"}">${ok ? "✓" : "✕"} ${esc(label)}${extra}</span>`;
      $("#caps").innerHTML = [
        cap("ffmpeg", c.ffmpeg),
        cap("transcribe", c.transcription.faster_whisper || c.transcription.http),
        cap("captions", c.libass),
        cap("face track", c.face_tracking ?? c.opencv),
        cap("yt-dlp", c.yt_dlp),
        cap("no watermark", c.watermark === false),
      ].join("");
      if (!c.ffmpeg) {
        showErr("FFmpeg was not found. Install it and restart the server:\n  winget install Gyan.FFmpeg\nor set FFMPEG_BIN / FFPROBE_BIN to absolute paths.");
      }
      if (!c.transcription.faster_whisper && !c.transcription.http) {
        showErr("No transcription backend. Clips will still be found from audio signals, but without a transcript. Install with:  pip install faster-whisper");
      }
      if (!c.opencv) {
        $("#reframe").checked = false;
        $("#reframe").disabled = true;
      } else if (c.face_tracking === false) {
        $("#reframe").checked = false;
        $("#reframe").disabled = true;
        showErr("OpenCV is installed but ships no face cascade (OpenCV 5 dropped it).\n" +
                "Speaker-aware reframing is off. Fix with:\n" +
                "  pip install \"opencv-python-headless<5\"\n" +
                "Auto-zoom and everything else still work.");
      }
      $("#usellm").disabled = !c.llm_configured;
      $("#llmnote").textContent = c.llm_configured ? "" : "(not configured — heuristic only)";
      if (c.user_music?.length) {
        $("#musicnote").textContent = `· ${c.user_music.length} of your tracks found`;
      } else {
        $("#musicnote").textContent = "· synthesised";
      }
    } catch (e) {
      $("#caps").innerHTML = `<span class="cap off">✕ server unreachable</span>`;
    }
  }

  // ------------------------------------------------------------- submit
  $("#go").addEventListener("click", start);
  $("#cancel").addEventListener("click", async () => {
    if (currentJob) await fetch(`/api/jobs/${currentJob}/cancel`, { method: "POST" });
  });

  async function start() {
    hideErr();
    const file = fileInput.files?.[0];
    const url = $("#url").value.trim();
    if (!file && !url) return showErr("Pick a file or paste a link first.");

    const fd = new FormData();
    if (file) fd.append("file", file);
    if (url) fd.append("url", url);
    fd.append("persona", pick("#persona"));
    fd.append("clip_length", pick("#clen"));
    fd.append("aspect", pick("#aspect"));
    fd.append("music", pick("#mood"));
    const cs = pick("#cstyle");
    fd.append("caption_style", cs === "none" ? "clean" : cs);
    fd.append("captions_on", String(cs !== "none"));
    fd.append("reframe", String($("#reframe").checked));
    fd.append("auto_zoom", String($("#autozoom").checked));
    fd.append("auto_sfx", String($("#sfx").checked));
    fd.append("auto_hook", String($("#hook").checked));
    fd.append("use_graph", String($("#usegraph").checked));
    fd.append("use_llm", String($("#usellm").checked));

    $("#go").disabled = true;
    $("#progress").classList.remove("hidden");
    setProgress(1, "uploading…");

    try {
      const res = await fetch("/api/jobs", { method: "POST", body: fd });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
      currentJob = data.id;
      poll();
    } catch (e) {
      $("#go").disabled = false;
      showErr(e.message);
    }
  }

  function poll() {
    clearTimeout(pollTimer);
    pollTimer = setTimeout(async () => {
      try {
        const j = await (await fetch(`/api/jobs/${currentJob}`)).json();
        setProgress(j.progress, j.step);
        if (j.status === "running" || j.status === "queued") return poll();
        $("#go").disabled = false;
        if (j.status === "done") { render(j.result); return; }
        if (j.status === "cancelled") return showErr("Cancelled.");
        showErr(j.error || "Job failed.");
      } catch (e) {
        $("#go").disabled = false;
        showErr(e.message);
      }
    }, 900);
  }

  function setProgress(pct, text) {
    $("#barfill").style.width = `${Math.max(2, pct)}%`;
    $("#progtext").textContent = text || "";
  }

  // ------------------------------------------------------------- render
  function render(result) {
    $("#progress").classList.add("hidden");
    $("#intake").classList.add("hidden");
    $("#results").classList.remove("hidden");

    const a = result.analysis;
    const clips = result.clips || [];
    $("#summary").innerHTML = `
      <h2>Found ${clips.length} clip${clips.length === 1 ? "" : "s"} in ${fmtDur(a.duration)}</h2>
      <div class="stats">
        ${stat(clips.length, "clips")}
        ${stat(a.moments, "candidates scanned")}
        ${stat(Object.values(a.events || {}).reduce((x, y) => x + y, 0), "signal events")}
        ${stat(a.graph?.communities ?? "?", "graph arcs")}
        ${stat(fmtDur(result.elapsed_seconds), "took")}
      </div>
      <div class="bars">
        ${Object.entries(a.events || {}).sort((x, y) => y[1] - x[1]).map(([k, v]) =>
          barrow(k.replace(/_/g, " "), v, Math.max(1, Object.values(a.events).reduce((x, y) => x + y, 0))))
          .join("") || `<div class="time">no signal events detected</div>`}
      </div>`;

    // graph panel -- the recurring bits
    const g = a.graph || {};
    if ((g.recurring_bits || []).length) {
      $("#graphbox").innerHTML = `
        <div class="card">
          <h3>🕸 Recurring threads <span class="tag">${esc(g.backend || "")}</span></h3>
          <p class="time" style="margin:0 0 8px">
            ${g.nodes} nodes · ${g.edges} edges · these moments tie the most threads together
          </p>
          ${g.recurring_bits.map((b) => `
            <div class="bit">
              <span class="d">×${b.degree}</span>
              <span>${esc(b.text || "")}</span>
              <span class="x">${b.start != null ? fmtDur(b.start) : ""}</span>
            </div>`).join("")}
        </div>`;
    } else {
      $("#graphbox").innerHTML = "";
    }

    $("#clips").innerHTML = clips.map((c) => clipCard(c, result.job_id)).join("");
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function clipCard(c, jobId) {
    const vid = c.video
      ? `/api/jobs/${jobId}/clips/${c.id}.mp4`
      : null;
    const caps = c.captions ? `/api/jobs/${jobId}/artifact/clips/${c.id}.srt` : null;
    const chips = [
      ...(c.features?.spectacle > 0.4 ? ["<span class='chip'>reaction</span>"] : []),
      ...(c.features?.surprise > 0.3 ? ["<span class='chip'>surprising</span>"] : []),
      ...(c.features?.centrality > 0.6 ? ["<span class='chip'>recurring bit</span>"] : []),
      ...(c.features?.hook > 0.3 ? ["<span class='chip'>strong hook</span>"] : []),
      ...(c.features?.llm != null ? [`<span class='chip'>LLM ${(c.features.llm * 10).toFixed(0)}/10</span>`] : []),
    ].join("");
    const notes = (c.render_notes || []).filter((n) => !/libass/.test(n));

    return `
      <article class="clip">
        ${vid ? `<video src="${vid}" controls preload="metadata" playsinline></video>`
              : `<div class="time" style="padding:34px;text-align:center">render failed</div>`}
        <div class="body">
          <div class="row">
            <span class="score">${(c.score * 100).toFixed(0)}</span>
            <span class="tag ${esc(c.tag || "")}">${esc(c.tag || "clip")}</span>
            <span class="time">${fmtDur(c.start)} → ${fmtDur(c.end)}</span>
          </div>
          <p class="reason">${esc(c.reason || "")}</p>
          ${c.text ? `<p class="quote">“${esc(c.text.slice(0, 260))}”</p>` : ""}
          ${chips ? `<div class="chips">${chips}</div>` : ""}
          <div class="actions">
            ${vid ? `<a class="primary" href="${vid}" download="clip_${c.id}.mp4">Download</a>` : ""}
            ${caps ? `<a href="${caps}" download>Captions</a>` : ""}
          </div>
          ${notes.length ? `<div class="notes">${notes.map(esc).join("<br>")}</div>` : ""}
        </div>
      </article>`;
  }

  // ------------------------------------------------------------ helpers
  const stat = (v, l) => `<div class="stat"><b>${esc(v)}</b><span>${esc(l)}</span></div>`;
  const barrow = (l, v, max) =>
    `<div class="barrow"><span>${esc(l)}</span><span class="t"><i style="width:${(v / max) * 100}%"></i></span><span class="v">${v}</span></div>`;

  function fmtDur(s) {
    if (s == null) return "—";
    s = Math.round(Number(s));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
    return h ? `${h}:${String(m).padStart(2, "0")}:${String(x).padStart(2, "0")}`
             : `${m}:${String(x).padStart(2, "0")}`;
  }

  function showErr(msg) {
    const n = $("#err");
    n.textContent = msg;
    n.classList.remove("hidden");
  }
  function hideErr() { $("#err").classList.add("hidden"); }

  loadCaps();
})();