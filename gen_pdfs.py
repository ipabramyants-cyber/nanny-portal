#!/usr/bin/env python3
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
import os

OUT_DIR = "/home/user/nanny_app/static/contracts"

# Try register Cyrillic font
try:
    pdfmetrics.registerFont(TTFont("DejaVu", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"))
    pdfmetrics.registerFont(TTFont("DejaVuBold", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"))
    FONT = "DejaVu"
    FONT_BOLD = "DejaVuBold"
except:
    FONT = "Helvetica"
    FONT_BOLD = "Helvetica-Bold"

GOLD = colors.HexColor("#D4AF37")
DARK = colors.HexColor("#1a1a1a")
GRAY = colors.HexColor("#666666")
LIGHT = colors.HexColor("#F9F5E7")


def base_styles():
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("Title2", fontName=FONT_BOLD, fontSize=18, textColor=DARK,
                                  spaceAfter=6, alignment=1)
    subtitle_style = ParagraphStyle("Subtitle", fontName=FONT, fontSize=11, textColor=GRAY,
                                     spaceAfter=16, alignment=1)
    heading_style = ParagraphStyle("Heading2", fontName=FONT_BOLD, fontSize=13, textColor=DARK,
                                    spaceBefore=14, spaceAfter=6)
    body_style = ParagraphStyle("Body2", fontName=FONT, fontSize=10, textColor=DARK,
                                 leading=16, spaceAfter=4)
    small_style = ParagraphStyle("Small", fontName=FONT, fontSize=8, textColor=GRAY, leading=12)
    return title_style, subtitle_style, heading_style, body_style, small_style


def add_header(story, title, subtitle, t, s, h, b, sm):
    story.append(Paragraph(title, t))
    story.append(Paragraph(subtitle, s))
    story.append(HRFlowable(width="100%", thickness=2, color=GOLD))
    story.append(Spacer(1, 0.3 * cm))


def make_contract(filename, title, subtitle, content_fn):
    path = os.path.join(OUT_DIR, filename)
    doc = SimpleDocTemplate(path, pagesize=A4,
                            leftMargin=2*cm, rightMargin=2*cm,
                            topMargin=2*cm, bottomMargin=2*cm)
    t, s, h, b, sm = base_styles()
    story = []
    add_header(story, title, subtitle, t, s, h, b, sm)
    content_fn(story, h, b, sm)
    doc.build(story)
    print(f"Saved: {path}")


def contract_agency_parents(story, h, b, sm):
    story.append(Paragraph("г. Нячанг, Вьетнам", sm))
    story.append(Spacer(1, 0.4*cm))

    sections = [
        ("1. ПРЕДМЕТ ДОГОВОРА",
         "1.1. Агентство обязуется оказать услуги по подбору и предоставлению квалифицированного персонала (няни) для ухода за детьми Клиента.\n"
         "1.2. Услуги оказываются на основании заявки Клиента, поданной через Telegram-бот или по телефону.\n"
         "1.3. Агентство гарантирует соответствие персонала заявленным квалификационным требованиям."),

        ("2. ПРАВА И ОБЯЗАННОСТИ СТОРОН",
         "2.1. Агентство обязуется:\n"
         "— предоставить няню в согласованное время;\n"
         "— обеспечить замену в случае отмены менее чем за 2 часа до начала;\n"
         "— соблюдать конфиденциальность данных Клиента.\n\n"
         "2.2. Клиент обязуется:\n"
         "— своевременно производить оплату услуг;\n"
         "— обеспечить безопасные условия работы для персонала;\n"
         "— при необходимости предоставить информацию об особенностях детей."),

        ("3. СТОИМОСТЬ И ПОРЯДОК ОПЛАТЫ",
         "3.1. Стоимость услуг определяется действующим прайс-листом агентства.\n"
         "3.2. Оплата производится наличными или переводом до начала оказания услуги.\n"
         "3.3. При отмене менее чем за 2 часа до начала взимается штраф в размере 30% от стоимости."),

        ("4. ОТВЕТСТВЕННОСТЬ СТОРОН",
         "4.1. Агентство несёт ответственность за качество подбора персонала.\n"
         "4.2. Клиент несёт ответственность за имущество, переданное персоналу в пользование.\n"
         "4.3. Агентство не несёт ответственности за вред, причинённый вследствие предоставления Клиентом недостоверной информации."),

        ("5. КОНФИДЕНЦИАЛЬНОСТЬ",
         "5.1. Стороны обязуются сохранять конфиденциальность персональных данных, полученных в ходе исполнения договора.\n"
         "5.2. Фотографии и данные детей не передаются третьим лицам."),

        ("6. СРОК ДЕЙСТВИЯ И РАСТОРЖЕНИЕ",
         "6.1. Договор вступает в силу с момента подписания и действует бессрочно.\n"
         "6.2. Каждая из сторон вправе расторгнуть договор, уведомив другую сторону за 3 дня."),
    ]

    for sec_title, sec_body in sections:
        story.append(Paragraph(sec_title, h))
        for para in sec_body.split("\n\n"):
            story.append(Paragraph(para.replace("\n", "<br/>"), b))
        story.append(Spacer(1, 0.2*cm))

    story.append(HRFlowable(width="100%", thickness=1, color=GOLD))
    story.append(Spacer(1, 0.5*cm))

    sig_data = [
        ["АГЕНТСТВО", "КЛИЕНТ"],
        ["Nanny Nha Trang", "ФИО: ___________________"],
        ["ИП / представитель", "Телефон: ___________________"],
        ["Подпись: __________", "Подпись: __________"],
        ["Дата: __________", "Дата: __________"],
    ]
    sig_table = Table(sig_data, colWidths=[8*cm, 8*cm])
    sig_table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, 0), FONT_BOLD),
        ("FONTNAME", (0, 1), (-1, -1), FONT),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("TEXTCOLOR", (0, 0), (-1, 0), GOLD),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    story.append(sig_table)


def contract_nanny_parents(story, h, b, sm):
    story.append(Paragraph("г. Нячанг, Вьетнам", sm))
    story.append(Spacer(1, 0.4*cm))

    sections = [
        ("1. ПРЕДМЕТ ДОГОВОРА",
         "1.1. Настоящий договор регулирует отношения между Родителями и Няней при оказании услуг по уходу за детьми.\n"
         "1.2. Няня обязуется оказывать услуги по уходу, воспитанию и развитию детей в соответствии с инструкциями Родителей."),

        ("2. ОБЯЗАННОСТИ НЯНи",
         "2.1. Обеспечивать безопасность ребёнка в течение всего времени нахождения с ним.\n"
         "2.2. Кормить ребёнка согласно инструкциям Родителей.\n"
         "2.3. Организовывать развивающие занятия и прогулки.\n"
         "2.4. Незамедлительно сообщать Родителям о любых нештатных ситуациях.\n"
         "2.5. Соблюдать режим дня ребёнка."),

        ("3. ОБЯЗАННОСТИ РОДИТЕЛЕЙ",
         "3.1. Предоставить Няне всю необходимую информацию об особенностях и здоровье ребёнка.\n"
         "3.2. Обеспечить Няню всем необходимым для ухода за ребёнком.\n"
         "3.3. Своевременно производить оплату услуг согласно договорённости.\n"
         "3.4. Сообщить об имеющихся аллергиях, хронических заболеваниях ребёнка."),

        ("4. ОПЛАТА",
         "4.1. Оплата производится согласно тарифам агентства Nanny Nha Trang.\n"
         "4.2. При досрочном завершении работы Родителями оплата производится за полный заказанный период.\n"
         "4.3. Дополнительные расходы (транспорт, питание няни) оговариваются отдельно."),

        ("5. КОНФИДЕНЦИАЛЬНОСТЬ И БЕЗОПАСНОСТЬ",
         "5.1. Няня обязуется не разглашать персональные данные семьи третьим лицам.\n"
         "5.2. Фото и видео детей не публикуются без явного согласия Родителей.\n"
         "5.3. Родители не привлекают Няню к работам, не связанным с уходом за детьми."),

        ("6. ФОРС-МАЖОР И ОТМЕНА",
         "6.1. При отмене заказа менее чем за 2 часа — штраф 30% от стоимости.\n"
         "6.2. При болезни Няни агентство обязуется предоставить замену или вернуть оплату.\n"
         "6.3. Форс-мажорные обстоятельства (стихийные бедствия, чрезвычайные ситуации) освобождают стороны от ответственности."),
    ]

    for sec_title, sec_body in sections:
        story.append(Paragraph(sec_title, h))
        for para in sec_body.split("\n\n"):
            story.append(Paragraph(para.replace("\n", "<br/>"), b))
        story.append(Spacer(1, 0.2*cm))

    story.append(HRFlowable(width="100%", thickness=1, color=GOLD))
    story.append(Spacer(1, 0.5*cm))

    sig_data = [
        ["РОДИТЕЛИ / КЛИЕНТ", "НЯНЯ"],
        ["ФИО: ___________________", "ФИО: ___________________"],
        ["Телефон: ___________________", "Телефон: ___________________"],
        ["Подпись: __________", "Подпись: __________"],
        ["Дата: __________", "Дата: __________"],
    ]
    sig_table = Table(sig_data, colWidths=[8*cm, 8*cm])
    sig_table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, 0), FONT_BOLD),
        ("FONTNAME", (0, 1), (-1, -1), FONT),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("TEXTCOLOR", (0, 0), (-1, 0), GOLD),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    story.append(sig_table)


