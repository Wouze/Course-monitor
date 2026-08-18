import json
import logging
import random
import re
import threading
import time
from datetime import datetime
from pathlib import Path

import telebot

import config
from edugate import EdugateClient, group_by_course

log = logging.getLogger("bot")


def _setup_logging():
    if logging.getLogger().handlers:
        return
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-5s  %(name)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


_setup_logging()

bot = telebot.TeleBot(config.BOT_TOKEN)
USERS_FILE = config.USERS_FILE
_users_lock = threading.Lock()
edugate = EdugateClient()
_last_manual_check = {}


def _without_secrets(user_data):
    return {k: v for k, v in user_data.items() if k not in ("username", "password")}


def load_users():
    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            users = json.load(f)
    except FileNotFoundError:
        return {}
    return {uid: _without_secrets(data) for uid, data in users.items()}


def save_users(users):
    cleaned = {uid: _without_secrets(data) for uid, data in users.items()}
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(cleaned, f, ensure_ascii=False, indent=2)


def all_users():
    with _users_lock:
        return load_users()


def get_user(chat_id):
    with _users_lock:
        return load_users().get(str(chat_id))


def save_user(chat_id, user_data):
    with _users_lock:
        users = load_users()
        users[str(chat_id)] = user_data
        save_users(users)


def delete_user(chat_id):
    with _users_lock:
        users = load_users()
        if str(chat_id) not in users:
            return False
        del users[str(chat_id)]
        save_users(users)
        return True


def is_admin(chat_id):
    return chat_id == config.ADMIN_ID


def md(text):
    return re.sub(r"([_*`\[\]])", r"\\\1", str(text or ""))


def send_long(chat_id, msg):
    if len(msg) > 4000:
        for i in range(0, len(msg), 4000):
            bot.send_message(chat_id, msg[i : i + 4000], parse_mode="Markdown")
    else:
        bot.send_message(chat_id, msg, parse_mode="Markdown")


def _section_line(sec):
    extra = []
    if sec.get("activity"):
        extra.append(sec["activity"])
    if sec.get("time"):
        extra.append(sec["time"])
    suffix = f" — {' · '.join(extra)}" if extra else ""
    doctor = sec.get("doctor") or "غير معروف"
    return (
        f"   • شعبة {md(sec.get('section_num'))} "
        f"(ID: `{md(sec.get('section_id'))}`) - {md(doctor)}{md(suffix)}\n"
    )


def _edugate_user_error(error):
    err = str(error)
    if err in {
        "ConnectionError",
        "Connection timeout",
        "Timeout",
        "ChunkedEncodingError",
        "ProxyError",
        "DNSError",
        "SSLError",
    }:
        return (
            "⚠️ تعذر الاتصال بإيدوجيت من هذا الخادم.\n"
            "إذا كان البوت على VPS فالجامعة قد تحجب ذلك العنوان.\n"
            "شغّله من شبكة المنزل أو عيّن `EDUGATE_PROXY`."
        )
    return f"⚠️ *خطأ في الفحص:*\n`{md(error)}`"


def _notify_busy_once():
    wait = edugate.backoff_remaining()
    if not wait or not edugate.consume_busy_alert():
        return
    for uid in load_users():
        try:
            bot.send_message(
                int(uid),
                f"⚠️ إيدوجيت لا يستجيب. سأعيد المحاولة بعد {wait} ثانية.",
            )
        except Exception:
            pass


def _catalog_snapshot(force=False):
    sections, error = edugate.fetch_catalog(force=force)
    if error and str(error).startswith("busy_backoff:"):
        _notify_busy_once()
        return None, error
    return sections, error


