"""Edugate client: cookie session, catalog parse, official section lookup."""
import json
import logging
import os
import random
import re
import socket
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import requests as std_requests
from bs4 import BeautifulSoup
from curl_cffi import CurlHttpVersion, CurlOpt
from curl_cffi import requests as http
from curl_cffi.requests.exceptions import (
    ChunkedEncodingError as CurlChunkedEncodingError,
    ConnectionError as CurlConnectionError,
    RequestException as CurlRequestException,
    Timeout as CurlTimeout,
)
from requests.adapters import HTTPAdapter
from requests.exceptions import (
    ChunkedEncodingError as RequestsChunkedEncodingError,
    ConnectionError as RequestsConnectionError,
    RequestException as RequestsRequestException,
    Timeout as RequestsTimeout,
)
import urllib3.util.connection as _urllib3_conn

import config

log = logging.getLogger("edugate")

LOGIN_URL = "https://edugate.ksu.edu.sa/ksu/ui/home.faces"
REGISTRATION_URL = (
    "https://edugate.ksu.edu.sa/ksu/ui/student/registration/index/forwardMainReg.faces"
)
ADD_COURSES_URL = "https://edugate.ksu.edu.sa/ksu/addCourses"
COURSES_PATH = "/ksu/ui/student/registration/index/allCoursesIndex.faces"
SECTION_SERVLET = "https://edugate.ksu.edu.sa/ksu/ajaxsectionservlet"
BASE_URL = "https://edugate.ksu.edu.sa"
HOME_PATH = "/ksu/ui/student/homeIndex.faces"
IMPERSONATE = "chrome"
CURL_IPRESOLVE_V4 = 1

BUSY_BACKOFF_START = 60
BUSY_BACKOFF_CAP = 15 * 60
REQUEST_TIMEOUT = 45
RETRY_PAUSE = 2.0
_TIMEOUT_ERRORS = (CurlTimeout, RequestsTimeout)
_NETWORK_ERRORS = (
    CurlConnectionError,
    CurlTimeout,
    CurlChunkedEncodingError,
    CurlRequestException,
    RequestsConnectionError,
    RequestsTimeout,
    RequestsChunkedEncodingError,
    RequestsRequestException,
)

_REQUESTS_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ar,en-US;q=0.9,en;q=0.8",
}


def _exc_detail(exc):
    text = str(exc).strip() or type(exc).__name__
    return f"{type(exc).__name__}: {text}"[:240]


def _stable_courses_url(url):
    """Drop one-shot ?reg= so a saved URL can be reused."""
    if not url:
        return None
    parsed = urlparse(url)
    if not parsed.path:
        return BASE_URL + COURSES_PATH
    return f"{BASE_URL}{parsed.path}"


def _cookie_dict(session):
    cookies = session.cookies
    if hasattr(cookies, "get_dict"):
        return cookies.get_dict()
    return {cookie.name: cookie.value for cookie in cookies}


def _in_docker():
    return Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()


def _host_proxy_urls():
    port = os.getenv("EDUGATE_HOST_PROXY_PORT", "18080").strip() or "18080"
    urls = [
        f"http://172.17.0.1:{port}",
        f"http://host.docker.internal:{port}",
        f"http://10.88.0.1:{port}",
    ]
    seen = set()
    out = []
    for url in urls:
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def _make_curl_session(http1=True, ipv4=True, cookies=None, proxy=None):
    kwargs = {"impersonate": IMPERSONATE, "timeout": REQUEST_TIMEOUT}
    if http1:
        kwargs["http_version"] = CurlHttpVersion.V1_1
    if ipv4:
        kwargs["curl_options"] = {CurlOpt.IPRESOLVE: CURL_IPRESOLVE_V4}
    proxy = proxy or config.EDUGATE_PROXY
    if proxy:
        kwargs["proxy"] = proxy
    session = http.Session(**kwargs)
    session.headers["Accept-Language"] = "ar,en-US;q=0.9,en;q=0.8"
    if cookies:
        session.cookies.update(cookies)
    return session


def _make_requests_session(ipv4=True, cookies=None, proxy=None):
    if ipv4:
        _urllib3_conn.allowed_gai_family = lambda: socket.AF_INET
    session = std_requests.Session()
    session.mount("https://", HTTPAdapter())
    session.mount("http://", HTTPAdapter())
    session.headers.update(_REQUESTS_HEADERS)
    proxy = proxy or config.EDUGATE_PROXY
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    if cookies:
        session.cookies.update(cookies)
    return session


