# مستند فنی MCP سرور Outline در `kb.arusha.dev`

## 1. هدف و دامنه این مستند

این سند بر اساس خروجی‌های واقعی دریافت‌شده از MCP Server در endpoint زیر تهیه شده است:

```text
https://kb.arusha.dev/mcp
```

اطلاعات این مستند از دو پاسخ اصلی به‌دست آمده‌اند:

1. پاسخ `initialize`
2. پاسخ `tools/list`

بنابراین بخش‌هایی که به نام Toolها، Schema ورودی، قابلیت‌ها، نسخه پروتکل، Server Info و توضیحات Toolها مربوط هستند **مشاهده‌شده و قطعی** هستند. هرجا از معماری یا رفتار داخلی نتیجه‌گیری شده، آن مورد به‌عنوان **استنباط** مشخص شده است.

---

## 2. مشخصات کلی MCP Server

### اطلاعات قطعی

Server در پاسخ `initialize` اطلاعات زیر را اعلام کرده است:

```json
{
  "protocolVersion": "2025-06-18",
  "serverInfo": {
    "name": "outline",
    "version": "1.9.2"
  },
  "capabilities": {
    "tools": {
      "listChanged": true
    }
  }
}
```

در نتیجه:

- نام MCP Server: `outline`
- نسخه Server: `1.9.2`
- نسخه MCP Protocol مورد مذاکره: `2025-06-18`
- قابلیت `tools` فعال است.
- مقدار `listChanged: true` نشان می‌دهد Server می‌تواند تغییر مجموعه Toolها را به Client اعلام کند.

---

## 3. Transport و نوع ارتباط

Endpoint از HTTP POST استفاده می‌کند:

```http
POST https://kb.arusha.dev/mcp
```

Headerهای لازم در تست موفق:

```http
Authorization: Bearer <API_KEY>
Content-Type: application/json
Accept: application/json, text/event-stream
```

Server در پاسخ موفق `initialize` از:

```http
Content-Type: text/event-stream
```

استفاده کرده و payload را به‌صورت SSE برگردانده است:

```text
event: message
data: {...}
```

بنابراین این MCP Server از الگوی **Streamable HTTP / SSE-compatible response** استفاده می‌کند.

در تست انجام‌شده، Response Header حاوی `Mcp-Session-Id` مشاهده نشد. از این مشاهده به‌تنهایی نمی‌توان با قطعیت نتیجه گرفت که Server همیشه Stateless است، اما در مسیر تست‌شده هیچ Session ID جداگانه‌ای لازم نبوده است.

---

## 4. Authentication

### رفتار مشاهده‌شده

استفاده از Cookie لاگین عادی مرورگر روی `/mcp` با خطای زیر مواجه شد:

```json
{
  "ok": false,
  "error": "authorization_error",
  "status": 403,
  "message": "Invalid authentication type"
}
```

اما استفاده از Bearer credential معتبر باعث عبور از مرحله Authentication شد.

بنابراین برای Client مستقل، شکل قابل اتکای احراز هویت مشاهده‌شده:

```http
Authorization: Bearer <API_KEY>
```

است.

### استنباط

Session Authentication مورد استفاده Frontend عادی Outline با Authentication مورد قبول MCP یکسان نیست. در نتیجه بهتر است MCP Client از credential اختصاصی مناسب API/MCP استفاده کند، نه Cookie Session مرورگر.

---

## 5. MCP Lifecycle مشاهده‌شده

### Initialize