def check_user_sections(chat_id, notify_errors=True, force=False):
    user = get_user(chat_id)
    if not user:
        return False

    watches = user.get("watches") or {}
    if watches:
        return _check_watches(chat_id, user, watches)

    t0 = time.time()
    log.info("check catalog  chat=%s", chat_id)
    current, error = _catalog_snapshot(force=force)
    if error:
        if str(error).startswith("busy_backoff:"):
            log.info("check skip  chat=%s reason=backoff wait=%ss", chat_id, error.split(":", 1)[1])
        else:
            log.error("check fail  chat=%s error=%s", chat_id, error)
        if notify_errors and not str(error).startswith("busy_backoff:"):
            try:
                bot.send_message(chat_id, _edugate_user_error(error), parse_mode="Markdown")
            except Exception:
                pass
        return False

    saved = user.get("sections", {})
    new_sections = [sec for key, sec in current.items() if key not in saved]
    removed_sections = [sec for key, sec in saved.items() if key not in current]

    user["total_checks"] = user.get("total_checks", 0) + 1
    user["last_check"] = datetime.now().isoformat()
    if new_sections:
        user["total_new"] = user.get("total_new", 0) + len(new_sections)
    if removed_sections:
        user["total_removed"] = user.get("total_removed", 0) + len(removed_sections)

    if new_sections:
        _send_section_group(chat_id, "🆕 *شعب جديدة متاحة!*\n\n", new_sections)
    if removed_sections:
        _send_section_group(chat_id, "❌ *شعب لم تعد متاحة (ممتلئة):*\n\n", removed_sections)

    log.info(
        "check ok  chat=%s sections=%s new=%s gone=%s ms=%s",
        chat_id,
        len(current),
        len(new_sections),
        len(removed_sections),
        int((time.time() - t0) * 1000),
    )
    user["sections"] = current
    save_user(chat_id, user)
    return True


def _send_section_group(chat_id, header, sections_list):
    msg = header
    grouped = group_by_course(sections_list)
    for code, info in sorted(grouped.items()):
        msg += f"📚 *{md(code)}* - {md(info['name'])}\n"
        for sec in info["sections"]:
            msg += _section_line(sec)
        msg += "\n"
    try:
        send_long(chat_id, msg)
    except Exception as exc:
        log.error("telegram send fail  chat=%s error=%s", chat_id, type(exc).__name__)


def _check_watches(chat_id, user, watches):
    t0 = time.time()
    log.info("check watches  chat=%s count=%s", chat_id, len(watches))
    opened = []
    closed = []
    ok = True

    for section_id, saved in list(watches.items()):
        result = edugate.lookup_section(section_id)
        status = result.get("status")
        if status == "busy":
            _notify_busy_once()
            ok = False
            break
        if status == "error":
            log.error("watch fail  id=%s error=%s", section_id, result.get("error"))
            ok = False
            continue

        prev = saved.get("status") or "unknown"
        merged = {**saved, **{k: v for k, v in result.items() if v}}
        merged["status"] = status
        merged["last_seen"] = datetime.now().isoformat()
        watches[section_id] = merged

        became_open = status == "open" and prev != "open" and prev != "unknown"
        became_closed = status in {"not_found", "unavailable"} and prev == "open"
        if became_open:
            opened.append(merged)
        if became_closed:
            closed.append(merged)

    user["watches"] = watches
    user["total_checks"] = user.get("total_checks", 0) + 1
    user["last_check"] = datetime.now().isoformat()
    if opened:
        user["total_new"] = user.get("total_new", 0) + len(opened)
        _send_section_group(chat_id, "🆕 *شعبة في قائمتك أصبحت متاحة!*\n\n", opened)
    if closed:
        user["total_removed"] = user.get("total_removed", 0) + len(closed)
        _send_section_group(chat_id, "❌ *شعبة في قائمتك لم تعد متاحة:*\n\n", closed)
    save_user(chat_id, user)
    log.info(
        "check watches done  chat=%s opened=%s closed=%s ok=%s ms=%s",
        chat_id,
        len(opened),
        len(closed),
        ok,
        int((time.time() - t0) * 1000),
    )
    return ok