def _log_dns():
    try:
        infos = socket.getaddrinfo("edugate.ksu.edu.sa", 443)
        addrs = sorted({item[4][0] for item in infos})
        log.info("dns  %s", " ".join(addrs))
    except OSError as exc:
        log.warning("dns fail  %s", exc)

_HIDDEN_PREFIXES = (
    "crsName",
    "crsSec",
    "crsActv",
    "groupTypeDesc",
    "time",
    "inst",
)


class EdugateClient:
    def __init__(self):
        self._lock = threading.Lock()
        self._session_factory = lambda cookies: _make_curl_session(
            http1=True, ipv4=True, cookies=cookies
        )
        self._session = self._session_factory({})
        self._courses_url = None
        self._backoff_until = 0.0
        self._backoff_seconds = BUSY_BACKOFF_START
        self._busy_alerted = False
        self._catalog_cache = (0.0, None)
        self._reachable = False
        log.info(
            "http  docker=%s  proxy=%s",
            "yes" if _in_docker() else "no",
            "yes" if config.EDUGATE_PROXY else "no",
        )
        self._pick_transport()
        self._load_session()

    def _adopt_session(self, factory, session):
        try:
            self._session.close()
        except Exception:
            pass
        self._session_factory = factory
        self._session = session
        self._reachable = True

    def _try_factory(self, name, factory):
        session = factory({})
        try:
            resp = session.get(LOGIN_URL, timeout=15)
            size = len(resp.content or b"")
            if resp.status_code == 200 and size > 500:
                self._adopt_session(factory, session)
                log.info(
                    "probe ok  transport=%s status=%s bytes=%s",
                    name,
                    resp.status_code,
                    size,
                )
                return True
            log.warning(
                "probe skip  transport=%s status=%s bytes=%s",
                name,
                resp.status_code,
                size,
            )
        except Exception as exc:
            log.warning("probe fail  transport=%s %s", name, _exc_detail(exc))
        try:
            session.close()
        except Exception:
            pass
        return False

    def _pick_transport(self):
        self._reachable = False
        strategies = [
            (
                "curl chrome http1 ipv4",
                lambda cookies: _make_curl_session(http1=True, ipv4=True, cookies=cookies),
            ),
            (
                "curl chrome ipv4",
                lambda cookies: _make_curl_session(http1=False, ipv4=True, cookies=cookies),
            ),
            (
                "curl chrome http1",
                lambda cookies: _make_curl_session(http1=True, ipv4=False, cookies=cookies),
            ),
            ("requests ipv4", lambda cookies: _make_requests_session(ipv4=True, cookies=cookies)),
        ]
        for name, factory in strategies:
            if self._try_factory(name, factory):
                return
            time.sleep(0.4)

        if not config.EDUGATE_PROXY:
            for proxy in _host_proxy_urls():
                name = f"curl chrome http1 ipv4 via {urlparse(proxy).hostname}"
                factory = lambda cookies, proxy=proxy: _make_curl_session(
                    http1=True, ipv4=True, cookies=cookies, proxy=proxy
                )
                if self._try_factory(name, factory):
                    return
                time.sleep(0.4)

        _log_dns()
        if _in_docker():
            log.warning(
                "probe none worked inside Docker — Edugate resets container traffic. "
                "Run python bot.py on the host, or start python edugate_proxy.py on the host"
            )
        else:
            log.warning("probe none worked, using curl chrome http1 ipv4")
        self._session_factory = strategies[0][1]
        try:
            self._session.close()
        except Exception:
            pass
        self._session = self._session_factory({})
        self._reachable = False

    def _rebuild_session(self, keep_cookies=True):
        cookies = _cookie_dict(self._session) if keep_cookies else {}
        try:
            self._session.close()
        except Exception:
            pass
        self._session = self._session_factory(cookies)

    def _get(self, url, **kwargs):
        kwargs.setdefault("timeout", REQUEST_TIMEOUT)
        headers = dict(kwargs.pop("headers", None) or {})
        headers.setdefault("Referer", BASE_URL + "/")
        try:
            return self._session.get(url, headers=headers, **kwargs)
        except _NETWORK_ERRORS as exc:
            log.warning(
                "GET %s failed (%s), wait %.0fs then retry",
                urlparse(url).path,
                _exc_detail(exc),
                RETRY_PAUSE,
            )
            time.sleep(RETRY_PAUSE)
            self._rebuild_session(keep_cookies=True)
            return self._session.get(url, headers=headers, **kwargs)

    def _post(self, url, **kwargs):
        kwargs.setdefault("timeout", REQUEST_TIMEOUT)
        headers = dict(kwargs.pop("headers", None) or {})
        headers.setdefault("Referer", url)
        try:
            return self._session.post(url, headers=headers, **kwargs)
        except _NETWORK_ERRORS as exc:
            log.warning(
                "POST %s failed (%s), wait %.0fs then retry",
                urlparse(url).path,
                _exc_detail(exc),
                RETRY_PAUSE,
            )
            time.sleep(RETRY_PAUSE)
            self._rebuild_session(keep_cookies=True)
            return self._session.post(url, headers=headers, **kwargs)

    def backoff_remaining(self):
        return max(0, int(self._backoff_until - time.time()))

    def fetch_catalog(self, force=False):
        """Return (sections_dict, error_or_none). Reuses cookies when possible."""
        with self._lock:
            started = time.time()
            if not force:
                wait = self.backoff_remaining()
                if wait:
                    log.info("catalog skip  reason=backoff wait=%ss", wait)
                    return None, f"busy_backoff:{wait}"
                cached_at, cached = self._catalog_cache
                if cached is not None and time.time() - cached_at < 20:
                    log.info("catalog ok  source=cache sections=%s", len(cached))
                    return cached, None

            if not self._reachable:
                self._pick_transport()
            if not self._reachable:
                self._trip_backoff()
                log.warning(
                    "catalog fail  error=ConnectionError backoff=%ss",
                    self.backoff_remaining(),
                )
                return None, "ConnectionError"

            had_cookies = bool(self._session.cookies)
            html = self._catalog_html_reused()
            source = "session" if html else "login"
            if html is None:
                if had_cookies:
                    time.sleep(RETRY_PAUSE)
                html, error = self._login_and_open_catalog()
                if error:
                    if error in {
                        "ConnectionError",
                        "Connection timeout",
                        "Timeout",
                        "ChunkedEncodingError",
                    }:
                        self._trip_backoff()
                        log.warning(
                            "catalog fail  error=%s backoff=%ss",
                            error,
                            self.backoff_remaining(),
                        )
                    else:
                        log.error("catalog fail  error=%s", error)
                    return None, error

            sections = parse_sections(html)
            if not sections:
                log.error("catalog fail  error=empty_parse source=%s", source)
                return None, "Could not parse any sections"
            self._catalog_cache = (time.time(), sections)
            self._clear_backoff()
            self._save_session()
            log.info(
                "catalog ok  source=%s sections=%s ms=%s",
                source,
                len(sections),
                int((time.time() - started) * 1000),
            )
            return sections, None

    def lookup_section(self, section_id):
        """Official add-box lookup. Returns a status dict. Never submits add."""
        section_id = str(section_id).strip()
        with self._lock:
            if not self._reachable:
                self._pick_transport()
            if not self._reachable:
                return {"section_id": section_id, "status": "error", "error": "ConnectionError"}
            wait = self.backoff_remaining()
            if wait:
                return {"section_id": section_id, "status": "busy", "backoff": wait}

            result = self._lookup_unlocked(section_id)
            if result.get("status") == "session_expired":
                log.info("lookup %s  session expired, re-login", section_id)
                html, error = self._login_and_open_catalog()
                if error:
                    log.error("lookup %s  re-login fail error=%s", section_id, error)
                    return {"section_id": section_id, "status": "error", "error": error}
                result = self._lookup_unlocked(section_id)
            if result.get("status") == "busy":
                self._trip_backoff()
                log.warning("lookup %s  busy backoff=%ss", section_id, self.backoff_remaining())
            elif result.get("status") == "error" and result.get("error") in {
                "ConnectionError",
                "timeout",
                "ChunkedEncodingError",
            }:
                self._trip_backoff()
                log.warning(
                    "lookup %s  error=%s backoff=%ss",
                    section_id,
                    result.get("error"),
                    self.backoff_remaining(),
                )
            elif result.get("status") == "open":
                self._clear_backoff()
                log.info("lookup %s  status=open", section_id)
            else:
                log.info("lookup %s  status=%s", section_id, result.get("status"))
            return result

    def _lookup_unlocked(self, section_id):
        if not self._session.cookies:
            return {"section_id": section_id, "status": "session_expired"}
        try:
            resp = self._post(
                SECTION_SERVLET,
                params={"section": section_id, "index": "0"},
                data="",
                headers={
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"
                },
            )
        except _TIMEOUT_ERRORS:
            return {"section_id": section_id, "status": "error", "error": "timeout"}
        except _NETWORK_ERRORS as exc:
            return {"section_id": section_id, "status": "error", "error": type(exc).__name__}

        body = (resp.text or "").strip()
        path = urlparse(resp.url).path
        if path.endswith("home.faces") or 'name="username"' in body:
            return {"section_id": section_id, "status": "session_expired"}
        if body == "busy":
            return {"section_id": section_id, "status": "busy"}
        if body == "":
            return {"section_id": section_id, "status": "error", "error": "empty"}
        if body == "-1":
            return {"section_id": section_id, "status": "not_found"}
        if body in {"-2", "-3"}:
            return {"section_id": section_id, "status": "unavailable"}
        if body == "-4":
            return {"section_id": section_id, "status": "unavailable"}

        parts = body.split("-@F1@-")
        if len(parts) < 8:
            return {"section_id": section_id, "status": "unavailable"}

        return {
            "section_id": section_id,
            "status": "open",
            "course_code": _clean(parts[2] if len(parts) > 2 else ""),
            "course_name": _clean(parts[4] if len(parts) > 4 else ""),
            "activity": _clean(parts[5] if len(parts) > 5 else ""),
            "time": _plain_time(parts[6] if len(parts) > 6 else ""),
            "doctor": _clean(parts[7] if len(parts) > 7 else ""),
            "group": _clean(parts[10] if len(parts) > 10 else ""),
            "section_num": _clean(parts[3] if len(parts) > 3 else ""),
        }

    def consume_busy_alert(self):
        """True once per busy episode so chats are not spammed."""
        with self._lock:
            if self._busy_alerted or self.backoff_remaining() <= 0:
                return False
            self._busy_alerted = True
            return True

    def _catalog_html_reused(self):
        if not self._session.cookies:
            return None
        url = _stable_courses_url(self._courses_url) or (BASE_URL + COURSES_PATH)
        try:
            resp = self._get(url)
            if _looks_like_catalog(resp.text):
                self._courses_url = url
                return resp.text
            log.info("saved catalog url is stale, will re-login")
            self._courses_url = None
            return None
        except _NETWORK_ERRORS as exc:
            log.warning("saved catalog url dropped (%s)", type(exc).__name__)
            self._courses_url = None
            time.sleep(RETRY_PAUSE)
        try:
            html, next_url = self._follow_add_courses()
            if html and _looks_like_catalog(html):
                self._courses_url = _stable_courses_url(next_url)
                return html
        except _NETWORK_ERRORS as exc:
            log.warning("addCourses dropped (%s)", type(exc).__name__)
        return None

    def _login_and_open_catalog(self):
        self._rebuild_session(keep_cookies=False)
        self._courses_url = None
        try:
            login_page = self._get(LOGIN_URL)
            soup = BeautifulSoup(login_page.text, "html.parser")
            viewstate = soup.find("input", {"name": "javax.faces.ViewState"})
            if not viewstate:
                return None, "Could not find ViewState on login page"

            login_response = self._post(
                LOGIN_URL,
                data={
                    "loginForm": "loginForm",
                    "biConnectionConfig": "true",
                    "token": "",
                    "username": config.EDUGATE_USERNAME,
                    "password": config.EDUGATE_PASSWORD,
                    "newsCode": "",
                    "javax.faces.ViewState": viewstate["value"],
                    "loginUsersLink": "loginUsersLink",
                },
            )
            if not _login_succeeded(login_response):
                return None, "Login failed — check EDUGATE credentials"

            reg_page = self._get(REGISTRATION_URL)
            soup = BeautifulSoup(reg_page.text, "html.parser")
            viewstate = soup.find("input", {"name": "javax.faces.ViewState"})
            if not viewstate:
                return None, "Could not access registration page"

            self._post(
                REGISTRATION_URL,
                data={
                    "myForm": "myForm",
                    "javax.faces.ViewState": viewstate["value"],
                    "myForm:serLinkDropAdd2": "myForm:serLinkDropAdd2",
                },
            )

            html, url = self._follow_add_courses()
            if not html:
                return None, "Could not find courses page redirect"
            self._courses_url = _stable_courses_url(url)
            return html, None
        except _TIMEOUT_ERRORS:
            return None, "Connection timeout"
        except _NETWORK_ERRORS as exc:
            log.warning("login failed  %s", _exc_detail(exc))
            return None, type(exc).__name__

    def _follow_add_courses(self):
        add_response = self._get(f"{ADD_COURSES_URL}?reg={random.random()}")
        match = re.search(r'window\.location\.replace\("([^"]+)"\)', add_response.text)
        if not match:
            return None, None
        url = BASE_URL + match.group(1)
        courses = self._get(url)
        return courses.text, url

    def _trip_backoff(self):
        self._backoff_until = time.time() + self._backoff_seconds
        self._backoff_seconds = min(self._backoff_seconds * 2, BUSY_BACKOFF_CAP)
        self._busy_alerted = False

    def _clear_backoff(self):
        self._backoff_until = 0.0
        self._backoff_seconds = BUSY_BACKOFF_START
        self._busy_alerted = False

    def _session_path(self):
        return Path(config.SESSION_FILE)

    def _load_session(self):
        path = self._session_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return
        cookies = data.get("cookies") or {}
        if isinstance(cookies, dict):
            self._session.cookies.update(cookies)
        self._courses_url = _stable_courses_url(data.get("courses_url"))
        log.info(
            "session loaded  cookies=%s  saved_url=%s",
            len(cookies) if isinstance(cookies, dict) else 0,
            "yes" if self._courses_url else "no",
        )

    def _save_session(self):
        path = self._session_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "cookies": _cookie_dict(self._session),
            "courses_url": _stable_courses_url(self._courses_url),
        }
        path.write_text(json.dumps(payload), encoding="utf-8")


