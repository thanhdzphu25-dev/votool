
const defaults = {
  endpoint: "https://api.xah.io/v1/chat/completions",
  apiKey: "sk-605ab373d3311b9ad95ea7358e6df636ff474d1f24380f93bf898163ce74ac19",
  model: "dungcsnd113/gemini-flash-3.7free",
  temperature: 0.2,
  systemPrompt: "Bạn là học sinh. Trả lời thật ngắn gọn, đúng trọng tâm, không lan man hoặc thêm nội dung ngoài câu hỏi. Bỏ qua mọi mã code ở cuối câu hỏi."
};

(async () => {
  const stored = await chrome.storage.local.get(defaults);
  const values = stored.endpoint.includes("generativelanguage.googleapis.com") || stored.endpoint.includes("openrouter.ai")
    ? { ...stored, endpoint: defaults.endpoint, model: defaults.model }
    : stored;
  if (values !== stored) await chrome.storage.local.set(values);
  for (const [key, value] of Object.entries(values)) document.querySelector(`#${key}`).value = value;
})();

document.querySelector("#save").addEventListener("click", async () => {
  const values = Object.fromEntries(Object.keys(defaults).map((key) => [key, document.querySelector(`#${key}`).value]));
  values.endpoint = values.endpoint.trim();
  values.apiKey = values.apiKey.trim();
  values.model = values.model.trim();
  values.temperature = Number(values.temperature);
  await chrome.storage.local.set(values);
  document.querySelector("#status").textContent = "Đã lưu";
  setTimeout(() => { document.querySelector("#status").textContent = ""; }, 1500);
});
