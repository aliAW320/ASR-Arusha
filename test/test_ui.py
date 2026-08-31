from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_ui_is_an_independent_compose_service_behind_same_origin_proxy():
    compose = (ROOT / "docker-compose.yml").read_text()
    assert "dockerfile: Docker/ui.Dockerfile" in compose
    assert '"127.0.0.1:${UI_PORT:-3000}:80"' in compose
    assert "condition: service_started" in compose

    nginx = (ROOT / "Docker" / "ui.nginx.conf").read_text()
    assert "proxy_pass http://api:8000/;" in nginx
    assert "client_max_body_size 500m;" in nginx
    assert "Content-Security-Policy" in nginx
    assert "base-uri 'none'" in nginx
    assert "form-action 'self'" in nginx
    assert "object-src 'none'" in nginx


def test_ui_exposes_current_backend_flows_without_external_assets():
    pages = {
        name: (ROOT / "ui" / name).read_text()
        for name in ("login.html", "register.html", "meetings.html", "meeting.html", "history.html", "transcript.html")
    }
    javascript = "\n".join(path.read_text() for path in (ROOT / "ui" / "js").glob("*.js"))

    assert all('dir="rtl"' in page for page in pages.values())
    assert 'data-mode="login"' in pages["login.html"]
    assert 'data-mode="register"' in pages["register.html"]
    assert 'id="meetings-grid"' in pages["meetings.html"]
    assert 'accept="audio/*"' in pages["meeting.html"]
    assert 'id="history-list"' in pages["history.html"]
    assert 'id="transcript-content"' in pages["transcript.html"]
    assert all("https://" not in page for page in pages.values())

    for endpoint_fragment in (
        '`/auth/${form.dataset.mode}`',
        "/auth/me",
        "/meetings",
        "/members",
        "/voices",
        "/history",
        "/results/",
    ):
        assert endpoint_fragment in javascript
    assert 'headers.set("Authorization", `Bearer ${getToken()}`)' in javascript
    assert 'headers.set("X-Request-ID", requestId())' in javascript
    assert "setInterval(pollCollections, 3000)" in javascript
    assert "localStorage" not in javascript
    assert "sessionStorage" in javascript
    assert "transcript-page.js" in javascript or (ROOT / "ui/js/transcript-page.js").is_file()


def test_each_ui_page_has_its_own_module():
    expected = {
        "login.html": "auth-page.js",
        "register.html": "auth-page.js",
        "meetings.html": "meetings-page.js",
        "meeting.html": "meeting-page.js",
        "history.html": "history-page.js",
    }
    for page, module in expected.items():
        assert f'/js/{module}' in (ROOT / "ui" / page).read_text()
        assert (ROOT / "ui" / "js" / module).is_file()
    assert (ROOT / "ui" / "js" / "api.js").is_file()
    assert (ROOT / "ui" / "js" / "layout.js").is_file()


def test_ui_escapes_server_controlled_content_before_template_rendering():
    javascript = {
        path.name: path.read_text() for path in (ROOT / "ui" / "js").glob("*.js")
    }

    assert "export const escapeHtml" in javascript["layout.js"]
    assert "escapeHtml(item.event_type)" in javascript["history-page.js"]
    assert "escapeHtml(item.action_description)" in javascript["history-page.js"]
    assert "escapeHtml(voice.original_filename" in javascript["meeting-page.js"]
    assert "escapeHtml(member.user.email)" in javascript["meeting-page.js"]


def test_ci_builds_and_publishes_the_ui_image():
    workflow = (ROOT / ".github" / "workflows" / "ci-cd.yml").read_text()

    assert "dockerfile: Docker/ui.Dockerfile" in workflow
    assert "local_image: asr-arusha-ui:ci" in workflow
    assert "registry_image: asr-arusha-ui" in workflow
    assert "up -d --no-build postgres minio api" in workflow