def _login_succeeded(response):
    path = urlparse(response.url).path
    if path.startswith("/ksu/ui/student/"):
        return True
    soup = BeautifulSoup(response.text, "html.parser")
    if soup.find("input", {"name": "username"}):
        return False
    return HOME_PATH in path


def _looks_like_catalog(html):
    if not html:
        return False
    if 'name="allData"' in html:
        return True
    return html.count("showToolTip") >= 3


def _clean(text):
    if not text:
        return ""
    return BeautifulSoup(str(text), "html.parser").get_text(" ", strip=True)


def _plain_time(raw):
    if not raw:
        return ""
    text = raw.replace("@n", " | ").replace("@t", " ").replace("@r", " ")
    return _clean(text)


def parse_sections(html):
    """Merge tooltip IDs with allData hidden fields (name, time, activity)."""
    from_tip = _parse_tooltips(html)
    from_hidden = _parse_hidden(html)
    if not from_tip and not from_hidden:
        return {}
    if not from_tip:
        return from_hidden
    hidden_by_num = {}
    for sec in from_hidden.values():
        hidden_by_num.setdefault(str(sec.get("section_num", "")), []).append(sec)

    used = set()
    for key, sec in from_tip.items():
        cands = [
            h
            for h in hidden_by_num.get(str(sec.get("section_num", "")), [])
            if id(h) not in used
        ]
        match = None
        if len(cands) == 1:
            match = cands[0]
        elif cands and sec.get("course_name"):
            for h in cands:
                if h.get("course_name") and (
                    h["course_name"] in sec["course_name"]
                    or sec["course_name"] in h["course_name"]
                ):
                    match = h
                    break
        if match is None and cands:
            match = cands[0]
        if match:
            used.add(id(match))
            for field in ("course_name", "course_code", "activity", "time", "doctor", "group"):
                if match.get(field) and not sec.get(field):
                    sec[field] = match[field]
                elif match.get(field) and field in ("activity", "time", "group"):
                    sec[field] = match[field]
            if match.get("doctor") and (not sec.get("doctor") or sec.get("doctor") == "غير معروف"):
                sec["doctor"] = match["doctor"]
            if match.get("course_name"):
                sec["course_name"] = match["course_name"]
    return from_tip


