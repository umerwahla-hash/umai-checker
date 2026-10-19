import asyncio
import os
import re
import smtplib
import sys
from email.message import EmailMessage
from datetime import datetime, timedelta
from playwright.async_api import async_playwright

URL = "https://reservation.umai.io/en/widget/rembayung"
PARTY_SIZE = "5"
RECEIVER_EMAIL = "umerwahla@gmail.com"

def get_next_weekend_dates():
    today = datetime.now()
    saturday = today + timedelta((5 - today.weekday()) % 7)
    sunday = saturday + timedelta(days=1)
    return {
        "Saturday": saturday.strftime("%d"),
        "Sunday": sunday.strftime("%d")
    }

def is_within_time_window(day_name, time_str):
    clean_time = re.sub(r'[^0-9:]', '', time_str)
    if ":" in clean_time:
        hours, minutes = clean_time.split(":")
        time_val = int(hours) * 100 + int(minutes)
        if "PM" in time_str.upper() and int(hours) < 12:
            time_val += 1200
    else:
        return False

    if day_name == "Saturday" and time_val >= 1400:
        return True
    if day_name == "Sunday" and time_val <= 1400:
        return True
    
    return False

def send_email_alert(subject, body, attachment_path):
    sender_email = os.environ.get("umerwahla@gmail.com")
    sender_password = os.environ.get("cgoh jprq itrv fbzv")

    if not sender_email or not sender_password:
        print("❌ Email credentials not found in environment variables. Cannot send email.")
        return

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
    except Exception as e:
        print(f"❌ Failed to send email: {e}")

async def check_availability():
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Checking UMAI for 5 pax...")
    weekend_dates = get_next_weekend_dates()
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        
        try:
            await page.goto(URL)
            await page.wait_for_load_state("networkidle")
            
            guest_selector = "select[name*='guest'], select[name*='pax'], .guest-selector"
            if await page.locator(guest_selector).count() > 0:
                await page.select_option(guest_selector, PARTY_SIZE)
            
            await page.wait_for_timeout(2000)
            
            found_slots = []

            for day, date_str in weekend_dates.items():
                date_element = page.locator(f"text='{date_str}'").first
                if await date_element.is_visible():
                    await date_element.click()
                    await page.wait_for_timeout(2000)
                    
                    time_buttons = page.locator("button:has-text(':'), .time-slot")
                    count = await time_buttons.count()
                    
                    for i in range(count):
                        time_text = await time_buttons.nth(i).inner_text()
                        if is_within_time_window(day, time_text):
                            found_slots.append(f"{day} at {time_text.strip()}")
            
            if found_slots:
                msg_body = f"Slots for 5 pax found between Sat 2pm and Sun 2pm!\n\nDetails:\n" + "\n".join(found_slots)
                print(msg_body)
                
                # Take screenshot for validation
                screenshot_path = "umai_availability.png"
                await page.screenshot(path=screenshot_path)
                
                # Send the email
                send_email_alert("UMAI Reservation Available!", msg_body, screenshot_path)
            else:
                print("❌ No matching slots found.")
                
        except Exception as e:
            print(f"Script Error: {e}")
            sys.exit(1)
            
        finally:
            await browser.close()

if __name__ == "__main__":
    asyncio.run(check_availability())