def _next_interval(base_interval):
    jitter = config.CHECK_JITTER
    offset = random_offset(jitter)
    wait = edugate.backoff_remaining()
    return max(1, base_interval + offset, wait)


def random_offset(jitter):
    if not jitter:
        return 0
    return random.randint(-jitter, jitter)


def scheduler():
    next_check_at = {}
    while True:
        users = all_users()
        now = time.time()
        soonest = None
        for chat_id_str, user_data in users.items():
            chat_id = int(chat_id_str)
            base_interval = user_data.get("check_interval", config.DEFAULT_CHECK_INTERVAL)
            if chat_id not in next_check_at:
                next_check_at[chat_id] = now + 8
                log.info("scheduler  chat=%s first check in 8s", chat_id)
            due = next_check_at[chat_id]
            if now >= due:
                try:
                    check_user_sections(chat_id, notify_errors=False)
                except Exception as exc:
                    log.exception("check crash  chat=%s", chat_id)
                next_check_at[chat_id] = time.time() + _next_interval(base_interval)
                time.sleep(1)
            remaining = next_check_at.get(chat_id, now) - time.time()
            if soonest is None or remaining < soonest:
                soonest = remaining
        sleep_for = 5 if soonest is None else max(1, min(soonest, 5))
        time.sleep(sleep_for)


def _require_user(message):
    user = get_user(message.chat.id)
    if not user:
        bot.reply_to(message, "⚠️ أنت غير مسجل. أرسل /start للتسجيل.")
        return None
    return user


@bot.message_handler(commands=["start"])
def cmd_start(message):
    chat_id = message.chat.id
    user = get_user(chat_id)
    log.info("cmd /start  chat=%s  already=%s", chat_id, bool(user))
    if user:
        interval_mins = user.get("check_interval", config.DEFAULT_CHECK_INTERVAL) // 60
        watches = user.get("watches") or {}
        bot.reply_to(
            message,
            f"👋 مرحباً! أنت مسجل بالفعل.\n\n"
            f"📊 الشعب المحفوظة: {len(user.get('sections', {}))}\n"
            f"👀 المراقبة: {len(watches)} شعبة\n"
            f"⏰ الفحص كل: {interval_mins} دقيقة\n\n"
            f"أرسل /help لعرض الأوامر",
            parse_mode="Markdown",
        )
        return

    bot.reply_to(message, "🔄 جاري التحقق من حساب إيدوجيت...")
    sections, error = _catalog_snapshot()
    if error:
        bot.send_message(chat_id, _edugate_user_error(error), parse_mode="Markdown")
        return

    save_user(
        chat_id,
        {
            "sections": sections,
            "watches": {},
            "check_interval": config.DEFAULT_CHECK_INTERVAL,
            "registered_at": datetime.now().isoformat(),
            "total_checks": 0,
            "total_new": 0,
            "total_removed": 0,
        },
    )
    bot.send_message(
        chat_id,
        f"✅ *تم التسجيل بنجاح!*\n\n"
        f"📊 تم العثور على {len(sections)} شعبة متاحة\n\n"
        f"راقب شعبة محددة:\n"
        f"`/watch 12345`\n\n"
        f"بدون قائمة مراقبة سأخبرك بأي تغيير في الكتالوج.\n"
        f"أرسل /help لعرض الأوامر",
        parse_mode="Markdown",
    )


@bot.message_handler(commands=["help"])
def cmd_help(message):
    help_text = """📖 *أوامر البوت:*

*الأساسية:*
/start - الاشتراك في التنبيهات
/check - فحص التغييرات الآن
/sections - عرض الشعب المتاحة
/stats - إحصائياتك
/settings - إعداداتك
/logout - إلغاء التسجيل

*المراقبة:*
/watch `[معرف]` - راقب شعبة
/unwatch `[معرف]` - أوقف المراقبة
/watches - قائمة المراقبة

*الإعدادات:*
/interval `[دقائق]` - تغيير وقت الفحص
   مثال: `/interval 30`

*كيف يعمل:*
• بدون `/watch` يُقارن كتالوج الشعب كلها
• مع `/watch` يسأل إيدوجيت عن تلك المعرفات فقط
"""
    if is_admin(message.chat.id):
        help_text += """
*أوامر المشرف:*
/admin - لوحة التحكم
/users - قائمة المستخدمين
/broadcast `[رسالة]` - إرسال للجميع
"""
    bot.send_message(message.chat.id, help_text, parse_mode="Markdown")