def _parse_tooltips(html):
    soup = BeautifulSoup(html, "html.parser")
    sections = {}
    for link in soup.find_all(
        "a", onclick=lambda x: x and "showToolTip(this,event," in x
    ):
        parts = re.findall(r"'([^']*)'", link.get("onclick", ""))
        if len(parts) < 11:
            continue
        section_nums = parts[0].strip("-").split("-") if parts[0].strip("-") else []
        section_ids = parts[1].strip("-").split("-") if parts[1].strip("-") else []
        course_id = parts[6]
        doctor_names = [d.strip() for d in parts[10].split("@-@-@") if d.strip()]
        course_code, course_name = _course_from_row(link)
        for idx, (sec_num, sec_id) in enumerate(zip(section_nums, section_ids)):
            if not sec_id:
                continue
            doctor = doctor_names[idx] if idx < len(doctor_names) else "غير معروف"
            sections[f"{course_id}_{sec_id}"] = {
                "course_id": course_id,
                "course_code": course_code,
                "course_name": course_name,
                "section_num": sec_num,
                "section_id": sec_id,
                "doctor": doctor,
                "activity": "",
                "time": "",
                "group": "",
            }
    return sections


def _course_from_row(link):
    course_code = ""
    course_name = ""
    parent_tr = link.find_parent("tr")
    if not parent_tr:
        return course_code, course_name
    for td in parent_tr.find_all("td"):
        text = td.get_text(strip=True).replace("\xa0", " ").strip()
        if re.match(r"^\d+\s+\S+$", text) and len(text) < 20:
            course_code = text
        elif (
            len(text) > 5
            and not text.startswith(("إبحث", "إجبارية", "إختيارية", "انتظام"))
            and not text.isdigit()
            and not re.match(r"^\d+\s+\S+$", text)
        ):
            if not course_name:
                course_name = text
    return course_code, course_name


