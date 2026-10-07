"use strict";

const $ = (id) => document.getElementById(id);
const emptyPlaces = [];
const state = {
  id: sessionStorage.getItem("vistra-conversation"),
  messages: [],
  busy: false,
  ready: false,
  image: null,
  preview: null,
  deleteChat: null,
  maxUploadMb: 8,
  mapMessage: null,
};

const element = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
};

function notice(message = "") {
  $("notice").textContent = message;
  $("notice").hidden = !message;
}

async function api(path, options) {
  let response;
  try {
    response = await fetch(path, options);
  } catch {
    throw new Error("Cannot reach the backend. Check that it is running, then try again.");
  }
  const data = await response.json();
  if (!response.ok) {
    const error = new Error(typeof data.detail === "string" ? data.detail : "The request could not finish. Please try again.");
    error.status = response.status;
    throw error;
  }
  return data;
}

function safeUrl(value) {
  if (typeof value !== "string" || !value.trim()) return null;
  try {
    const url = new URL(value, location.origin);
    return ["https:", "http:"].includes(url.protocol) ? url.href : null;
  } catch {
    return null;
  }
}

if (window.marked) marked.use({renderer: {html() { return ""; }, image() { return ""; }}});

function markdown(text) {
  const node = element("div", "markdown");
  if (window.marked && window.DOMPurify) {
    node.append(DOMPurify.sanitize(marked.parse(String(text)), {
      RETURN_DOM_FRAGMENT: true,
      FORBID_TAGS: ["img", "style", "iframe", "form", "input", "button"],
    }));
    node.querySelectorAll("a").forEach((link) => {
      const href = safeUrl(link.getAttribute("href"));
      if (href) {
        link.href = href;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
      } else link.removeAttribute("href");
    });
  } else node.textContent = text;
  return node;
}

function activityView(message) {
  const panel = element("details", "activity");
  panel.open = message.activityOpen ?? Boolean(message.pending);
  panel.ontoggle = () => { message.activityOpen = panel.open; };
  const events = message.activity || [];
  const context = events.find((item) => item.step === "context");
  const current = events.at(-1);
  const heading = message.pending ? current?.title || "Starting request…" : current?.status === "error" ? current.title : context?.title || "Search activity";
  panel.append(element("summary", "activity-summary", heading));
  const list = element("ol", "activity-steps");
  for (const event of events) {
    const item = element("li", `activity-step ${event.status}`);
    const title = event.seconds === undefined ? event.title : `${event.title} · ${event.seconds}s`;
    item.append(element("div", "activity-title", title));
    if (event.detail) item.append(element("div", "activity-detail", event.detail));
    for (const source of event.sources || []) {
      const entry = element("details", "activity-source");
      const kind = source.source === "text" ? "Wikivoyage" : "Photo metadata";
      entry.append(element("summary", "", `${source.result_number}. ${source.name || source.entity_id} · ${kind}`));
      const url = safeUrl(source.url);
      if (url) {
        const link = element("a", "", "Open source");
        link.href = url; link.target = "_blank"; link.rel = "noopener noreferrer";
        entry.append(link);
      }
      if (source.relation) entry.append(element("p", "", "Mapped destination context"));
      for (const excerpt of source.excerpts || []) {
        if (excerpt.section) entry.append(element("strong", "", excerpt.section));
        entry.append(element("p", "", excerpt.text));
      }
      for (const [key, value] of Object.entries(source.place_metadata || {})) {
        if (value) entry.append(element("p", "", `${key}: ${typeof value === "object" ? JSON.stringify(value) : value}`));
      }
      for (const photo of source.images || []) {
        const photoUrl = safeUrl(photo.source_url);
        const label = element(photoUrl ? "a" : "p", "", `Photo: ${photo.image_id}`);
        if (photoUrl) { label.href = photoUrl; label.target = "_blank"; label.rel = "noopener noreferrer"; }
        entry.append(label);
      }
      item.append(entry);
    }
    list.append(item);
  }
  panel.append(list);
  return panel;
}

