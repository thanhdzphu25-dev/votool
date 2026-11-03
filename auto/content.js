(() => {
  if (window.__aiAnswerHelperLoaded) return;
  window.__aiAnswerHelperLoaded = true;

  const state = {
    busy: false,
    auto: false,
    awaitingNext: false,
    lastQuestion: "",
    lastError: "",
    clickCount: 0,
    clickTimer: null,
    statusTimer: null,
    shortcutTimer: null,
    watchTimer: null,
    nextCandidate: "",
    nextCandidateChecks: 0,
    canvasIds: new WeakMap(),
    nextCanvasId: 0
  };
  const selectors = {
    messages: ".message-container.message-assistant",
    textarea: "textarea.w-md-editor-text-input"
  };
  const completionMessage = "Chúc mừng em hoàn thành buổi học!";

  const trigger = document.createElement("button");
  trigger.type = "button";
  trigger.setAttribute("aria-label", "AI helper");
  Object.assign(trigger.style, {
    position: "fixed", right: "0", bottom: "0", zIndex: "2147483647",
    width: "48px", height: "48px", opacity: "0", border: "0", padding: "0",
    background: "transparent", cursor: "default"
  });
  document.documentElement.appendChild(trigger);

  const status = document.createElement("span");
  Object.assign(status.style, {
    position: "fixed", right: "16px", bottom: "16px", zIndex: "2147483647",
    display: "none", width: "10px", height: "10px", borderRadius: "50%",
    background: "#dc2626", boxShadow: "0 0 0 3px #dc262633"
  });
  document.documentElement.appendChild(status);

  const thinking = document.createElement("span");
  thinking.setAttribute("aria-label", "AI đang suy nghĩ");
  Object.assign(thinking.style, {
    position: "fixed", right: "16px", bottom: "16px", zIndex: "2147483647",
    display: "none", width: "8px", height: "8px", borderRadius: "50%",
    background: "#dc2626", boxShadow: "0 0 0 3px #dc262633"
  });
  document.documentElement.appendChild(thinking);

  const debugButton = document.createElement("button");
  debugButton.type = "button";
  debugButton.textContent = "AI trả lời";
  Object.assign(debugButton.style, {
    position: "fixed", right: "18px", bottom: "18px", zIndex: "2147483646",
    display: "none", border: "0", borderRadius: "6px", padding: "8px 10px",
    background: "#1f2937", color: "#fff", font: "600 12px system-ui", cursor: "pointer"
  });
  document.documentElement.appendChild(debugButton);

  document.addEventListener("keydown", (event) => {
    if (!event.ctrlKey || event.repeat) return;

    if (event.key.toLowerCase() === "b") {
      event.preventDefault();
      state.shortcutTimer = setTimeout(() => { state.shortcutTimer = null; }, 1200);
      toggleDebug();
      return;
    }

    if (event.key.toLowerCase() === "a" && state.shortcutTimer) {
      event.preventDefault();
      clearTimeout(state.shortcutTimer);
      state.shortcutTimer = null;
      toggleAuto();
    }
  }, true);

  chrome.runtime.onMessage.addListener((message) => {
    if (message.type === "TOGGLE_DEBUG") {
      toggleDebug();
    }
  });

  debugButton.addEventListener("click", askLatestQuestion);

  trigger.addEventListener("click", () => {
    state.clickCount += 1;
    clearTimeout(state.clickTimer);
    state.clickTimer = setTimeout(() => { state.clickCount = 0; }, 1500);
    if (state.clickCount >= 3) {
      state.clickCount = 0;
      askLatestQuestion();
    }
  });

  async function askLatestQuestion() {
    if (state.busy) return;
    state.busy = true;
    state.lastError = "";
    clearTimeout(state.statusTimer);
    hideStatus();
    if (state.auto) showStatus("#f59e0b");

    try {
      const question = await waitForLatestQuestion();
      if (!question.text && question.images.length === 0 && question.canvasRects.length === 0) {
        throw new Error("Không tìm thấy nội dung câu hỏi mới nhất");
      }
      if (state.auto && isCompletionMessage(question.text)) {
        stopAuto();
        return;
      }
      const questionFingerprint = getQuestionFingerprint(question);
      if (questionFingerprint === state.lastQuestion) {
        state.awaitingNext = true;
        return;
      }

      fillAnswer(".", false);
      showThinking();
      const result = await chrome.runtime.sendMessage({ type: "ASK_AI", question });
      if (!result?.ok) throw new Error(result?.error || "Không gọi được AI");
      fillAnswer(result.answer, true);
      state.lastQuestion = questionFingerprint;
      state.awaitingNext = state.auto;
      if (state.auto) watchForNextQuestion();
      else {
        showStatus("#16a34a");
        state.statusTimer = setTimeout(hideStatus, 1000);
      }
    } catch (error) {
      if (isInvalidatedContextError(error)) {
        stopAuto();
        return;
      }
      console.error("AI Answer Helper:", error);
      state.lastError = error.message;
      debugButton.textContent = `Lỗi: ${state.lastError}`;
      debugButton.style.background = "#991b1b";
      showStatus("#dc2626");
      if (!state.auto) state.statusTimer = setTimeout(hideStatus, 4000);
      if (state.auto) watchForNextQuestion();
    } finally {
      state.busy = false;
      hideThinking();
    }
  }

  function toggleDebug() {
    if (debugButton.style.display !== "none") {
      debugButton.style.display = "none";
      return;
    }
    debugButton.textContent = state.lastError ? `Lỗi: ${state.lastError}` : "AI trả lời";
    debugButton.style.background = state.lastError ? "#991b1b" : "#1f2937";
    debugButton.style.display = "block";
  }

  function toggleAuto() {
    if (state.auto) {
      stopAuto();
      return;
    }
    state.auto = true;
    state.awaitingNext = false;
    state.lastError = "";
    showStatus("#f59e0b");
    askLatestQuestion();
  }

  function stopAuto() {
    state.auto = false;
    state.awaitingNext = false;
    state.nextCandidate = "";
    state.nextCandidateChecks = 0;
    clearTimeout(state.watchTimer);
    state.watchTimer = null;
    if (!state.busy) hideStatus();
  }

  function watchForNextQuestion() {
    clearTimeout(state.watchTimer);
    if (!state.auto) return;
    state.watchTimer = setTimeout(() => {
      if (!state.auto || state.busy) return watchForNextQuestion();
      let question;
      try {
        question = readLatestQuestion();
      } catch (error) {
        console.error("AI Answer Helper:", error);
        state.lastError = error.message;
        debugButton.textContent = `Lỗi: ${state.lastError}`;
        debugButton.style.background = "#991b1b";
        stopAuto();
        showStatus("#dc2626");
        setTimeout(hideStatus, 1800);
        return;
      }
      const fingerprint = question ? getQuestionFingerprint(question) : "";
      if (question && isCompletionMessage(question.text)) {
        stopAuto();
      } else if (question.element && fingerprint && fingerprint !== state.lastQuestion) {
        if (fingerprint === state.nextCandidate) state.nextCandidateChecks += 1;
        else {
          state.nextCandidate = fingerprint;
          state.nextCandidateChecks = 1;
        }

        if (state.nextCandidateChecks >= 2) {
          state.nextCandidate = "";
          state.nextCandidateChecks = 0;
          state.awaitingNext = false;
          askLatestQuestion();
        } else {
          watchForNextQuestion();
        }
      } else {
        state.nextCandidate = "";
        state.nextCandidateChecks = 0;
        watchForNextQuestion();
      }
    }, 700);
  }

  function readLatestQuestion() {
    const messages = [...document.querySelectorAll(selectors.messages)];
    const latest = messages.at(-1);
    if (!latest) return { text: "", images: [], canvasSignatures: [], canvasRects: [] };
    if (latest.querySelector(".chat-pending-placeholder")) {
      return { element: latest, text: "", images: [], canvasSignatures: [], canvasRects: [] };
    }

    const clone = latest.cloneNode(true);
    clone.querySelectorAll("style, script, svg, button, textarea, video, [aria-hidden=\"true\"]").forEach((node) => node.remove());
    const imageSources = [...clone.querySelectorAll("img")]
      .map((image) => image.currentSrc || image.getAttribute("src") || image.getAttribute("data-src") || image.src)
      .filter((src) => src.startsWith("http://") || src.startsWith("https://") || src.startsWith("data:image/"));
    const images = [...imageSources];
    const canvasSignatures = [];
    const canvasRects = [];
    for (const canvas of latest.querySelectorAll('canvas[data-testid="chat-canvas-message"]')) {
      let canvasId = state.canvasIds.get(canvas);
      if (!canvasId) {
        canvasId = ++state.nextCanvasId;
        state.canvasIds.set(canvas, canvasId);
      }
      const rect = canvas.getBoundingClientRect();
      canvasSignatures.push(`${canvasId}:${canvas.width}x${canvas.height}`);
      if (rect.width > 0 && rect.height > 0) {
        canvasRects.push({
          x: rect.x,
          y: rect.y,
          width: rect.width,
          height: rect.height,
          viewportWidth: window.innerWidth,
          viewportHeight: window.innerHeight
        });
      }
    }
    clone.querySelectorAll("img").forEach((image) => image.remove());
    clone.querySelectorAll("canvas").forEach((canvas) => canvas.remove());
    return {
      element: latest,
      text: clone.innerText.replace(/\s+/g, " ").trim(),
      images: [...new Set(images)],
      imageFingerprints: imageSources.map((src) => src.startsWith("data:image/") ? `${src.length}:${hashText(src)}` : src),
      canvasSignatures,
      canvasRects
    };
  }

  async function waitForLatestQuestion() {
    const deadline = Date.now() + 5000;
    let previousFingerprint = "";
    let stableReads = 0;
    let lastQuestion;

    while (Date.now() < deadline) {
      const question = readLatestQuestion();
      const hasContent = Boolean(
        question.text ||
        question.images.length ||
        question.canvasSignatures.length
      );
      const fingerprint = hasContent ? getQuestionFingerprint(question) : "";

      if (fingerprint && fingerprint === previousFingerprint) {
        stableReads += 1;
        if (stableReads >= 2) return readLatestQuestion(true);
      } else {
        previousFingerprint = fingerprint;
        stableReads = 0;
      }
      lastQuestion = question;
      await new Promise((resolve) => setTimeout(resolve, 300));
    }

    if (lastQuestion && (lastQuestion.text || lastQuestion.images.length || lastQuestion.canvasSignatures.length)) {
      throw new Error("Nội dung câu hỏi vẫn đang thay đổi. Hãy chờ trang hiển thị xong rồi thử lại.");
    }
    throw new Error("Không tìm thấy nội dung câu hỏi. Hãy chờ trang hoàn tất tải rồi thử lại.");
  }

  function getQuestionFingerprint(question) {
    return `${question.text}|${(question.imageFingerprints || question.images).join("|")}|${(question.canvasSignatures || []).join("|")}`;
  }

  function hashText(text) {
    let hash = 2166136261;
    for (let index = 0; index < text.length; index += 1) {
      hash = Math.imul(hash ^ text.charCodeAt(index), 16777619);
    }
    return hash >>> 0;
  }

  function isCompletionMessage(text) {
    return String(text || "").includes(completionMessage);
  }

  function isInvalidatedContextError(error) {
    return String(error?.message || error).toLowerCase().includes("extension context invalidated");
  }

  function fillAnswer(answer, submit) {
    const textarea = document.querySelector(selectors.textarea);
    if (!textarea) throw new Error("Không tìm thấy text box trả lời");

    const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value").set;
    setter.call(textarea, answer);
    textarea.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: answer }));
    textarea.dispatchEvent(new Event("change", { bubbles: true }));
    textarea.focus();

    if (submit) submitAnswer(textarea);
  }

  function submitAnswer(textarea) {
    const enterOptions = {
      key: "Enter",
      code: "Enter",
      keyCode: 13,
      which: 13,
      bubbles: true,
      cancelable: true
    };
    textarea.dispatchEvent(new KeyboardEvent("keydown", enterOptions));
    textarea.dispatchEvent(new KeyboardEvent("keyup", enterOptions));
  }

  function showStatus(color) {
    status.style.background = color;
    status.style.display = "block";
  }

  function hideStatus() {
    status.style.display = "none";
  }

  function showThinking() {
    thinking.style.display = "block";
  }

  function hideThinking() {
    thinking.style.display = "none";
  }
})();
