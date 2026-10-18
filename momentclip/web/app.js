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
    wire("#mood", () => {});
  wire("#model", () => {
    const m = pick("#model");
    $("#promptwrap").classList.toggle("hidden", m !== "anything");
    $("#persona").style.opacity = m === "anything" ? 0.45 : 1;
  });

  function showProv() {
    const list = CAPS_PROV || [];
    const p = list.find((x) => x.key === $("#provider").value);
    const n = $("#provnote");
    if (!p) { n.innerHTML = ""; return; }
    const free = p.free
      ? `<span class="free">${esc(p.free)}</span>`
      : `<span class="paid">costs money per token</span>`;
    const local = p.local_models?.length
      ? `<br>models pulled: <code>${esc(p.local_models.join(", "))}</code>` : "";
    const vision = p.vision ? " · reads images" : "";
    n.innerHTML = `${free}${local}<br>${esc(p.note || "")} · ${esc(p.status)}${vision}`;
  }
  let CAPS_PROV = [];

  // ---------- caption style cards ----------
  // Thumbnail is drawn from the SAME parameters the .ass file is built from,
  // so a card that looks right here looks right in the render. Hover plays a
  // real burned sample from the server; the card itself is instant.
  let CAPS = [];
  let CAPS_PROV = [];
  let chosenStyle = "pop";

  function pick(sel) {
    const on = $(`${sel} button.on`);
    return on ? on.dataset.v : null;
  }

  function cssText(p) {
    const weight = p.bold ? 900 : 500;
    const stroke = p.outline_w
      ? `-webkit-text-stroke:${Math.max(1, p.outline_w * 0.5)}px ${p.outline_css}` : "";
    const fam = p.font.includes("Black") ? "900 15px Arial Black, Impact, sans-serif"
            : p.font.includes("Impact") ? "800 15px Impact, Haettenschweiler, sans-serif"
            : p.font.includes("Montserrat") ? "700 15px Montserrat, Segoe UI, sans-serif"
            : `600 15px ${p.font}, Segoe UI, sans-serif`;
    return `${fam}; color:${p.primary_css}; ${stroke};`
         + ` text-shadow:${p.shadow ? "0 2px 4px #000" : "none"};`
         + ` transform:scale(${p.pop_scale / 100});`;
  }

  function buildPresetCards() {
    const grid = $("#cstyle");
    grid.innerHTML = "";
    const names = CAPS.map((c) => c.name);
    (names.includes(chosenStyle) ? names : [...names]).forEach((name) => {
      const p = CAPS.find((c) => c.name === name);
      const card = el("button", "pcard" + (name === chosenStyle ? " on" : ""));
      card.dataset.v = name;
      card.type = "button";
      const sample = p.uppercase ? "TO GET\nSTARTED" : "to get\nstarted";
      const thumb = el("div", "thumb");
      thumb.innerHTML = `<span style="${cssText(p)}">${esc(sample)}</span>`;
      card.appendChild(thumb);
      card.appendChild(el("div", "plabel", esc(name)));
      card.addEventListener("click", () => {
        chosenStyle = name;
        $$("#cstyle .pcard").forEach((c) => c.classList.toggle("on", c.dataset.v === name));
        showCapMeta(name);
      });
      card.addEventListener("mouseenter", () => previewOn(name));
      grid.appendChild(card);
    });

    // "no caption" stays a plain tile so it can sit in the same grid
    const off = el("button", "pcard" + (chosenStyle === "none" ? " on" : ""));
    off.dataset.v = "none";
    off.type = "button";
    off.innerHTML =
      `<div class="thumb" style="color:#777;font-size:26px">⊘</div>` +
      `<div class="plabel">No caption</div>`;
    off.addEventListener("click", () => {
      chosenStyle = "none";
      $$("#cstyle .pcard").forEach((c) => c.classList.toggle("on", c.dataset.v === "none"));
      showCapMeta("none");
    });
    grid.appendChild(off);
    showCapMeta(chosenStyle);
  }

  let prevBox = null;
  function previewOn(name) {
    if (prevBox) prevBox.remove();
    prevBox = el("div", "");
    prevBox.style.cssText =
      "position:fixed;z-index:99;pointer-events:none;box-shadow:0 12px 40px #000a;border-radius:12px;overflow:hidden";
    const v = document.createElement("video");
    v.src = `/api/preview/caption.mp4?style=${encodeURIComponent(name)}`;
    v.muted = true; v.loop = true; v.playsInline = true;
    v.style.cssText = "width:220px;display:block";
    prevBox.appendChild(v);
    document.body.appendChild(prevBox);
    const move = (e) => {
      prevBox.style.left = Math.min(window.innerWidth - 240, e.clientX + 16) + "px";
      prevBox.style.top = Math.min(window.innerHeight - 420, e.clientY + 12) + "px";
    };
    prevBox.addEventListener("mousemove", move);
    v.play().catch(() => {});
  }
  window.addEventListener("scroll", () => prevBox && prevBox.remove(), { passive: true });

  function showCapMeta(name) {
    const meta = $("#capmeta");
    if (name === "none") {
      meta.innerHTML = `<b>No caption.</b> The clip is rendered with nothing burned in.`;
      return;
    }
    const p = CAPS.find((c) => c.name === name);
    if (!p) { meta.innerHTML = ""; return; }
    meta.innerHTML =
      `<b>${esc(p.name)}</b> — ${esc(p.font)} ${p.size}px` +
      `${p.uppercase ? " · UPPERCASE" : ""} · ${p.max_words} words/line<br>` +
      `rest <span style="color:${p.primary_css}">■</span> ` +
      `active <span style="color:${p.active_css}">■</span> ` +
      `outline ${p.outline_w}px · pop to ${p.pop_scale}%<br>` +
      `word-by-word: <code>\\k</code> karaoke timing + <code>\\t</code> scale pop. ` +
      `Hover a card to play a real burned sample.`;
  }

  // ---------- prompt (ClipAnything) ----------
  let pTimer = null;
  function onPromptInput() {
    clearTimeout(pTimer);
    pTimer = setTimeout(compilePrompt, 350);
  }
  async function compilePrompt() {
    const text = $("#customprompt").value.trim();
    const box = $("#promptinfo");
    if (!text) { box.innerHTML = ""; return; }
    try {
      const r = await (await fetch("/api/preview/prompt?text=" +
        encodeURIComponent(text))).json();
      const bits = [];
      if (r.keywords?.length) bits.push(`topics <span class="ok">${esc(r.keywords.join(", "))}</span>`);
      if (r.sentiments?.length) bits.push(`mood <span class="ok">${esc(r.sentiments.join(", "))}</span>`);
      if (r.sound_cues?.length) bits.push(`sound <span class="ok">${esc(r.sound_cues.join(", "))}</span>`);
      bits.push(`intent <span class="ok">${esc(r.intent)}</span>`);
      let html = "Understood → " + bits.join(" · ");
      if (!r.active) {
        html += `<span class="warn">Nothing specific to match on — this will rank like the default.</span>`;
      }
      (r.notes || []).forEach((n) => { html += `<span class="warn">${esc(n)}</span>`; });
      (r.unsupported || []).forEach((n) => { html += `<span class="bad">Not supported: ${esc(n)}</span>`; });
      box.innerHTML = html;
    } catch (e) {
      box.innerHTML = `<span class="bad">${esc(e.message)}</span>`;
    }
  }

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
      const cap = (label, ok) =>
        `<span class="cap ${ok ? "on" : "off"}">${ok ? "✓" : "✕"} ${esc(label)}</span>`;
      $("#caps").innerHTML = [
        cap("ffmpeg", c.ffmpeg),
        cap("transcribe", c.transcription.faster_whisper || c.transcription.http),
        cap("captions", c.libass),
        cap("face track", c.face_tracking ?? c.opencv),
        cap("AI judge", c.llm_configured),
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
      // provider picker
      const sel = $("#provider");
      sel.innerHTML = "";
      (c.providers || []).forEach((p) => {
        const o = document.createElement("option");
        const freeTag = p.free ? " · free" : " · PAID";
        o.value = p.key;
        o.textContent = `${p.label}${freeTag} — ${p.status}`;
        sel.appendChild(o);
      });
      const ready = (c.providers || []).filter((p) => p.status === "ready");
      if (ready.length) sel.value = ready[0].key;
      showProv();
      sel.addEventListener("change", showProv);
      const vis = c.vision_available;
      $("#usevision").disabled = !vis;
      if (!vis) {
        $("#usevision").checked = false;
        $("#visionnote").textContent =
          "(needs a vision model — Gemini/OpenRouter free tier, or Ollama with a VL model)";
      }
      CAPS = c.caption_detail || [];
      CAPS_PROV = c.providers || [];
      buildPresetCards();
      if (c.user_music?.length) {
        $("#musicnote").textContent = `· ${c.user_music.length} of your tracks`;
      } else {
        $("#musicnote").textContent = "· synthesised, per clip";
      }
      // prompt examples
      const ex = $("#promptchips");
      (c.prompt_examples || []).forEach((p) => {
        const b = el("button", "", esc(p));
        b.addEventListener("click", () => {
          $("#customprompt").value = p;
          compilePrompt();
        });
        ex.appendChild(b);
      });
      $("#customprompt").addEventListener("input", onPromptInput);
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
    fd.append("custom_prompt", $("#customprompt").value.trim());
    fd.append("clip_length", pick("#clen"));
    fd.append("aspect", pick("#aspect"));
    fd.append("music", pick("#mood"));
    const cs = chosenStyle;
    fd.append("caption_style", cs === "none" ? "clean" : cs);
    fd.append("captions_on", String(cs !== "none"));
    fd.append("reframe", String($("#reframe").checked));
    fd.append("auto_zoom", String($("#autozoom").checked));
    fd.append("auto_sfx", String($("#sfx").checked));
    fd.append("auto_hook", String($("#hook").checked));
    fd.append("use_graph", String($("#usegraph").checked));
    fd.append("use_llm", String($("#usellm").checked));
    fd.append("use_vision", String($("#usevision").checked));
    const pv = $("#provider").value;
    if (pv) fd.append("llm_provider", pv);

    $("#go").disabled = true;
    $("#progress").classList.remove("hidden");
    setProgress(1, "uploading…");

    try {
      const res = await fetch("/api/jobs", { method: "POST", body: fd });
      const data = await readJson(res);
      if (!res.ok) throw new Error(data.detail || data.error || `HTTP ${res.status}`);
      currentJob = data.id;
      poll();
    } catch (e) {
      $("#go").disabled = false;
      showErr(e.message);
    }
  }

  // A 500 from any middleware or proxy arrives as plain text or HTML, so
  // parsing blindly with res.json() throws a message that tells the user
  // nothing. Read the body as text first and make a real message from it.
  async function readJson(res) {
    const text = await res.text();
    if (!text) return {};
    try {
      return JSON.parse(text);
    } catch {
      const snippet = text.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim();
      if (res.status >= 500) {
        throw new Error(
          `Server error ${res.status}. Is the server still running?\n` +
          `Restart it with:  python run.py\n\nServer said: ${snippet.slice(0, 200)}`);
      }
      throw new Error(`Unexpected response from the server: ${snippet.slice(0, 200)}`);
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
      ...(c.music ? [`<span class='chip'>🎵 ${esc(c.music)}</span>`] : []),
      ...(c.sfx?.length ? [`<span class='chip'>${c.sfx.length}× SFX</span>`] : []),
      ...(c.zoom > 1.05 ? [`<span class='chip'>zoom ${c.zoom}</span>`] : []),
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