@bot.message_handler(commands=["check"])
def cmd_check(message):
    user = _require_user(message)
    if not user:
        return
    chat_id = message.chat.id
    now = time.time()
    last = _last_manual_check.get(chat_id, 0)
    if now - last < 15:
        bot.reply_to(message, "⏳ انتظر قليلاً قبل الفحص اليدوي.")
        return
    _last_manual_check[chat_id] = now
    log.info("cmd /check  chat=%s", chat_id)
    bot.reply_to(message, "🔍 جاري الفحص...")
    if check_user_sections(chat_id, notify_errors=True, force=True):
        bot.send_message(chat_id, "✅ تم الفحص!")
    else:
        bot.send_message(chat_id, "⚠️ لم يكتمل الفحص. حاول لاحقاً.")


@bot.message_handler(commands=["sections"])
def cmd_sections(message):
    user = _require_user(message)
    if not user:
        return
    bot.reply_to(message, "📥 جاري جلب الشعب...")
    sections, error = _catalog_snapshot()
    if error:
        bot.send_message(message.chat.id, _edugate_user_error(error), parse_mode="Markdown")
        return
    user["sections"] = sections
    save_user(message.chat.id, user)
    courses = group_by_course(list(sections.values()))
    msg = f"📊 *الشعب المتاحة ({len(sections)} شعبة):*\n\n"
    for code, info in sorted(courses.items()):
        msg += f"📚 *{md(code)}* - {md(info['name'])}\n"
        for sec in info["sections"]:
            msg += _section_line(sec)
        msg += "\n"
    send_long(message.chat.id, msg)


@bot.message_handler(commands=["watch"])
def cmd_watch(message):
    user = _require_user(message)
    if not user:
        return
    ids = [p for p in message.text.split()[1:] if p]
    log.info("cmd /watch  chat=%s  ids=%s", message.chat.id, len(ids))
    if not ids:
        bot.reply_to(message, "⚠️ أرسل معرف الشعبة\nمثال: `/watch 12345`", parse_mode="Markdown")
        return
    watches = user.get("watches") or {}
    added = []
    for raw in ids:
        if not re.fullmatch(r"\d+", raw):
            bot.reply_to(message, f"⚠️ معرف غير صالح: `{md(raw)}`", parse_mode="Markdown")
            return
        if raw in watches:
            continue
        if len(watches) >= config.MAX_WATCHES:
            bot.reply_to(message, f"⚠️ الحد الأقصى {config.MAX_WATCHES} شعبة")
            return
        result = edugate.lookup_section(raw)
        if result.get("status") == "busy":
            _notify_busy_once()
            bot.reply_to(message, "⚠️ إيدوجيت مشغول، جرّب بعد قليل.")
            return
        if result.get("status") in {"error", "session_expired"}:
            bot.reply_to(
                message,
                f"⚠️ تعذر التحقق من `{md(raw)}`: {md(result.get('error') or result.get('status'))}",
                parse_mode="Markdown",
            )
            return
        watches[raw] = {
            "section_id": raw,
            "status": result.get("status") or "unknown",
            "course_code": result.get("course_code") or "",
            "course_name": result.get("course_name") or "",
            "section_num": result.get("section_num") or "",
            "doctor": result.get("doctor") or "",
            "time": result.get("time") or "",
            "activity": result.get("activity") or "",
            "last_seen": datetime.now().isoformat(),
        }
        added.append(watches[raw])
    user["watches"] = watches
    save_user(message.chat.id, user)
    if not added:
        bot.reply_to(message, "هذه الشعب مُراقبة مسبقاً.")
        return
    msg = f"✅ تمت إضافة {len(added)} للمراقبة.\n\n"
    for sec in added:
        msg += f"• ID `{md(sec['section_id'])}` — {md(sec.get('status'))}"
        if sec.get("course_name"):
            msg += f" — {md(sec['course_name'])}"
        msg += "\n"
    bot.send_message(message.chat.id, msg, parse_mode="Markdown")


