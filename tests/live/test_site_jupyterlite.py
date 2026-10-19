"""Live checks of JupyterLite and the Python client on a deployed site.

Run against a scratch site in the release check (``doc/RELEASING.md``)::

    make test-live-site URL=https://127.0.0.1:18743

Skipped unless ``ANTHIVE_LIVE_URL`` is set. Credentials come from
``ANTHIVE_LIVE_USER`` / ``ANTHIVE_LIVE_PASSWORD`` or, when those are unset,
from a ``~/.netrc`` entry for the site's host. They are never printed.

Environment:

* ``ANTHIVE_LIVE_URL``       the site's public URL (scheme + host [+ port])
* ``ANTHIVE_LIVE_USER``, ``ANTHIVE_LIVE_PASSWORD``  a site account
* ``ANTHIVE_LIVE_LOGIN``     ``authelia`` | ``basic`` | ``none``; default:
  detected from how the site answers an anonymous request
* ``ANTHIVE_LIVE_JUPYTER_PATH``  default ``/jupyter/``
* ``ANTHIVE_LIVE_VERIFY_TLS=1``  check the certificate (off by default:
  scratch sites use ``tls internal``)

Two parts. The HTTP part needs only ``requests``: it signs in like a
browser, downloads the JupyterLite files and compares them with this
checkout (the vendored vark files by sha256, the notebooks byte for byte),
checks the site's API version and ``compatible_pyvark``, and the login gate
on ``/jupyter/`` and ``/api/``. The browser part (headless Chromium through
the real login, Pyodide from the CDN) starts a kernel, imports vark,
connects with the same-origin default the shipped notebooks use, reads
``/health`` and ``/version``, lists datasets (an empty catalogue is fine),
queries expression and requests a plot when a dataset exists, runs the
shipped notebooks, compares the signed-in kernel's datasets with the
anonymous view, and checks that vark retries a 503 ``warming_up`` answer
inside Pyodide (simulated by intercepting the request: a real one exists
only for seconds after a restart).
"""
from __future__ import annotations

import hashlib
import json
import netrc
import os
import re
import urllib.parse
from pathlib import Path
from typing import Optional, Tuple

import pytest

SITE_URL = os.environ.get("ANTHIVE_LIVE_URL", "").rstrip("/")
if not SITE_URL:
    pytest.skip("live site tests: set ANTHIVE_LIVE_URL (make test-live-site URL=...)",
                allow_module_level=True)

requests = pytest.importorskip("requests")
import urllib3  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
CONTENTS = REPO / "frontend" / "jupyter" / "jupyter"
RECORD = json.loads((REPO / "frontend" / "jupyter" / "vendored_pyvark.json").read_text())
SERVE_SOURCE = (REPO / "bin" / "api" / "ant-serve").read_text()
CHECKOUT_API_VERSION = re.search(r'^API_VERSION = "([^"]+)"', SERVE_SOURCE, re.M).group(1)
JUPYTER_PATH = os.environ.get("ANTHIVE_LIVE_JUPYTER_PATH", "/jupyter/")
if not JUPYTER_PATH.endswith("/"):
    JUPYTER_PATH += "/"
VERIFY_TLS = os.environ.get("ANTHIVE_LIVE_VERIFY_TLS") == "1"
if not VERIFY_TLS:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

pytestmark = pytest.mark.live


def _credentials() -> Tuple[Optional[str], Optional[str]]:
    user = os.environ.get("ANTHIVE_LIVE_USER")
    password = os.environ.get("ANTHIVE_LIVE_PASSWORD")
    if user and password:
        return user, password
    host = urllib.parse.urlsplit(SITE_URL).hostname or ""
    try:
        entry = netrc.netrc().authenticators(host)
    except (FileNotFoundError, netrc.NetrcParseError):
        entry = None
    if entry:
        return entry[0], entry[2]
    return None, None


USER, PASSWORD = _credentials()