def _parse_hidden(html):
    soup = BeautifulSoup(html, "html.parser")
    form = soup.find("form", {"name": "allData"})
    if not form:
        return {}
    grouped = {}
    for inp in form.find_all("input"):
        name = inp.get("name") or ""
        for prefix in _HIDDEN_PREFIXES:
            if name.startswith(prefix) and len(name) > len(prefix):
                suffix = name[len(prefix) :]
                grouped.setdefault(suffix, {})[prefix] = inp.get("value") or ""
                break
    sections = {}
    for suffix, fields in grouped.items():
        sec_num = (fields.get("crsSec") or "").strip()
        if not sec_num:
            continue
        key = f"hidden_{suffix}"
        sections[key] = {
            "course_id": suffix.split("_")[0] if "_" in suffix else suffix,
            "course_code": "",
            "course_name": _clean(fields.get("crsName")),
            "section_num": sec_num,
            "section_id": sec_num,
            "doctor": _clean(fields.get("inst")) or "غير معروف",
            "activity": _clean(fields.get("crsActv")),
            "time": _plain_time(fields.get("time")),
            "group": _clean(fields.get("groupTypeDesc")),
        }
    return sections


def group_by_course(sections_list):
    courses = {}
    for sec in sections_list:
        code = sec.get("course_code") or sec.get("course_name") or "?"
        if code not in courses:
            courses[code] = {"name": sec.get("course_name") or "", "sections": []}
        courses[code]["sections"].append(sec)
    return courses


def _norm_course_query(query):
    return re.sub(r"\s+", " ", str(query or "").strip()).lower()


def section_matches_course(sec, query):
    """True if a catalog row belongs to course 339 / '123 339' / course id."""
    q = _norm_course_query(query)
    if not q:
        return False
    code = _norm_course_query(sec.get("course_code") or "")
    course_id = _norm_course_query(sec.get("course_id") or "")
    compact_q = re.sub(r"\s+", "", q)
    compact_code = re.sub(r"\s+", "", code)
    if q in {code, course_id} or compact_q in {compact_code, course_id}:
        return True
    tokens = re.findall(r"[a-z0-9]+", code)
    return q in tokens or compact_q in tokens


def filter_sections_for_course(sections, query):
    return {key: sec for key, sec in (sections or {}).items() if section_matches_course(sec, query)}
