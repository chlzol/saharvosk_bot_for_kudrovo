import calendar
import importlib
from datetime import datetime, date, timedelta

try:
    from services import booking_data
    RAW_BRANCHES = booking_data.BRANCHES
    CATEGORIES = booking_data.CATEGORIES
    SERVICES = booking_data.SERVICES
    MASTERS = booking_data.MASTERS
except ImportError:
    RAW_BRANCHES, CATEGORIES, SERVICES, MASTERS = {}, {}, {}, {}


def reload_booking_data():
    global RAW_BRANCHES, CATEGORIES, SERVICES, MASTERS
    try:
        from services import booking_data as bd_mod
        importlib.reload(bd_mod)
        RAW_BRANCHES = bd_mod.BRANCHES
        CATEGORIES = bd_mod.CATEGORIES
        SERVICES = bd_mod.SERVICES
        MASTERS = bd_mod.MASTERS
    except Exception:
        pass


BASE_TIME_SLOTS = ["10:00", "11:30", "13:00", "14:30", "16:00", "17:30", "19:00", "20:30"]

BRANCH_OVERRIDES = {
    "prazhskaya": {
        "id": "prazhskaya",
        "title": "Пражская ул., 3",
        "short_title": "Пражская, 3",
        "address": "г. Кудрово, Пражская ул., 3",
        "phone": "+7 (812) 988-98-53",
        "lat": 59.908861,
        "lon": 30.509854,
        "yandex_map_url": "https://yandex.ru/maps/118515/kudrovo/?ll=30.510682%2C59.908655&mode=poi&poi%5Bpoint%5D=30.509854%2C59.908861&poi%5Buri%5D=ymapsbm1%3A%2F%2Forg%3Foid%3D205293396818&z=18.95",
        "yandex_review_url": "https://yandex.ru/maps/118515/kudrovo/?add-review=true&ll=30.510682%2C59.908655&mode=poi&poi%5Bpoint%5D=30.509854%2C59.908861&poi%5Buri%5D=ymapsbm1%3A%2F%2Forg%3Foid%3D205293396818&tab=reviews&z=18.95"
    },
    "leningradskaya": {
        "id": "leningradskaya",
        "title": "Ленинградская ул., 7",
        "short_title": "Ленинградская, 7",
        "address": "г. Кудрово, Ленинградская ул., 7",
        "phone": "+7 (812) 988-98-53",
        "lat": 59.914521,
        "lon": 30.513750,
        "yandex_map_url": "https://yandex.ru/maps/118515/kudrovo/?ll=30.513180%2C59.914316&mode=poi&poi%5Bpoint%5D=30.513750%2C59.914521&poi%5Buri%5D=ymapsbm1%3A%2F%2Forg%3Foid%3D122184351844&z=18.83",
        "yandex_review_url": "https://yandex.ru/maps/118515/kudrovo/?add-review=true&ll=30.513834%2C59.914549&mode=poi&poi%5Bpoint%5D=30.513750%2C59.914521&poi%5Buri%5D=ymapsbm1%3A%2F%2Forg%3Foid%3D122184351844&z=19.46"
    }
}

MAIN_CATEGORIES_CONFIG = {
    "promo": {
        "title": "🔥 Акции и Выгодные предложения",
        "keywords": [
            "акции для новых", "выгодные предложения", "первое посещение",
            "1-й визит", "2-й визит", "3-й визит", "скидка -", "вместо "
        ]
    },
    "hair_removal": {
        "title": "✨ Удаление волос (Лазер / Шугаринг / Воск)",
        "keywords": [
            "лазер", "shr", "шугаринг", "сахар", "полимер",
            "skins", "скинс", "ваксинг", "воск", "депиляц", "эпиляц"
        ]
    },
    "manicure": {
        "title": "💅 Маникюр",
        "keywords": [
            "маникюр", "наращивание длинных ногтей", "наращивание коротких ногтей",
            "коррекция длинных нарощенных", "коррекция коротких нарощенных",
            "снятие нарощенных ногтей", "укрепление ногтей", "дизайн 1 ногтя",
            "кошачий глаз", "втирка", "френч", "парафиновый spa"
        ]
    },
    "pedicure": {
        "title": "🦶 Педикюр",
        "keywords": [
            "педикюр", "обработка стоп", "обработка натоптыша",
            "обработка мозоли", "обработка трещин", "вросший ноготь"
        ]
    },
    "brows": {
        "title": "🤨 Оформление бровей",
        "keywords": ["бров"]
    },
    "lashes": {
        "title": "👁 Ресницы",
        "keywords": ["ресниц"]
    },
    "massage": {
        "title": "💆‍♀️ Массаж, SPA и Коррекция фигуры",
        "keywords": [
            "массаж", "лимфодренаж", "антицеллюлит", "полный релакс",
            "прессотерап", "кедровая бочка", "обертыван", "обёртыван",
            "detox", "мегаскульптор", "влезу в s-ку", "бой с целлюлитом",
            "для тех, кто устал", "stop морщинам", "wow-эффект"
        ]
    },
    "cosmetology": {
        "title": "🌸 Эстетическая косметология",
        "keywords": [
            "косметолог", "чистка лица", "пилинг поверхностный",
            "карбокситерап", "christina", "фарфоровая кожа",
            "витапластика", "микротоковый", "маска по типу кожи"
        ]
    },
    "men": {
        "title": "🧔 Мужской зал",
        "keywords": ["для мужчин", "мужск"]
    },
    "certs_extra": {
        "title": "🎁 Сертификаты, Обучение и Доп. услуги",
        "keywords": [
            "сертификат", "обучение депиляц", "курс \"",
            "дополнения к услугам", "мастер без слов", "ранний выход",
            "поздний выход", "лист ожидания", "обратный звонок",
            "оставить обращение", "перезвоните мне"
        ]
    },
    "other": {
        "title": "📋 Другое",
        "keywords": []
    }
}

