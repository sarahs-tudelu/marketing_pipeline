(() => {
  const pending = new Map();
  let sessionRefresh;

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
    generate: ["Writing caption…", "Finding the right words. This can take a minute."],
    "generate-asset": ["Generating media…", "Creating your media. This may take a few minutes."],
    check: ["Checking post…", "Reviewing the caption, facts, and media."],
    approve: ["Rechecking post…", "Running the approval checks."],
    edit: ["Saving and checking…", "Reviewing your changes."],
    delete: ["Deleting draft…", "Removing this draft from your workspace."],
    assets: ["Uploading media…", "Saving the original file and its details."],
    posts: ["Creating draft…", "Preparing your post."],
  };

  document.addEventListener("submit", async (event) => {
    if (event.defaultPrevented) return;
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || form.method.toLowerCase() !== "post") return;
    if (new URL(form.action).origin !== window.location.origin) return;
    event.preventDefault();
    if (pending.has(form)) {
      return;
    }
    if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) {
      return;
    }
    form.querySelector(".session-error")?.remove();
    const action = new URL(form.action).pathname.split("/").filter(Boolean).pop();
    const [label, detail] = labels[action] || ["Working…", "Please wait."];
    const buttons = [...form.querySelectorAll('button, input[type="submit"]')];
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
    form.append(status);
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
      form.append(error);
    }
  });

  // Restore controls when the browser brings a submitted page back from its cache.
  window.addEventListener("pageshow", () => {
    pending.forEach((_, form) => { restore(form); });
  });
})();
