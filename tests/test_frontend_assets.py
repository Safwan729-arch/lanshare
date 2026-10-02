"""Static checks on the frontend, for bugs the Python tests cannot see.

There is no browser in this suite, so these parse the shipped files and assert
the properties that were actually broken in a browser. The `hidden` one shipped
broken: the server said the right thing, the JS set the right attribute, and the
element stayed on screen anyway.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from httpx import AsyncClient

FRONTEND = Path(__file__).resolve().parents[1] / "frontend"
HTML = FRONTEND / "index.html"
CSS = FRONTEND / "css" / "styles.css"


def classes_that_set_display(css: str) -> set[str]:
    """Every class the stylesheet gives an explicit ``display`` to."""
    found: set[str] = set()
    for rule in re.finditer(r"([^{}]+)\{([^}]*)\}", css):
        if not re.search(r"(^|;|\s)display\s*:", rule.group(2)):
            continue
        for selector in rule.group(1).split(","):
            found.update(re.findall(r"\.([A-Za-z0-9_-]+)", selector))
    return found


def elements_toggled_with_hidden(html: str) -> list[tuple[str, list[str]]]:
    """``(id, classes)`` for each element carrying the ``hidden`` attribute."""
    elements = []
    for tag in re.finditer(r"<\w+([^>]*)>", html):
        attrs = tag.group(1)
        if not re.search(r"\bhidden\b", attrs):
            continue
        found_id = re.search(r'id="([^"]+)"', attrs)
        if not found_id:
            continue
        found_class = re.search(r'class="([^"]*)"', attrs)
        elements.append((found_id.group(1), found_class.group(1).split() if found_class else []))
    return elements


@pytest.fixture(scope="module")
def css() -> str:
    # Strip comments first: the override is explained in a comment that quotes
    # the rule text, which would otherwise be matched as if it were a rule.
    return re.sub(r"/\*.*?\*/", "", CSS.read_text(encoding="utf-8"), flags=re.S)


@pytest.fixture(scope="module")
def html() -> str:
    return HTML.read_text(encoding="utf-8")


def test_the_stylesheet_forces_hidden_to_win(css: str) -> None:
    """``[hidden]`` and a class have identical specificity (0,1,0).

    The browser's `[hidden] { display: none }` lives in the user-agent sheet, so
    an author rule at equal specificity beats it. Without an explicit override,
    `.panel { display: grid }` re-shows an element the JS just hid - silently,
    with the attribute correctly set in the DOM.
    """
    rule = re.search(r"\[hidden\][^{]*\{([^}]*)\}", css)
    assert rule is not None, "styles.css must override [hidden] itself"
    body = rule.group(1)
    assert re.search(r"display\s*:\s*none", body), "[hidden] must set display: none"
    assert "!important" in body, "without !important a later class rule still wins"


def test_every_hidden_element_is_actually_hideable(css: str, html: str) -> None:
    """The bug this file exists for, stated as a property.

    Any element the JS hides must really disappear. That holds if its classes
    set no ``display``, or if the global ``[hidden]`` override is present.
    """
    display_classes = classes_that_set_display(css)
    at_risk = {
        element_id: [c for c in element_classes if c in display_classes]
        for element_id, element_classes in elements_toggled_with_hidden(html)
        if any(c in display_classes for c in element_classes)
    }

    # These are exactly the elements that were showing through.
    assert "waiting-screen" in at_risk, (
        "the waiting screen should still depend on the override - if this fails, "
        "the test has stopped watching the thing it was written for"
    )

    override = re.search(r"\[hidden\][^{]*\{[^}]*display\s*:\s*none[^}]*!important", css)
    assert override is not None, (
        f"these elements set a display and so ignore `hidden` without the override: "
        f"{sorted(at_risk)}"
    )


def test_the_waiting_screen_starts_hidden(html: str) -> None:
    """It is an overlay the size of the viewport; it must not be the first
    thing a trusted device sees while registration is still in flight."""
    tag = re.search(r'<div[^>]*id="waiting-screen"[^>]*>', html)
    assert tag is not None
    assert re.search(r"\bhidden\b", tag.group(0))


# -- the client must carry its credential on every call ----------------------

API_JS = FRONTEND / "js" / "api.js"

#: Endpoints the browser may call without a credential. `/api/health` is
#: liveness and `/api/devices/register` issues the credential in the first
#: place - but it still sends what it has, because refreshing an existing
#: registration has to prove the device id is yours.
OPEN_TO_THE_BROWSER: set[str] = set()


@pytest.fixture(scope="module")
def api_js() -> str:
    return API_JS.read_text(encoding="utf-8")


def api_calls(source: str) -> list[tuple[str, str]]:
    """``(path, whole call)`` for each call written against a literal path.

    Only literal paths: `request()` forwards its own arguments to `fetch`, and
    that inner call has no path of its own. A call whose URL comes from a
    helper is not covered here - `downloadFile` is the only one, and it has a
    test to itself.
    """
    calls = []
    for match in re.finditer(r"(?:return |await )(?:request|fetch)\((.*?)\);", source, re.S):
        body = " ".join(match.group(1).split())
        path = re.search(r"""['"`]([^'"`]*?/api/[^'"`]*)['"`]""", body)
        if path is None:
            continue
        calls.append((path.group(1), body))
    return calls


def test_every_server_call_sends_the_credential(api_js: str) -> None:
    """`getServerInfo` shipped without one.

    It was open until the security review locked it down, and the caller was
    never updated - so the page registered fine, then failed on the very next
    request with "Unknown device or invalid token", leaving a broken QR and a
    disconnected socket. The Python tests missed it because they were updated
    to send the header; nothing checked the browser.
    """
    missing = [
        path
        for path, body in api_calls(api_js)
        if "deviceHeaders" not in body and path not in OPEN_TO_THE_BROWSER
    ]
    assert missing == [], f"these calls would be rejected with 401: {missing}"


def test_the_download_helper_sends_the_credential(api_js: str) -> None:
    """Downloads cannot use a plain <a download> any more, precisely because
    the endpoint needs a header. The manual fetch has to carry it."""
    helper = re.search(r"export async function downloadFile.*?\n}", api_js, re.S)
    assert helper is not None
    assert "deviceHeaders()" in helper.group(0)


# -- the phone must not be able to run stale code ----------------------------


def test_the_client_announces_its_version(api_js: str) -> None:
    """A browser cache turned a fixed bug into a bug that looked unfixed.

    Starlette serves the frontend with an ETag but no Cache-Control, so Safari
    invents its own freshness lifetime and a reload can change nothing. Riding
    a version marker along in the registered user agent is what makes "is the
    phone running the new code?" answerable from the server.
    """
    assert re.search(r"const CLIENT_VERSION = '[^']+'", api_js), "api.js must define CLIENT_VERSION"

    register = re.search(r"export function registerDevice.*?\n}", api_js, re.S)
    assert register is not None
    assert "CLIENT_VERSION" in register.group(0), (
        "registerDevice must send the client version, or the server cannot tell "
        "a stale cached page from a current one"
    )


async def test_the_frontend_is_served_with_revalidation(client: AsyncClient) -> None:
    """`no-cache` means "ask first", not "do not store" - the ETag keeps it cheap."""
    for path in ("/", "/js/api.js", "/css/styles.css"):
        response = await client.get(path)
        assert response.status_code == 200, f"{path} -> {response.status_code}"
        assert "no-cache" in response.headers.get("cache-control", ""), (
            f"{path} was served without Cache-Control: no-cache, so a phone may "
            f"keep running the previous build with no way to notice"
        )


def test_every_module_url_carries_the_same_version(api_js: str, html: str) -> None:
    """Versioned URLs are the only way to force a cache miss without a build step.

    Safari cached the frontend under a heuristic lifetime of its own invention
    and then would not even revalidate, so a fix could not reach the phone:
    adding `Cache-Control` did not help, because the browser never asked. A new
    URL it has to fetch.

    They must all agree. Two importers asking for `api.js` under different
    versions would give them two separate module instances of it.
    """
    versions = set()
    sources = {"index.html": html}
    for path in sorted((FRONTEND / "js").glob("*.js")):
        sources[path.name] = path.read_text(encoding="utf-8")

    unversioned = []
    for name, source in sources.items():
        versions.update(re.findall(r"\?v=(\d+)", source))
        # An import that forgot the version pins that module to the old cache.
        unversioned += [f"{name}: {spec}" for spec in re.findall(r"from '(\./[a-z]+\.js)'", source)]

    assert unversioned == [], f"these imports would still be served from cache: {unversioned}"
    assert len(versions) == 1, f"module URLs disagree on the version: {sorted(versions)}"

    declared = re.search(r"const CLIENT_VERSION = '([^']+)'", api_js)
    assert declared is not None
    assert declared.group(1) == versions.pop(), (
        "CLIENT_VERSION must match the asset version, or the build a device "
        "reports is not the build it is running"
    )


def test_the_requests_panel_is_wired_up() -> None:
    """Markup that nothing in the JS ever touches is dead furniture.

    This panel is where a person refuses a file, so a typo in an id would mean
    the prompt never appears and every incoming file waits out its two minutes
    with nobody able to answer.
    """
    html = HTML.read_text(encoding="utf-8")
    app_js = (FRONTEND / "js" / "app.js").read_text(encoding="utf-8")
    ui_js = (FRONTEND / "js" / "ui.js").read_text(encoding="utf-8")

    for element_id in ("requests-panel", "request-list", "accept-all"):
        assert f'id="{element_id}"' in html, f"{element_id} is missing from the markup"
        assert f"'{element_id}'" in ui_js, f"{element_id} is never looked up"

    assert "renderRequests" in ui_js
    assert "renderRequests" in app_js
    assert "consentToTransfer" in app_js