PROMO_SUBCATEGORIES = {
    "pr_visits": {
        "title": "🎁 Скидки на 1-й, 2-й и 3-й визит",
        "keywords": ["1-й визит", "2-й визит", "3-й визит"]
    },
    "pr_laser": {
        "title": "⚡ Лазерная SHR-эпиляция (-50% на первое посещение)",
        "keywords": ["лазер", "shr"]
    },
    "pr_cosmet": {
        "title": "🌸 Акции на Косметологию и Массаж лица",
        "keywords": ["преображение", "сияние и детокс", "пилинг", "карбокситерап", "вместо", "косметолог"]
    },
    "other": {
        "title": "📋 Другое",
        "keywords": []
    }
}

HAIR_REMOVAL_SUBCATEGORIES = {
    "hr_laser": {
        "title": "⚡ Лазерная SHR-эпиляция (выбор зоны)",
        "keywords": ["лазер", "shr", "бритье 1 зоны"],
        "direct_services": True
    },
    "hr_shugaring": {
        "title": "🍯 Шугаринг (Сахарная депиляция)",
        "keywords": ["шугаринг", "сахар"],
        "has_sub2": True
    },
    "hr_wax": {
        "title": "🕯 Полимерный воск / Skins / Ваксинг",
        "keywords": ["полимер", "skins", "скинс", "ваксинг", "воск"],
        "has_sub2": True
    },
    "other": {
        "title": "📋 Другое",
        "keywords": []
    }
}

DEPIL_ZONES_SUB2 = {
    "dz_comp": {
        "title": "📦 Выгодные комплексы (Комплекс 1, 2, 3)",
        "keywords": ["комплекс"]
    },
    "dz_bikini": {
        "title": "👙 Бикини (глубокое и классика)",
        "keywords": ["бикини"]
    },
    "dz_underarms_arms": {
        "title": "🙌 Подмышечные впадины и Руки",
        "keywords": ["подмышечные", "подмышк", "руки"]
    },
    "dz_legs": {
        "title": "🦵 Ноги (до колен, выше колен, полностью)",
        "keywords": ["ноги"]
    },
    "dz_face_neck": {
        "title": "✨ Лицо и Шея",
        "keywords": ["область лица", "лицо полностью", "шея", "над губой", "подбородок"]
    },
    "dz_body": {
        "title": "🧘‍♀️ Тело (живот, дорожка, спина, поясница, ягодицы, ареолы)",
        "keywords": ["дорожка", "живот", "спина", "поясница", "ягодицы", "ареолы", "грудь"]
    },
    "dz_care": {
        "title": "🌿 Энзимный пилинг против вросших волос",
        "keywords": ["энзимный пилинг", "вросших волос"]
    },
    "all": {
        "title": "📋 Показать все зоны списком",
        "keywords": []
    }
}

MANICURE_SUBCATEGORIES = {
    "m_cov_expert": {
        "title": "💅 Маникюр с покрытием (Мастер-эксперт)",
        "keywords": []
    },
    "m_cov_top": {
        "title": "⭐ Маникюр с покрытием (ТОП-мастер)",
        "keywords": []
    },
    "m_clean": {
        "title": "🤲 Маникюр без покрытия и детский маникюр",
        "keywords": ["маникюр без покрытия", "детский маникюр"]
    },
    "m_ext_corr": {
        "title": "💎 Наращивание и коррекция нарощенных ногтей",
        "keywords": [
            "наращивание длинных ногтей", "наращивание коротких ногтей",
            "коррекция длинных нарощенных", "коррекция коротких нарощенных"
        ]
    },
    "m_strength": {
        "title": "🛡 Укрепление ногтей (гелем / акриловой пудрой)",
        "keywords": ["укрепление ногтей"]
    },
    "m_design": {
        "title": "🎨 Дизайн ногтей, френч, втирка, кошачий глаз",
        "keywords": [
            "простой дизайн", "сложный дизайн", "кошачий глаз",
            "втирка, блестки", "френч, лунки"
        ]
    },
    "m_remove_spa": {
        "title": "🔧 Снятие покрытия, Парафиновый SPA и услуги в 4 руки",
        "keywords": [
            "снятие гель-лака", "снятие нарощенных ногтей",
            "снятие покрытия лак", "парафиновый spa", "в 4 руки", "в 6 рук"
        ]
    },
    "other": {
        "title": "📋 Другое",
        "keywords": []
    }
}

PEDICURE_SUBCATEGORIES = {
    "p_full_expert": {
        "title": "🦶 Педикюр с покрытием и без (Мастер-эксперт)",
        "keywords": []
    },
    "p_full_top": {
        "title": "🌟 Педикюр с покрытием и без (ТОП-мастер)",
        "keywords": []
    },
    "p_express_stops": {
        "title": "⚡ Экспресс-педикюр (пальчики) и обработка стоп",
        "keywords": ["экспресс-педикюр", "обработка стоп и пяток"]
    },
    "p_podology": {
        "title": "🩺 Подология (вросший ноготь, мозоли, натоптыши, трещины)",
        "keywords": [
            "обработка натоптыша", "обработка мозоли",
            "обработка трещин", "вросший ноготь"
        ]
    },
    "p_remove_4hands": {
        "title": "🔧 Снятие покрытия и комплексы в 4–6 рук",
        "keywords": [
            "снятие гель-лака", "снятие покрытия лак", "в 4 руки", "в 6 рук"
        ]
    },
    "other": {
        "title": "📋 Другое",
        "keywords": []
    }
}