درخواست:

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "initialize",
  "params": {
    "protocolVersion": "2025-06-18",
    "capabilities": {},
    "clientInfo": {
      "name": "curl-client",
      "version": "1.0.0"
    }
  }
}
```

پاسخ Server شامل:

- نسخه Protocol
- Capabilities
- Server Info
- Instructions عمومی برای Agent

است.

### Initialized Notification

Notification استاندارد بعد از Initialize:

```json
{
  "jsonrpc": "2.0",
  "method": "notifications/initialized"
}
```

### Tool Discovery

برای دریافت Toolها:

```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "tools/list",
  "params": {}
}
```

### Tool Invocation

ساختار عمومی اجرای Tool:

```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "method": "tools/call",
  "params": {
    "name": "<tool_name>",
    "arguments": {}
  }
}
```

---

## 6. Instructions عمومی Server برای Agent

Server در `initialize` چند دستور مهم برای Client/Agent اعلام کرده است.

### 6.1. عنوان Document

محتوای Markdown سند نباید با H1 شروع شود:

```markdown
# Title
```

زیرا Title در فیلد جداگانه ذخیره می‌شود.

روش صحیح:

```json
{
  "title": "Backend Architecture",
  "text": "متن سند...\n\n## جزئیات"
}
```

نه:

```json
{
  "title": "Backend Architecture",
  "text": "# Backend Architecture\n..."
}
```

---

### 6.2. Mention کاربران

Document و Collection Markdown از Mention با Syntax زیر پشتیبانی می‌کنند:

```text
@[Display Name](mention://user/userId)
```

مثال:

```text
@[John Doe](mention://user/c9a1b2e3-...)
```

برای یافتن `userId` باید از Tool زیر استفاده شود:

```text
list_users
```

---

### 6.3. Attachmentها

برای خواندن Image یا Attachment باید:

```text
fetch
```

با:

```json
{
  "resource": "attachment",
  "id": "<attachment-id-or-url>"
}
```

استفاده شود.

Server یک Signed URL کوتاه‌عمر برای Download فایل برمی‌گرداند.

---

### 6.4. Templateها

برای ایجاد Document از Template، Server توصیه می‌کند ابتدا:

```text
list_templates
```

فراخوانی شود.

هر نتیجه Template متن Markdown Template را نیز شامل می‌شود.

دو سناریو وجود دارد:

#### استفاده بدون تغییر

```text
list_templates
    ↓
templateId
    ↓
create_document(templateId=...)
```

#### استفاده با تغییر

```text
list_templates
    ↓
template body
    ↓
تغییر محتوا
    ↓
create_document(text=modifiedBody)
```

در این جریان نیازی به `fetch` جداگانه برای Template نیست.

---

# 7. نمای کلی Toolها

در خروجی مشاهده‌شده، 19 Tool وجود دارد.

| حوزه | Tool |
|---|---|
| Attachment | `create_attachment` |
| Collection | `list_collections` |
| Collection | `create_collection` |
| Collection | `update_collection` |
| Collection | `delete_collection` |
| Comment | `list_comments` |
| Comment | `create_comment` |
| Comment | `update_comment` |
| Comment | `delete_comment` |
| Document | `list_documents` |
| Document | `list_collection_documents` |
| Document | `create_document` |
| Document | `move_document` |
| Document | `update_document` |
| Document | `delete_document` |
| Document | `restore_document` |
| Generic Resource | `fetch` |
| Template | `list_templates` |
| User | `list_users` |

---

# 8. Attachment Tools

## 8.1. `create_attachment`

### هدف

درخواست URL از نوع Pre-signed برای Upload فایل.

### ورودی

```json
{
  "contentType": "image/png",
  "name": "screenshot.png",
  "size": 123456
}
```

### Schema

فیلدهای اجباری:

- `contentType`
- `name`
- `size`

`size` بر حسب Byte است.

### رفتار

خود فایل از داخل JSON-RPC عبور نمی‌کند. Server اطلاعات لازم برای Upload مستقیم فایل را برمی‌گرداند.

معماری محتمل:

```text
MCP Client
   │
   │ create_attachment
   ▼
Outline MCP
   │
   │ pre-signed upload data
   ▼
MCP Client
   │
   │ multipart/form-data
   ▼
Object Storage
```

این طراحی باعث می‌شود Binary Data مستقیماً از MCP JSON-RPC عبور نکند.

### Metadata

```text
readOnlyHint: false
idempotentHint: false
taskSupport: forbidden
```

---

# 9. Collection Tools

## 9.1. `list_collections`

Collectionهایی را برمی‌گرداند که کاربر Authenticate‌شده به آن‌ها دسترسی دارد.

پارامترها:

```json
{
  "query": "optional search",
  "offset": 0,
  "limit": 25
}
```

محدودیت:

```text
1 <= limit <= 100
```

Metadata:

```text
readOnlyHint: true
idempotentHint: true
```

---

## 9.2. `create_collection`

یک Collection جدید ایجاد می‌کند.

ورودی:

```json
{
  "name": "Engineering",
  "description": "Engineering documentation",
  "icon": "🛠️",
  "color": "#FF0000"
}
```

فقط `name` اجباری است.

Metadata:

```text
readOnlyHint: false
idempotentHint: false
```

---

## 9.3. `update_collection`

یک Collection موجود را بر اساس ID تغییر می‌دهد.

```json
{
  "id": "collection-id",
  "name": "New Name",
  "description": "Updated description",
  "icon": "📚",
  "color": "#00FF00"
}
```

تنها `id` اجباری است.

`icon` و `color` می‌توانند `null` باشند تا مقدار موجود حذف شود.

---

## 9.4. `delete_collection`

Collection را حذف یا Archive می‌کند.

```json
{
  "id": "collection-id",
  "archive": true
}
```

اگر `archive=true` باشد Collection به‌جای Delete شدن Archive می‌شود.

### نکته مهم

طبق Description Tool، حذف Collection می‌تواند Documentهای Non-Archived داخل آن را نیز حذف کند.

بنابراین این Tool یک عملیات **Destructive** محسوب می‌شود.

---

# 10. Comment Tools

## 10.1. `list_comments`

Commentهایی را که User به آن‌ها دسترسی دارد لیست می‌کند.

حداقل یکی از موارد زیر لازم است:

```text
documentId
collectionId
```

پارامترها:

```json
{
  "documentId": "...",
  "collectionId": "...",
  "parentCommentId": "...",
  "statusFilter": ["resolved", "unresolved"],
  "offset": 0,
  "limit": 25
}
```

Metadata:

```text
readOnlyHint: true
idempotentHint: true
```

---

## 10.2. `create_comment`

روی Document Comment ایجاد می‌کند.

ورودی پایه:

```json
{
  "documentId": "document-id",
  "text": "Comment body"
}
```

### Reply

برای Reply به Thread:

```json
{
  "documentId": "document-id",
  "text": "Reply",
  "parentCommentId": "comment-id"
}
```

### Inline Comment

Tool از Comment متصل به بخش مشخصی از Document نیز پشتیبانی می‌کند:

```json
{
  "documentId": "document-id",
  "text": "This section needs clarification",
  "anchorText": "target text"
}
```

اگر `anchorText` بیش از یک بار در سند وجود داشته باشد:

```json
{
  "anchorText": "target text",
  "anchorPrefix": "text before target",
  "anchorSuffix": "text after target"
}
```

می‌تواند برای انتخاب occurrence دقیق استفاده شود.

---

## 10.3. `update_comment`

Comment موجود را تغییر می‌دهد.

```json
{
  "id": "comment-id",
  "text": "Updated comment",
  "status": "resolved"
}
```

مقادیر Status:

```text
resolved
unresolved
```

فقط Top-level Commentها می‌توانند Resolve/Unresolve شوند.

---

## 10.4. `delete_comment`

Comment را بر اساس ID حذف می‌کند.

```json
{
  "id": "comment-id"
}
```

طبق Description، User باید:

- Author همان Comment باشد
- یا Team Admin باشد.

این یکی از معدود نقاطی است که شرط Permission صریحاً در Description Tool ذکر شده است.

---

# 11. Document Tools

Documentها کامل‌ترین بخش MCP مشاهده‌شده هستند.

---

## 11.1. `list_documents`

Documentهایی را که User به آن‌ها دسترسی دارد Search می‌کند.

### با Query

```json
{
  "query": "architecture",
  "limit": 25
}
```

Full-text Search روی Title یا Content انجام می‌شود.

### بدون Query

در نبود Query، Documentهای Recent برگردانده می‌شوند.

### Filter بر اساس Collection

```json
{
  "query": "database",
  "collectionId": "collection-id"
}
```

### Pagination

```json
{
  "offset": 0,
  "limit": 100
}
```

---

## 11.2. `list_collection_documents`

Tree کامل Published Documentهای یک Collection را برمی‌گرداند.

```json
{
  "collectionId": "collection-id"
}
```

این Tool برای:

- Enumeration کامل اسناد
- تشخیص Parent/Child relationship
- فهم Hierarchy
- پیمایش ساختار Collection

مناسب است.

### محدودیت مهم

طبق Description:

- Draftها شامل نمی‌شوند.
- Archived Documentها شامل نمی‌شوند.

---

## 11.3. `create_document`

یک Document جدید از Markdown یا HTML ایجاد می‌کند.

پارامترهای ممکن:

```json
{
  "title": "Document title",
  "text": "Document body",
  "format": "markdown",
  "collectionId": "collection-id",
  "parentDocumentId": "parent-document-id",
  "templateId": "template-id",
  "icon": "📄",
  "color": "#FF0000",
  "publish": true,
  "fullWidth": false
}
```

### Format

مقادیر:

```text
markdown
html
```

Default:

```text
markdown
```

### محل Document

Document باید در Collection قرار گیرد یا زیر Document دیگری ساخته شود:

```text
collectionId
OR
parentDocumentId
```

### نکته Schema

Description این constraint را اعلام می‌کند، اما JSON Schema مشاهده‌شده آن را با `oneOf`/`anyOf` enforce نمی‌کند.

در نتیجه این اعتبارسنجی احتمالاً در Application Layer انجام می‌شود.

### Draft

```json
{
  "publish": false
}
```

Document را به‌صورت Draft ایجاد می‌کند.

Default طبق Description:

```text
publish = true
```

### Full Width

```json
{
  "fullWidth": true
}
```

برای HTML نباید بدون درخواست صریح User به `true` تنظیم شود.

---

## 11.4. `move_document`

Document را Move یا Reorder می‌کند.

```json
{
  "id": "document-id",
  "collectionId": "destination-collection",
  "index": 0
}
```

یا:

```json
{
  "id": "document-id",
  "parentDocumentId": "parent-document-id",
  "index": 2
}
```

`index` یک Position صفرمبنا میان Siblingها است.

اگر حذف شود، Document در انتهای لیست قرار می‌گیرد.

---

## 11.5. `update_document`

یکی از مهم‌ترین Toolهای MCP است.

پارامترها:

```json
{
  "id": "document-id",
  "title": "New title",
  "text": "...",
  "editMode": "patch",
  "findText": "...",
  "collectionId": "...",
  "icon": "📘",
  "color": "#123456",
  "publish": true,
  "fullWidth": false
}
```

### Edit Modes

چهار Mode:

```text
replace
append
prepend
patch
```

### `replace`

کل محتوای Document را جایگزین می‌کند.

```json
{
  "id": "...",
  "editMode": "replace",
  "text": "Entire new document"
}
```

### `append`

متن را به انتهای Document اضافه می‌کند.

### `prepend`

متن را به ابتدای Document اضافه می‌کند.

### `patch`

بخش مشخصی از Markdown را به‌صورت موضعی جایگزین می‌کند.

```json
{
  "id": "document-id",
  "editMode": "patch",
  "findText": "Old paragraph",
  "text": "New paragraph"
}
```

در حالت `patch`، `findText` لازم است.

### توصیه خود Server

Server صراحتاً توصیه می‌کند برای Edit سند موجود از `patch` استفاده شود، چون `replace` می‌تواند Formattingهایی را که در Markdown قابل نمایش نیستند از بین ببرد، از جمله مواردی مانند:

- Highlights
- Comments
- Table widths
- Rich formatting

بنابراین برای Agentهای خودکار:

```text
patch > replace
```

مگر در شرایطی که بازنویسی کامل عمداً مورد نظر باشد.

---

## 11.6. `delete_document`

Document را حذف یا Archive می‌کند.

```json
{
  "id": "document-id",
  "archive": false
}
```

Delete طبق Description سند را به Trash منتقل می‌کند و قابل Restore است.

اگر:

```json
{
  "archive": true
}
```

ارسال شود، Document Archive می‌شود.

---

## 11.7. `restore_document`

Document حذف‌شده یا Archive‌شده را Restore می‌کند.

```json
{
  "id": "document-id"
}
```

یا:

```json
{
  "id": "document-id",
  "collectionId": "new-collection-id"
}
```

اگر `collectionId` داده نشود، طبق Description Document به Collection اصلی خود برمی‌گردد.

---

# 12. Generic Fetch

## `fetch`

یک Resource مشخص را بر اساس Type و ID دریافت می‌کند.

Resourceهای پشتیبانی‌شده:

```text
document
collection
user
attachment
template
```

Schema:

```json
{
  "resource": "document",
  "id": "resource-id"
}
```

### Current User

برای دریافت User فعلی:

```json
{
  "resource": "user",
  "id": "current_user"
}
```

### Collection

Fetch کردن Collection، Tree کامل Documentهای آن را نیز برمی‌گرداند.

### Attachment

برای Attachment یک Signed URL کوتاه‌عمر برگردانده می‌شود.

### Template

برای Template، Body به‌صورت Markdown برگردانده می‌شود.

Metadata:

```text
readOnlyHint: true
idempotentHint: true
```

---

# 13. Template Tools

## `list_templates`

Templateهایی را که User به آن‌ها دسترسی دارد لیست می‌کند.

شامل:

- Workspace-wide Templates
- Templates متعلق به Collectionهای قابل دسترسی

پارامترها:

```json
{
  "collectionId": "optional",
  "offset": 0,
  "limit": 25
}
```

هر نتیجه Template طبق Description شامل Markdown Body است.

بنابراین Flow پیشنهادی:

```text
list_templates
      │
      ├── unchanged → create_document(templateId)
      │
      └── modified  → create_document(text=modifiedBody)
```

---

# 14. User Tools

## `list_users`

Userهای Workspace را لیست می‌کند.

### Search

```json
{
  "query": "ali"
}
```

Search روی Name یا Email انجام می‌شود.

### Role Filter

Roleهای پشتیبانی‌شده:

```text
admin
member
viewer
guest
```

مثال:

```json
{
  "role": "admin"
}
```

### Status Filter

```text
active
suspended
invited
all
```

مثال:

```json
{
  "filter": "active"
}
```

طبق Description:

```text
suspended
```

تنها برای Admin قابل Filter است.

### Pagination

```json
{
  "offset": 0,
  "limit": 100
}
```

### نکته معماری

هیچ Tool مشاهده‌شده‌ای برای:

```text
create_user
update_user
delete_user
change_role
```

وجود ندارد.

بنابراین User Management در MCP فعلی، در سطح Toolهای مشاهده‌شده، Read-only است.

---

# 15. Pagination Pattern

چند Tool از الگوی مشترک Pagination استفاده می‌کنند:

```json
{
  "offset": 0,
  "limit": 25
}
```

حداکثر Limit:

```text
100
```

برای دریافت Dataset بزرگ:

```text
offset=0   limit=100
offset=100 limit=100
offset=200 limit=100
...
```

Client باید تا زمانی که نتیجه کامل دریافت نشده Pagination را ادامه دهد.

Toolهایی که Pagination دارند شامل مواردی مانند:

- `list_collections`
- `list_comments`
- `list_documents`
- `list_templates`
- `list_users`

هستند.

---

# 16. JSON Schema

Input Schema Toolها بر اساس:

```text
JSON Schema Draft-07
```

تعریف شده است:

```text
http://json-schema.org/draft-07/schema#
```

این موضوع امکان Validation استاندارد آرگومان‌ها را در MCP Client فراهم می‌کند.

Schemaها مواردی مانند:

- Type
- Required Fields
- Enum
- Minimum
- Maximum
- Nullable Values

را تعریف کرده‌اند.

مثال:

```json
{
  "role": {
    "type": "string",
    "enum": [
      "admin",
      "member",
      "viewer",
      "guest"
    ]
  }
}
```

---

# 17. Tool Annotations

هر Tool دارای Annotationهایی است.

دو Annotation اصلی مشاهده‌شده:

```text
readOnlyHint
idempotentHint
```

### `readOnlyHint`

اگر:

```text
true
```

باشد Tool قرار نیست State را تغییر دهد.

مثال:

```text
list_documents
list_users
fetch
```

### `idempotentHint`

اگر:

```text
true
```

باشد اجرای دوباره Operation با همان ورودی ماهیتاً باید همان اثر منطقی را داشته باشد.

Toolهای Read معمولاً:

```text
readOnlyHint = true
idempotentHint = true
```

دارند.

Toolهای Create/Update/Delete مشاهده‌شده عمدتاً:

```text
readOnlyHint = false
idempotentHint = false
```

دارند.

---

# 18. Task Support

در تمام Toolهای مشاهده‌شده:

```json
{
  "execution": {
    "taskSupport": "forbidden"
  }
}
```

وجود دارد.

این یعنی Toolهای فعلی برای MCP Task-based execution expose نشده‌اند.

این مقدار به معنی غیرقابل اجرا بودن Tool نیست؛ بلکه Invocation به شکل عادی MCP انجام می‌شود و Task execution برای آن فعال نیست.

---

# 19. Permission Model

چند Description صریحاً می‌گویند داده‌ها در محدوده دسترسی User Authenticate‌شده هستند.

برای مثال:

```text
Lists all collections the authenticated user has access to.
```

و:

```text
Searches documents the user has access to.
```

بنابراین Toolها از نظر Interface، Permission-aware طراحی شده‌اند.

### استنباط

احتمال زیاد Authorization در Backend بر مبنای User/Token فعلی انجام می‌شود.

اما خروجی `tools/list` به‌تنهایی برای اثبات جزئیات Enforcement داخلی Permission کافی نیست. برای تأیید کامل باید Implementation Backend یا رفتار Toolها با کاربران دارای Roleهای مختلف بررسی شود.

---

# 20. عملیات Read-only

Toolهای مشاهده‌شده با:

```text
readOnlyHint = true
```

عبارت‌اند از:

```text
list_collections
list_comments
list_documents
list_collection_documents
fetch
list_templates
list_users
```

این Toolها مناسب Exploration، Search و Retrieval هستند.

---

# 21. عملیات Write و Destructive

Toolهای Write شامل:

```text
create_attachment
create_collection
update_collection
delete_collection

create_comment
update_comment
delete_comment

create_document
move_document
update_document
delete_document
restore_document
```

برخی از آن‌ها Destructive محسوب می‌شوند:

```text
delete_collection
delete_comment
delete_document
```

و برخی می‌توانند ساختار Knowledge Base را به شکل قابل توجه تغییر دهند:

```text
move_document
update_document
update_collection
```

برای Agent خودکار مناسب است پیش از عملیات Destructive Confirmation Policy وجود داشته باشد.

---

# 22. مدل قابلیت‌های MCP

بر اساس Toolهای مشاهده‌شده، MCP را می‌توان به سه گروه اصلی تقسیم کرد:

```text
                         Outline MCP
                              │
             ┌────────────────┼────────────────┐
             │                │                │
           Retrieval        Mutation        Organization
             │                │                │
          fetch            create_*         collections
          list_*           update_*         hierarchy
          search           delete_*         move_document
                           restore_*
```

---

# 23. جریان‌های Agentic قابل پیاده‌سازی

## 23.1. Search و خلاصه‌سازی

```text
User Request
    ↓
list_documents(query)
    ↓
fetch(document)
    ↓
LLM Analysis
    ↓
Answer
```

---

## 23.2. ایجاد Document از Knowledge Base

```text
list_collections
      ↓
انتخاب Collection
      ↓
list_documents / fetch
      ↓
LLM generates content
      ↓
create_document
```

---

## 23.3. ایجاد Document از Template

```text
list_templates
      ↓
انتخاب Template
      ↓
create_document(templateId)
```

یا:

```text
list_templates
      ↓
modify template body
      ↓
create_document(text)
```

---

## 23.4. اصلاح بخشی از Document

```text
fetch(document)
      ↓
تشخیص substring دقیق
      ↓
update_document(
    editMode="patch",
    findText=...,
    text=...
)
```

این Flow نسبت به Replace کامل کم‌ریسک‌تر است.

---

## 23.5. Review مبتنی بر Comment

```text
fetch(document)
      ↓
LLM review
      ↓
create_comment(
    anchorText=...
)
```

Agent می‌تواند Review را به‌صورت Inline Comment ثبت کند.

---

## 23.6. Upload تصویر و استفاده در Document

```text
create_attachment
      ↓
pre-signed upload URL
      ↓
multipart upload
      ↓
attachment URL
      ↓
create_document / update_document
```

---

## 23.7. Mention کردن User

```text
list_users(query)
      ↓
userId
      ↓
@[Name](mention://user/userId)
      ↓
create_document / update_document / create_collection
```

---

# 24. نقاط قوت طراحی مشاهده‌شده

## 24.1. پوشش گسترده Document Lifecycle

چرخه تقریباً کاملی از:

```text
Search
Create
Read
Move
Update
Delete
Restore
```

وجود دارد.

---

## 24.2. Hierarchy-aware

MCP فقط Document flat ارائه نمی‌کند؛ Parent/Child hierarchy و Tree کامل Collection را نیز expose می‌کند.

---

## 24.3. Patch-based Editing

`update_document` قابلیت Patch موضعی دارد که برای Agent بسیار ارزشمند است و احتمال تخریب Formatting را کاهش می‌دهد.

---

## 24.4. Template-aware

Templateها مستقیماً وارد Workflow Agent شده‌اند و نیاز به Fetch اضافه ندارند.

---

## 24.5. Attachment Upload مناسب

استفاده از Pre-signed URL معماری مناسب‌تری از عبور Binary File از MCP JSON-RPC است.

---

## 24.6. Inline Comments

وجود:

```text
anchorText
anchorPrefix
anchorSuffix
```

امکان Review دقیق مبتنی بر بخش خاصی از Document را فراهم می‌کند.

---

## 24.7. Tool Metadata

وجود:

```text
readOnlyHint
idempotentHint
taskSupport
```

به Client اجازه می‌دهد تصمیمات بهتری درباره Execution Policy بگیرد.

---

# 25. محدودیت‌ها و قابلیت‌هایی که در Tool Set فعلی مشاهده نشدند

موارد زیر در لیست Toolهای فعلی مشاهده نشدند:

### Permission Management

```text
share_document
add_collection_member
remove_collection_member
update_permission
```

### User Administration

```text
create_user
delete_user
change_user_role
suspend_user
```

### Revision / History

```text
list_revisions
restore_revision
compare_revisions
```

### Workspace Administration

```text
workspace_settings
update_workspace
manage_integrations
```

### Import / Export

```text
import_document
export_document
export_collection
```

### Audit / Events

```text
list_audit_logs
list_events
```

نبود این Toolها فقط درباره خروجی `tools/list` مشاهده‌شده صدق می‌کند. از آنجا که Server:

```text
listChanged: true
```

اعلام کرده، Tool Set می‌تواند در نسخه‌ها یا شرایط دیگر تغییر کند.

---

# 26. نکات امنیتی برای Client

به دلیل وجود Write Toolهای قوی، MCP Client بهتر است Toolها را بر اساس Risk Class دسته‌بندی کند.

### Low Risk

```text
list_*
fetch
```

### Medium Risk

```text
create_comment
create_document
create_collection
update_comment
update_document
update_collection
move_document
restore_document
```

### High Risk / Destructive

```text
delete_comment
delete_document
delete_collection
```

برای گروه High Risk بهتر است Client قبل از اجرا Confirmation صریح User بگیرد.

---

# 27. پیشنهاد Policy برای Agent

یک Agent متصل به این MCP بهتر است قواعد زیر را رعایت کند:

1. ابتدا Read Toolها را برای Context Gathering استفاده کند.
2. پیش از Update Document، ابتدا `fetch(document)` انجام دهد.
3. برای تغییر محدود Content، `patch` را به `replace` ترجیح دهد.
4. پیش از Delete عملیات را به User اعلام و Confirmation بگیرد.
5. برای Mention ابتدا `list_users` اجرا کند.
6. برای Template ابتدا `list_templates` را اجرا کند.
7. برای Upload فایل از `create_attachment` و Upload مستقیم استفاده کند.
8. Pagination را برای Result Setهای بزرگ مدیریت کند.
9. Permission Errorها را به‌عنوان بخشی از Policy Backend در نظر بگیرد و دور نزند.
10. به `readOnlyHint` و `idempotentHint` در Execution Planning توجه کند.

---

# 28. نمونه درخواست `tools/list`

```bash
curl -i 'https://kb.arusha.dev/mcp' \
  -X POST \
  -H 'Authorization: Bearer YOUR_API_KEY' \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H 'MCP-Protocol-Version: 2025-06-18' \
  --data-raw '{
    "jsonrpc": "2.0",
    "id": 2,
    "method": "tools/list",
    "params": {}
  }'
```

---

# 29. نمونه عمومی `tools/call`

```bash
curl -i 'https://kb.arusha.dev/mcp' \
  -X POST \
  -H 'Authorization: Bearer YOUR_API_KEY' \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H 'MCP-Protocol-Version: 2025-06-18' \
  --data-raw '{
    "jsonrpc": "2.0",
    "id": 3,
    "method": "tools/call",
    "params": {
      "name": "list_collections",
      "arguments": {
        "limit": 25
      }
    }
  }'
```

---

# 30. جمع‌بندی معماری

MCP مشاهده‌شده را می‌توان یک **Agent-facing API برای Knowledge Base مبتنی بر Outline** در نظر گرفت.

این Interface صرفاً Retrieval نیست، بلکه Agent می‌تواند:

```text
Read
Search
Create
Update
Patch
Move
Comment
Upload
Delete
Restore
```

انجام دهد.

از منظر Functional Coverage:

```text
Users          → Read
Templates      → Read
Attachments    → Create + Fetch
Collections    → CRUD-like
Comments       → CRUD-like
Documents      → Full lifecycle
```

بخش Document قوی‌ترین و کامل‌ترین قسمت طراحی است و ویژگی‌های مهمی مانند:

```text
Full-text Search
Hierarchy
Templates
Draft/Publish
Patch Editing
Restore
Inline Comments
Attachments
Mentions
```

را در Workflow یک Agent قابل استفاده می‌کند.

در نتیجه این MCP برای ساخت Agentهایی از جنس زیر مناسب است:

- Knowledge Base Assistant
- Documentation Agent
- Documentation Maintenance Agent
- Review Agent
- Meeting-to-Documentation Agent
- Internal Search Agent
- Automated Knowledge Curator

---

# 31. وضعیت اطمینان اطلاعات

## قطعی و مشاهده‌شده

- Endpoint
- نسخه Protocol
- Server Name
- Server Version
- Tool Names
- Tool Descriptions
- Input Schemas
- Tool Annotations
- `taskSupport`
- Instructions مربوط به Markdown، Mention، Attachment و Template
- نوع Response در تست (`text/event-stream`)
- Bearer Authentication در تست موفق

## استنباط‌شده

- معماری داخلی Object Storage برای Attachment
- جزئیات دقیق Authorization Enforcement
- Stateless بودن کامل Server
- نحوه پیاده‌سازی داخلی Toolها
- نحوه اتصال MCP Layer به APIهای داخلی Outline

برای اثبات موارد استنباط‌شده باید Source Code Backend، Configuration یا رفتار Runtime بیشتری بررسی شود.
