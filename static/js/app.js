(() => {
  "use strict";

  /** @typedef {{id:string, kind:'file'|'url', title:string, sourceLabel:string,
   *             file?:File, url?:string, status:'loading'|'ready'|'error', error?:string}} Item */

  /** @type {Item[]} */
  let items = [];
  let dragId = null;

  const queueEl = document.getElementById("queue");
  const emptyStateEl = document.getElementById("empty-state");
  const rowTemplate = document.getElementById("row-template");
  const bindBtn = document.getElementById("btn-bind");
  const statusLine = document.getElementById("status-line");
  const banner = document.getElementById("banner");
  const outputFilename = document.getElementById("output-filename");

  const uid = () =>
    (crypto.randomUUID ? crypto.randomUUID() : `id-${Date.now()}-${Math.random().toString(16).slice(2)}`);

  // ---------------------------------------------------------------- render

  function render() {
    queueEl.innerHTML = "";
    items.forEach((item, index) => {
      const node = rowTemplate.content.firstElementChild.cloneNode(true);
      node.dataset.id = item.id;
      node.classList.toggle("row-error", item.status === "error");

      node.querySelector(".row-index").textContent = `No. ${String(index + 1).padStart(3, "0")}`;

      const titleInput = node.querySelector(".row-title");
      titleInput.value = item.title;
      titleInput.addEventListener("input", (e) => {
        item.title = e.target.value;
      });

      node.querySelector(".row-source").textContent = item.sourceLabel;
      node.querySelector(".row-source").title = item.sourceLabel;

      const statusEl = node.querySelector(".row-status");
      if (item.status === "loading") {
        statusEl.textContent = "checking…";
      } else if (item.status === "error") {
        statusEl.textContent = "error";
        statusEl.classList.add("error");
        titleInput.title = item.error || "";
      } else {
        statusEl.textContent = "";
      }

      node.querySelector(".row-remove").addEventListener("click", () => removeItem(item.id));

      node.addEventListener("dragstart", () => {
        dragId = item.id;
        requestAnimationFrame(() => node.classList.add("dragging"));
      });
      node.addEventListener("dragend", () => {
        node.classList.remove("dragging");
        queueEl.querySelectorAll(".row").forEach((r) => r.classList.remove("drag-over"));
        dragId = null;
      });
      node.addEventListener("dragover", (e) => {
        e.preventDefault();
        node.classList.add("drag-over");
      });
      node.addEventListener("dragleave", () => node.classList.remove("drag-over"));
      node.addEventListener("drop", (e) => {
        e.preventDefault();
        node.classList.remove("drag-over");
        if (!dragId || dragId === item.id) return;
        reorder(dragId, item.id);
      });

      queueEl.appendChild(node);
    });

    emptyStateEl.hidden = items.length > 0;
    const hasValidItem = items.some((i) => i.status !== "error");
    bindBtn.disabled = items.length === 0 || !hasValidItem;
  }

  function reorder(sourceId, targetId) {
    const from = items.findIndex((i) => i.id === sourceId);
    const to = items.findIndex((i) => i.id === targetId);
    if (from === -1 || to === -1) return;
    const [moved] = items.splice(from, 1);
    items.splice(to, 0, moved);
    render();
  }

  function removeItem(id) {
    items = items.filter((i) => i.id !== id);
    render();
  }

  function showBanner(message) {
    if (!message) {
      banner.hidden = true;
      return;
    }
    banner.textContent = message;
    banner.hidden = false;
  }

  function setStatus(text, kind) {
    statusLine.textContent = text;
    statusLine.classList.remove("error", "success");
    if (kind) statusLine.classList.add(kind);
  }

  // ---------------------------------------------------------------- adding

  async function addFiles(fileList) {
    for (const file of Array.from(fileList)) {
      const item = {
        id: uid(),
        kind: "file",
        title: file.name.replace(/\.pdf$/i, ""),
        sourceLabel: file.name,
        file,
        status: "loading",
      };
      items.push(item);
      render();
      inspectFile(item);
    }
  }

  async function inspectFile(item) {
    const form = new FormData();
    form.append("file", item.file);
    try {
      const res = await fetch("/api/inspect", { method: "POST", body: form });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Couldn't read that file.");
      item.title = data.title || item.title;
      item.sourceLabel = `${item.file.name} · ${data.pages} page${data.pages === 1 ? "" : "s"}`;
      item.status = "ready";
    } catch (err) {
      item.status = "error";
      item.error = err.message;
    }
    render();
  }

  async function addUrl(rawUrl) {
    const url = rawUrl.trim();
    if (!url) return;
    const item = {
      id: uid(),
      kind: "url",
      title: guessTitleFromUrl(url),
      sourceLabel: url,
      url,
      status: "loading",
    };
    items.push(item);
    render();

    try {
      const res = await fetch("/api/check-url", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Couldn't verify that URL.");
      item.status = "ready";
    } catch (err) {
      item.status = "error";
      item.error = err.message;
    }
    render();
  }

  function guessTitleFromUrl(url) {
    try {
      const path = new URL(url).pathname;
      const last = path.split("/").filter(Boolean).pop() || url;
      return decodeURIComponent(last).replace(/\.pdf$/i, "");
    } catch {
      return url;
    }
  }

  // ---------------------------------------------------------------- bind

  async function bindAll() {
    showBanner("");
    const validItems = items.filter((i) => i.status !== "error");
    if (validItems.length === 0) return;

    bindBtn.disabled = true;
    setStatus("Binding documents…");

    const meta = validItems.map((i) => ({
      id: i.id,
      kind: i.kind,
      title: i.title,
      url: i.kind === "url" ? i.url : undefined,
    }));

    const form = new FormData();
    form.append("meta", JSON.stringify(meta));
    form.append("filename", outputFilename.value || "merged.pdf");
    validItems.forEach((i) => {
      if (i.kind === "file") form.append(`file_${i.id}`, i.file);
    });

    try {
      const res = await fetch("/api/merge", { method: "POST", body: form });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.error || "The merge failed.");
      }
      const blob = await res.blob();
      const link = document.createElement("a");
      link.href = URL.createObjectURL(blob);
      link.download = outputFilename.value || "merged.pdf";
      document.body.appendChild(link);
      link.click();
      link.remove();
      setStatus("Bound successfully — download started.", "success");
    } catch (err) {
      setStatus("", null);
      showBanner(err.message);
    } finally {
      bindBtn.disabled = items.length === 0;
    }
  }

  // ---------------------------------------------------------------- wiring

  document.getElementById("btn-add-files").addEventListener("click", () => {
    document.getElementById("file-input").click();
  });
  document.getElementById("file-input").addEventListener("change", (e) => {
    addFiles(e.target.files);
    e.target.value = "";
  });

  const dialogUrl = document.getElementById("dialog-url");
  document.getElementById("btn-add-url").addEventListener("click", () => {
    document.getElementById("input-url").value = "";
    dialogUrl.showModal();
  });
  document.getElementById("form-url").addEventListener("submit", (e) => {
    e.preventDefault();
    addUrl(document.getElementById("input-url").value);
    dialogUrl.close();
  });

  const dialogPaste = document.getElementById("dialog-paste");
  document.getElementById("btn-add-paste").addEventListener("click", () => {
    document.getElementById("input-paste").value = "";
    dialogPaste.showModal();
  });
  document.getElementById("form-paste").addEventListener("submit", (e) => {
    e.preventDefault();
    const lines = document.getElementById("input-paste").value
      .split("\n")
      .map((l) => l.trim())
      .filter(Boolean);
    lines.forEach((line) => addUrl(line));
    dialogPaste.close();
  });

  document.querySelectorAll("[data-close]").forEach((btn) => {
    btn.addEventListener("click", () => btn.closest("dialog").close());
  });

  bindBtn.addEventListener("click", bindAll);

  render();
})();