def package_clients(story, h, b, sm):
    story.append(Spacer(1, 0.3*cm))

    # Package table
    pkg_data = [
        ["ПАКЕТ", "ОПИСАНИЕ", "СТОИМОСТЬ"],
        ["СТАРТ", "5 дней × 8 часов\nНяня на дом\nОтчёт родителям", "11 500 ₽"],
        ["КОМФОРТ", "10 дней × 8 часов\nНяня + развив. занятия\nПриоритетная замена", "22 000 ₽"],
        ["ПРЕМИУМ", "20 дней × 8 часов\nОпытная няня\nПедагогическое образование\nЕжедневный отчёт", "42 000 ₽"],
        ["VIP", "Полный месяц пн–пт\nЛичная няня семьи\nГибкий график\nАбонентская поддержка", "от 55 000 ₽"],
    ]
    pkg_table = Table(pkg_data, colWidths=[3.5*cm, 8*cm, 4*cm])
    pkg_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), GOLD),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), FONT_BOLD),
        ("FONTNAME", (0, 1), (-1, -1), FONT),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("BACKGROUND", (0, 1), (-1, 1), LIGHT),
        ("BACKGROUND", (0, 3), (-1, 3), LIGHT),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT]),
        ("ALIGN", (2, 0), (2, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 10),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#DDDDDD")),
        ("FONTNAME", (0, 1), (0, -1), FONT_BOLD),
        ("TEXTCOLOR", (0, 1), (0, -1), GOLD),
    ]))
    story.append(pkg_table)
    story.append(Spacer(1, 0.5*cm))

    story.append(Paragraph("ЧТО ВКЛЮЧЕНО В ЛЮБОЙ ПАКЕТ", h))
    includes = [
        "✓ Верификация и проверка анкеты няни",
        "✓ Страховка ответственности агентства",
        "✓ Бесплатная замена при форс-мажоре",
        "✓ Поддержка менеджера 7 дней в неделю",
        "✓ Договор с каждой семьёй",
    ]
    for item in includes:
        story.append(Paragraph(item, b))

    story.append(Spacer(1, 0.3*cm))
    story.append(Paragraph("КАК ОФОРМИТЬ ПАКЕТ", h))
    steps = [
        "1. Напишите нам в Telegram @Nannynhatrang_bot",
        "2. Выберите пакет и удобное время",
        "3. Подпишите договор (электронно или лично)",
        "4. Оплатите удобным способом",
        "5. Ваша няня прибудет в назначенное время",
    ]
    for step in steps:
        story.append(Paragraph(step, b))

    story.append(Spacer(1, 0.4*cm))
    story.append(HRFlowable(width="100%", thickness=1, color=GOLD))
    story.append(Spacer(1, 0.2*cm))
    story.append(Paragraph(
        "Контакты: @Nannynhatrang_bot | nannynhatrang.ru | Нячанг, Вьетнам",
        sm
    ))


# Generate all three PDFs
make_contract(
    "contract_agency_parents.pdf",
    "ДОГОВОР НА ОКАЗАНИЕ УСЛУГ",
    "Агентство Nanny Nha Trang — Клиент (Родители)",
    contract_agency_parents
)

make_contract(
    "contract_nanny_parents.pdf",
    "ДОГОВОР НАЙМА НЯНИ",
    "Родители — Няня | Агентство Nanny Nha Trang",
    contract_nanny_parents
)

make_contract(
    "package_clients.pdf",
    "ПАКЕТЫ УСЛУГ",
    "Агентство Nanny Nha Trang — Нячанг, Вьетнам",
    package_clients
)

print("All PDFs generated!")