BROWS_SUBCATEGORIES = {
    "br_arch_tint": {
        "title": "🤨 Архитектура, коррекция и окрашивание бровей",
        "keywords": [
            "архитектура + окрашивание бровей", "архитектура бровей",
            "окрашивание бровей"
        ]
    },
    "br_lami": {
        "title": "✨ Долговременная укладка (ламинирование) бровей",
        "keywords": [
            "долговременная укладка (ламинирование) бровей",
            "долговоременная укладка бровей"
        ]
    },
    "br_comp": {
        "title": "📦 Комплекс «Брови + Ресницы» и услуги в 4–6 рук",
        "keywords": [
            "комплекс: долговременная укладка бровей",
            "услуги бровиста в 4 или 6 рук", "услуги бровиста в 6 рук"
        ]
    },
    "br_care": {
        "title": "🌿 Осветление, прореживание, маска для бровей и воск лица",
        "keywords": [
            "осветление бровей", "прореживание бровей", "маска для бровей",
            "обработка воском зоны над губой", "парафиновый spa"
        ]
    },
    "other": {
        "title": "📋 Другое",
        "keywords": []
    }
}

LASHES_SUBCATEGORIES = {
    "ls_classic_2d": {
        "title": "👁 Наращивание: Классика (1D), 1.5D, 2D, Экспресс, LED",
        "keywords": [
            "классика / 1d", "полуторный объем / 1,5d", "двойной объем / 2d",
            "экспресс наращивание ресниц", "лэд наращивание ресниц"
        ]
    },
    "ls_volume": {
        "title": "👁 Наращивание: Объём 2.5D, 3D и Гипер-объём 4D / 5D",
        "keywords": [
            "2,5d", "тройной объем / 3d", "гипер-объем / 4d", "гипер-объем / 5d"
        ]
    },
    "ls_effects": {
        "title": "💎 Эффекты (мокрый, лучи, Ким), изгибы M/L, цветные и стразы",
        "keywords": ["изгибы m, l", "эффекты: мокрый эффект"]
    },
    "ls_lami_comp": {
        "title": "💫 Ламинирование, ботокс, окрашивание и комплекс с бровями",
        "keywords": [
            "ламинирование ресниц", "окрашивание ресниц",
            "комплекс: долговременная укладка бровей"
        ]
    },
    "ls_remove": {
        "title": "💧 Снятие нарощенных ресниц",
        "keywords": ["снятие нарощенных ресниц"]
    },
    "other": {
        "title": "📋 Другое",
        "keywords": []
    }
}

MASSAGE_SUBCATEGORIES = {
    "m_classic_relax": {
        "title": "👐 Общий классический, релакс и комбинированный массаж",
        "keywords": [
            "общий классический", "полный релакс", "комбинированный 2-хчасовой",
            "массаж на все 100"
        ]
    },
    "m_back_local": {
        "title": "🧘‍♀️ Массаж спины, ШВЗ, стоп и головы",
        "keywords": [
            "массаж спины", "массаж швз", "массаж стоп",
            "массаж головы", "бирманский релакс"
        ]
    },
    "m_slim_lymph": {
        "title": "🍊 Антицеллюлитный, лимфодренажный и WOW-массаж",
        "keywords": [
            "антицеллюлитный", "лимфодренажный", "моделирующий wow-массаж"
        ]
    },
    "m_sport": {
        "title": "🏋️‍♀️ Спортивный массаж тела",
        "keywords": ["спортивный массаж"]
    },
    "m_hardware": {
        "title": "⚡ Аппаратный массаж и прессотерапия",
        "keywords": ["аппаратный массаж", "прессотерапия"]
    },
    "m_spa": {
        "title": "🌿 SPA: DETOX Мегаскульптор, Кедровая бочка и обёртывания",
        "keywords": [
            "кедровая бочка", "обертывание", "обёртывание",
            "detox", "мегаскульптор"
        ]
    },
    "m_face": {
        "title": "✨ Массаж лица (скульптурный, Кобидо, буккальный, RF, маски)",
        "keywords": [
            "классический массаж лица", "скульптурный массаж лица",
            "моделирующий массаж лица", "альгинатная маска", "rf-лифтинг лица",
            "массаж лица +", "массаж лица кобидо", "массаж криосферами",
            "буккальные техники"
        ]
    },
    "m_couples": {
        "title": "💑 Парный массаж для двоих («Свидание», «Релакс», «СПА»)",
        "keywords": [
            "парный массаж", "для двоих", "2-х человек в одном кабинете"
        ]
    },
    "m_kids_preg": {
        "title": "🤰 Массаж для беременных, детей и подростков",
        "keywords": [
            "для беременных", "детский массаж", "для подростков"
        ]
    },
    "m_abon_prog": {
        "title": "🎫 Абонементы, программы коррекции и Консультация",
        "keywords": [
            "абонемент массаж", "программа \"", "влезу в s-ку", "wow-эффект",
            "бой с целлюлитом", "для тех, кто устал", "stop морщинам", "консультация"
        ]
    },
    "other": {
        "title": "📋 Другое",
        "keywords": []
    }
}

