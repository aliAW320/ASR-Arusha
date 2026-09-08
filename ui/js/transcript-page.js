import { api } from "./api.js?v=20260908b";
import { $, renderSidebar, requireUser, toast } from "./layout.js?v=20260908b";

const resultId = new URLSearchParams(location.search).get("result_id");
const STATUS_LABELS = { queued: "در صف", running: "در حال پردازش", succeeded: "آماده", failed: "ناموفق", cancelled: "لغوشده" };
const user = await requireUser();
if (user) renderSidebar(user);

function formatTime(milliseconds) {
  const totalSeconds = Math.floor((milliseconds || 0) / 1000);
  const minutes = Math.floor(totalSeconds / 60);
  const part = (value) => value.toLocaleString("fa-IR", { minimumIntegerDigits: 2, useGrouping: false });
  return `${part(minutes)}:${part(totalSeconds % 60)}`;
}

function renderTranscript(transcript) {
  const content = $("#transcript-content");
  content.replaceChildren();
  const speakerSegments = (transcript.segments || []).filter((segment) => segment.speaker_id);
  if (!speakerSegments.length) {
    content.textContent = transcript.text || "متنی برای نمایش وجود ندارد.";
    return;
  }
  content.classList.add("speaker-transcript");
  for (const segment of speakerSegments) {
    const turn = document.createElement("section");
    turn.className = "speaker-turn";
    const heading = document.createElement("header");
    const speaker = document.createElement("strong");
    speaker.textContent = segment.speaker_id;
    const time = document.createElement("span");
    time.textContent = `${formatTime(segment.start_ms)} تا ${formatTime(segment.end_ms)}`;
    const text = document.createElement("p");
    text.textContent = segment.text || "—";
    heading.append(speaker, time);
    turn.append(heading, text);
    content.append(turn);
  }
}

function renderProcessingError(error) {
  if (!error) return;
  const panel = $("#processing-error");
  panel.classList.remove("hidden");
  $("#processing-error-title").textContent = error.stage === "cleaning"
    ? "پاک‌سازی متن ناموفق بود"
    : "تفکیک گوینده ناموفق بود";
  $("#processing-error-code").textContent = error.code || "diarization_error";
  $("#processing-error-message").textContent = error.message || "علت خطا ثبت نشده است.";
}

if (!resultId) {
  $("#transcript-content").textContent = "شناسه متن مشخص نشده است.";
} else {
  try {
    const transcript = await api(`/results/${encodeURIComponent(resultId)}/transcript`);
    renderTranscript(transcript);
    renderProcessingError(transcript.processing_error);
    $("#transcript-language").textContent = transcript.language === "fa" ? "فارسی" : transcript.language || "";
    $("#transcript-meta").textContent = `نسخه ${transcript.schema_version || ""} · ${transcript.segments?.length || 0} بخش · وضعیت ${STATUS_LABELS[transcript.processing_status] || "نامشخص"}`;
    document.title = "متن تبدیل‌شده | هم‌نشین";
  } catch (exception) {
    $("#transcript-content").textContent = "متن هنوز آماده نیست یا دسترسی به آن ممکن نیست.";
    toast(exception.message, "error");
  }
}
