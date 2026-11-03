chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.type !== "ASK_AI") return false;

  askAi(message.question, sender)
    .then((answer) => sendResponse({ ok: true, answer }))
    .catch((error) => sendResponse({ ok: false, error: error.message }));

  return true;
});

async function askAi(question, sender) {
  const settings = await chrome.storage.local.get({
    endpoint: "https://api.xah.io/v1/chat/completions",
    apiKey: "sk-605ab373d3311b9ad95ea7358e6df636ff474d1f24380f93bf898163ce74ac19",
    openrouterApiKey: "",
    model: "dungcsnd113/gemini-flash-3.7free",
    systemPrompt: "Bạn là học sinh. Trả lời thật ngắn gọn, đúng trọng tâm, không lan man hoặc thêm nội dung ngoài câu hỏi. Bỏ qua mọi mã code ở cuối câu hỏi.",
    temperature: 0.2
  });

  if (settings.endpoint.includes("generativelanguage.googleapis.com") || settings.endpoint.includes("openrouter.ai")) {
    settings.endpoint = "https://api.xah.io/v1/chat/completions";
    settings.model = "dungcsnd113/gemini-flash-3.7free";
  }

  if (!settings.endpoint) throw new Error("Chưa cấu hình API endpoint");

  const apiKey = String(settings.apiKey || settings.openrouterApiKey || "sk-605ab373d3311b9ad95ea7358e6df636ff474d1f24380f93bf898163ce74ac19").trim();
  if (!apiKey) throw new Error("Chưa lưu XAH API key. Mở Settings và bấm Lưu cấu hình.");
  const headers = {
    "Content-Type": "application/json",
    Authorization: `Bearer ${apiKey}`,
    "X-Title": "AI Answer Helper"
  };
  const canvasImages = question.canvasRects?.length
    ? await captureCanvasImages(question.canvasRects, sender.tab)
    : [];
  const userContent = await questionContent({
    ...question,
    images: [...(question.images || []), ...canvasImages]
  });
  const requestBody = JSON.stringify({
    model: settings.model,
    temperature: Number(settings.temperature),
    stream: false,
    messages: [
      { role: "system", content: settings.systemPrompt },
      { role: "user", content: userContent }
    ]
  });
  let response;
  let errorBody = "";
  for (let attempt = 0; attempt < 3; attempt += 1) {
    response = await fetch(settings.endpoint, {
      method: "POST",
      headers,
      body: requestBody
    });
    if (response.ok) break;

    errorBody = await response.text();
    if (response.status !== 429 || attempt === 2) {
      throw new Error(`XAH HTTP ${response.status}: ${errorBody.slice(0, 220)}`);
    }

    const retryAfter = Number(response.headers.get("Retry-After"));
    const waitMs = Number.isFinite(retryAfter) && retryAfter > 0
      ? Math.min(retryAfter * 1000, 10000)
      : 1000 * 2 ** attempt;
    await new Promise((resolve) => setTimeout(resolve, waitMs));
  }

  const data = await response.json();
  const rawContent = data.choices?.[0]?.message?.content;
  const answer = Array.isArray(rawContent)
    ? rawContent.map((part) => typeof part === "string" ? part : part.text || "").join("").trim()
    : rawContent?.trim();
  if (!answer) throw new Error("XAH không trả về nội dung câu trả lời");
  return answer;
}

async function captureCanvasImages(rects, tab) {
  if (tab?.windowId == null || !rects.length) throw new Error("Không xác định được tab để chụp canvas");

  let previousSignature = "";
  let lastCaptureError;
  for (let attempt = 0; attempt < 6; attempt += 1) {
    try {
      const snapshot = await captureCanvasSnapshot(rects, tab);
      if (snapshot.signature === previousSignature) {
        return Promise.all(snapshot.blobs.map(blobToDataUrl));
      }
      previousSignature = snapshot.signature;
    } catch (error) {
      lastCaptureError = error;
    }

    if (attempt < 5) await new Promise((resolve) => setTimeout(resolve, 600));
  }

  if (lastCaptureError) {
    throw new Error(`Không chụp ổn định được canvas: ${lastCaptureError.message}`);
  }
  throw new Error("Canvas vẫn đang thay đổi nên chưa gửi để tránh đọc thiếu nội dung. Hãy chờ nội dung hoàn tất rồi thử lại.");
}