COSMETOLOGY_SUBCATEGORIES = {
    "c_promo_consult": {
        "title": "🔥 Выгодные акции и Консультация косметолога",
        "keywords": ["вместо", "консультация"]
    },
    "c_clean": {
        "title": "🧼 Чистка лица (комбинированная, УЗ, атравматическая, гальваническая)",
        "keywords": ["чистка лица", "уз-чистка"]
    },
    "c_peel_carboxy": {
        "title": "🧪 Пилинги и неинвазивная карбокситерапия",
        "keywords": [
            "пилинг поверхностный", "неинвазивная карбокситерапия",
            "омолаживающая программа с пилингом"
        ]
    },
    "c_care_christina": {
        "title": "🧖‍♀️ Комплексные уходы Christina, маски и «Фарфоровая кожа»",
        "keywords": [
            "мгновенное преображение", "сияние и детокс", "фарфоровая кожа",
            "восстанавливающий уход", "маска по типу кожи"
        ]
    },
    "c_hardware_3d": {
        "title": "⚡ Микротоки и 3D-моделирование лица (Витапластика)",
        "keywords": ["микротоковый", "витапластика", "3д моделирование"]
    },
    "c_abon": {
        "title": "🎫 Абонементы к косметологу (на 5 и 10 процедур)",
        "keywords": ["абонемент"]
    },
    "other": {
        "title": "📋 Другое",
        "keywords": []
    }
}

MEN_SUBCATEGORIES = {
    "men_laser": {
        "title": "⚡ Лазерная эпиляция",
        "keywords": ["лазерная shr-эпиляция"],
        "direct_services": True
    },
    "men_depil": {
        "title": "🍯 Депиляция (Шугаринг / Воск)",
        "keywords": ["шугаринг ", "полимер "],
        "has_sub2": True
    },
    "men_manic": {
        "title": "💅 Маникюр",
        "keywords": ["мужской маникюр"],
        "direct_services": True
    },
    "men_pedic": {
        "title": "🦶 Педикюр",
        "keywords": ["мужской педикюр"],
        "direct_services": True
    },
    "men_massage": {
        "title": "💆‍♂️ Массаж",
        "keywords": ["массаж"],
        "direct_services": True
    },
    "men_brows": {
        "title": "🤨 Коррекция бровей",
        "keywords": ["мужская коррекция бровей"],
        "direct_services": True
    },
    "other": {
        "title": "📋 Другое",
        "keywords": []
    }
}

MEN_DEPIL_SUB2 = {
    "md_shug": {
        "title": "🍯 Шугаринг для мужчин",
        "keywords": ["шугаринг"]
    },
    "md_wax": {
        "title": "🕯 Полимерный воск для мужчин",
        "keywords": ["полимер", "воск", "ваксинг"]
    }
}

CERTS_EXTRA_SUBCATEGORIES = {
    "ce_certs": {
        "title": "🎁 Подарочные сертификаты (500 – 10 000 ₽ и на массаж)",
        "keywords": ["подарочный сертификат", "сертификат"]
    },
    "ce_edu": {
        "title": "🎓 Обучение депиляции (Курсы шугаринга и воска)",
        "keywords": ["курс \"", "базовый шугаринг", "углубленный шугаринг", "восковая депиляция", "идеальное бикини", "вросшие волосы"]
    },
    "ce_addons": {
        "title": "➕ Дополнения к услугам (Мастер без слов, в 4 руки, ранний/поздний выход)",
        "keywords": ["мастер без слов", "услуги в 4 руки", "ранний выход", "поздний выход", "в одном кабинете", "услуги бровиста"]
    },
    "ce_callback": {
        "title": "📞 Заказать звонок / Лист ожидания / Оставить обращение",
        "keywords": ["обратный звонок", "лист ожидания", "оставить обращение", "перезвоните мне"]
    },
    "other": {
        "title": "📋 Другое",
        "keywords": []
    }
}

OTHER_SUBCATEGORIES = {
    "other": {
        "title": "📋 Другое",
        "keywords": []
    }
}

EXCLUDED_MEN_MASSAGE_KEYWORDS = [
    "антицеллюлит", "аппаратн", "прессотерап", "detox", "детокс",
    "мегаскульптор", "обертыван", "обёртыван", "беременн", "детский",
    "подростк", "влезу в s-ку", "бой с целлюлитом", "wow-эффект",
    "stop морщинам", "для тех, кто устал", "моделирующий wow-массаж",
    "абонемент", "альгинатная маска 25 мин", "rf-лифтинг"
]

STANDALONE_ADDON_KEYWORDS = [
    "с последующим", "дизайн 1 ногтя", "сложный дизайн", "простой дизайн",
    "врасший ноготь", "в 4 руки", "в 6 рук"
]


def _service_in_branch(service: dict, branch_id: str) -> bool:
    branches = service.get("branches")
    if branches:
        return branch_id in branches
    return service.get("branch_id") == branch_id


def _get_service_full_text(service: dict) -> str:
    srv_title = (service.get("title") or "").lower()
    cat_title = (CATEGORIES.get(str(service.get("cat_id")), "") or "").lower()
    return f"{cat_title} {srv_title}"


def _is_male_service(service: dict) -> bool:
    cat_id = str(service.get("cat_id", ""))
    if cat_id in ("14065336", "14065319", "14065321"):
        return True
    srv_title = (service.get("title") or "").strip().lower()
    cat_title = (CATEGORIES.get(cat_id, "") or "").lower()
    if "для мужчин" in cat_title or "мужск" in srv_title or srv_title.endswith(" м"):
        return True
    return False


def _is_allowed_men_massage(service: dict) -> bool:
    cat_id = str(service.get("cat_id", ""))
    if cat_id not in ("14065328", "14065327", "14065325"):
        return False
    srv_title = (service.get("title") or "").lower()
    for bad_kw in EXCLUDED_MEN_MASSAGE_KEYWORDS:
        if bad_kw in srv_title:
            return False
    return True


def is_standalone_addon_service(service: dict) -> bool:
    """Защита: определяет, является ли услуга зависимой доп. опцией (снятие, дизайн), которую нельзя выбирать первой."""
    title = (service.get("title") or "").lower()
    for kw in STANDALONE_ADDON_KEYWORDS:
        if kw in title:
            return True
    return False