@bot.message_handler(commands=["unwatch"])
def cmd_unwatch(message):
    user = _require_user(message)
    if not user:
        return
    ids = [p for p in message.text.split()[1:] if p]
    watches = user.get("watches") or {}
    if not ids:
        bot.reply_to(message, "⚠️ أرسل المعرف\nمثال: `/unwatch 12345`", parse_mode="Markdown")
        return
    removed = 0
    for raw in ids:
        if raw in watches:
            del watches[raw]
            removed += 1
    user["watches"] = watches
    save_user(message.chat.id, user)
    bot.reply_to(message, f"✅ تم حذف {removed}. المتبقي: {len(watches)}")


@bot.message_handler(commands=["watches"])
def cmd_watches(message):
    user = _require_user(message)
    if not user:
        return
    watches = user.get("watches") or {}
    if not watches:
        bot.reply_to(
            message,
            "لا توجد شعب مُراقبة. أضف بـ `/watch 12345`",
            parse_mode="Markdown",
        )
        return
    msg = f"👀 *قائمة المراقبة ({len(watches)}):*\n\n"
    for sid, sec in watches.items():
        msg += f"• `{md(sid)}` — {md(sec.get('status'))}"
        if sec.get("course_name"):
            msg += f" — {md(sec['course_name'])}"
        if sec.get("section_num"):
            msg += f" شعبة {md(sec['section_num'])}"
        msg += "\n"
    send_long(message.chat.id, msg)


@bot.message_handler(commands=["stats"])
def cmd_stats(message):
    user = _require_user(message)
    if not user:
        return
    interval_mins = user.get("check_interval", config.DEFAULT_CHECK_INTERVAL) // 60
    last_check = user.get("last_check", "لم يتم بعد")
    if last_check != "لم يتم بعد":
        try:
            last_check = datetime.fromisoformat(last_check).strftime("%Y-%m-%d %H:%M")
        except Exception:
            pass
    watches = user.get("watches") or {}
    msg = f"""📈 *إحصائياتك:*

📊 الشعب في آخر لقطة: {len(user.get('sections', {}))}
👀 المراقبة: {len(watches)}

⏰ الفحص كل: {interval_mins} دقيقة
🕐 آخر فحص: {last_check}
🔄 عدد الفحوصات: {user.get('total_checks', 0)}

🆕 فتحات مكتشفة: {user.get('total_new', 0)}
❌ امتلاءات: {user.get('total_removed', 0)}
"""
    bot.send_message(message.chat.id, msg, parse_mode="Markdown")


@bot.message_handler(commands=["settings"])
def cmd_settings(message):
    user = _require_user(message)
    if not user:
        return
    interval_mins = user.get("check_interval", config.DEFAULT_CHECK_INTERVAL) // 60
    watches = user.get("watches") or {}
    msg = f"""⚙️ *إعداداتك:*

⏰ وقت الفحص: كل {interval_mins} دقيقة
👀 المراقبة: {len(watches)} / {config.MAX_WATCHES}

`/interval [دقائق]`
`/watch [معرف]`
"""
    bot.send_message(message.chat.id, msg, parse_mode="Markdown")


