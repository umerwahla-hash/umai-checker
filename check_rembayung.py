import asyncio
import json
import os
import re
import smtplib
import sys
from email.message import EmailMessage
from datetime import datetime, timedelta
from playwright.async_api import async_playwright

BASE_URL = "https://reservation.umai.io/en/widget/rembayung"
PARTY_SIZE = 5
RECEIVER_EMAIL = "umerwahla@gmail.com"
STATE_FILE = ".umai_state.json"
SCREENSHOT_PATH = "umai_availability.png"

# Sat >= 2:00 PM and Sun <= 2:00 PM (minutes since midnight)
CUTOFF_MINUTES = 14 * 60

# Max time to sit in Cloudflare's virtual waiting room before giving up
WAITING_ROOM_TIMEOUT_S = 8 * 60

# Slots under a heading containing one of these words are not dine-in tables
EXCLUDED_SECTION_WORDS = ("takeaway",)

# Walks the widget's text in document order. The most recent non-button text
# before a time is treated as that slot's section heading.
EXTRACT_SLOTS_JS = r"""
() => {
  const out = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  const timeRe = /^\s*\d{1,2}:\d{2}\s*(AM|PM)?\s*$/i;
  let label = '';
  let n;
  while ((n = walker.nextNode())) {
    const text = n.textContent.trim();
    if (!text) continue;
    const parent = n.parentElement;
    const btn = parent ? parent.closest('button, [role=button], a') : null;
    if (timeRe.test(text)) {
      const disabled = !!(btn && (btn.disabled || btn.getAttribute('aria-disabled') === 'true'));
      out.push({ time: text, label: label, disabled: disabled });
    } else if (!btn) {
      label = text;
    }
  }
  return out;
}
"""


def get_next_weekend_dates(today=None):
    today = today or datetime.now()
    saturday = today + timedelta((5 - today.weekday()) % 7)
    sunday = saturday + timedelta(days=1)
    return {"Saturday": saturday, "Sunday": sunday}


def parse_time_to_minutes(time_str):
    """Parse '6:15 PM' / '12:45 PM' / '12:00 AM' / '18:15' into minutes since midnight."""
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*(AM|PM)?\s*$", time_str, re.IGNORECASE)
    if not m:
        return None
    hours, minutes, meridiem = int(m.group(1)), int(m.group(2)), m.group(3)
    if meridiem:
        meridiem = meridiem.upper()
        hours = hours % 12
        if meridiem == "PM":
            hours += 12
    return hours * 60 + minutes


def is_within_time_window(day_name, time_str):
    minutes = parse_time_to_minutes(time_str)
    if minutes is None:
        return False
    if day_name == "Saturday":
        return minutes >= CUTOFF_MINUTES
    if day_name == "Sunday":
        return minutes <= CUTOFF_MINUTES
    return False


def is_excluded_section(label):
    label = (label or "").lower()
    return any(word in label for word in EXCLUDED_SECTION_WORDS)


def load_state():
    try:
        with open(STATE_FILE) as f:
            return set(json.load(f).get("alerted", []))
    except (FileNotFoundError, ValueError):
        return set()


def save_state(alerted):
    with open(STATE_FILE, "w") as f:
        json.dump({"alerted": sorted(alerted)}, f)


def send_email_alert(subject, body, attachment_path):
    sender_email = os.environ.get("SENDER_EMAIL")
    sender_password = os.environ.get("SENDER_PASSWORD")

    if not sender_email or not sender_password:
        print("❌ Email credentials not found in environment variables. Cannot send email.")
        return False

    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = sender_email
    msg['To'] = RECEIVER_EMAIL
    msg.set_content(body)

    if os.path.exists(attachment_path):
        with open(attachment_path, 'rb') as f:
            img_data = f.read()
        msg.add_attachment(img_data, maintype='image', subtype='png', filename=os.path.basename(attachment_path))

    try:
        # Assumes the sender is using a Gmail account
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp:
            smtp.login(sender_email, sender_password)
            smtp.send_message(msg)
        print("✅ Email alert sent successfully!")
        return True
    except Exception as e:
        print(f"❌ Failed to send email: {e}")
        return False