def _match_service_to_main_categories(service: dict) -> set[str]:
    cat_id = str(service.get("cat_id", ""))
    srv_title = (service.get("title") or "").lower()
    full_text = _get_service_full_text(service)
    matched = set()

    if cat_id == "14066119":
        matched.add("promo")
    elif cat_id == "16708600":
        matched.add("promo")
        matched.add("cosmetology")
    elif cat_id == "15369406":
        matched.add("promo")
    elif cat_id in ("3977589", "14065334", "14065318", "14065314", "14065317"):
        if "абонемент" not in srv_title:
            matched.add("hair_removal")
    elif cat_id in ("14065328", "14065327", "14065325", "15369407"):
        matched.add("massage")
        if _is_allowed_men_massage(service):
            matched.add("men")
    elif cat_id == "14066120":
        matched.add("cosmetology")
    elif cat_id == "14065316":
        matched.add("brows")
        if "ресниц" in srv_title:
            matched.add("lashes")
        if "мужская" in srv_title:
            matched.add("men")
    elif cat_id == "14065331":
        matched.add("lashes")
    elif cat_id in ("14065322", "14065324"):
        matched.add("manicure")
        if "мужской" in srv_title:
            matched.add("men")
    elif cat_id in ("14065329", "14065330"):
        matched.add("pedicure")
        if "мужской" in srv_title:
            matched.add("men")
    elif cat_id == "14065326":
        matched.add("manicure")
        matched.add("pedicure")
        if "бровиста" in srv_title:
            matched.add("brows")
    elif cat_id in ("14065336", "14065319", "14065321"):
        matched.add("men")
    elif cat_id in ("15369412", "15369410", "18880115", "15369409"):
        matched.add("certs_extra")
        if "массаж 2-х человек" in srv_title:
            matched.add("massage")
        if "бровиста" in srv_title:
            matched.add("brows")

    if not matched:
        if _is_male_service(service):
            matched.add("men")
        for main_id, cfg in MAIN_CATEGORIES_CONFIG.items():
            if main_id in ("other", "men"):
                continue
            for kw in cfg["keywords"]:
                if kw in full_text:
                    if main_id == "promo" and "снятие в подарок" in srv_title:
                        continue
                    matched.add(main_id)
                    break

    if not matched:
        matched.add("other")

    return matched


def _get_subcat_rules_for_main(main_cat_id: str) -> dict:
    mapping = {
        "promo": PROMO_SUBCATEGORIES,
        "hair_removal": HAIR_REMOVAL_SUBCATEGORIES,
        "manicure": MANICURE_SUBCATEGORIES,
        "pedicure": PEDICURE_SUBCATEGORIES,
        "brows": BROWS_SUBCATEGORIES,
        "lashes": LASHES_SUBCATEGORIES,
        "massage": MASSAGE_SUBCATEGORIES,
        "cosmetology": COSMETOLOGY_SUBCATEGORIES,
        "men": MEN_SUBCATEGORIES,
        "certs_extra": CERTS_EXTRA_SUBCATEGORIES,
        "other": OTHER_SUBCATEGORIES
    }
    return mapping.get(main_cat_id, OTHER_SUBCATEGORIES)


def _match_service_to_subcategories(service: dict, main_cat_id: str) -> set[str]:
    rules = _get_subcat_rules_for_main(main_cat_id)
    cat_id = str(service.get("cat_id", ""))
    srv_title = (service.get("title") or "").lower()
    full_text = _get_service_full_text(service)
    matched = set()

    if main_cat_id == "hair_removal":
        if "абонемент" in srv_title or cat_id == "15369406":
            return set()
        if cat_id == "3977589" or "лазер" in srv_title or "shr" in srv_title or "бритье 1 зоны" in srv_title:
            matched.add("hr_laser")
        elif cat_id == "14065334" or "шугаринг" in srv_title or "сахар" in srv_title:
            matched.add("hr_shugaring")
        elif cat_id in ("14065318", "14065314", "14065317") or any(k in srv_title for k in ("полимер", "skins", "ваксинг", "воск")):
            matched.add("hr_wax")
        else:
            matched.add("other")
        return matched

    if main_cat_id == "manicure":
        if any(kw in srv_title for kw in MANICURE_SUBCATEGORIES["m_remove_spa"]["keywords"]) and "маникюр с покрытием" not in srv_title:
            matched.add("m_remove_spa")
        if any(kw in srv_title for kw in MANICURE_SUBCATEGORIES["m_design"]["keywords"]):
            matched.add("m_design")
        if any(kw in srv_title for kw in MANICURE_SUBCATEGORIES["m_strength"]["keywords"]):
            matched.add("m_strength")
        if any(kw in srv_title for kw in MANICURE_SUBCATEGORIES["m_ext_corr"]["keywords"]):
            matched.add("m_ext_corr")
        if any(kw in srv_title for kw in MANICURE_SUBCATEGORIES["m_clean"]["keywords"]):
            matched.add("m_clean")
        if ("маникюр с покрытием" in srv_title or "покрытие лаком" in srv_title) and "наращивание" not in srv_title and "коррекция" not in srv_title and "детский" not in srv_title:
            if cat_id == "14065324":
                matched.add("m_cov_top")
            else:
                matched.add("m_cov_expert")
        if not matched:
            matched.add("other")
        return matched

    if main_cat_id == "pedicure":
        if any(kw in srv_title for kw in PEDICURE_SUBCATEGORIES["p_podology"]["keywords"]):
            matched.add("p_podology")
        if any(kw in srv_title for kw in PEDICURE_SUBCATEGORIES["p_express_stops"]["keywords"]):
            matched.add("p_express_stops")
        if srv_title.startswith("снятие ") or "в 4 руки" in srv_title or "в 6 рук" in srv_title:
            matched.add("p_remove_4hands")
        if "педикюр" in srv_title and "экспресс" not in srv_title and not srv_title.startswith("снятие ") and "в 4 руки" not in srv_title and "в 6 рук" not in srv_title and "вросший" not in srv_title and "мужской" not in srv_title:
            if cat_id == "14065330":
                matched.add("p_full_top")
            else:
                matched.add("p_full_expert")
        if not matched:
            matched.add("other")
        return matched

    if main_cat_id == "massage":
        is_megasculptor = "мегаскульптор" in srv_title or "detox" in srv_title
        for sub_id, cfg in rules.items():
            if sub_id == "other":
                continue
            if sub_id == "m_hardware" and is_megasculptor:
                continue
            for kw in cfg["keywords"]:
                if kw in srv_title:
                    matched.add(sub_id)
                    break
        if not matched:
            matched.add("other")
        return matched

    if main_cat_id == "men":
        if cat_id == "14065321" or ("лазер" in srv_title and _is_male_service(service)):
            if "абонемент" not in srv_title:
                matched.add("men_laser")
        elif cat_id in ("14065336", "14065319") or (("шугаринг" in srv_title or "полимер" in srv_title) and _is_male_service(service)):
            matched.add("men_depil")
        elif "мужской маникюр" in srv_title:
            matched.add("men_manic")
        elif "мужской педикюр" in srv_title:
            matched.add("men_pedic")
        elif "мужская коррекция бровей" in srv_title:
            matched.add("men_brows")
        elif _is_allowed_men_massage(service):
            matched.add("men_massage")
        elif _is_male_service(service):
            matched.add("other")
        return matched

    target_text = full_text if main_cat_id == "promo" else srv_title
    for sub_id, cfg in rules.items():
        if sub_id == "other":
            continue
        for kw in cfg["keywords"]:
            if kw in target_text:
                matched.add(sub_id)
                break

    if not matched:
        matched.add("other")

    return matched