@bot.message_handler(commands=["interval"])
def cmd_interval(message):
    user = _require_user(message)
    if not user:
        return
    parts = message.text.split()
    if len(parts) < 2:
        bot.reply_to(message, "⚠️ أرسل الوقت بالدقائق\nمثال: `/interval 30`", parse_mode="Markdown")
        return
    try:
        minutes = int(parts[1])
    except ValueError:
        bot.reply_to(message, "⚠️ أرسل رقماً صحيحاً")
        return
    min_minutes = config.MIN_CHECK_INTERVAL // 60
    if minutes < min_minutes:
        bot.reply_to(message, f"⚠️ الحد الأدنى {min_minutes} دقيقة")
        return
    user["check_interval"] = minutes * 60
    save_user(message.chat.id, user)
    bot.reply_to(message, f"✅ تم تغيير وقت الفحص إلى كل {minutes} دقيقة")


@bot.message_handler(commands=["logout"])
def cmd_logout(message):
    if delete_user(message.chat.id):
        bot.reply_to(message, "✅ تم إلغاء تسجيلك. أرسل /start للتسجيل مرة أخرى.")
    else:
        bot.reply_to(message, "⚠️ أنت غير مسجل.")


@bot.message_handler(commands=["admin"])
def cmd_admin(message):
    if not is_admin(message.chat.id):
        return
    users = load_users()
    wait = edugate.backoff_remaining()
    backoff = f"⏸ backoff {wait}s" if wait else "جاهز"
    bot.send_message(
        message.chat.id,
        f"🔧 *لوحة المشرف:*\n\n"
        f"👥 المستخدمون: {len(users)}\n"
        f"🌐 إيدوجيت: {backoff}\n\n"
        f"/users\n/broadcast `[رسالة]`",
        parse_mode="Markdown",
    )


@bot.message_handler(commands=["users"])
def cmd_users(message):
    if not is_admin(message.chat.id):
        return
    users = load_users()
    if not users:
        bot.reply_to(message, "📭 لا يوجد مستخدمين مسجلين")
        return
    msg = f"👥 *المستخدمين ({len(users)}):*\n\n"
    for uid, data in users.items():
        interval = data.get("check_interval", config.DEFAULT_CHECK_INTERVAL) // 60
        sections = len(data.get("sections", {}))
        watches = len(data.get("watches") or {})
        msg += f"• `{uid}` ({sections} شعبة, {watches} مراقبة, كل {interval}د)\n"
    bot.send_message(message.chat.id, msg, parse_mode="Markdown")


@bot.message_handler(commands=["broadcast"])
def cmd_broadcast(message):
    if not is_admin(message.chat.id):
        return
    text = message.text.replace("/broadcast", "").strip()
    if not text:
        bot.reply_to(message, "⚠️ أرسل الرسالة بعد الأمر\nمثال: `/broadcast مرحباً!`", parse_mode="Markdown")
        return
    users = load_users()
    sent = 0
    for uid in users:
        try:
            bot.send_message(int(uid), f"📢 *رسالة من المشرف:*\n\n{text}", parse_mode="Markdown")
            sent += 1
        except Exception:
            pass
    bot.reply_to(message, f"✅ تم الإرسال إلى {sent}/{len(users)} مستخدم")


if __name__ == "__main__":
    Path(USERS_FILE).parent.mkdir(parents=True, exist_ok=True)
    Path(config.SESSION_FILE).parent.mkdir(parents=True, exist_ok=True)
    users = load_users()
    if users:
        save_users(users)
    session_ok = Path(config.SESSION_FILE).is_file()
    log.info("starting")
    log.info(
        "chats=%s  interval=%sm  jitter=±%ss  min=%sm  session_file=%s",
        len(users),
        config.DEFAULT_CHECK_INTERVAL // 60,
        config.CHECK_JITTER,
        config.MIN_CHECK_INTERVAL // 60,
        "yes" if session_ok else "no",
    )
    thread = threading.Thread(target=scheduler, daemon=True)
    thread.start()
    log.info("telegram polling  (Ctrl+C to stop)")
    bot.infinity_polling()
