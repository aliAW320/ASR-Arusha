import { api } from "./api.js?v=20260912a";
import { $, escapeHtml, formatDate, renderSidebar, requireUser, toast } from "./layout.js?v=20260912a";

const EVENT_LABELS = {
  "auth.registered": "ثبت‌نام کاربر",
  "auth.login_succeeded": "ورود موفق",
  "auth.login_failed": "ورود ناموفق",
  "auth.login_blocked": "ورود مسدودشده",
  "admin.history_viewed": "مشاهده تاریخچه",
  "meeting.created": "ساخت جلسه",
  "meeting.updated": "ویرایش جلسه",
  "meeting.deleted": "حذف جلسه",
  "meeting.member_added": "افزودن عضو جلسه",
  "meeting.member_removed": "حذف عضو جلسه",
  "meeting.member_role_changed": "تغییر نقش عضو",
  "meeting_files.uploaded": "بارگذاری فایل‌های جلسه",
  "meeting_attachment.uploaded": "بارگذاری پیوست",
  "meeting_attachment.deleted": "حذف پیوست",
  "meeting_composition.queued": "قرارگیری ترکیب جلسه در صف",
  "meeting_composition.started": "شروع ترکیب جلسه",
  "meeting_composition.succeeded": "تکمیل ترکیب جلسه",
  "meeting_publication.destination_changed": "تغییر مقصد پایگاه دانش",
  "meeting_publication.queued": "قرارگیری انتشار در صف",
  "meeting_publication.started": "شروع انتشار",
  "meeting_publication.succeeded": "تکمیل انتشار",
  "voice.uploaded": "بارگذاری فایل صوتی",
  "voice.deleted": "حذف فایل صوتی",
  "processing.queued": "قرارگیری پردازش در صف",
  "processing.started": "شروع پردازش",
  "processing.retry_queued": "تلاش دوباره برای پردازش",
  "processing.succeeded": "تکمیل پردازش",
  "processing.failed": "شکست پردازش",
  "processing.cancelled": "لغو پردازش",
  "processing.diarization_choice": "انتخاب تفکیک گویندگان",
};

async function loadHistory(filters = {}) {
  try {
    const params = new URLSearchParams(Object.entries(filters).filter(([, value]) => value));
    const items = await api(`/history?${params}`);
    $("#history-list").innerHTML = items.length ? items.map((item) => `<article class="timeline-item"><span class="timeline-dot"></span><div><h4>${escapeHtml(EVENT_LABELS[item.event_type] || "رویداد سامانه")}</h4><p dir="auto">${escapeHtml(item.action_description)}</p><small>${item.correlation_id ? `شناسه هم‌بستگی: ${escapeHtml(item.correlation_id)}` : "بدون شناسه هم‌بستگی"}</small></div><div class="timeline-meta">${formatDate(item.created_at, true)}<br>${escapeHtml(item.ip_address || "")}</div></article>`).join("") : `<div class="empty-state"><p>رویدادی پیدا نشد.</p></div>`;
  } catch (exception) { toast(exception.message, "error"); }
}
const user = await requireUser({ admin: true });
if (user) { renderSidebar(user, "history"); loadHistory(); }
$("#refresh-history").addEventListener("click", () => loadHistory());
$("#history-filter").addEventListener("submit", (event) => { event.preventDefault(); loadHistory(Object.fromEntries(new FormData(event.currentTarget).entries())); });