def _match_service_to_sub2(service: dict, main_cat_id: str, subcat_id: str) -> set[str]:
    srv_title = (service.get("title") or "").lower()
    cat_id = str(service.get("cat_id", ""))
    matched = set()

    if main_cat_id == "men" and subcat_id == "men_depil":
        if cat_id == "14065336" or "шугаринг" in srv_title:
            matched.add("md_shug")
        if cat_id == "14065319" or "полимер" in srv_title or "воск" in srv_title:
            matched.add("md_wax")
        return matched

    if main_cat_id == "hair_removal" and subcat_id in ("hr_shugaring", "hr_wax"):
        if "комплекс" in srv_title:
            matched.add("dz_comp")
            return matched
        for s2_id, cfg in DEPIL_ZONES_SUB2.items():
            if s2_id in ("dz_comp", "all"):
                continue
            for kw in cfg["keywords"]:
                if kw in srv_title:
                    matched.add(s2_id)
                    break
        return matched

    return {"all"}


def _format_service_for_display(service: dict, subcat_id: str) -> dict:
    cat_id = str(service.get("cat_id", ""))
    if subcat_id not in ("m_cov_expert", "m_cov_top", "p_full_expert", "p_full_top"):
        if cat_id in ("14065322", "14065329"):
            s_copy = dict(service)
            s_copy["title"] = f"[Эксперт] {service['title']}"
            return s_copy
        elif cat_id in ("14065324", "14065330"):
            s_copy = dict(service)
            s_copy["title"] = f"[ТОП] {service['title']}"
            return s_copy
    return service


def get_branches() -> dict:
    result = {}
    for b_key, override in BRANCH_OVERRIDES.items():
        raw = RAW_BRANCHES.get(b_key, {})
        result[b_key] = {
            "id": b_key,
            "company_id": raw.get("company_id", 0),
            "title": override["title"],
            "short_title": override["short_title"],
            "address": override["address"],
            "phone": override["phone"],
            "lat": override["lat"],
            "lon": override["lon"],
            "yandex_map_url": override["yandex_map_url"],
            "yandex_review_url": override["yandex_review_url"]
        }
    return result


def get_memo_keys_for_appointment(cat_id: str, service_title: str) -> tuple[str, str]:
    title_low = (service_title or "").lower()
    if "лазер" in title_low or "shr" in title_low:
        return "memo_prep_laser", "memo_after_laser"
    if any(k in title_low for k in ("шугаринг", "сахар", "полимер", "воск", "ваксинг", "skins", "депиляц")) or cat_id == "hair_removal":
        return "memo_prep_depil", "memo_after_depil"
    return "memo_prep_general", "memo_after_general"


def get_categories_by_branch(branch_id: str) -> dict:
    counts = {k: 0 for k in MAIN_CATEGORIES_CONFIG}
    for s in SERVICES.values():
        if not _service_in_branch(s, branch_id):
            continue
        for m_id in _match_service_to_main_categories(s):
            if m_id in counts:
                counts[m_id] += 1

    return {
        m_id: cfg["title"]
        for m_id, cfg in MAIN_CATEGORIES_CONFIG.items()
        if counts.get(m_id, 0) > 0
    }


def get_main_category_title(main_cat_id: str) -> str:
    if main_cat_id in MAIN_CATEGORIES_CONFIG:
        return MAIN_CATEGORIES_CONFIG[main_cat_id]["title"]
    return CATEGORIES.get(str(main_cat_id), "Направление")


