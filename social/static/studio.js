(() => {
  const pending = new Map();
  let sessionRefresh;
  const postProgress = document.getElementById("post-progress");
  const postForms = [...document.querySelectorAll('form[action^="/forms/posts/"]')];
  const remoteDisabled = new Map();
  let postBusy = false;
  let postEdited = false;
  postForms.forEach((form) => form.addEventListener("input", () => { postEdited = true; }));

  function relatedForms(form) {
    const match = new URL(form.action).pathname.match(/^\/forms\/posts\/([^/]+)\//);
    return match ? postForms.filter((other) => new URL(other.action).pathname.startsWith(`/forms/posts/${match[1]}/`)) : [form];
  }

  function showPostBusy(busy) {
    postBusy = busy;
    if (!postProgress) return;
    postProgress.hidden = !busy;
    if (busy) {
      postProgress.querySelector(".loading-spinner").hidden = false;
      const publishing = postProgress.dataset.phase === "publishing";
      postProgress.querySelector("strong").textContent = publishing ? "Preparing delivery…" : "Processing post…";
      postProgress.querySelector("small").textContent = publishing ? "Running final checks or waiting for the platform. Publication is not confirmed yet." : "Work is already running. The result will update here automatically.";
    }
    postForms.forEach((form) => {
      form.querySelectorAll('button, input[type="submit"]').forEach((button) => {
        if (busy) {
          if (!remoteDisabled.has(button)) remoteDisabled.set(button, button.disabled);
          button.disabled = true;
        } else if (remoteDisabled.has(button)) {
          button.disabled = remoteDisabled.get(button);
          remoteDisabled.delete(button);
        }
      });
    });
  }

  function refreshSession() {
    // Share concurrent refreshes so an expired session gets only one new cookie.
    if (!sessionRefresh) {
      sessionRefresh = fetch("/api/session", {
        credentials: "same-origin",
        cache: "no-store",
        redirect: "error",
        signal: AbortSignal.timeout(10000),
      }).then(async (response) => {
        if (!response.ok) throw new Error("Session refresh failed");
        const data = await response.json();
        if (typeof data.csrf !== "string" || !data.csrf) throw new Error("Missing session token");
        return data.csrf;
      }).finally(() => { sessionRefresh = null; });
    }
    return sessionRefresh;
  }

  function restore(form) {
    const state = pending.get(form);
    if (!state) return;
    state.states.forEach(({button, disabled}) => { button.disabled = disabled; });
    state.status.remove();
    state.submitterInput?.remove();
    form.removeAttribute("aria-busy");
    pending.delete(form);
  }
  const labels = {
    availability: ["Checking availability…", "Reading the original post from the platform."],
    repost: ["Preparing repost…", "Rechecking the original and creating a draft for your review."],
    "save-media": ["Saving media details…", "Updating permissions and source information."],
    automation: ["Updating automation…", "Saving your automatic-post preference."],
    "media-studio": ["Saving creative brief…", "Preparing your media queue or recurring campaign."],
    plan: ["Planning your idea…", "Saving any files you added, then reading your idea, tudelu.com and the approved facts. This can take up to a minute."],
    create: ["Saving your plan…", "Adding it to the generation queue."],
    replan: ["Planning the edit again…", "The editor looks at your media again. This can take a minute."],
    "change-media": ["Queuing your changes…", "The current media is reused; a new version comes back for your review."],
    "import-bucket-asset": ["Importing bucket asset…", "Preserving the original media and its provenance."],
    connect: ["Opening authorization…", "Preparing your account connection."],
    generate: ["Writing caption…", "Finding the right words. This can take a minute."],
    "generate-asset": ["Generating media…", "Creating your media. This may take a few minutes."],
    check: ["Checking post…", "Reviewing the caption, facts, and media."],
    approve: ["Rechecking post…", "Checking the reviewed content before scheduling."],
    edit: ["Saving and checking…", "Reviewing your changes."],
    delete: ["Deleting draft…", "Removing this draft from your workspace."],
    stop: ["Stopping scheduled delivery...", "Saving this post for review."],
    restart: ["Starting again...", "Keeping the original idea and saved media."],
    regenerate: ["Queuing it…", "Everything else is reused."],
    rewrite: ["Writing a new caption…", "Then running the checks again. This can take a minute."],
    resubmit: ["Starting it again…", "Adding it back to the generation queue."],
    retry: ["Preparing retry…", "Reusing the saved caption and media, then queuing fresh checks for your review."],
    assets: ["Uploading media…", "Saving the original file and its details."],
    "delete-files": ["Deleting files…", "Removing them from the asset library."],
    "restore-files": ["Restoring files…", "Bringing them back into the asset library."],
    "import-again": ["Queuing the import…", "The file is imported on the next Google Drive scan."],
    posts: ["Creating draft…", "Preparing your post."],
  };

  document.addEventListener("submit", async (event) => {
    if (event.defaultPrevented) return;
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || form.method.toLowerCase() !== "post") return;
    if (new URL(form.action).origin !== window.location.origin) return;
    event.preventDefault();
    if (relatedForms(form).some((other) => pending.has(other)) || (postForms.includes(form) && postBusy)) {
      return;
    }
    if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) {
      return;
    }
    form.querySelector(".session-error")?.remove();
    // A button can name its own progress label, such as Plan the edit again in the plan form.
    const action = event.submitter?.dataset.busy || new URL(form.action).pathname.split("/").filter(Boolean).pop();
    const [label, detail] = labels[action] || ["Working…", "Please wait."];
    const buttons = relatedForms(form).flatMap((other) => [...other.querySelectorAll('button, input[type="submit"]')]);
    const states = buttons.map((button) => ({button, disabled: button.disabled}));
    const status = document.createElement("div");
    status.className = "loading-state";
    status.setAttribute("role", "status");
    status.setAttribute("aria-live", "polite");
    const spinner = document.createElement("span");
    spinner.className = "loading-spinner";
    spinner.setAttribute("aria-hidden", "true");
    const message = document.createElement("span");
    const title = document.createElement("strong");
    title.textContent = label;
    const note = document.createElement("small");
    note.textContent = detail;
    message.append(title, note);
    status.append(spinner, message);
    // A form can mark where its progress shows, such as under a button in a side column.
    const statusSlot = form.querySelector("[data-form-status]") || form;
    statusSlot.append(status);
    form.setAttribute("aria-busy", "true");
    pending.set(form, {states, status});
    // Keep named submitters in the native request even after disabling buttons.
    if (event.submitter?.name) {
      const input = document.createElement("input");
      input.type = "hidden";
      input.name = event.submitter.name;
      input.value = event.submitter.value;
      form.append(input);
      pending.get(form).submitterInput = input;
    }
    buttons.forEach((button) => { button.disabled = true; });
    const state = pending.get(form);
    try {
      const token = await refreshSession();
      // A Back navigation may already have restored this form while the GET ran.
      if (pending.get(form) !== state) return;
      form.querySelectorAll('input[name="csrf"]').forEach((input) => { input.value = token; });
      if (!form.reportValidity()) {
        restore(form);
        return;
      }
      // Only the read-only session request is made with fetch. Submit the action
      // once, natively, preserving file uploads and normal redirect navigation.
      HTMLFormElement.prototype.submit.call(form);
    } catch {
      if (pending.get(form) !== state) return;
      restore(form);
      const error = document.createElement("p");
      error.className = "error session-error";
      error.setAttribute("role", "alert");
      error.textContent = "We couldn't refresh your session. Nothing was submitted. Your entries are still here; check your connection and try again, or reload this page.";
      statusSlot.append(error);
    }
  });

  // On phones the menu and the idea steps are rows that scroll sideways; show the current one,
  // again once the brand font has loaded and widened the items.
  const showCurrent = () => [[document.querySelector("nav"), 'a[aria-current="page"]'], [document.querySelector(".steps"), "li.current"]]
    .forEach(([row, current]) => {
      const item = row?.querySelector(current);
      if (!item || row.scrollWidth <= row.clientWidth) return;
      const offset = item.getBoundingClientRect().left - row.getBoundingClientRect().left;
      row.scrollLeft += offset - (row.clientWidth - item.offsetWidth) / 2;
    });
  showCurrent();
  document.fonts?.ready.then(showCurrent);

  // New post from an idea: files dropped anywhere on the files box are added to it, and a
  // drop that misses the box is ignored instead of the browser opening the file in this tab.
  const ideaMedia = document.getElementById("idea-media");
  const ideaFiles = ideaMedia?.querySelector('input[type="file"]');
  if (ideaFiles) {
    const carriesFiles = (event) => [...(event.dataTransfer?.types || [])].includes("Files");
    document.addEventListener("dragover", (event) => {
      if (!carriesFiles(event)) return;
      event.preventDefault();
      const inside = ideaMedia.contains(event.target) && !ideaFiles.disabled;
      event.dataTransfer.dropEffect = inside ? "copy" : "none";
      ideaMedia.classList.toggle("dragging", inside);
    });
    document.addEventListener("dragleave", (event) => {
      if (!event.relatedTarget || !ideaMedia.contains(event.relatedTarget)) ideaMedia.classList.remove("dragging");
    });
    document.addEventListener("drop", (event) => {
      if (!carriesFiles(event)) return;
      event.preventDefault();
      ideaMedia.classList.remove("dragging");
      if (!ideaMedia.contains(event.target) || ideaFiles.disabled || !event.dataTransfer.files.length) return;
      ideaFiles.files = event.dataTransfer.files;
      ideaFiles.dispatchEvent(new Event("change", {bubbles: true}));
    });
  }

  // Restore controls when the browser brings a submitted page back from its cache.
  window.addEventListener("pageshow", () => {
    pending.forEach((_, form) => { restore(form); });
  });

  if (postProgress) {
    let polling = false;
    showPostBusy(postProgress.dataset.processing === "true");
    const pollPost = async () => {
      if (document.hidden || pending.size || polling) return;
      polling = true;
      try {
        const response = await fetch(postProgress.dataset.activityUrl, {
          credentials: "same-origin", cache: "no-store", signal: AbortSignal.timeout(10000),
        });
        if (!response.ok) return;
        const activity = await response.json();
        if (pending.size) return;
        postProgress.dataset.phase = activity.status;
        showPostBusy(activity.processing);
        if (!activity.processing && String(activity.revision) !== postProgress.dataset.revision) {
          if (!postEdited) window.location.reload();
          else {
            postProgress.hidden = false;
            postProgress.querySelector(".loading-spinner").hidden = true;
            postProgress.querySelector("strong").textContent = "Post updated";
            postProgress.querySelector("small").textContent = "Your edits are still here. Reload when ready to see the latest results.";
          }
        }
      } catch { /* A temporary polling failure never submits or repeats an action. */ }
      finally { polling = false; }
    };
    setInterval(pollPost, 2000);
    window.addEventListener("focus", pollPost);
  }

  // Show only the plan fields that apply to the chosen format (image, video or video edit).
  document.querySelectorAll("select[data-format-switch]").forEach((select) => {
    const update = () => {
      select.form.querySelectorAll("[data-show-for]").forEach((element) => {
        element.hidden = !element.dataset.showFor.split(" ").includes(select.value);
      });
    };
    select.addEventListener("change", update);
    update();
  });

  if (document.getElementById("media-queue")) {
    let polling = false;
    const pollMedia = async () => {
      const queue = document.getElementById("media-queue");
      if (document.hidden || pending.size || polling || queue.contains(document.activeElement)) return;
      polling = true;
      try {
        const response = await fetch("/api/media-jobs", {credentials: "same-origin", cache: "no-store", signal: AbortSignal.timeout(10000)});
        if (!response.ok) return;
        const jobs = await response.json();
        const current = [...queue.querySelectorAll("[data-job-id]")].map((card) => [
          card.dataset.jobId, card.dataset.jobState, card.dataset.jobStage, card.dataset.jobError, card.dataset.jobOutput, card.dataset.jobPost, card.dataset.jobPostStatus, Number(card.querySelector("[data-completed-clips]")?.dataset.completedClips || 0), card.dataset.jobPhase || "", card.dataset.jobTask || "", Number(card.dataset.jobRetries || 0), Number(card.dataset.jobScenes || 0),
        ]);
        const updated = jobs.map((job) => [job.id, job.state, job.stage, job.error, job.asset_id || "", job.post_id || "", job.post_status || "", job.completed_clips || 0, job.generation_phase || "", job.task_id || "", job.retry_count || 0, job.saved_scenes || 0]);
        if (JSON.stringify(current) === JSON.stringify(updated)) return;
        const html = await fetch("/admin/media-queue", {credentials: "same-origin", cache: "no-store", signal: AbortSignal.timeout(10000)});
        if (!html.ok) return;
        const replacement = new DOMParser().parseFromString(await html.text(), "text/html").getElementById("media-queue");
        if (replacement && !pending.size && !queue.contains(document.activeElement)) queue.replaceWith(replacement);
      } catch { /* Keep the last known state and the manual refresh link available. */ }
      finally { polling = false; }
    };
    setInterval(pollMedia, 3000);
  }

  // Media pickers: search, filter and sort a list of library files inside a form. The controls have no
  // name, so only the ticked files are submitted; files hidden by a filter stay ticked.
  document.querySelectorAll("[data-picker]").forEach((picker) => {
    const list = picker.querySelector("[data-picker-list]");
    if (!list) return;
    const items = [...list.children];
    const field = (selector) => picker.querySelector(selector);
    const rank = (item) => (item.dataset.rank === "" ? Infinity : Number(item.dataset.rank));
    const newest = (a, b) => b.dataset.created.localeCompare(a.dataset.created);
    const orders = {
      match: (a, b) => rank(a) - rank(b) || newest(a, b),
      newest,
      oldest: (a, b) => a.dataset.created.localeCompare(b.dataset.created),
      name: (a, b) => a.dataset.name.localeCompare(b.dataset.name),
      longest: (a, b) => Number(b.dataset.seconds) - Number(a.dataset.seconds),
    };
    let order = "";
    const update = () => {
      const words = (field("[data-picker-search]")?.value || "").toLowerCase().split(/\s+/).filter(Boolean);
      const filters = [...picker.querySelectorAll("[data-picker-filter]")].map((select) => [select.dataset.pickerFilter, select.value]);
      const chosenOnly = field("[data-picker-chosen-only]")?.checked;
      let shown = 0;
      let ticked = 0;
      items.forEach((item) => {
        const box = item.querySelector('input[type="checkbox"]');
        ticked += box.checked;
        const fits = filters.every(([key, wanted]) => {
          if (!wanted) return true;
          if (key !== "product") return item.dataset[key] === wanted;
          return wanted === "none" ? !item.dataset.products : item.dataset.products.split("|").includes(wanted);
        });
        item.hidden = !(fits && words.every((word) => item.dataset.text.includes(word)) && (!chosenOnly || box.checked));
        shown += !item.hidden;
      });
      const wanted = field("[data-picker-sort]")?.value || "newest";
      if (wanted !== order) {
        order = wanted;
        items.slice().sort(orders[wanted] || newest).forEach((item) => list.append(item));
      }
      const total = items.length;
      field("[data-picker-count]").textContent = shown === total ? `${total} file${total === 1 ? "" : "s"}` : `Showing ${shown} of ${total} files`;
      field("[data-picker-chosen]").textContent = ticked ? `${ticked} chosen` : "";
      field("[data-picker-empty]").hidden = shown > 0;
    };
    field("[data-picker-filters]").hidden = false;
    picker.addEventListener("input", update);
    picker.addEventListener("change", update);
    update();
  });

  // The asset library's filters apply as soon as a choice changes; the search box waits for Apply or Enter.
  document.querySelectorAll("form[data-auto-submit]").forEach((form) => {
    form.querySelectorAll("select").forEach((select) => select.addEventListener("change", () => form.requestSubmit()));
  });
  // A "tick all" box ticks every file on the page for the form it names.
  document.querySelectorAll("[data-tick-all]").forEach((all) => {
    all.addEventListener("change", () => {
      document.querySelectorAll(`input[type="checkbox"][form="${all.dataset.tickAll}"]`).forEach((box) => { box.checked = all.checked; });
    });
  });

  // A link to a closed part opens it, so the jump lands on that part's prompt and button.
  function openPart(hash) {
    let target;
    try { target = hash.length > 1 && document.getElementById(decodeURIComponent(hash.slice(1))); } catch { return; }
    if (target instanceof HTMLDetailsElement) target.open = true;
  }
  document.addEventListener("click", (event) => {
    const link = event.target instanceof Element && event.target.closest('a[href^="#"]');
    if (link) openPart(link.getAttribute("href"));
  });
  window.addEventListener("hashchange", () => openPart(location.hash));
  openPart(location.hash);
})();
