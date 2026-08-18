"""Edugate client: cookie session, catalog parse, official section lookup."""
import json
import logging
import random
import re
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup

import config

log = logging.getLogger("edugate")

LOGIN_URL = "https://edugate.ksu.edu.sa/ksu/ui/home.faces"
REGISTRATION_URL = (
    "https://edugate.ksu.edu.sa/ksu/ui/student/registration/index/forwardMainReg.faces"
)
ADD_COURSES_URL = "https://edugate.ksu.edu.sa/ksu/addCourses"
SECTION_SERVLET = "https://edugate.ksu.edu.sa/ksu/ajaxsectionservlet"
BASE_URL = "https://edugate.ksu.edu.sa"
HOME_PATH = "/ksu/ui/student/homeIndex.faces"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

BUSY_BACKOFF_START = 60
BUSY_BACKOFF_CAP = 15 * 60
REQUEST_TIMEOUT = (15, 30)
_NETWORK_ERRORS = (
    requests.ConnectionError,
    requests.Timeout,
    requests.exceptions.ChunkedEncodingError,
)


def _new_session(cookies=None):
    session = requests.Session()
    retry = Retry(
        total=2,
        connect=2,
        read=1,
        backoff_factor=0.6,
        status_forcelist=(502, 503, 504),
        allowed_methods=frozenset(["GET", "POST"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(HEADERS)
    # Avoid stale keep-alive sockets that show up as ConnectionError later
    session.headers["Connection"] = "close"
    if cookies:
        session.cookies.update(cookies)
    return session

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
        self._session = _new_session()
        self._courses_url = None
        self._backoff_until = 0.0
        self._backoff_seconds = BUSY_BACKOFF_START
        self._busy_alerted = False
        self._catalog_cache = (0.0, None)
        self._load_session()

    def _rebuild_session(self, keep_cookies=True):
        cookies = self._session.cookies.get_dict() if keep_cookies else {}
        try:
            self._session.close()
        except Exception:
            pass
        self._session = _new_session(cookies)

    def _get(self, url, **kwargs):
        kwargs.setdefault("timeout", REQUEST_TIMEOUT)
        try:
            return self._session.get(url, **kwargs)
        except _NETWORK_ERRORS as exc:
            log.warning("retry GET after %s", type(exc).__name__)
            self._rebuild_session(keep_cookies=True)
            return self._session.get(url, **kwargs)

    def _post(self, url, **kwargs):
        kwargs.setdefault("timeout", REQUEST_TIMEOUT)
        try:
            return self._session.post(url, **kwargs)
        except _NETWORK_ERRORS as exc:
            log.warning("retry POST after %s", type(exc).__name__)
            self._rebuild_session(keep_cookies=True)
            return self._session.post(url, **kwargs)

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

            html = self._catalog_html_reused()
            source = "session" if html else "login"
            if html is None:
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
        except requests.Timeout:
            return {"section_id": section_id, "status": "error", "error": "timeout"}
        except requests.RequestException as exc:
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
        if self._courses_url:
            try:
                resp = self._get(self._courses_url)
                if _looks_like_catalog(resp.text):
                    return resp.text
            except _NETWORK_ERRORS:
                return None
            except requests.RequestException:
                pass
        try:
            html, url = self._follow_add_courses()
            if html and _looks_like_catalog(html):
                self._courses_url = url
                return html
        except _NETWORK_ERRORS:
            return None
        except requests.RequestException:
            pass
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
            self._courses_url = url
            return html, None
        except requests.Timeout:
            return None, "Connection timeout"
        except requests.RequestException as exc:
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
        self._courses_url = data.get("courses_url")
        log.info(
            "session loaded  cookies=%s  saved_url=%s",
            len(cookies) if isinstance(cookies, dict) else 0,
            "yes" if self._courses_url else "no",
        )

    def _save_session(self):
        path = self._session_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "cookies": self._session.cookies.get_dict(),
            "courses_url": self._courses_url,
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
