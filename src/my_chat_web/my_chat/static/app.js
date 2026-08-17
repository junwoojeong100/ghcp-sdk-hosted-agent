(() => {
  "use strict";

  const csrfToken =
    document.querySelector('meta[name="csrf-token"]')?.content || "";
  const preferenceKey = `my-chat-preferences:${document.body.dataset.username || "user"}`;
  const conversationKey = `my-chat-current:${document.body.dataset.username || "user"}`;
  const state = {
    conversations: [],
    currentConversation: null,
    models: [],
    modelsReady: false,
    modelSource: "loading",
    sending: false,
    pendingFiles: [],
    pptMode: false,
    autoFollow: true,
  };

  const elements = {
    sidebar: document.getElementById("sidebar"),
    sidebarBackdrop: document.getElementById("sidebar-backdrop"),
    conversationList: document.getElementById("conversation-list"),
    newChat: document.getElementById("new-chat"),
    deleteChat: document.getElementById("delete-chat"),
    messages: document.getElementById("messages"),
    scroller: document.getElementById("message-scroller"),
    emptyState: document.getElementById("empty-state"),
    composer: document.getElementById("composer"),
    input: document.getElementById("message-input"),
    sendButton: document.getElementById("send-button"),
    attachFile: document.getElementById("attach-file"),
    fileInput: document.getElementById("file-input"),
    pendingAttachments: document.getElementById("pending-attachments"),
    pptMode: document.getElementById("ppt-mode"),
    modelSelect: document.getElementById("model-select"),
    reasoningSelect: document.getElementById("reasoning-select"),
    webSearchSelect: document.getElementById("web-search-select"),
    connectionStatus: document.getElementById("connection-status"),
    connectionStatusText: document.getElementById("connection-status-text"),
    modelWarning: document.getElementById("model-warning"),
    modelWarningText: document.getElementById("model-warning-text"),
    retryModels: document.getElementById("retry-models"),
    memoryDialog: document.getElementById("memory-dialog"),
    memoryForm: document.getElementById("memory-form"),
    memoryInput: document.getElementById("memory-input"),
    memoryCount: document.getElementById("memory-count"),
    settingsDialog: document.getElementById("settings-dialog"),
    passwordForm: document.getElementById("password-form"),
    passwordMessage: document.getElementById("password-message"),
    toast: document.getElementById("toast"),
  };

  async function api(path, options = {}) {
    const headers = new Headers(options.headers || {});
    if (
      options.body &&
      !(options.body instanceof FormData) &&
      !headers.has("Content-Type")
    ) {
      headers.set("Content-Type", "application/json");
    }
    if (
      options.method &&
      !["GET", "HEAD"].includes(options.method.toUpperCase())
    ) {
      headers.set("X-CSRF-Token", csrfToken);
    }

    const response = await fetch(path, { ...options, headers });
    if (response.status === 401) {
      window.location.assign("/login");
      throw new Error("로그인이 만료되었습니다.");
    }
    if (response.status === 204) {
      return null;
    }

    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail =
        typeof payload.detail === "string"
          ? payload.detail
          : payload.detail?.code || "요청을 처리하지 못했습니다.";
      const error = new Error(detail);
      error.status = response.status;
      throw error;
    }
    return payload;
  }

  async function streamApi(path, options, onEvent) {
    const headers = new Headers(options.headers || {});
    headers.set("X-CSRF-Token", csrfToken);
    const response = await fetch(path, { ...options, headers });
    if (response.status === 401) {
      window.location.assign("/login");
      throw new Error("로그인이 만료되었습니다.");
    }
    if (!response.ok || !response.body) {
      const payload = await response.json().catch(() => ({}));
      throw new Error(payload.detail || "스트리밍 요청을 시작하지 못했습니다.");
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { done, value } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
      const lines = buffer.split("\n");
      buffer = lines.pop() || "";
      for (const line of lines) {
        if (!line.trim()) continue;
        const event = JSON.parse(line);
        onEvent(event);
      }
      if (done) break;
    }
    if (buffer.trim()) onEvent(JSON.parse(buffer));
  }

  function showToast(message) {
    elements.toast.textContent = message;
    elements.toast.classList.add("visible");
    window.clearTimeout(showToast.timeout);
    showToast.timeout = window.setTimeout(
      () => elements.toast.classList.remove("visible"),
      3000,
    );
  }

  function setConnectionStatus(kind, text) {
    elements.connectionStatus.className = `connection-status status-${kind}`;
    elements.connectionStatusText.textContent = text;
  }

  function restoreConnectionStatus() {
    if (!state.modelsReady) {
      setConnectionStatus("error", "GitHub Copilot 연결 실패");
    } else if (state.modelSource === "configured_fallback") {
      setConnectionStatus("degraded", "모델 확인 필요");
    } else {
      setConnectionStatus("connected", "GitHub Copilot에 연결됨");
    }
  }

  function showModelWarning(message) {
    elements.modelWarningText.textContent = message;
    elements.modelWarning.classList.remove("hidden");
  }

  function hideModelWarning() {
    elements.modelWarning.classList.add("hidden");
    elements.modelWarningText.textContent = "";
  }

  function updateInteractiveState() {
    const hasModel = state.modelsReady && Boolean(elements.modelSelect.value);
    elements.modelSelect.disabled = !state.modelsReady || state.sending;
    elements.reasoningSelect.disabled = !state.modelsReady || state.sending;
    elements.webSearchSelect.disabled = !state.modelsReady || state.sending;
    elements.input.disabled = !state.modelsReady || state.sending;
    elements.deleteChat.disabled = !state.currentConversation || state.sending;
    elements.newChat.disabled = state.sending;
    elements.attachFile.disabled = !state.modelsReady || state.sending;
    elements.pptMode.disabled = !state.modelsReady || state.sending;
    elements.conversationList
      .querySelectorAll("button")
      .forEach((button) => {
        button.disabled = state.sending;
      });
    elements.sendButton.disabled =
      !hasModel ||
      state.sending ||
      (!elements.input.value.trim() && !state.pendingFiles.length);
    elements.composer.classList.toggle(
      "composer-disabled",
      !state.modelsReady,
    );
    elements.input.placeholder = state.modelsReady
      ? state.pptMode
        ? "만들고 싶은 PPT 주제와 대상 독자를 입력하세요"
        : "메시지를 입력하세요"
      : "GitHub Copilot에 연결하는 중...";
  }

  function closeSidebar() {
    elements.sidebar?.classList.remove("open");
    elements.sidebarBackdrop?.classList.remove("visible");
  }

  function formatDate(value) {
    if (!value) return "";
    const date = new Date(value);
    return new Intl.DateTimeFormat("ko-KR", {
      month: "numeric",
      day: "numeric",
    }).format(date);
  }

  function showConversationLoading() {
    elements.conversationList.replaceChildren();
    for (let index = 0; index < 4; index += 1) {
      const skeleton = document.createElement("div");
      skeleton.className = "conversation-skeleton";
      elements.conversationList.append(skeleton);
    }
  }

  function renderConversationList() {
    elements.conversationList.replaceChildren();
    if (!state.conversations.length) {
      const empty = document.createElement("div");
      empty.className = "conversation-empty";
      empty.textContent = "저장된 대화가 없습니다.";
      elements.conversationList.append(empty);
      return;
    }

    for (const conversation of state.conversations) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "conversation-item";
      if (conversation.id === state.currentConversation?.id) {
        button.classList.add("active");
      }

      const title = document.createElement("span");
      title.textContent = conversation.title;
      const date = document.createElement("small");
      date.textContent = formatDate(conversation.updated_at);
      button.append(title, date);
      button.addEventListener("click", () => openConversation(conversation.id));
      elements.conversationList.append(button);
    }
  }

  function renderMessages(messages) {
    elements.messages.replaceChildren();
    const hasMessages = messages.length > 0;
    elements.emptyState.classList.toggle("hidden", hasMessages);
    elements.messages.classList.toggle("hidden", !hasMessages);

    for (const message of messages) {
      appendMessage(message);
    }
    state.autoFollow = true;
    scrollToBottom(true);
  }

  function appendMessage(message, temporary = false) {
    elements.emptyState.classList.add("hidden");
    elements.messages.classList.remove("hidden");

    const row = document.createElement("article");
    row.className = `message-row ${message.role}`;
    if (temporary) row.dataset.temporary = "true";

    const bubble = document.createElement("div");
    bubble.className = "message-bubble";
    if (message.status === "error") {
      bubble.classList.add("message-error");
    }
    bubble.textContent = message.content;

    if (message.role === "assistant" && message.model) {
      const meta = document.createElement("div");
      meta.className = "message-meta";
      const durationMs = Number(message.duration_ms);
      const duration =
        Number.isFinite(durationMs) && durationMs >= 0
          ? ` · ${(durationMs / 1000).toFixed(durationMs < 10000 ? 1 : 0)}초`
          : "";
      meta.textContent = `${message.model} · ${
        message.reasoning_effort || "default"
      }${duration}`;
      bubble.append(meta);
    } else if (message.status === "error" && message.error) {
      const meta = document.createElement("div");
      meta.className = "message-meta";
      meta.textContent = message.error;
      bubble.append(meta);
    }

    const attachments = message.attachments || [];
    if (attachments.length) {
      const container = document.createElement("div");
      container.className = "message-attachments";
      for (const attachment of attachments) {
        const item = document.createElement(
          attachment.download_url ? "a" : "span",
        );
        item.className = `message-attachment${
          attachment.download_url ? "" : " pending"
        }`;
        if (attachment.download_url) {
          item.href = attachment.download_url;
          item.setAttribute("download", attachment.filename);
        }
        item.textContent = `▣ ${attachment.filename}`;
        container.append(item);
      }
      bubble.append(container);
    }

    row.append(bubble);
    elements.messages.append(row);
    scrollToBottom();
    return row;
  }

  function markMessageFailed(row, content, error) {
    const bubble = row.querySelector(".message-bubble");
    bubble.classList.add("message-error");

    const detail = document.createElement("div");
    detail.className = "message-meta";
    detail.textContent = `전송 실패: ${error.message}`;

    const retry = document.createElement("button");
    retry.type = "button";
    retry.className = "message-retry";
    retry.textContent = "내용 다시 가져오기";
    retry.addEventListener("click", () => {
      elements.input.value = content;
      resizeComposer();
      updateInteractiveState();
      elements.input.focus();
    });
    bubble.append(detail, retry);
  }

  function appendTyping() {
    const row = document.createElement("article");
    row.className = "message-row assistant";
    row.dataset.typing = "true";
    const bubble = document.createElement("div");
    bubble.className = "message-bubble typing";
    bubble.setAttribute("aria-label", "답변 생성 중");
    bubble.append(
      document.createElement("i"),
      document.createElement("i"),
      document.createElement("i"),
    );
    row.append(bubble);
    elements.messages.append(row);
    scrollToBottom();
    return row;
  }

  function isNearBottom() {
    const remaining =
      elements.scroller.scrollHeight -
      elements.scroller.scrollTop -
      elements.scroller.clientHeight;
    return remaining < 96;
  }

  function scrollToBottom(force = false) {
    if (!force && !state.autoFollow) return;
    window.requestAnimationFrame(() => {
      elements.scroller.scrollTop = elements.scroller.scrollHeight;
    });
  }

  function formatFileSize(size) {
    if (size < 1024) return `${size} B`;
    if (size < 1024 * 1024) return `${Math.ceil(size / 1024)} KB`;
    return `${(size / (1024 * 1024)).toFixed(1)} MB`;
  }

  function renderPendingFiles() {
    elements.pendingAttachments.replaceChildren();
    elements.pendingAttachments.classList.toggle(
      "hidden",
      !state.pendingFiles.length,
    );
    for (const [index, file] of state.pendingFiles.entries()) {
      const chip = document.createElement("div");
      chip.className = "pending-file";
      const name = document.createElement("span");
      name.className = "pending-file-name";
      name.textContent = file.name;
      const size = document.createElement("span");
      size.className = "pending-file-size";
      size.textContent = formatFileSize(file.size);
      const remove = document.createElement("button");
      remove.type = "button";
      remove.setAttribute("aria-label", `${file.name} 제거`);
      remove.textContent = "×";
      remove.addEventListener("click", () => {
        state.pendingFiles.splice(index, 1);
        renderPendingFiles();
        updateInteractiveState();
      });
      chip.append(name, size, remove);
      elements.pendingAttachments.append(chip);
    }
  }

  function addPendingFiles(files) {
    const allowedExtensions = new Set([
      "txt",
      "md",
      "csv",
      "json",
      "pdf",
      "png",
      "jpg",
      "jpeg",
      "gif",
      "webp",
      "docx",
      "xlsx",
      "pptx",
    ]);
    const merged = [...state.pendingFiles];
    for (const file of files) {
      const extension = file.name.split(".").pop()?.toLowerCase() || "";
      if (!allowedExtensions.has(extension)) {
        showToast(`지원하지 않는 파일 형식입니다: ${file.name}`);
        continue;
      }
      if (file.size > 8 * 1024 * 1024) {
        showToast(`8MB를 초과한 파일입니다: ${file.name}`);
        continue;
      }
      if (
        merged.some(
          (item) =>
            item.name === file.name &&
            item.size === file.size &&
            item.lastModified === file.lastModified,
        )
      ) {
        continue;
      }
      if (merged.length >= 5) {
        showToast("파일은 한 번에 최대 5개까지 첨부할 수 있습니다.");
        break;
      }
      const totalSize =
        merged.reduce((total, item) => total + item.size, 0) + file.size;
      if (totalSize > 16 * 1024 * 1024) {
        showToast("전체 첨부 파일은 16MB 이하여야 합니다.");
        break;
      }
      merged.push(file);
    }
    state.pendingFiles = merged;
    renderPendingFiles();
    updateInteractiveState();
  }

  function clearPendingFiles() {
    state.pendingFiles = [];
    elements.fileInput.value = "";
    renderPendingFiles();
  }

  function setPptMode(enabled) {
    state.pptMode = enabled;
    elements.pptMode.classList.toggle("active", enabled);
    elements.pptMode.setAttribute("aria-pressed", String(enabled));
    updateInteractiveState();
  }

  function selectedModel() {
    return (
      state.models.find((model) => model.id === elements.modelSelect.value) ||
      state.models[0]
    );
  }

  function readPreferences() {
    try {
      return JSON.parse(window.localStorage.getItem(preferenceKey) || "{}");
    } catch {
      return {};
    }
  }

  function savePreferences() {
    window.localStorage.setItem(
      preferenceKey,
      JSON.stringify({
        model: elements.modelSelect.value,
        reasoningEffort: elements.reasoningSelect.value,
        webSearchMode: elements.webSearchSelect.value,
      }),
    );
  }

  function refreshReasoningOptions(preferred = "default") {
    const model = selectedModel();
    const efforts = model?.reasoning_efforts || [];
    elements.reasoningSelect.replaceChildren();

    const defaultOption = document.createElement("option");
    defaultOption.value = "default";
    defaultOption.textContent = model?.default_reasoning_effort
      ? `모델 기본값 (${model.default_reasoning_effort})`
      : "모델 기본값";
    elements.reasoningSelect.append(defaultOption);

    const labels = {
      low: "낮음",
      medium: "보통",
      high: "높음",
      xhigh: "매우 높음",
      max: "최대",
    };
    for (const effort of efforts) {
      if (!labels[effort]) continue;
      const option = document.createElement("option");
      option.value = effort;
      option.textContent = labels[effort];
      elements.reasoningSelect.append(option);
    }

    const available = [...elements.reasoningSelect.options].some(
      (option) => option.value === preferred,
    );
    const fastDefault =
      preferred === "default" && efforts.includes("low") ? "low" : preferred;
    const fastDefaultAvailable = [...elements.reasoningSelect.options].some(
      (option) => option.value === fastDefault,
    );
    elements.reasoningSelect.value =
      available && fastDefaultAvailable ? fastDefault : "default";
  }

  async function loadModels(forceRefresh = false) {
    state.modelsReady = false;
    setConnectionStatus("loading", "GitHub Copilot 연결 중");
    hideModelWarning();
    elements.modelSelect.replaceChildren();
    const loadingOption = document.createElement("option");
    loadingOption.textContent = "모델 불러오는 중...";
    elements.modelSelect.append(loadingOption);
    updateInteractiveState();

    try {
      const suffix = forceRefresh ? "?refresh=true" : "";
      const payload = await api(`/api/models${suffix}`);
      state.models = payload.models || [];
      if (!state.models.length) {
        throw new Error("사용 가능한 GitHub Copilot 모델이 없습니다.");
      }

      elements.modelSelect.replaceChildren();
      for (const model of state.models) {
        const option = document.createElement("option");
        option.value = model.id;
        const multiplier = model.billing_multiplier
          ? ` · ${model.billing_multiplier}x`
          : "";
        option.textContent = `${model.name || model.id}${multiplier}`;
        elements.modelSelect.append(option);
      }

      const preferences = readPreferences();
      const preferredModel = state.currentConversation?.model || preferences.model;
      if (state.models.some((model) => model.id === preferredModel)) {
        elements.modelSelect.value = preferredModel;
      } else if (state.models.some((model) => model.id === "gpt-5.6-sol")) {
        elements.modelSelect.value = "gpt-5.6-sol";
      } else {
        elements.modelSelect.value = state.models[0].id;
      }

      state.modelsReady = true;
      state.modelSource = payload.source;
      refreshReasoningOptions(
        state.currentConversation?.reasoning_effort ||
          preferences.reasoningEffort ||
          "default",
      );
      elements.webSearchSelect.value =
        ["auto", "required", "disabled"].includes(preferences.webSearchMode)
          ? preferences.webSearchMode
          : "auto";
      savePreferences();

      if (payload.warning) {
        showModelWarning(payload.warning);
      } else if (payload.missing_requested_models?.length) {
        showModelWarning(
          `현재 Copilot 계정에서 사용할 수 없는 모델: ${payload.missing_requested_models.join(", ")}`,
        );
      } else {
        hideModelWarning();
      }
      restoreConnectionStatus();
    } catch (error) {
      state.models = [];
      state.modelsReady = false;
      state.modelSource = "error";
      elements.modelSelect.replaceChildren();
      const errorOption = document.createElement("option");
      errorOption.textContent = "모델 연결 실패";
      elements.modelSelect.append(errorOption);
      setConnectionStatus("error", "GitHub Copilot 연결 실패");
      showModelWarning(
        `모델 목록을 불러오지 못했습니다. 잠시 후 다시 시도해 주세요. (${error.message})`,
      );
      showToast("Copilot 모델 연결에 실패했습니다.");
    } finally {
      updateInteractiveState();
    }
  }

  async function loadConversations(showLoading = true) {
    if (showLoading) {
      showConversationLoading();
    }
    try {
      const payload = await api("/api/conversations");
      state.conversations = payload.conversations;
      renderConversationList();
    } catch (error) {
      elements.conversationList.replaceChildren();
      const failure = document.createElement("div");
      failure.className = "conversation-empty";
      failure.textContent = "대화 기록을 불러오지 못했습니다.";
      elements.conversationList.append(failure);
      showToast(error.message);
    }
  }

  function startNewConversation() {
    if (state.sending) {
      showToast("답변 생성이 끝난 뒤 새 대화를 시작해 주세요.");
      return;
    }
    state.currentConversation = null;
    clearPendingFiles();
    setPptMode(false);
    window.localStorage.removeItem(conversationKey);
    renderConversationList();
    renderMessages([]);
    updateInteractiveState();
    closeSidebar();
    elements.input.focus();
  }

  async function createConversation() {
    const payload = await api("/api/conversations", {
      method: "POST",
      body: JSON.stringify({
        model: elements.modelSelect.value,
        reasoning_effort: elements.reasoningSelect.value,
      }),
    });
    state.conversations.unshift(payload.conversation);
    state.currentConversation = payload.conversation;
    window.localStorage.setItem(conversationKey, payload.conversation.id);
    renderConversationList();
    updateInteractiveState();
    return payload.conversation;
  }

  async function openConversation(conversationId) {
    if (state.sending) {
      showToast("답변 생성이 끝난 뒤 다른 대화를 열어 주세요.");
      return;
    }
    elements.scroller.setAttribute("aria-busy", "true");
    try {
      const payload = await api(`/api/conversations/${conversationId}`);
      state.currentConversation = payload.conversation;
      window.localStorage.setItem(conversationKey, payload.conversation.id);
      if (
        state.models.some(
          (model) => model.id === payload.conversation.model,
        )
      ) {
        elements.modelSelect.value = payload.conversation.model;
      }
      refreshReasoningOptions(payload.conversation.reasoning_effort);
      savePreferences();
      renderConversationList();
      renderMessages(payload.messages);
      updateInteractiveState();
      closeSidebar();
    } catch (error) {
      showToast(error.message);
    } finally {
      elements.scroller.removeAttribute("aria-busy");
    }
  }

  async function deleteCurrentConversation() {
    if (!state.currentConversation) return;
    if (!window.confirm("현재 대화를 영구적으로 삭제할까요?")) return;
    try {
      await api(`/api/conversations/${state.currentConversation.id}`, {
        method: "DELETE",
      });
      state.conversations = state.conversations.filter(
        (item) => item.id !== state.currentConversation.id,
      );
      startNewConversation();
      showToast("대화를 삭제했습니다.");
    } catch (error) {
      showToast(error.message);
    }
  }

  async function sendMessage(event) {
    event.preventDefault();
    const content = elements.input.value.trim();
    if (
      (!content && !state.pendingFiles.length) ||
      state.sending ||
      !state.modelsReady
    ) {
      return;
    }
    const effectiveContent = content || "첨부 파일을 분석해줘.";
    const pendingFileMetadata = state.pendingFiles.map((file) => ({
      filename: file.name,
      size_bytes: file.size,
    }));

    state.sending = true;
    state.autoFollow = true;
    scrollToBottom(true);
    setConnectionStatus(
      "working",
      `${selectedModel()?.name || "Copilot"} 답변 생성 중`,
    );
    elements.input.value = "";
    resizeComposer();
    updateInteractiveState();

    let optimistic = null;
    let typing = null;
    let streamingRow = null;
    try {
      if (!state.currentConversation) {
        await createConversation();
      }
      const targetConversationId = state.currentConversation.id;
      optimistic = appendMessage(
        {
          role: "user",
          content: effectiveContent,
          attachments: pendingFileMetadata,
        },
        true,
      );
      typing = appendTyping();

      const formData = new FormData();
      formData.append("content", effectiveContent);
      formData.append("model", elements.modelSelect.value);
      formData.append("reasoning_effort", elements.reasoningSelect.value);
      formData.append("web_search_mode", elements.webSearchSelect.value);
      formData.append("output_format", state.pptMode ? "pptx" : "text");
      for (const file of state.pendingFiles) {
        formData.append("files", file);
      }
      let payload = null;
      if (state.pptMode) {
        payload = await api(
          `/api/conversations/${targetConversationId}/messages`,
          {
            method: "POST",
            body: formData,
          },
        );
      } else {
        let streamText = null;
        await streamApi(
          `/api/conversations/${targetConversationId}/messages/stream`,
          { method: "POST", body: formData },
          (event) => {
            if (event.type === "delta") {
              typing?.remove();
              typing = null;
              if (!streamingRow) {
                streamingRow = appendMessage(
                  {
                    role: "assistant",
                    content: "",
                    model:
                      elements.webSearchSelect.value === "required"
                        ? "gpt-5.6-luna"
                        : elements.modelSelect.value,
                    reasoning_effort: elements.reasoningSelect.value,
                  },
                  true,
                );
                const bubble =
                  streamingRow.querySelector(".message-bubble");
                const meta = bubble.querySelector(".message-meta");
                streamText = document.createTextNode("");
                bubble.insertBefore(streamText, meta);
              }
              streamText.data += event.delta;
              scrollToBottom();
            } else if (event.type === "done") {
              payload = event;
            } else if (event.type === "error") {
              throw new Error(event.detail || "답변 스트리밍에 실패했습니다.");
            }
          },
        );
        if (!payload) {
          throw new Error("답변 완료 이벤트를 받지 못했습니다.");
        }
      }
      if (state.currentConversation?.id !== targetConversationId) {
        optimistic.remove();
        typing?.remove();
        streamingRow?.remove();
        await loadConversations(false);
        return;
      }
      optimistic.remove();
      typing?.remove();
      streamingRow?.remove();
      appendMessage(payload.user_message);
      appendMessage(payload.assistant_message);
      state.currentConversation = payload.conversation;
      clearPendingFiles();
      setPptMode(false);
      await loadConversations(false);
    } catch (error) {
      typing?.remove();
      streamingRow?.remove();
      if (optimistic) {
        markMessageFailed(optimistic, effectiveContent, error);
      } else {
        elements.input.value = effectiveContent;
        resizeComposer();
      }
      showToast(error.message);
    } finally {
      state.sending = false;
      restoreConnectionStatus();
      updateInteractiveState();
      elements.input.focus();
    }
  }

  function resizeComposer() {
    elements.input.style.height = "auto";
    elements.input.style.height = `${Math.min(
      elements.input.scrollHeight,
      180,
    )}px`;
  }

  async function openMemory() {
    try {
      const payload = await api("/api/memory");
      elements.memoryInput.value = payload.memory.content || "";
      elements.memoryCount.textContent = String(elements.memoryInput.value.length);
      elements.memoryDialog.showModal();
    } catch (error) {
      showToast(error.message);
    }
  }

  async function saveMemory(event) {
    event.preventDefault();
    try {
      await api("/api/memory", {
        method: "PUT",
        body: JSON.stringify({ content: elements.memoryInput.value }),
      });
      elements.memoryDialog.close();
      showToast("개인 메모리를 저장했습니다.");
    } catch (error) {
      showToast(error.message);
    }
  }

  async function deleteMemory() {
    if (!window.confirm("개인 메모리를 모두 삭제할까요?")) return;
    try {
      await api("/api/memory", { method: "DELETE" });
      elements.memoryInput.value = "";
      elements.memoryCount.textContent = "0";
      elements.memoryDialog.close();
      showToast("개인 메모리를 삭제했습니다.");
    } catch (error) {
      showToast(error.message);
    }
  }

  async function updatePassword(event) {
    event.preventDefault();
    elements.passwordMessage.className = "form-message";
    elements.passwordMessage.textContent = "";
    try {
      await api("/api/password", {
        method: "PUT",
        body: JSON.stringify({
          current_password: document.getElementById("current-password").value,
          new_password: document.getElementById("settings-new-password").value,
          confirm_password: document.getElementById(
            "settings-confirm-password",
          ).value,
        }),
      });
      elements.passwordForm.reset();
      elements.passwordMessage.classList.add("success");
      elements.passwordMessage.textContent = "비밀번호를 변경했습니다.";
    } catch (error) {
      elements.passwordMessage.classList.add("error");
      elements.passwordMessage.textContent = error.message;
    }
  }

  async function deleteAllChats() {
    if (!window.confirm("내 채팅 기록 전체를 영구적으로 삭제할까요?")) return;
    try {
      await api("/api/conversations", { method: "DELETE" });
      state.conversations = [];
      startNewConversation();
      elements.settingsDialog.close();
      showToast("모든 채팅 기록을 삭제했습니다.");
    } catch (error) {
      showToast(error.message);
    }
  }

  function bindEvents() {
    elements.newChat.addEventListener("click", startNewConversation);
    elements.deleteChat.addEventListener("click", deleteCurrentConversation);
    elements.composer.addEventListener("submit", sendMessage);
    elements.input.addEventListener("input", () => {
      resizeComposer();
      updateInteractiveState();
    });
    elements.input.addEventListener("keydown", (event) => {
      if (event.isComposing || event.keyCode === 229) {
        return;
      }
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        elements.composer.requestSubmit();
      }
    });
    elements.modelSelect.addEventListener("change", () => {
      refreshReasoningOptions();
      savePreferences();
      updateInteractiveState();
    });
    elements.reasoningSelect.addEventListener("change", savePreferences);
    elements.webSearchSelect.addEventListener("change", savePreferences);
    elements.scroller.addEventListener("scroll", () => {
      state.autoFollow = isNearBottom();
    });
    elements.retryModels.addEventListener("click", () => loadModels(true));
    elements.attachFile.addEventListener("click", () =>
      elements.fileInput.click(),
    );
    elements.fileInput.addEventListener("change", () => {
      addPendingFiles([...elements.fileInput.files]);
      elements.fileInput.value = "";
    });
    elements.pptMode.addEventListener("click", () =>
      setPptMode(!state.pptMode),
    );

    document.querySelectorAll("[data-suggestion]").forEach((button) => {
      button.addEventListener("click", () => {
        elements.input.value = button.dataset.suggestion;
        if (button.dataset.pptSuggestion === "true") {
          setPptMode(true);
        }
        resizeComposer();
        updateInteractiveState();
        elements.input.focus();
      });
    });

    document.getElementById("open-memory").addEventListener("click", openMemory);
    document.getElementById("close-memory").addEventListener("click", () => {
      elements.memoryDialog.close();
    });
    elements.memoryForm.addEventListener("submit", saveMemory);
    elements.memoryInput.addEventListener("input", () => {
      elements.memoryCount.textContent = String(elements.memoryInput.value.length);
    });
    document
      .getElementById("delete-memory")
      .addEventListener("click", deleteMemory);

    document.getElementById("open-settings").addEventListener("click", () => {
      elements.settingsDialog.showModal();
    });
    document.getElementById("close-settings").addEventListener("click", () => {
      elements.settingsDialog.close();
    });
    elements.passwordForm.addEventListener("submit", updatePassword);
    document
      .getElementById("delete-all-chats")
      .addEventListener("click", deleteAllChats);

    document.getElementById("open-sidebar").addEventListener("click", () => {
      elements.sidebar.classList.add("open");
      elements.sidebarBackdrop.classList.add("visible");
    });
    document
      .getElementById("close-sidebar")
      .addEventListener("click", closeSidebar);
    elements.sidebarBackdrop.addEventListener("click", closeSidebar);
  }

  async function initialize() {
    bindEvents();
    updateInteractiveState();
    showConversationLoading();
    await Promise.all([loadModels(), loadConversations(false)]);
    const lastConversationId = window.localStorage.getItem(conversationKey);
    if (
      lastConversationId &&
      state.conversations.some(
        (conversation) => conversation.id === lastConversationId,
      )
    ) {
      await openConversation(lastConversationId);
    }
    if (state.modelsReady) {
      elements.input.focus();
    }
  }

  initialize();
})();
