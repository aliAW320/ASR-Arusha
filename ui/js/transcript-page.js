import { api } from "./api.js";
import { $, renderSidebar, requireUser, toast } from "./layout.js";

const resultId = new URLSearchParams(location.search).get("result_id");
const user = await requireUser();
if (user) renderSidebar(user);

if (!resultId) {
  $("#transcript-content").textContent = "شناسه متن مشخص نشده است.";
} else {
  try {
    const transcript = await api(`/results/${encodeURIComponent(resultId)}/transcript`);
    $("#transcript-content").textContent = transcript.text || "متنی برای نمایش وجود ندارد.";
    $("#transcript-language").textContent = transcript.language || "";
    $("#transcript-meta").textContent = `نسخه ${transcript.schema_version || ""} · ${transcript.segments?.length || 0} بخش`;
    document.title = "متن تبدیل‌شده | هم‌نشین";
  } catch (exception) {
    $("#transcript-content").textContent = "متن هنوز آماده نیست یا دسترسی به آن ممکن نیست.";
    toast(exception.message, "error");
  }
}