async function chatStream(form, pending) {
  form.append("stream", "true");
  let response;
  try { response = await fetch("/api/chat", {method: "POST", body: form}); }
  catch { throw new Error("Cannot reach the backend. Check that it is running, then try again."); }
  if (!response.ok) {
    const data = await response.json();
    throw new Error(typeof data.detail === "string" ? data.detail : "The request could not finish.");
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "", result;
  function receive(line) {
    if (!line.trim()) return;
    const event = JSON.parse(line);
    if (event.type === "error") throw new Error(event.detail);
    if (event.type === "result") result = event;
    if (event.type === "activity") {
      pending.activity = [...(pending.activity || []).filter((item) => item.step !== event.activity.step), event.activity];
      const nearBottom = $("feed").scrollHeight - $("feed").scrollTop - $("feed").clientHeight < 100;
      render();
      if (nearBottom) scrollToBottom();
    }
  }
  try {
    while (true) {
      const {value, done} = await reader.read();
      buffer += done ? decoder.decode() : decoder.decode(value, {stream: true});
      const lines = buffer.split("\n");
      buffer = lines.pop();
      lines.forEach(receive);
      if (done) { receive(buffer); break; }
    }
  } finally { reader.releaseLock(); }
  if (!result) throw new Error("The connection ended before the reply arrived. Reopen this chat to check whether it finished.");
  return result;
}

async function revealAnswer(message) {
  const words = message.content.match(/\S+\s*|\s+/gu) || [];
  if (!words.length || matchMedia("(prefers-reduced-motion: reduce)").matches) {
    render();
    return;
  }
  const wordDelay = Math.min(35, 5000 / words.length);
  message.visibleContent = "";
  render();
  await new Promise((resolve) => {
    const started = performance.now();
    let index = 0;
    function frame(now) {
      const count = Math.min(words.length, Math.floor((now - started) / wordDelay));
      if (count > index) {
        const nearBottom = $("feed").scrollHeight - $("feed").scrollTop - $("feed").clientHeight < 100;
        message.visibleContent += words.slice(index, count).join("");
        index = count;
        const current = $("messages").lastElementChild.querySelector(".markdown");
        current.replaceWith(markdown(message.visibleContent));
        if (nearBottom) scrollToBottom();
      }
      if (index < words.length) requestAnimationFrame(frame);
      else resolve();
    }
    requestAnimationFrame(frame);
  });
  delete message.visibleContent;
  render();
}

function messageView(message) {
  const article = element("article", `message ${message.role}`);
  if (message.role === "user") {
    if (message.image_url) {
      const image = element("img", "message-image");
      image.src = message.image_url;
      image.alt = "Your inspiration photo";
      article.append(image);
    }
    if (message.content) article.append(element("div", "user-text", message.content));
  } else {
    const heading = element("div", "assistant-heading");
    heading.append(element("span", "assistant-avatar", "V"), element("span", "", "VISTRA"));
    const body = element("div", "assistant-body");
    if (message.pending || message.activity?.length) body.append(activityView(message));
    if (message.pending) {
      const pending = element("div", "pending", message.activity?.at(-1)?.title || "Starting request…");
      pending.setAttribute("role", "status");
      pending.setAttribute("aria-live", "polite");
      body.append(pending);
    } else {
      const revealing = message.visibleContent !== undefined;
      body.append(markdown(revealing ? message.visibleContent : message.content));
      if (!revealing) {
        if (message.places?.length) {
          const places = element("button", "view-places-button", `View ${message.places.length} place${message.places.length === 1 ? "" : "s"} on map`);
          places.type = "button";
          places.onclick = () => {
            state.mapMessage = message;
            placeMap.show(message.places);
            if (matchMedia("(max-width: 1100px)").matches) showMap(true);
          };
          body.append(places);
        }
        const copy = element("button", "copy-button", "Copy response");
        copy.type = "button";
        copy.onclick = async () => {
          try {
            await navigator.clipboard.writeText(message.content);
            copy.textContent = "Copied";
            setTimeout(() => { copy.textContent = "Copy response"; }, 1800);
          } catch { notice("The response could not be copied. Select its text to copy it."); }
        };
        body.append(copy);
      }
    }
    article.append(heading, body);
  }
  return article;
}

function updateControls() {
  $("send").disabled = !state.ready || state.busy || (!$("message").value.trim() && !state.image);
  for (const id of ["message", "add-image", "image", "remove-image", "new-chat", "memory-button"]) {
    $(id).disabled = state.busy;
  }
  document.querySelectorAll(".chat-link, .delete-chat").forEach((button) => { button.disabled = state.busy; });
  $("composer").setAttribute("aria-busy", String(state.busy));
}

function render() {
  $("welcome").hidden = state.messages.length > 0;
  $("messages").replaceChildren(...state.messages.map(messageView));
  placeMap.show(state.mapMessage?.places || emptyPlaces);
  document.querySelectorAll(".chat-link").forEach((button) => {
    const active = button.dataset.id === state.id;
    button.classList.toggle("active", active);
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  updateControls();
}

function scrollToBottom() { $("feed").scrollTop = $("feed").scrollHeight; }

function showMap(visible) {
  document.body.classList.toggle("map-open", visible);
  $("toggle-map").textContent = visible ? "Show chat" : "Show map";
  $("toggle-map").setAttribute("aria-expanded", String(visible));
  requestAnimationFrame(() => {
    placeMap.map?.invalidateSize({pan: false});
    if (visible) placeMap.fit();
  });
}

function resizeMessage() {
  $("message").style.height = "auto";
  $("message").style.height = `${Math.min($("message").scrollHeight, 160)}px`;
  updateControls();
}

function removeImage() {
  if (state.preview) URL.revokeObjectURL(state.preview);
  state.preview = null;
  state.image = null;
  $("image").value = "";
  $("preview").removeAttribute("src");
  $("attachment").hidden = true;
  updateControls();
}

function closeSidebar() {
  document.body.classList.remove("sidebar-open");
  $("sidebar-backdrop").hidden = true;
  $("toggle-sidebar").setAttribute("aria-expanded", "false");
}

function newChat() {
  if (state.busy) return;
  state.id = null;
  state.messages = [];
  state.mapMessage = null;
  showMap(false);
  sessionStorage.removeItem("vistra-conversation");
  $("message").value = "";
  removeImage();
  notice();
  closeSidebar();
  render();
  resizeMessage();
  $("message").focus();
}

async function listChats() {
  const chats = await api("/api/conversations");
  const list = $("chat-list");
  list.replaceChildren();
  if (!chats.length) list.append(element("p", "muted sidebar-empty", "No chats yet."));
  for (const chat of chats) {
    const row = element("div", "chat-row");
    const button = element("button", "chat-link", chat.title);
    button.dataset.id = chat.id;
    button.title = chat.title;
    button.onclick = () => openChat(chat.id);
    const remove = element("button", "icon-button delete-chat");
    remove.type = "button";
    remove.title = `Delete chat: ${chat.title}`;
    remove.setAttribute("aria-label", remove.title);
    const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
    use.setAttribute("href", "#i-delete");
    icon.setAttribute("aria-hidden", "true");
    icon.append(use);
    remove.append(icon);
    remove.onclick = () => {
      if (state.busy) return;
      state.deleteChat = chat.id;
      $("delete-chat-title").textContent = chat.title;
      $("delete-dialog").showModal();
    };
    row.append(button, remove);
    list.append(row);
  }
  render();
}

$("cancel-delete").onclick = () => $("delete-dialog").close();
$("delete-dialog").addEventListener("close", () => { state.deleteChat = null; });
$("confirm-delete").onclick = async () => {
  const id = state.deleteChat;
  if (!id || state.busy) return;
  $("delete-dialog").close();
  state.busy = true;
  updateControls();
  notice();
  try {
    await api(`/api/conversations/${encodeURIComponent(id)}`, {method: "DELETE"});
    state.busy = false;
    if (state.id === id) newChat();
    await listChats();
  } catch (error) {
    notice(error.message);
  } finally {
    state.busy = false;
    updateControls();
  }
};

async function openChat(id) {
  if (state.busy) return;
  state.busy = true;
  state.mapMessage = null;
  placeMap.show(emptyPlaces);
  showMap(false);
  updateControls();
  notice();
  try {
    let chat = await api(`/api/conversations/${encodeURIComponent(id)}`);
    state.id = id;
    sessionStorage.setItem("vistra-conversation", id);
    removeImage();
    $("message").value = "";
    closeSidebar();
    while (chat.busy) {
      state.messages = [...chat.messages, {role: "assistant", pending: true}];
      render();
      scrollToBottom();
      await new Promise((resolve) => setTimeout(resolve, 1200));
      chat = await api(`/api/conversations/${encodeURIComponent(id)}`);
    }
    state.messages = chat.messages;
    state.mapMessage = chat.messages.findLast((message) => message.role === "assistant") || null;
  } catch (error) {
    if (error.status === 404) {
      state.id = null;
      state.messages = [];
      state.mapMessage = null;
      sessionStorage.removeItem("vistra-conversation");
    }
    notice(error.message);
  } finally {
    state.busy = false;
    render();
    resizeMessage();
    scrollToBottom();
    $("message").focus();
  }
}

$("composer").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!state.ready || state.busy) return;
  const message = $("message").value.trim();
  if (!message && !state.image) return;
  state.busy = true;
  notice();
  const previous = [...state.messages];
  const userMessage = {role: "user", content: message || "Find places with scenery like this image.", image_url: state.preview};
  const pending = {role: "assistant", pending: true, activity: []};
  state.mapMessage = null;
  state.messages.push(userMessage, pending);
  render();
  scrollToBottom();
  try {
    if (!state.id) {
      const chat = await api("/api/conversations", {method: "POST"});
      state.id = chat.id;
      sessionStorage.setItem("vistra-conversation", state.id);
    }
    const form = new FormData();
    form.append("conversation_id", state.id);
    form.append("message", message);
    if (state.image) form.append("image", state.image);
    const result = await chatStream(form, pending);
    userMessage.image_url = result.image_url;
    const reply = {role: "assistant", content: result.answer, images: result.images, activity: result.activity, places: result.places || emptyPlaces};
    state.mapMessage = reply;
    state.messages = [...previous, userMessage, reply];
    $("message").value = "";
    removeImage();
    await revealAnswer(reply);
    await listChats().catch((error) => notice(error.message));
  } catch (error) {
    if (pending.activity.length) {
      const activity = [...pending.activity.map((item) => item.status === "running" ? {...item, status: "error", title: `${item.title} (interrupted)`} : item), {
        step: "error", title: "Request failed", detail: error.message, status: "error",
      }];
      state.messages = [...previous, userMessage, {role: "assistant", content: error.message, activity, activityOpen: true}];
    } else state.messages = previous;
    notice(error.message);
  } finally {
    state.busy = false;
    render();
    resizeMessage();
    scrollToBottom();
    $("message").focus();
  }
});