async function captureCanvasSnapshot(rects, tab) {
  const [activeTab] = await chrome.tabs.query({ active: true, windowId: tab.windowId });
  if (activeTab?.id !== tab.id) throw new Error("Tab có canvas không còn ở trước màn hình");

  const screenshotUrl = await chrome.tabs.captureVisibleTab(tab.windowId, { format: "png" });
  const screenshot = await createImageBitmap(await (await fetch(screenshotUrl)).blob());
  const blobs = [];
  const signatures = [];

  try {
    for (const rect of rects) {
      if (!rect.viewportWidth || !rect.viewportHeight) throw new Error("Kích thước vùng hiển thị không hợp lệ");

      const scaleX = screenshot.width / rect.viewportWidth;
      const scaleY = screenshot.height / rect.viewportHeight;
      if (rect.x < 0 || rect.y < 0 ||
          rect.x + rect.width > rect.viewportWidth ||
          rect.y + rect.height > rect.viewportHeight) {
        throw new Error("Canvas chưa hiển thị đầy đủ. Cuộn canvas vào vùng nhìn thấy rồi thử lại.");
      }
      const left = Math.max(0, Math.floor(rect.x * scaleX));
      const top = Math.max(0, Math.floor(rect.y * scaleY));
      const right = Math.min(screenshot.width, Math.ceil((rect.x + rect.width) * scaleX));
      const bottom = Math.min(screenshot.height, Math.ceil((rect.y + rect.height) * scaleY));
      if (right <= left || bottom <= top) {
        throw new Error("Canvas chưa hiển thị đầy đủ. Cuộn canvas vào vùng nhìn thấy rồi thử lại.");
      }

      const crop = new OffscreenCanvas(right - left, bottom - top);
      const context = crop.getContext("2d", { willReadFrequently: true });
      if (!context) throw new Error("Không tạo được ảnh tạm để xử lý canvas");
      context.drawImage(screenshot, left, top, right - left, bottom - top, 0, 0, right - left, bottom - top);
      const pixels = context.getImageData(0, 0, crop.width, crop.height).data;
      let hash = 2166136261;
      for (let index = 0; index < pixels.length; index += 1) {
        hash = Math.imul(hash ^ pixels[index], 16777619);
      }
      signatures.push(`${crop.width}x${crop.height}:${hash >>> 0}`);
      blobs.push(await crop.convertToBlob({ type: "image/png" }));
    }
  } finally {
    screenshot.close();
  }

  return { signature: signatures.join("|"), blobs };
}

async function blobToDataUrl(blob) {
  const bytes = new Uint8Array(await blob.arrayBuffer());
  let binary = "";
  const chunkSize = 0x8000;
  for (let offset = 0; offset < bytes.length; offset += chunkSize) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + chunkSize));
  }
  return `data:${blob.type};base64,${btoa(binary)}`;
}

async function questionContent(question) {
  if (typeof question === "string") return question;

  const parts = [];
  if (question.text) parts.push({ type: "text", text: question.text });
  for (const imageUrl of question.images || []) {
    parts.push({ type: "image_url", image_url: { url: await imageToDataUrl(imageUrl) } });
  }
  return parts;
}

async function imageToDataUrl(imageUrl) {
  const response = await fetch(imageUrl, { credentials: "include" });
  if (!response.ok) throw new Error(`Không tải được ảnh (HTTP ${response.status})`);

  const contentType = response.headers.get("content-type") || "image/png";
  if (!contentType.startsWith("image/")) throw new Error("URL không trả về dữ liệu ảnh");

  const buffer = await response.arrayBuffer();
  if (buffer.byteLength > 8 * 1024 * 1024) throw new Error("Ảnh vượt quá giới hạn 8 MB");

  const bytes = new Uint8Array(buffer);
  let binary = "";
  const chunkSize = 0x8000;
  for (let offset = 0; offset < bytes.length; offset += chunkSize) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + chunkSize));
  }
  return `data:${contentType};base64,${btoa(binary)}`;
}