def _version_tuple(text: str) -> tuple:
    return tuple(int(part) for part in re.findall(r"\d+", text)[:3])


def _detect_login_mode() -> str:
    forced = os.environ.get("ANTHIVE_LIVE_LOGIN")
    if forced:
        return forced
    response = requests.get(f"{SITE_URL}{JUPYTER_PATH}jupyter-lite.json",
                            allow_redirects=False, verify=VERIFY_TLS, timeout=30)
    if response.status_code == 200:
        return "none"
    if response.status_code == 401 and "basic" in response.headers.get(
            "WWW-Authenticate", "").lower():
        return "basic"
    if response.status_code in (301, 302, 303, 307) and "authelia" in response.headers.get(
            "Location", ""):
        return "authelia"
    raise AssertionError(f"cannot tell the login mode: anonymous GET {JUPYTER_PATH} answered "
                         f"{response.status_code} {response.headers.get('Location', '')}")


LOGIN_MODE = _detect_login_mode()


def _need_credentials() -> None:
    if LOGIN_MODE != "none" and not (USER and PASSWORD):
        pytest.skip(f"site uses {LOGIN_MODE} login: set ANTHIVE_LIVE_USER / "
                    f"ANTHIVE_LIVE_PASSWORD or a ~/.netrc entry for the host")


@pytest.fixture(scope="module")
def signed_in():
    """A requests session signed in the way a browser user is."""
    _need_credentials()
    session = requests.Session()
    session.verify = VERIFY_TLS
    if LOGIN_MODE == "basic":
        session.auth = (USER, PASSWORD)
    elif LOGIN_MODE == "authelia":
        response = session.post(
            f"{SITE_URL}/authelia/api/firstfactor",
            json={"username": USER, "password": PASSWORD, "keepMeLoggedIn": False,
                  "targetURL": f"{SITE_URL}{JUPYTER_PATH}"},
            timeout=30)
        assert response.status_code == 200, (
            f"Authelia first factor answered {response.status_code} (wrong credentials?)")
    probe = session.get(f"{SITE_URL}{JUPYTER_PATH}jupyter-lite.json",
                        allow_redirects=False, timeout=30)
    assert probe.status_code == 200, f"{JUPYTER_PATH} still gated after login: {probe.status_code}"
    return session


@pytest.fixture(scope="module")
def anonymous():
    session = requests.Session()
    session.verify = VERIFY_TLS
    return session


# ── HTTP part ────────────────────────────────────────────────────────────

def test_jupyter_requires_login_unless_site_is_open(anonymous):
    response = anonymous.get(f"{SITE_URL}{JUPYTER_PATH}", allow_redirects=False, timeout=30)
    if LOGIN_MODE == "none":
        assert response.status_code == 200
    else:
        assert response.status_code in (302, 303, 401), (
            f"anonymous {JUPYTER_PATH} answered {response.status_code} on a {LOGIN_MODE} site")


def test_served_jupyter_config_is_valid(signed_in):
    response = signed_in.get(f"{SITE_URL}{JUPYTER_PATH}jupyter-lite.json", timeout=30)
    assert response.status_code == 200
    config = response.json()["jupyter-config-data"]
    extensions = {entry["name"] for entry in config.get("federated_extensions", [])}
    assert "@jupyterlite/pyodide-kernel-extension" in extensions, sorted(extensions)
    assert "jupyterlab-open-url-parameter" in extensions, sorted(extensions)
    lab = signed_in.get(f"{SITE_URL}{JUPYTER_PATH}lab/index.html", timeout=30)
    assert lab.status_code == 200
    assert lab.headers.get("Cross-Origin-Embedder-Policy") == "require-corp", (
        "COEP header missing on /jupyter/: the Pyodide kernel needs cross-origin isolation")