async def wait_for_waiting_room(page):
    """Sit out Cloudflare's virtual queue. Does not try to get around it."""
    waited = 0
    while waited <= WAITING_ROOM_TIMEOUT_S:
        title = (await page.title()).lower()
        if "waiting room" in title:
            if waited == 0:
                print("In Cloudflare waiting room, waiting for our turn...")
            await page.wait_for_timeout(10_000)
            waited += 10
            continue
        if "just a moment" in title or "attention required" in title:
            raise RuntimeError("Blocked by a Cloudflare challenge page; cannot continue.")
        return
    raise RuntimeError("Timed out in Cloudflare waiting room.")


async def get_widget_frame(page, timeout_s=60):
    for _ in range(timeout_s):
        for frame in page.frames:
            if "letsumai.com" in frame.url:
                return frame
        await page.wait_for_timeout(1000)
    raise RuntimeError("UMAI widget iframe never appeared.")


async def fetch_slots(page, date, day_name):
    """Load the widget for one date and return [(time_text, section_label)] for dine-in slots."""
    date_param = date.strftime("%Y-%m-%d")
    url = f"{BASE_URL}?party_size={PARTY_SIZE}&date={date_param}"
    await page.goto(url, wait_until="domcontentloaded")
    await wait_for_waiting_room(page)

    frame = await get_widget_frame(page)

    # The form must show our party size and date, otherwise results are for the wrong query.
    expected_date = date.strftime("%d/%m/%Y")
    for _ in range(60):
        text = await frame.locator("body").inner_text()
        if f"{PARTY_SIZE} people" in text and expected_date in text:
            break
        await page.wait_for_timeout(1000)
    else:
        raise RuntimeError(f"Widget did not load for {PARTY_SIZE} people on {expected_date}.")

    # Let the slot list finish rendering after the form appears.
    await page.wait_for_timeout(3000)
    raw = await frame.evaluate(EXTRACT_SLOTS_JS)

    dine_in = []
    for slot in raw:
        tag = f"{day_name} {date_param} {slot['time']} [{slot['label'][:50]}]"
        if slot["disabled"]:
            print(f"  skip (disabled): {tag}")
        elif is_excluded_section(slot["label"]):
            print(f"  skip (takeaway): {tag}")
        else:
            print(f"  slot: {tag}")
            dine_in.append((slot["time"], slot["label"]))
    return dine_in


async def check_availability():
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Checking UMAI for {PARTY_SIZE} pax...")
    weekend_dates = get_next_weekend_dates()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 800, "height": 2400})

        try:
            found_slots = {}
            for day, date in weekend_dates.items():
                print(f"{day} {date.strftime('%Y-%m-%d')}:")
                for time_text, _label in await fetch_slots(page, date, day):
                    if is_within_time_window(day, time_text):
                        key = f"{day} {date.strftime('%Y-%m-%d')} at {time_text.strip()}"
                        found_slots[key] = date.strftime("%Y-%m-%d")

            previously_alerted = load_state()
            current = set(found_slots)
            new_slots = sorted(current - previously_alerted)

            if not current:
                print("❌ No matching dine-in slots found.")
                save_state(set())
            elif not new_slots:
                print(f"Slots still available but already alerted: {sorted(current)}")
            else:
                links = sorted({f"{BASE_URL}?party_size={PARTY_SIZE}&date={d}" for d in found_slots.values()})
                msg_body = (
                    f"Dine-in slots for {PARTY_SIZE} pax found between Sat 2pm and Sun 2pm!\n\n"
                    "New slots:\n" + "\n".join(new_slots) + "\n\nBook here:\n" + "\n".join(links)
                )
                print(msg_body)
                await page.screenshot(path=SCREENSHOT_PATH)
                if send_email_alert("UMAI Reservation Available!", msg_body, SCREENSHOT_PATH):
                    save_state(current)
                else:
                    sys.exit(1)

        except Exception as e:
            print(f"Script Error: {e}")
            sys.exit(1)

        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(check_availability())
