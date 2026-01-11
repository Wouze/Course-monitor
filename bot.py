import json
import time
import random
import re
import threading
from datetime import datetime
import requests
from bs4 import BeautifulSoup
import telebot
from telebot import types
import config

# --- Bot Setup ---
bot = telebot.TeleBot(config.BOT_TOKEN)
USERS_FILE = "users.json"

# --- URLs ---
LOGIN_URL = "https://edugate.ksu.edu.sa/ksu/ui/home.faces"
REGISTRATION_URL = "https://edugate.ksu.edu.sa/ksu/ui/student/registration/index/forwardMainReg.faces"
ADD_COURSES_URL = "https://edugate.ksu.edu.sa/ksu/addCourses"
BASE_URL = "https://edugate.ksu.edu.sa"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

# User states for registration flow
user_states = {}  # {chat_id: {'state': 'waiting_username'/'waiting_password', 'username': '...'}}


# --- User Data Management ---
def load_users():
    """Load all users from JSON file."""
    try:
        with open(USERS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_users(users):
    """Save all users to JSON file."""
    with open(USERS_FILE, 'w', encoding='utf-8') as f:
        json.dump(users, f, ensure_ascii=False, indent=2)


def get_user(chat_id):
    """Get a specific user by chat_id."""
    users = load_users()
    return users.get(str(chat_id))


def save_user(chat_id, user_data):
    """Save a specific user's data."""
    users = load_users()
    users[str(chat_id)] = user_data
    save_users(users)


def delete_user(chat_id):
    """Delete a user."""
    users = load_users()
    if str(chat_id) in users:
        del users[str(chat_id)]
        save_users(users)
        return True
    return False


def is_admin(chat_id):
    """Check if user is admin."""
    return chat_id == config.ADMIN_ID


# --- Edugate Functions ---
def fetch_courses_page(username, password):
    """Login and fetch the all courses page for a specific user."""
    session = requests.Session()
    session.headers.update(HEADERS)
    
    try:
        # Step 1: Get login page
        login_page = session.get(LOGIN_URL, timeout=30)
        soup = BeautifulSoup(login_page.text, 'html.parser')
        viewstate = soup.find("input", {"name": "javax.faces.ViewState"})
        if not viewstate:
            return None, "Could not find ViewState on login page"
        
        # Step 2: Login
        login_data = {
            "loginForm": "loginForm",
            "biConnectionConfig": "true",
            "token": "",
            "username": username,
            "password": password,
            "newsCode": "",
            "javax.faces.ViewState": viewstate["value"],
            "loginUsersLink": "loginUsersLink"
        }
        login_response = session.post(LOGIN_URL, data=login_data, timeout=30)
        
        # Check if login failed
        if "خطأ" in login_response.text or "error" in login_response.text.lower():
            return None, "Login failed - check credentials"
        
        # Step 3: Go to registration page
        reg_page = session.get(REGISTRATION_URL, timeout=30)
        soup = BeautifulSoup(reg_page.text, 'html.parser')
        viewstate = soup.find("input", {"name": "javax.faces.ViewState"})
        if not viewstate:
            return None, "Could not access registration page"
        
        # Step 4: Click the auto-load link
        post_data = {
            "myForm": "myForm",
            "javax.faces.ViewState": viewstate["value"],
            "myForm:serLinkDropAdd2": "myForm:serLinkDropAdd2"
        }
        session.post(REGISTRATION_URL, data=post_data, timeout=30)
        
        # Step 5: Click "Add Courses" button
        random_num = random.random()
        add_url = f"{ADD_COURSES_URL}?reg={random_num}"
        add_response = session.get(add_url, timeout=30)
        
        # Step 6: Follow JavaScript redirect
        match = re.search(r'window\.location\.replace\("([^"]+)"\)', add_response.text)
        if match:
            redirect_path = match.group(1)
            all_courses_url = BASE_URL + redirect_path
            all_courses = session.get(all_courses_url, timeout=30)
            return all_courses.text, None
        else:
            return None, "Could not find courses page redirect"
            
    except requests.Timeout:
        return None, "Connection timeout"
    except Exception as e:
        return None, str(e)


def parse_sections(html):
    """Parse the all courses page and extract section info."""
    soup = BeautifulSoup(html, 'html.parser')
    sections = {}
    
    for link in soup.find_all('a', onclick=lambda x: x and 'showToolTip(this,event,' in x):
        onclick = link.get('onclick', '')
        parts = re.findall(r"'([^']*)'", onclick)
        if len(parts) < 11:
            continue
        
        section_nums = parts[0].strip('-').split('-') if parts[0].strip('-') else []
        section_ids = parts[1].strip('-').split('-') if parts[1].strip('-') else []
        course_id = parts[6]
        doctor_names = [d.strip() for d in parts[10].split('@-@-@') if d.strip()]
        
        parent_tr = link.find_parent('tr')
        course_code = ""
        course_name = ""
        
        if parent_tr:
            tds = parent_tr.find_all('td')
            for td in tds:
                text = td.get_text(strip=True).replace('\xa0', ' ').strip()
                if re.match(r'^\d+\s+\S+$', text) and len(text) < 20:
                    course_code = text
                elif (len(text) > 5 and 
                      not text.startswith(('إبحث', 'إجبارية', 'إختيارية', 'انتظام')) and 
                      not text.isdigit() and
                      not re.match(r'^\d+\s+\S+$', text)):
                    if not course_name:
                        course_name = text
        
        for idx, (sec_num, sec_id) in enumerate(zip(section_nums, section_ids)):
            if sec_id:
                doctor = doctor_names[idx] if idx < len(doctor_names) else "غير معروف"
                key = f"{course_id}_{sec_id}"
                sections[key] = {
                    "course_id": course_id,
                    "course_code": course_code,
                    "course_name": course_name,
                    "section_num": sec_num,
                    "section_id": sec_id,
                    "doctor": doctor
                }
    
    return sections


def group_by_course(sections_list):
    """Group sections by course for display."""
    courses = {}
    for sec in sections_list:
        code = sec['course_code']
        if code not in courses:
            courses[code] = {'name': sec['course_name'], 'sections': []}
        courses[code]['sections'].append(sec)
    return courses


def check_user_sections(chat_id):
    """Check sections for a specific user and notify of changes."""
    user = get_user(chat_id)
    if not user:
        return
    
    print(f"🔍 Checking sections for user {chat_id}...")
    
    html, error = fetch_courses_page(user['username'], user['password'])
    if error:
        print(f"❌ Error for user {chat_id}: {error}")
        try:
            bot.send_message(chat_id, f"⚠️ *خطأ في الفحص:*\n`{error}`", parse_mode="Markdown")
        except:
            pass
        return
    
    current = parse_sections(html)
    saved = user.get('sections', {})
    
    print(f"📊 User {chat_id}: Found {len(current)} sections")
    
    # Find changes
    new_sections = [sec for key, sec in current.items() if key not in saved]
    removed_sections = [sec for key, sec in saved.items() if key not in current]
    
    # Update stats
    user['total_checks'] = user.get('total_checks', 0) + 1
    user['last_check'] = datetime.now().isoformat()
    if new_sections:
        user['total_new'] = user.get('total_new', 0) + len(new_sections)
    if removed_sections:
        user['total_removed'] = user.get('total_removed', 0) + len(removed_sections)
    
    # Send notifications
    if new_sections:
        msg = "🆕 *شعب جديدة متاحة!*\n\n"
        grouped = group_by_course(new_sections)
        for code, info in sorted(grouped.items()):
            msg += f"📚 *{code}* - {info['name']}\n"
            for sec in info['sections']:
                msg += f"   • شعبة {sec['section_num']} (ID: {sec['section_id']}) - {sec['doctor']}\n"
            msg += "\n"
        try:
            if len(msg) > 4000:
                for i in range(0, len(msg), 4000):
                    bot.send_message(chat_id, msg[i:i+4000], parse_mode="Markdown")
            else:
                bot.send_message(chat_id, msg, parse_mode="Markdown")
        except Exception as e:
            print(f"❌ Failed to send to {chat_id}: {e}")
    
    if removed_sections:
        msg = "❌ *شعب لم تعد متاحة (ممتلئة):*\n\n"
        grouped = group_by_course(removed_sections)
        for code, info in sorted(grouped.items()):
            msg += f"📚 *{code}* - {info['name']}\n"
            for sec in info['sections']:
                msg += f"   • شعبة {sec['section_num']} (ID: {sec['section_id']}) - {sec['doctor']}\n"
            msg += "\n"
        try:
            if len(msg) > 4000:
                for i in range(0, len(msg), 4000):
                    bot.send_message(chat_id, msg[i:i+4000], parse_mode="Markdown")
            else:
                bot.send_message(chat_id, msg, parse_mode="Markdown")
        except Exception as e:
            print(f"❌ Failed to send to {chat_id}: {e}")
    
    if not new_sections and not removed_sections:
        print(f"✅ User {chat_id}: No changes")
    
    # Save updated sections and stats
    user['sections'] = current
    save_user(chat_id, user)


def scheduler():
    """Run section check for all users based on their intervals."""
    user_last_check = {}  # {chat_id: timestamp}
    
    while True:
        users = load_users()
        now = time.time()
        
        for chat_id_str, user_data in users.items():
            chat_id = int(chat_id_str)
            base_interval = user_data.get('check_interval', config.DEFAULT_CHECK_INTERVAL)
            # Add random delay of ±60 seconds
            random_offset = random.randint(-60, 60)
            interval = base_interval + random_offset
            last = user_last_check.get(chat_id, 0)
            
            if now - last >= interval:
                try:
                    check_user_sections(chat_id)
                    user_last_check[chat_id] = now
                except Exception as e:
                    print(f"❌ Error checking user {chat_id}: {e}")
                time.sleep(2)  # Small delay between users
        
        time.sleep(60)  # Check every minute if any user needs checking


# --- Bot Commands ---
@bot.message_handler(commands=['start'])
def cmd_start(message):
    chat_id = message.chat.id
    user = get_user(chat_id)
    
    if user:
        interval_mins = user.get('check_interval', config.DEFAULT_CHECK_INTERVAL) // 60
        bot.reply_to(message, 
            f"👋 مرحباً! أنت مسجل بالفعل.\n\n"
            f"👤 المستخدم: `{user['username']}`\n"
            f"📊 الشعب المحفوظة: {len(user.get('sections', {}))}\n"
            f"⏰ الفحص كل: {interval_mins} دقيقة\n\n"
            f"أرسل /help لعرض الأوامر",
            parse_mode="Markdown"
        )
    else:
        user_states[chat_id] = {'state': 'waiting_username'}
        bot.reply_to(message,
            "👋 مرحباً بك في بوت مراقبة الشعب!\n\n"
            "سأساعدك في مراقبة الشعب المتاحة وإخبارك عند فتح شعب جديدة.\n\n"
            "📝 *للتسجيل، أرسل رقمك الجامعي:*",
            parse_mode="Markdown"
        )


@bot.message_handler(commands=['help'])
def cmd_help(message):
    chat_id = message.chat.id
    help_text = """📖 *أوامر البوت:*

*الأساسية:*
/start - بدء التسجيل
/check - فحص التغييرات الآن
/sections - عرض الشعب المتاحة
/stats - إحصائياتك
/settings - إعداداتك
/logout - إلغاء التسجيل

*الإعدادات:*
/interval [دقائق] - تغيير وقت الفحص
   مثال: `/interval 30`
   الحد الأدنى: 15 دقيقة

*كيف يعمل البوت:*
1️⃣ يفحص الشعب المتاحة بشكل دوري
2️⃣ يرسل إشعار عند فتح شعبة جديدة 🆕
3️⃣ يرسل إشعار عند امتلاء شعبة ❌
"""
    
    if is_admin(chat_id):
        help_text += """
*أوامر المشرف:*
/admin - لوحة التحكم
/users - قائمة المستخدمين
/broadcast [رسالة] - إرسال للجميع
"""
    
    bot.send_message(chat_id, help_text, parse_mode="Markdown")


@bot.message_handler(commands=['check'])
def cmd_check(message):
    chat_id = message.chat.id
    user = get_user(chat_id)
    
    if not user:
        bot.reply_to(message, "⚠️ أنت غير مسجل. أرسل /start للتسجيل.")
        return
    
    bot.reply_to(message, "🔍 جاري الفحص...")
    check_user_sections(chat_id)
    bot.send_message(chat_id, "✅ تم الفحص!")


@bot.message_handler(commands=['sections'])
def cmd_sections(message):
    chat_id = message.chat.id
    user = get_user(chat_id)
    
    if not user:
        bot.reply_to(message, "⚠️ أنت غير مسجل. أرسل /start للتسجيل.")
        return
    
    bot.reply_to(message, "📥 جاري جلب الشعب...")
    
    html, error = fetch_courses_page(user['username'], user['password'])
    if error:
        bot.send_message(chat_id, f"❌ *خطأ:* {error}", parse_mode="Markdown")
        return
    
    sections = parse_sections(html)
    user['sections'] = sections
    save_user(chat_id, user)
    
    courses = group_by_course(list(sections.values()))
    
    msg = f"📊 *الشعب المتاحة ({len(sections)} شعبة):*\n\n"
    for code, info in sorted(courses.items()):
        msg += f"📚 *{code}* - {info['name']}\n"
        for sec in info['sections']:
            msg += f"   • شعبة {sec['section_num']} (ID: {sec['section_id']}) - {sec['doctor']}\n"
        msg += "\n"
    
    if len(msg) > 4000:
        for i in range(0, len(msg), 4000):
            bot.send_message(chat_id, msg[i:i+4000], parse_mode="Markdown")
    else:
        bot.send_message(chat_id, msg, parse_mode="Markdown")


@bot.message_handler(commands=['stats'])
def cmd_stats(message):
    chat_id = message.chat.id
    user = get_user(chat_id)
    
    if not user:
        bot.reply_to(message, "⚠️ أنت غير مسجل. أرسل /start للتسجيل.")
        return
    
    interval_mins = user.get('check_interval', config.DEFAULT_CHECK_INTERVAL) // 60
    last_check = user.get('last_check', 'لم يتم بعد')
    if last_check != 'لم يتم بعد':
        try:
            dt = datetime.fromisoformat(last_check)
            last_check = dt.strftime('%Y-%m-%d %H:%M')
        except:
            pass
    
    msg = f"""📈 *إحصائياتك:*

👤 المستخدم: `{user['username']}`
📊 الشعب المتاحة: {len(user.get('sections', {}))}

⏰ الفحص كل: {interval_mins} دقيقة
🕐 آخر فحص: {last_check}
🔄 عدد الفحوصات: {user.get('total_checks', 0)}

🆕 شعب جديدة تم اكتشافها: {user.get('total_new', 0)}
❌ شعب امتلأت: {user.get('total_removed', 0)}
"""
    bot.send_message(chat_id, msg, parse_mode="Markdown")


@bot.message_handler(commands=['settings'])
def cmd_settings(message):
    chat_id = message.chat.id
    user = get_user(chat_id)
    
    if not user:
        bot.reply_to(message, "⚠️ أنت غير مسجل. أرسل /start للتسجيل.")
        return
    
    interval_mins = user.get('check_interval', config.DEFAULT_CHECK_INTERVAL) // 60
    
    msg = f"""⚙️ *إعداداتك:*

⏰ وقت الفحص: كل {interval_mins} دقيقة

*لتغيير وقت الفحص:*
`/interval [دقائق]`
مثال: `/interval 30`
الحد الأدنى: 15 دقيقة
"""
    bot.send_message(chat_id, msg, parse_mode="Markdown")


@bot.message_handler(commands=['interval'])
def cmd_interval(message):
    chat_id = message.chat.id
    user = get_user(chat_id)
    
    if not user:
        bot.reply_to(message, "⚠️ أنت غير مسجل. أرسل /start للتسجيل.")
        return
    
    parts = message.text.split()
    if len(parts) < 2:
        bot.reply_to(message, "⚠️ أرسل الوقت بالدقائق\nمثال: `/interval 30`", parse_mode="Markdown")
        return
    
    try:
        minutes = int(parts[1])
        min_minutes = config.MIN_CHECK_INTERVAL // 60
        
        if minutes < min_minutes:
            bot.reply_to(message, f"⚠️ الحد الأدنى {min_minutes} دقيقة")
            return
        
        user['check_interval'] = minutes * 60
        save_user(chat_id, user)
        bot.reply_to(message, f"✅ تم تغيير وقت الفحص إلى كل {minutes} دقيقة")
        
    except ValueError:
        bot.reply_to(message, "⚠️ أرسل رقماً صحيحاً")


@bot.message_handler(commands=['logout'])
def cmd_logout(message):
    chat_id = message.chat.id
    
    if delete_user(chat_id):
        if chat_id in user_states:
            del user_states[chat_id]
        bot.reply_to(message, "✅ تم إلغاء تسجيلك بنجاح. أرسل /start للتسجيل مرة أخرى.")
    else:
        bot.reply_to(message, "⚠️ أنت غير مسجل.")


# --- Admin Commands ---
@bot.message_handler(commands=['admin'])
def cmd_admin(message):
    chat_id = message.chat.id
    if not is_admin(chat_id):
        return
    
    users = load_users()
    msg = f"""🔧 *لوحة المشرف:*

👥 عدد المستخدمين: {len(users)}
🤖 حالة البوت: يعمل ✅

*الأوامر:*
/users - قائمة المستخدمين
/broadcast [رسالة] - إرسال للجميع
"""
    bot.send_message(chat_id, msg, parse_mode="Markdown")


@bot.message_handler(commands=['users'])
def cmd_users(message):
    chat_id = message.chat.id
    if not is_admin(chat_id):
        return
    
    users = load_users()
    if not users:
        bot.reply_to(message, "📭 لا يوجد مستخدمين مسجلين")
        return
    
    msg = f"👥 *المستخدمين ({len(users)}):*\n\n"
    for uid, data in users.items():
        interval = data.get('check_interval', config.DEFAULT_CHECK_INTERVAL) // 60
        sections = len(data.get('sections', {}))
        msg += f"• `{uid}` - {data['username']} ({sections} شعبة, كل {interval}د)\n"
    
    bot.send_message(chat_id, msg, parse_mode="Markdown")


@bot.message_handler(commands=['broadcast'])
def cmd_broadcast(message):
    chat_id = message.chat.id
    if not is_admin(chat_id):
        return
    
    text = message.text.replace('/broadcast', '').strip()
    if not text:
        bot.reply_to(message, "⚠️ أرسل الرسالة بعد الأمر\nمثال: `/broadcast مرحباً!`", parse_mode="Markdown")
        return
    
    users = load_users()
    sent = 0
    for uid in users:
        try:
            bot.send_message(int(uid), f"📢 *رسالة من المشرف:*\n\n{text}", parse_mode="Markdown")
            sent += 1
        except:
            pass
    
    bot.reply_to(message, f"✅ تم الإرسال إلى {sent}/{len(users)} مستخدم")


@bot.message_handler(func=lambda m: m.chat.id in user_states)
def handle_registration(message):
    """Handle registration flow messages."""
    chat_id = message.chat.id
    state = user_states.get(chat_id, {})
    text = message.text.strip()
    
    if state.get('state') == 'waiting_username':
        user_states[chat_id] = {'state': 'waiting_password', 'username': text}
        bot.reply_to(message, 
            f"✅ الرقم الجامعي: `{text}`\n\n"
            f"🔑 *الآن أرسل كلمة المرور:*",
            parse_mode="Markdown"
        )
    
    elif state.get('state') == 'waiting_password':
        username = state['username']
        password = text
        
        try:
            bot.delete_message(chat_id, message.message_id)
        except:
            pass
        
        bot.send_message(chat_id, "🔄 جاري التحقق من بياناتك...")
        
        html, error = fetch_courses_page(username, password)
        
        if error:
            del user_states[chat_id]
            bot.send_message(chat_id, 
                f"❌ *فشل تسجيل الدخول:*\n`{error}`\n\n"
                f"أرسل /start للمحاولة مرة أخرى.",
                parse_mode="Markdown"
            )
            return
        
        sections = parse_sections(html)
        save_user(chat_id, {
            'username': username,
            'password': password,
            'sections': sections,
            'check_interval': config.DEFAULT_CHECK_INTERVAL,
            'registered_at': datetime.now().isoformat(),
            'total_checks': 0,
            'total_new': 0,
            'total_removed': 0
        })
        del user_states[chat_id]
        
        bot.send_message(chat_id,
            f"✅ *تم التسجيل بنجاح!*\n\n"
            f"👤 المستخدم: `{username}`\n"
            f"📊 تم العثور على {len(sections)} شعبة متاحة\n\n"
            f"سأخبرك تلقائياً عند:\n"
            f"🆕 فتح شعب جديدة\n"
            f"❌ امتلاء شعب موجودة\n\n"
            f"أرسل /help لعرض الأوامر",
            parse_mode="Markdown"
        )


# --- Main ---
if __name__ == "__main__":
    print("🤖 Multi-User Section Monitor Bot starting...")
    users = load_users()
    print(f"📬 {len(users)} registered users")
    print(f"👑 Admin ID: {config.ADMIN_ID}")
    
    # Start scheduler in background
    thread = threading.Thread(target=scheduler, daemon=True)
    thread.start()
    
    # Start bot
    print("✅ Bot is running! Press Ctrl+C to stop.")
    bot.infinity_polling()