def get_subcategories_by_category(branch_id: str, main_cat_id: str) -> dict:
    rules = _get_subcat_rules_for_main(main_cat_id)
    counts = {sub_id: 0 for sub_id in rules}
    total_in_main = 0

    for s in SERVICES.values():
        if not _service_in_branch(s, branch_id):
            continue
        if main_cat_id not in _match_service_to_main_categories(s):
            continue

        matched_subs = _match_service_to_subcategories(s, main_cat_id)
        if matched_subs:
            total_in_main += 1
        for sub_id in matched_subs:
            if sub_id in counts:
                counts[sub_id] += 1

    result = {
        sub_id: cfg["title"]
        for sub_id, cfg in rules.items()
        if counts.get(sub_id, 0) > 0
    }

    if total_in_main > 0 and main_cat_id not in ("men", "hair_removal"):
        result["all"] = "📋 Все услуги направления"

    return result


def is_subcategory_direct(main_cat_id: str, subcat_id: str) -> bool:
    if subcat_id == "all":
        return True
    rules = _get_subcat_rules_for_main(main_cat_id)
    cfg = rules.get(subcat_id, {})
    return not cfg.get("has_sub2", False)


def get_sub2_options(branch_id: str, main_cat_id: str, subcat_id: str) -> dict:
    if main_cat_id == "men" and subcat_id == "men_depil":
        rules = MEN_DEPIL_SUB2
    elif main_cat_id == "hair_removal" and subcat_id in ("hr_shugaring", "hr_wax"):
        rules = DEPIL_ZONES_SUB2
    else:
        return {}

    counts = {k: 0 for k in rules}
    for s in SERVICES.values():
        if not _service_in_branch(s, branch_id):
            continue
        if main_cat_id not in _match_service_to_main_categories(s):
            continue
        if subcat_id not in _match_service_to_subcategories(s, main_cat_id):
            continue
        for s2_id in _match_service_to_sub2(s, main_cat_id, subcat_id):
            if s2_id in counts:
                counts[s2_id] += 1

    result = {
        s2_id: cfg["title"]
        for s2_id, cfg in rules.items()
        if s2_id == "all" or counts.get(s2_id, 0) > 0
    }
    return result


def get_subcategory_title(main_cat_id: str, subcat_id: str, sub2_id: str | None = None) -> str:
    if subcat_id == "all":
        return "Все услуги направления"
    rules = _get_subcat_rules_for_main(main_cat_id)
    base_title = rules.get(subcat_id, {}).get("title", "Категория")
    if sub2_id and sub2_id != "all":
        if main_cat_id == "men" and subcat_id == "men_depil":
            s2_title = MEN_DEPIL_SUB2.get(sub2_id, {}).get("title", "")
            return f"{base_title} ➔ {s2_title}" if s2_title else base_title
        if main_cat_id == "hair_removal":
            s2_title = DEPIL_ZONES_SUB2.get(sub2_id, {}).get("title", "")
            return f"{base_title} ➔ {s2_title}" if s2_title else base_title
    return base_title


def get_services_by_subcategory(
    branch_id: str,
    main_cat_id: str,
    subcat_id: str,
    sub2_id: str | None = None
) -> list[dict]:
    result = []
    for s in SERVICES.values():
        if not _service_in_branch(s, branch_id):
            continue
        if main_cat_id not in _match_service_to_main_categories(s):
            continue
        if subcat_id != "all" and subcat_id not in _match_service_to_subcategories(s, main_cat_id):
            continue
        if sub2_id and sub2_id != "all":
            if sub2_id not in _match_service_to_sub2(s, main_cat_id, subcat_id):
                continue
        result.append(_format_service_for_display(s, subcat_id))
    return result


def get_services_by_category(branch_id: str, cat_id: str) -> list[dict]:
    if cat_id in MAIN_CATEGORIES_CONFIG:
        return get_services_by_subcategory(branch_id, cat_id, "all")
    return [
        s for s in SERVICES.values()
        if _service_in_branch(s, branch_id) and str(s.get("cat_id")) == str(cat_id)
    ]


def get_service_by_id(service_id: str) -> dict | None:
    return SERVICES.get(str(service_id))


def _get_allowed_staff_set_for_service(branch_id: str, service_id: str) -> set[str]:
    branch_master_ids = {
        str(m["id"]) for m in MASTERS.values()
        if branch_id in m.get("branches", [])
    }
    service = get_service_by_id(service_id)
    if not service:
        return branch_master_ids

    staff_by_branch = service.get("staff_by_branch", {})
    if branch_id in staff_by_branch and staff_by_branch[branch_id]:
        s_ids = {str(x) for x in staff_by_branch[branch_id]} & branch_master_ids
        if s_ids:
            return s_ids

    allowed_staff_ids = {str(x) for x in service.get("staff_ids", [])} & branch_master_ids
    if allowed_staff_ids:
        return allowed_staff_ids

    return branch_master_ids


def get_masters(branch_id: str, service_ids: str | list[str] | None = None) -> list[dict]:
    branch_masters = [
        m for m in MASTERS.values()
        if branch_id in m.get("branches", [])
    ]
    if not service_ids:
        return branch_masters

    if isinstance(service_ids, str):
        srv_list = [s.strip() for s in service_ids.split(",") if s.strip()]
    else:
        srv_list = [str(s).strip() for s in service_ids if str(s).strip()]

    if not srv_list:
        return branch_masters

    common_staff_ids: set[str] | None = None
    for sid in srv_list:
        s_set = _get_allowed_staff_set_for_service(branch_id, sid)
        if common_staff_ids is None:
            common_staff_ids = s_set
        else:
            common_staff_ids = common_staff_ids & s_set

    if common_staff_ids:
        return [m for m in branch_masters if str(m["id"]) in common_staff_ids]

    return []


