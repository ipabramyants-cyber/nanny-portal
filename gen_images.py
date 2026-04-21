#!/usr/bin/env python3
from PIL import Image, ImageDraw, ImageFont
import os

OUT_DIR = "/home/user/nanny_app/static/contracts"

def wrap_text(text, font, draw, max_width):
    words = text.split()
    lines = []
    current = ""
    for word in words:
        test = (current + " " + word).strip()
        bbox = draw.textbbox((0, 0), test, font=font)
        if bbox[2] <= max_width:
            current = test
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines

def make_image(filename, title, sections, bg_color="#FFFFFF", accent="#D4AF37"):
    W, H = 900, 1200
    img = Image.new("RGB", (W, H), color=bg_color)
    draw = ImageDraw.Draw(img)

    # Background gradient effect (simple)
    for y in range(H):
        r = int(255 - (y / H) * 20)
        g = int(250 - (y / H) * 30)
        b = int(240 - (y / H) * 20)
        draw.line([(0, y), (W, y)], fill=(r, g, b))

    # Header bar
    draw.rectangle([(0, 0), (W, 120)], fill=accent)

    try:
        font_title = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 38)
        font_section = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 24)
        font_body = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 20)
        font_small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 16)
    except:
        font_title = ImageFont.load_default()
        font_section = font_title
        font_body = font_title
        font_small = font_title

    # Title
    draw.text((W // 2, 60), title, font=font_title, fill="white", anchor="mm")

    # Agency name subtitle
    draw.text((W // 2, 100), "Агентство Nanny Nha Trang", font=font_small, fill="white", anchor="mm")

    y_pos = 140
    for section in sections:
        if section.get("type") == "divider":
            draw.rectangle([(40, y_pos + 8), (W - 40, y_pos + 10)], fill=accent)
            y_pos += 25
            continue

        if section.get("type") == "header":
            draw.rectangle([(30, y_pos), (W - 30, y_pos + 40)], fill=accent + "33" if len(accent) == 7 else "#F5E6B0")
            draw.rectangle([(30, y_pos), (33, y_pos + 40)], fill=accent)
            draw.text((50, y_pos + 20), section["text"], font=font_section, fill="#333333", anchor="lm")
            y_pos += 55
            continue

        if section.get("type") == "item":
            # Bullet
            draw.ellipse([(48, y_pos + 7), (58, y_pos + 17)], fill=accent)
            lines = wrap_text(section["text"], font_body, draw, W - 130)
            for line in lines:
                draw.text((70, y_pos), line, font=font_body, fill="#333333")
                y_pos += 28
            if section.get("price"):
                draw.text((70, y_pos), section["price"], font=font_section, fill=accent)
                y_pos += 35
            y_pos += 5
            continue

        if section.get("type") == "note":
            draw.rectangle([(30, y_pos), (W - 30, y_pos + 8)], fill="#F0F0F0")
            lines = wrap_text(section["text"], font_small, draw, W - 100)
            for line in lines:
                draw.text((50, y_pos), line, font=font_small, fill="#888888")
                y_pos += 22
            y_pos += 10
            continue

    # Footer
    draw.rectangle([(0, H - 60), (W, H)], fill=accent)
    draw.text((W // 2, H - 30), "Nanny Nha Trang • nannynhatrang.ru", font=font_small, fill="white", anchor="mm")

    path = os.path.join(OUT_DIR, filename)
    img.save(path, "JPEG", quality=95)
    print(f"Saved: {path}")

# === IMAGE 1: ТАРИФЫ ===
make_image(
    "photo_doc_1.jpg",
    "ТАРИФЫ АГЕНТСТВА",
    [
        {"type": "header", "text": "РАЗОВЫЕ УСЛУГИ"},
        {"type": "item", "text": "Няня на день (до 8 часов)", "price": "от 2 500 ₽"},
        {"type": "item", "text": "Ночная няня (с 21:00 до 8:00)", "price": "от 3 000 ₽"},
        {"type": "item", "text": "Няня на мероприятие / праздник", "price": "от 1 500 ₽ / 4 часа"},
        {"type": "divider"},
        {"type": "header", "text": "АБОНЕМЕНТЫ"},
        {"type": "item", "text": "Абонемент 10 дней (до 8 часов)", "price": "22 000 ₽ / месяц"},
        {"type": "item", "text": "Абонемент 20 дней (до 8 часов)", "price": "42 000 ₽ / месяц"},
        {"type": "item", "text": "Постоянная няня (пн–пт)", "price": "от 45 000 ₽ / месяц"},
        {"type": "divider"},
        {"type": "header", "text": "ДОПОЛНИТЕЛЬНО"},
        {"type": "item", "text": "Трансфер (аэропорт / отель)", "price": "500 ₽"},
        {"type": "item", "text": "Дополнительный час", "price": "350 ₽ / час"},
        {"type": "item", "text": "Двое детей (+1 ребёнок)", "price": "+500 ₽ / день"},
        {"type": "divider"},
        {"type": "note", "text": "* Цены указаны для Нячанга. Выезд за город — доп. соглашение. НДС не облагается."},
    ]
)

# === IMAGE 2: РЕФЕРАЛЬНАЯ ПРОГРАММА ===
make_image(
    "photo_doc_2.jpg",
    "РЕФЕРАЛЬНАЯ ПРОГРАММА",
    [
        {"type": "header", "text": "КАК ЭТО РАБОТАЕТ?"},
        {"type": "item", "text": "1. Порекомендуйте агентство другу или знакомому"},
        {"type": "item", "text": "2. Ваш друг бронирует услугу и оплачивает"},
        {"type": "item", "text": "3. Вы получаете вознаграждение на карту"},
        {"type": "divider"},
        {"type": "header", "text": "ВАШИ БОНУСЫ"},
        {"type": "item", "text": "За каждого нового клиента", "price": "500 ₽ на счёт"},
        {"type": "item", "text": "За клиента с абонементом", "price": "1 000 ₽ на счёт"},
        {"type": "item", "text": "За 5 приглашённых клиентов", "price": "Скидка 10% на след. заказ"},
        {"type": "divider"},
        {"type": "header", "text": "УСЛОВИЯ"},
        {"type": "item", "text": "Вознаграждение выплачивается после первой оплаты приглашённого клиента"},
        {"type": "item", "text": "Реферальный бонус не суммируется с акционными скидками"},
        {"type": "item", "text": "Выплата в течение 3 рабочих дней после подтверждения"},
        {"type": "divider"},
        {"type": "note", "text": "Поделитесь своим Telegram username с нашим менеджером — мы всё организуем!"},
    ]
)

print("Done!")