def test_served_vark_equals_checkout(signed_in):
    mismatched = []
    for name, expected_sha in RECORD["files"].items():
        response = signed_in.get(f"{SITE_URL}{JUPYTER_PATH}files/vark/{name}", timeout=30)
        assert response.status_code == 200, f"files/vark/{name}: HTTP {response.status_code}"
        if hashlib.sha256(response.content).hexdigest() != expected_sha:
            mismatched.append(name)
    served_init = signed_in.get(f"{SITE_URL}{JUPYTER_PATH}files/vark/__init__.py", timeout=30).text
    match = re.search(r'^__version__\s*=\s*["\']([^"\']+)', served_init, re.M)
    served_version = match.group(1) if match else "unknown"
    assert served_version == RECORD["version"] and not mismatched, (
        f"the site serves vark {served_version}, this checkout vendors {RECORD['version']} "
        f"(files differing: {mismatched}); the image was built from other sources")


def test_served_notebooks_equal_checkout(signed_in):
    stale = []
    for path in sorted(CONTENTS.rglob("*.ipynb")):
        relative = path.relative_to(CONTENTS).as_posix()
        response = signed_in.get(f"{SITE_URL}{JUPYTER_PATH}files/{relative}", timeout=30)
        if response.status_code != 200 or response.content != path.read_bytes():
            stale.append(f"{relative} (HTTP {response.status_code})")
    assert not stale, f"served notebooks differ from this checkout: {stale}"


def test_api_version_and_compatible_pyvark(signed_in):
    version = signed_in.get(f"{SITE_URL}/api/version", timeout=30)
    assert version.status_code == 200, version.status_code
    payload = version.json()
    assert payload["version"] == CHECKOUT_API_VERSION, (
        f"site runs API {payload['version']}, this checkout is {CHECKOUT_API_VERSION}")
    for clause in payload["compatible_pyvark"].split(","):
        minimum = re.fullmatch(r"\s*>=\s*([\d.]+)\s*", clause)
        assert minimum, f"unsupported compatible_pyvark clause {clause!r}"
        assert _version_tuple(RECORD["version"]) >= _version_tuple(minimum.group(1)), (
            f"bundled vark {RECORD['version']} does not satisfy {payload['compatible_pyvark']}")


def _ids(session) -> Optional[list]:
    response = session.get(f"{SITE_URL}/api/databases", timeout=60)
    if response.status_code in (401, 403):
        return None
    assert response.status_code == 200, response.status_code
    return [(row["id"], row.get("access")) for row in response.json()["databases"]]


def test_internal_datasets_only_when_signed_in(signed_in, anonymous):
    signed_rows = _ids(signed_in)
    assert signed_rows is not None, "signed-in /api/databases refused"
    anonymous_rows = _ids(anonymous)
    if anonymous_rows is None:
        return  # the API is gated: anonymous callers see nothing at all
    leaked = [db_id for db_id, access in anonymous_rows if access != "public"]
    assert not leaked, f"anonymous callers see non-public datasets: {leaked[:5]}"
    assert {db_id for db_id, _ in anonymous_rows} <= {db_id for db_id, _ in signed_rows}


# ── browser part ─────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def lite():
    _need_credentials()
    from jupyterlite_driver import (JupyterLiteBrowser, browser_unavailable_reason,
                                    network_unavailable_reason)
    for reason in (browser_unavailable_reason(), network_unavailable_reason()):
        if reason:
            pytest.skip(reason)
    browser = JupyterLiteBrowser(site_url=SITE_URL, jupyter_path=JUPYTER_PATH,
                                 login_mode=LOGIN_MODE, username=USER, password=PASSWORD,
                                 ignore_https_errors=not VERIFY_TLS)
    try:
        browser.login()
        browser.open_lab()
        yield browser
    finally:
        browser.close()


@pytest.fixture(scope="module")
def connected(lite):
    from jupyterlite_driver import CONNECT_CODE, SETUP_CODE, parse_last_json
    for code in (SETUP_CODE, CONNECT_CODE):
        result = lite.run(code)
        assert result.ok, result.describe()
    return parse_last_json(result.stdout)