$("message").addEventListener("input", resizeMessage);
$("message").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    if (!$("send").disabled) $("composer").requestSubmit();
  }
});
$("new-chat").onclick = newChat;
$("toggle-map").onclick = () => showMap(!document.body.classList.contains("map-open"));
$("fit-places").onclick = () => placeMap.fit();
$("add-image").onclick = () => $("image").click();
$("remove-image").onclick = removeImage;
$("image").addEventListener("change", () => {
  const file = $("image").files[0];
  if (!file) return;
  if (!["image/jpeg", "image/png", "image/webp"].includes(file.type) || file.size > state.maxUploadMb * 1024 * 1024) {
    $("image").value = "";
    notice(`Choose a JPEG, PNG, or WebP image under ${state.maxUploadMb} MB.`);
    return;
  }
  removeImage();
  state.image = file;
  state.preview = URL.createObjectURL(file);
  $("preview").src = state.preview;
  $("filename").textContent = file.name;
  $("attachment").hidden = false;
  notice();
  updateControls();
  $("message").focus();
});
$("toggle-sidebar").onclick = () => {
  const open = document.body.classList.toggle("sidebar-open");
  $("sidebar-backdrop").hidden = !open;
  $("toggle-sidebar").setAttribute("aria-expanded", String(open));
};
$("sidebar-backdrop").onclick = closeSidebar;
document.addEventListener("keydown", (event) => { if (event.key === "Escape") closeSidebar(); });
document.querySelectorAll("[data-query]").forEach((button) => {
  button.onclick = () => {
    if (state.busy) return;
    $("message").value = button.dataset.query;
    resizeMessage();
    $("message").focus();
  };
});
$("memory-button").onclick = async () => {
  closeSidebar();
  $("memory-list").replaceChildren(element("dd", "muted", "Loading preferences…"));
  $("memory-dialog").showModal();
  try {
    const facts = await api("/api/memories");
    $("memory-list").replaceChildren();
    if (!Object.keys(facts).length) $("memory-list").append(element("dd", "muted", "Ask VISTRA to remember a travel preference."));
    for (const [name, value] of Object.entries(facts)) {
      $("memory-list").append(element("dt", "", name.replaceAll("_", " ")), element("dd", "", value));
    }
  } catch (error) { $("memory-list").replaceChildren(element("dd", "", error.message)); }
};
$("close-memory").onclick = () => $("memory-dialog").close();

async function start() {
  render();
  try {
    const health = await api("/api/health");
    $("model").textContent = health.model;
    placeMap.init(health.map);
    state.maxUploadMb = health.max_upload_mb;
    $("message").maxLength = health.max_message_chars;
    state.ready = true;
    if (!health.ai_configured) notice("Add OPENAI_API_KEY to .env and restart the backend to start chatting.");
    await listChats();
    if (state.id) await openChat(state.id);
  } catch (error) { notice(error.message); }
  updateControls();
}

start();
