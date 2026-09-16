import { api } from "./api.js?v=20260912a";
import { $, renderSidebar, requireUser, toast } from "./layout.js?v=20260912a";

const resultId = new URLSearchParams(location.search).get("result_id");
const STATUS_LABELS = { queued: "در صف", running: "در حال پردازش", succeeded: "آماده", failed: "ناموفق", cancelled: "لغوشده" };
const VARIANT_NOTES = {
  cleaned: "نسخه نهایی؛ پس از بازنویسی و پاک‌سازی متن رونویسی‌شده.",
  original: "خروجی خام رونویسی، پیش از آنکه پاک‌سازی چیزی را تغییر دهد.",
  compare: "هر دو نسخه کنار هم؛ تفاوت‌ها نتیجه مرحله پاک‌سازی است.",
};
const user = await requireUser();
if (user) renderSidebar(user);

function formatTime(milliseconds) {
  const totalSeconds = Math.floor((milliseconds || 0) / 1000);
  const minutes = Math.floor(totalSeconds / 60);
  const part = (value) => value.toLocaleString("fa-IR", { minimumIntegerDigits: 2, useGrouping: false });
  return `${part(minutes)}:${part(totalSeconds % 60)}`;
}

function renderTranscript(container, transcript) {
  container.replaceChildren();
  container.classList.remove("speaker-transcript");
  if (!transcript) {
    container.textContent = "این نسخه در دسترس نیست.";
    return;
  }
  const speakerSegments = (transcript.segments || []).filter((segment) => segment.speaker_id);
  if (!speakerSegments.length) {
    container.textContent = transcript.text || "متنی برای نمایش وجود ندارد.";
    return;
  }
  container.classList.add("speaker-transcript");
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
    container.append(turn);
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

// Both stages of the same result: what ASR produced, and what the cleaner made
// of it. Only the cleaned one is guaranteed to exist.
const versions = { cleaned: null, original: null };

function showVariant(name) {
  for (const tab of document.querySelectorAll("[data-variant]")) {
    tab.setAttribute("aria-pressed", String(tab.dataset.variant === name));
  }
  const single = $("#transcript-content");
  const compare = $("#transcript-compare");
  single.classList.toggle("hidden", name === "compare");
  compare.classList.toggle("hidden", name !== "compare");
  // The ruler reads as one continuous track; it means nothing over two columns.
  $(".transcript-ruler").classList.toggle("hidden", name === "compare");
  $("#variant-note").textContent = VARIANT_NOTES[name] || "";
  if (name === "compare") {
    renderTranscript($("#compare-original"), versions.original);
    renderTranscript($("#compare-cleaned"), versions.cleaned);
  } else {
    renderTranscript(single, versions[name]);
  }
}

$("#variant-toolbar").addEventListener("click", (event) => {
  const tab = event.target.closest("[data-variant]");
  if (tab) showVariant(tab.dataset.variant);
});

if (!resultId) {
  $("#transcript-content").textContent = "شناسه متن مشخص نشده است.";
} else {
  const path = `/results/${encodeURIComponent(resultId)}/transcript`;
  const [cleaned, original, result] = await Promise.allSettled([
    api(path),
    api(`${path}?variant=original`),
    api(`/results/${encodeURIComponent(resultId)}`),
  ]);
  if (cleaned.status === "rejected") {
    $("#transcript-content").textContent = "متن هنوز آماده نیست یا دسترسی به آن ممکن نیست.";
    toast(cleaned.reason.message, "error");
  } else {
    const transcript = cleaned.value;
    versions.cleaned = transcript;
    versions.original = original.status === "fulfilled" ? original.value : null;
    const artifacts = result.status === "fulfilled" ? result.value.artifacts || [] : [];
    // Without a cleaned artifact both requests answer with the same ASR text,
    // so there is nothing to compare yet.
    const isCleaned = artifacts.some(
      (artifact) => artifact.artifact_type === "cleaned_text" && artifact.content_type === "application/json",
    );
    const comparable = isCleaned && versions.original !== null;
    $("#variant-toolbar").classList.toggle("hidden", !comparable);
    showVariant("cleaned");
    if (!comparable) {
      $("#variant-note").textContent = isCleaned
        ? "نسخه پیش از پاک‌سازی در دسترس نیست."
        : "پاک‌سازی هنوز انجام نشده است؛ آنچه می‌بینید خروجی خام رونویسی است.";
    }
    renderProcessingError(transcript.processing_error);
    $("#transcript-language").textContent = transcript.language === "fa" ? "فارسی" : transcript.language || "";
    $("#transcript-meta").textContent = `نسخه ${transcript.schema_version || ""} · ${transcript.segments?.length || 0} بخش · وضعیت ${STATUS_LABELS[transcript.processing_status] || "نامشخص"}`;
    document.title = "متن تبدیل‌شده | هم‌نشین";
  }
}