def can_combine_services(branch_id: str, current_service_ids: list[str], new_service_id: str) -> tuple[bool, str | None]:
    if not current_service_ids:
        # Защита: нельзя начинать корзину с доп. опции (снятие, дизайн, вросший ноготь)
        new_srv = get_service_by_id(new_service_id)
        if new_srv and is_standalone_addon_service(new_srv):
            return (
                False,
                "⚠️ Эту процедуру нельзя выбрать первой.\n"
                "Сначала выберите основную услугу (например, наращивание, покрытие или педикюр), а затем добавьте эту опцию."
            )
        return True, None

    if str(new_service_id) in [str(x) for x in current_service_ids]:
        return False, "Эта процедура уже добавлена в ваш список."

    combined = [str(x) for x in current_service_ids] + [str(new_service_id)]
    common_masters = get_masters(branch_id, combined)
    if not common_masters:
        return (
            False,
            "⚠️ Эту услугу выполняет другой профильный специалист, поэтому объединить их в один сеанс нельзя.\n\n"
            "Завершите текущую запись, а затем оформите вторую процедуру отдельно."
        )
    return True, None


def get_cart_summary(service_ids: list[str]) -> dict:
    services = []
    for sid in service_ids:
        s = get_service_by_id(sid)
        if s:
            services.append(s)

    total_price_num = sum(int(s.get("price", 0)) for s in services)
    total_price = f"{total_price_num} ₽" if total_price_num > 0 else "по прайсу"
    total_duration = sum(int(s.get("duration", 30)) for s in services)
    titles_inline = " + ".join(s["title"] for s in services)
    bullet_lines = "\n".join(
        f"{idx}. **{s['title']}** — {s['price']} ₽ ({s['duration']} мин.)"
        for idx, s in enumerate(services, start=1)
    )
    return {
        "services": services,
        "count": len(services),
        "total_price_num": total_price_num,
        "total_price": total_price,
        "total_duration": total_duration if total_duration > 0 else 30,
        "titles_inline": titles_inline or "Услуга",
        "bullet_lines": bullet_lines or "• Услуга"
    }


def get_master_name(master_id: str) -> str:
    if str(master_id) == "any":
        return "Любой свободный специалист"
    master = MASTERS.get(str(master_id))
    return master["name"] if master else "Специалист"


def generate_catalog_report() -> str:
    lines = [
        "==========================================================",
        "ПОЛНЫЙ КАТАЛОГ УСЛУГ Sahar&Vosk (ПРАЖСКАЯ И ЛЕНИНГРАДСКАЯ)",
        f"Всего услуг: {len(SERVICES)} | Категорий YCLIENTS: {len(CATEGORIES)} | Мастеров: {len(MASTERS)}",
        "==========================================================\n"
    ]

    for cat_id, cat_title in CATEGORIES.items():
        cat_services = [s for s in SERVICES.values() if str(s.get("cat_id")) == str(cat_id)]
        if not cat_services:
            continue

        lines.append(f"=== КАТЕГОРИЯ YCLIENTS [{cat_id}]: {cat_title} ({len(cat_services)} усл.) ===")
        for s in cat_services:
            br_list = s.get("branches", [s.get("branch_id", "")])
            br_tags = []
            if "prazhskaya" in br_list:
                br_tags.append("Пражская")
            if "leningradskaya" in br_list:
                br_tags.append("Ленинградская")
            br_str = ", ".join(br_tags)

            main_cats = _match_service_to_main_categories(s)
            sub_info = []
            for mc in main_cats:
                subs = _match_service_to_subcategories(s, mc)
                sub_info.append(f"{mc} -> {','.join(subs)}")

            staff_cnt_pr = len(s.get("staff_by_branch", {}).get("prazhskaya", []))
            staff_cnt_len = len(s.get("staff_by_branch", {}).get("leningradskaya", []))

            lines.append(
                f"  • [{s['id']}] {s['title']} | {s['price']} ₽ | {s['duration']} мин. | "
                f"Салоны: [{br_str}] | Мастеров (Пр:{staff_cnt_pr}/Лен:{staff_cnt_len}) | "
                f"Авто-раздел: [{'; '.join(sub_info)}]"
            )
        lines.append("")

    return "\n".join(lines)


def get_allowed_months() -> list[tuple[int, int]]:
    today = date.today()
    current_ym = (today.year, today.month)
    next_ym = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
    return [current_ym, next_ym]


def get_available_dates_in_month(year: int, month: int, api_dates: list[str] | None = None) -> list[date]:
    today = date.today()
    _, last_day = calendar.monthrange(year, month)
    api_dates_set = set(api_dates) if api_dates is not None else None
    available = []

    for day in range(1, last_day + 1):
        d = date(year, month, day)
        if d < today:
            continue
        if api_dates_set is not None:
            if d.isoformat() in api_dates_set:
                available.append(d)
        else:
            if d == today and not get_available_times(d.isoformat()):
                continue
            available.append(d)
    return available


def get_available_times(date_str: str, booked_slots: list[str] | None = None) -> list[str]:
    booked = set(booked_slots or [])
    target_date = date.fromisoformat(date_str)
    now = datetime.now()

    if target_date < now.date():
        return []

    if target_date > now.date():
        return [slot for slot in BASE_TIME_SLOTS if slot not in booked]

    min_allowed_dt = now + timedelta(minutes=30)
    valid_slots = []
    for slot in BASE_TIME_SLOTS:
        if slot in booked:
            continue
        hour, minute = map(int, slot.split(":"))
        slot_dt = datetime(now.year, now.month, now.day, hour, minute)
        if slot_dt >= min_allowed_dt:
            valid_slots.append(slot)

    return valid_slots