def test_lab_loads_isolated_with_bundled_vark(lite):
    from jupyterlite_driver import SETUP_CODE
    assert lite.cross_origin_isolated(), "page not cross-origin isolated (COOP/COEP)"
    result = lite.run(SETUP_CODE)
    assert result.ok, result.describe()
    version = result.stdout.strip().splitlines()[-2]
    assert version == RECORD["version"], f"kernel imports vark {version}, checkout {RECORD['version']}"


def test_kernel_reaches_api(connected):
    assert connected["health_status"] in ("healthy", "ok", "degraded"), connected
    assert connected["api_version"] == CHECKOUT_API_VERSION, connected
    assert connected["n_databases"] >= 0


def test_kernel_sees_the_signed_in_view(connected, signed_in):
    signed_rows = _ids(signed_in)
    expected = sorted(db_id for db_id, _ in signed_rows)
    assert sorted(connected["ids"]) == expected, (
        "the kernel's dataset list differs from the signed-in HTTP list: the browser "
        "session does not reach the API with the user's login")


def test_expression_query_and_plot(lite, connected):
    from jupyterlite_driver import QUERY_CODE, parse_last_json
    if connected["n_databases"] == 0:
        pytest.skip("empty catalogue: no dataset to query")
    result = lite.run(QUERY_CODE)
    assert result.ok, result.describe()
    payload = parse_last_json(result.stdout)
    assert payload["n_cells"] > 0 and payload["png_magic"], payload


@pytest.mark.parametrize("notebook", sorted(path.relative_to(CONTENTS).as_posix()
                                            for path in CONTENTS.rglob("*.ipynb")))
def test_shipped_notebook_runs(lite, connected, notebook):
    """All code cells with a dataset; on an empty catalogue only the first
    (the shipped notebooks stop with a message when there is no dataset)."""
    lite.open_notebook(notebook)
    code_cells = [cell for cell in lite.notebook_cells(notebook) if cell["type"] == "code"]
    count = len(code_cells) if connected["n_databases"] else 1
    results = lite.run_first_code_cells(notebook, count)
    for result in results:
        assert result.ok, result.describe()
    assert len(results) == count


def test_warming_up_answer_is_retried_in_pyodide(lite, connected):
    from jupyterlite_driver import WARMUP_CODE, parse_last_json
    counters = lite.simulate_warming_up("**/api/version", times=1, retry_after=1)
    result = lite.run(WARMUP_CODE)
    assert result.ok, result.describe()
    payload = parse_last_json(result.stdout)
    assert counters == {"503": 1, "passed": 1}, (
        f"vark did not retry the 503 warming_up answer (requests seen: {counters}, "
        f"kernel output: {payload})")
    assert payload["version"] == connected["api_version"]
    assert "ServerWarmingUpWarning" in payload["warnings"], payload


def test_anonymous_jupyterlite_sees_the_anonymous_view(anonymous):
    if LOGIN_MODE != "none":
        pytest.skip(f"{LOGIN_MODE} site: anonymous users cannot open JupyterLite")
    from jupyterlite_driver import (CONNECT_CODE, SETUP_CODE, JupyterLiteBrowser,
                                    browser_unavailable_reason, network_unavailable_reason,
                                    parse_last_json)
    for reason in (browser_unavailable_reason(), network_unavailable_reason()):
        if reason:
            pytest.skip(reason)
    browser = JupyterLiteBrowser(site_url=SITE_URL, jupyter_path=JUPYTER_PATH,
                                 ignore_https_errors=not VERIFY_TLS)
    try:
        browser.open_lab()
        for code in (SETUP_CODE, CONNECT_CODE):
            result = browser.run(code)
            assert result.ok, result.describe()
        payload = parse_last_json(result.stdout)
    finally:
        browser.close()
    assert payload["non_public_ids"] == [], payload["non_public_ids"][:5]
    anonymous_rows = _ids(anonymous) or []
    assert sorted(payload["ids"]) == sorted(db_id for db_id, _ in anonymous_rows)
