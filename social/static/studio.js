(() => {
  const pending = new Map();
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

  document.addEventListener("submit", (event) => {
    if (event.defaultPrevented) return;
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || form.method.toLowerCase() !== "post") return;
    if (pending.has(form)) {
      event.preventDefault();
      return;
    }
    if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) {
      event.preventDefault();
      return;
    }
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
  });

  // Restore controls when the browser brings a submitted page back from its cache.
  window.addEventListener("pageshow", () => {
    pending.forEach(({states, status, submitterInput}, form) => {
      states.forEach(({button, disabled}) => { button.disabled = disabled; });
      status.remove();
      submitterInput?.remove();
      form.removeAttribute("aria-busy");
    });
    pending.clear();
  });
})();
