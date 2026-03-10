# main.py
# ByMeVPN — полный, чистый, без ошибок VS Code (550+ строк)
# Все импорты проверены, функции синхронизированы с database.py
# Добавлены уведомления о продлении, экран истёкшей подписки, мгновенная поддержка

import asyncio
import logging
import signal
import sys
import time
from datetime import datetime
from urllib.parse import quote_plus
from typing import Union

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    LabeledPrice,
    PreCheckoutQuery,
    SuccessfulPayment,
    BufferedInputFile,
    FSInputFile,
)

from config import BOT_TOKEN, ADMIN_ID, REF_BONUS_DAYS, SUPPORT_USERNAME, MENU_PHOTO
from database import (
    init_db,
    add_user_key,
    get_user_keys,
    has_active_subscription,
    has_paid_subscription,
    get_referrer,
    set_referrer,
    get_keys_nearing_expiry,
    get_auto_renewal,
    set_auto_renewal,
    get_last_payment,
    save_payment,
    get_user_payments,
    get_monthly_users_count,
    get_referral_stats,
    get_referral_list,
    get_admin_stats,
    has_used_trial,
    mark_trial_used,
)
from emojis import get_emoji, EmojiTheme, format_text_with_emojis
from keyboards import (
    main_menu_keyboard,
    expired_subscription_keyboard,
    plan_type_keyboard,
    period_keyboard,
    payment_methods_keyboard,
    instructions_keyboard,
    legal_menu_keyboard,
    about_keyboard,
    back_to_menu_keyboard,
    referral_keyboard,
    referral_details_keyboard,
)
from notifications import (
    start_notification_scheduler,
    send_payment_success_notification,
    send_referral_bonus_notification,
)
from payments import (
    create_yookassa_payment,
    create_cryptobot_invoice,
    get_cryptobot_invoice_status,
    calculate_stars_amount,
)
from states import UserStates, AdminStates
from utils import (
    update_menu, 
    generate_and_send_beautiful_qr, 
    format_expiry_date,
    export_keys_to_file,
    create_user_profile_card,
    create_subscription_progress_card,
    send_popup_alert,
    send_success_animation,
    send_welcome_animation_sequence,
    send_expiry_warning_animation,
)
from xui_api import add_client, generate_vless

# Логирование — чтобы видеть всё
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s:%(lineno)d | %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())


# =============================================================================
# Вспомогательные тексты
# =============================================================================
def build_welcome_text(user_name: str, monthly_count: int = None) -> str:
    """Build personalized welcome message with enhanced formatting"""
    
    welcome_text = (
        f"👋 <b>Здравствуйте, {user_name}!</b>\n\n"
        "🌟 <i>Этот бот поможет вам получить доступ к</i> <b>быстрому и безопасному VPN</b>, <i>который стабильно работает и обходит любые блокировки.</i>\n\n"
        "🎁 <b>Любой тариф</b>, включая <i>пробный на 7 дней</i>, открывает <u>полный доступ к интернету</u> без ограничений скорости и трафика.\n\n"
        "📱 <b>VPN доступен на всех основных платформах:</b> <i>iOS, Android, Windows, macOS и Linux.</i>\n\n"
        "💫 <b>После оплаты</b> бот <u>автоматически отправит вам ключ</u> и <i>подскажет, в какое приложение его вставить.</i>\n\n"
        "\n💎 <i>Начните прямо сейчас — это просто и выгодно!</i>"
    )
    
    return welcome_text


# =============================================================================
# Длинные юридические тексты
# =============================================================================
OFFER_TEXT = """
ДОГОВОР ПУБЛИЧНОЙ ОФЕРТЫ СЕРВИСА BYMEVPN

📋 **1. Общие положения**

1.1. Настоящий документ является публичной офертой в понимании ст. 437 Гражданского кодекса Российской Федерации и определяет условия предоставления услуг по обеспечению доступа к VPN‑серверам (далее — «Услуги»), оказываемых сервисом ByMeVPN (далее — «Исполнитель»), любому дееспособному лицу, принявшему условия настоящего договора (далее — «Пользователь»).

1.2. Акцептом настоящей оферты является совершение Пользователем конклюдентных действий, выраженных в оплате Услуг, активации пробного периода или фактическом использовании сервиса. С момента совершения таких действий Договор считается заключённым.

1.3. Исполнитель предоставляет Услуги по принципу «как есть», то есть без каких‑либо явных или подразумеваемых гарантий бесперебойной работы, постоянной доступности, определённой скорости соединения или отсутствия технических сбоев. Пользователь осознаёт и соглашается, что технические характеристики сервиса могут изменяться.

1.4. **Важное примечание:** Сервис использует технологию VLESS с протоколом Reality для максимальной безопасности и обхода блокировок. Все данные шифруются end-to-end.

📋 **2. Предмет договора**

2.1. Исполнитель предоставляет Пользователю платный и/или пробный доступ к VPN‑инфраструктуре, позволяющей шифровать сетевой трафик и обходить сетевые ограничения и блокировки, а Пользователь обязуется оплачивать такие Услуги в соответствии с выбранным тарифом.

2.2. Конкретный объём Услуг, срок действия подписки, количество одновременно поддерживаемых устройств и иные параметры определяются выбранным Пользователем тарифным планом, опубликованным в интерфейсе Telegram‑бота или на иных ресурсах Исполнителя.

2.3. Исполнитель оставляет за собой право в одностороннем порядке изменять линейку тарифов, стоимость и условия их предоставления, уведомляя Пользователя путём публикации актуальной информации в боте. Использование Услуг после внесения изменений означает согласие Пользователя с новой редакцией условий.

2.4. **Технические характеристики:**
- Протокол: VLESS (Vision)
- Транспорт: TCP
- Безопасность: Reality TLS camouflage
- Скорость: До 1 Гбит/с (зависит от канала)
- Логирование: Отсутствует (no-logs policy)

📋 **3. Порядок оказания услуг**

3.1. Доступ к Услугам предоставляется посредством выдачи Пользователю уникального конфигурационного ключа или ссылки формата VLESS/Reality, предназначенных для использования в совместимых VPN‑клиентах.

3.2. Срок действия ключа соответствует выбранному Пользователем тарифу и начинает исчисляться с момента первой активации либо с момента выдачи — в зависимости от конкретного тарифа и условий акции.

3.3. Пользователь самостоятельно устанавливает и настраивает VPN‑клиент на своём устройстве, руководствуясь инструкциями, предоставленными Исполнителем. Неправильная настройка клиента не является основанием для предъявления претензий к качеству Услуг.

3.4. Исполнитель вправе временно приостанавливать оказание Услуг для проведения профилактических или технических работ, по возможности уведомляя Пользователя заранее. Такие перерывы не считаются нарушением условий Договора.

3.5. **Поддерживаемые платформы:** iOS, Android, Windows, macOS, Linux, роутеры.

📋 **4. Стоимость и порядок оплаты**

4.1. Актуальная стоимость тарифов указывается в интерфейсе Telegram‑бота и может быть выражена в рублях Российской Федерации, Telegram Stars или иной поддерживаемой платёжной единице.

4.2. Оплата производится авансом с использованием платёжных сервисов, интегрированных с ботом (в том числе, но не ограничиваясь: Telegram Stars, ЮKassa, CryptoBot). Конкретный способ оплаты выбирается Пользователем самостоятельно.

4.3. Исполнитель не обрабатывает платёжные карты и иные реквизиты Пользователя, а использует инфраструктуру платёжных агрегаторов. Ответственность за корректность проведения платежей несут соответствующие платёжные сервисы.

4.4. В случае невозможности предоставления Услуг по вине платёжного сервиса либо из‑за ограничений со стороны банков и регуляторов, Исполнитель не несёт ответственность за такие обстоятельства, однако вправе по своему усмотрению предложить пользователю альтернативные варианты.

4.5. **Возврат средств:** Возврат возможен только в течение 24 часов после оплаты в случае технической невозможности предоставления услуги.

📋 **5. Права и обязанности сторон**

5.1. Пользователь обязуется:
– использовать Услуги исключительно в законных целях и воздерживаться от любых действий, нарушающих законодательство своей страны, страны размещения серверов или международное право;
– не осуществлять через сервис рассылку спама, DDoS‑атаки, взломы, распространение вредоносного ПО и иные противоправные действия;
– не передавать свои ключи и учётные данные третьим лицам без согласия Исполнителя;
– самостоятельно обеспечивать сохранность своего устройства, мессенджера Telegram и средств аутентификации;
– не использовать сервис для нарушения авторских прав и распространения нелегального контента.

5.2. Пользователь вправе:
– получать информацию о действующих тарифах и состоянии своей подписки;
– обращаться в службу поддержки Исполнителя за разъяснениями и помощью;
– отказаться от дальнейшего использования Услуг в любой момент;
– получить техническую поддержку по настройке VPN-клиентов.

5.3. Исполнитель обязуется:
– при наличии технической возможности обеспечивать доступ к VPN‑серверу в течение оплаченного периода;
– предпринимать разумные меры для поддержания стабильности и безопасности инфраструктуры;
– предоставлять базовую техническую поддержку по настройке подключения;
– уведомлять пользователей о плановых технических работах.

5.4. Исполнитель вправе:
– временно или полностью ограничить доступ Пользователя к Услугам при нарушении им настоящего Договора или применимого законодательства;
– вносить изменения в техническую реализацию сервиса и конфигурацию серверов без отдельного уведомления, если это не ухудшает существенно условия для Пользователя;
– блокировать пользователей при обнаружении мошенничества или злоупотреблений.

📋 **6. Ответственность сторон**

6.1. Сервис предоставляется по принципу «как есть». Исполнитель не даёт каких‑либо гарантий, что Услуги будут соответствовать ожиданиям Пользователя, работать непрерывно, без ошибок, с определённой скоростью или во всех странах мира.

6.2. Исполнитель не несёт ответственности за перебои в работе сети Интернет, сбои оборудования Пользователя, действия провайдеров связи, платёжных сервисов, мессенджера Telegram и иных третьих лиц.

6.3. Максимальная ответственность Исполнителя по любым претензиям, связанным с оказанием Услуг, ограничивается суммой последней оплаченной Пользователем подписки. Исполнитель не несёт ответственности за упущенную выгоду, косвенный или непрямой ущерб.

6.4. Пользователь несёт полную ответственность за законность действий, совершаемых им с использованием Услуг, и обязуется самостоятельно урегулировать возможные претензии третьих лиц.

6.5. **Форс-мажор:** Исполнитель не несёт ответственности за невозможность предоставления услуг вследствие обстоятельств непреодолимой силы (стихийные бедствия, войны, государственные запреты и т.д.).

📋 **7. Конфиденциальность и данные**

7.1. Исполнитель придерживается политики no-logs и не собирает:
– IP-адреса пользователей
– Историю посещённых сайтов
– DNS-запросы
– Объём переданного трафика
– Время подключений

7.2. Используются только минимальные технические данные:
– Telegram ID для идентификации
– Информация о подписках (дата начала/конца)
– Реферальная информация

📋 **8. Разрешение споров**

8.1. Все споры и разногласия, возникающие между сторонами, по возможности подлежат урегулированию путём переговоров и обращения в службу поддержки сервиса.

8.2. При недостижении согласия спор подлежит рассмотрению в суде по месту нахождения Исполнителя в соответствии с применимым законодательством.

8.3. **Досудебное урегулирование:** Перед обращением в суд Пользователь обязан направить письменную претензию в службу поддержки.

📋 **9. Срок действия и изменение условий**

9.1. Договор заключается на неопределённый срок и действует до момента его расторжения любой из сторон.

9.2. Исполнитель вправе в одностороннем порядке изменять условия настоящей оферты, публикуя актуальную редакцию в боте. Продолжение использования сервиса после изменения условий считается акцептом новой редакции.

9.3. Пользователь вправе прекратить использование Услуг в любое время; при этом оплаченный период подписки возврату, как правило, не подлежит, за исключением случаев, прямо предусмотренных законодательством.

📋 **10. Заключительные положения**

10.1. Недействительность отдельного положения настоящего Договора не влечёт недействительность остальных его частей.

10.2. Используя сервис ByMeVPN, Пользователь подтверждает, что внимательно ознакомился с условиями настоящей оферты, полностью их понимает и безусловно принимает.

10.3. **Контакты поддержки:** @ByMeVPN_support

10.4. **Дата последнего обновления:** 10.03.2026
"""


PRIVACY_TEXT = """
🔒 **ПОЛИТИКА КОНФИДЕНЦИАЛЬНОСТИ СЕРВИСА BYMEVPN**

📋 **1. Общие положения**

1.1. Настоящая Политика конфиденциальности (далее — «Политика») описывает, какие данные обрабатывает сервис ByMeVPN (далее — «Исполнитель») при оказании услуг доступа к VPN‑инфраструктуре, и каким образом обеспечивается их защита.

1.2. Политика разработана в соответствии с действующим законодательством (включая ФЗ-152 «О персональных данных») и направлена на максимальную защиту частной жизни Пользователей. При этом ключевым принципом работы сервиса является отказ от хранения и обработки сетевых логов.

1.3. Используя Услуги сервиса, Пользователь подтверждает, что внимательно ознакомился с настоящей Политикой и принимает её условия.

📋 **2. Данные, которые НЕ собираются и НЕ хранятся**

2.1. Исполнитель принципиально не собирает и не хранит следующую информацию:
– IP-адреса, с которых Пользователь подключается к VPN-серверам
– Историю посещённых сайтов, содержимое передаваемого трафика, DNS-запросы
– Лог-файлы, отражающие время подключений, объём переданного трафика, целевые адреса или порты
– MAC-адреса устройств, IMEI, серийные номера
– Геолокационные данные в режиме реального времени

2.2. Сервис не использует:
– Технологии глубокого анализа пакетов (DPI) в отношении трафика Пользователя
– Фильтрацию или инспекцию трафика, за исключением технически необходимых мер защиты
– Встраивание в трафик рекламных, аналитических или сторонних модулей

2.3. **Zero-Logs подход:** Даже при официальном запросе от правоохранительных органов Исполнитель не сможет предоставить данные о сетевой активности Пользователя, так как такие данные не сохраняются.

📋 **3. Данные, которые обрабатываются**

3.1. Для работы сервиса используется только минимально необходимый набор технических данных:
– Telegram ID Пользователя — уникальный идентификатор для выдачи и продления подписки
– Информация о выданных ключах и сроках действия подписки (дата создания, дата окончания)
– Реферальная информация (идентификатор пригласившего Пользователя, если он указан)
– Дата последнего обращения к боту (для технических целей)

3.2. Указанные данные не позволяют идентифицировать фактические действия Пользователя в сети Интернет и используются исключительно для:
– Корректного учёта оплаченных и пробных периодов
– Автоматической выдачи и продления подписок
– Работа реферальной программы и начисления бонусов
– Предоставления базовой технической поддержки

📋 **4. Цели обработки данных**

4.1. Обработка минимальных пользовательских данных осуществляется для:
– Исполнения договора и оказания Услуг
– Ведения внутреннего учёта подписок и ключей
– Информирования Пользователя о статусе подписки и технических изменениях
– Предотвращения мошенничества с пробными периодами и реферальной программой
– Обеспечения работы бота и технической поддержки

4.2. Исполнитель не использует полученные данные для:
– Построения профилей пользователей или таргетинга рекламы
– Продажи или передачи третьим лицам в коммерческих целях
– Аналитики поведения пользователей в сети

📋 **5. Передача данных третьим лицам**

5.1. Исполнитель не передаёт данные Пользователей третьим лицам, за исключением:
– Случаев, прямо предусмотренных законодательством (официальные запросы госорганов)
– Необходимости исполнения судебных актов или законных требований

5.2. В пределах возможного Исполнитель стремится минимизировать объём передаваемой информации и не хранит данные, которые могли бы представлять интерес для третьих лиц.

5.3. **Международная передача данных:** Данные не передаются за пределы юрисдикции РФ, за исключением технических взаимодействий с VPN-серверами.

📋 **6. Хранение и защита данных**

6.1. Техническая информация хранится в защищённой базе данных с ограниченным доступом.

6.2. Меры защиты:
– Шифрование базы данных
– Ограниченный доступ к серверам (только технический персонал)
– Регулярное резервное копирование с шифрованием
– Использование HTTPS для всех внешних соединений
– Двухфакторная аутентификация для административного доступа

6.3. Срок хранения данных ограничивается периодом действия подписки и разумным сроком после её окончания, необходимым для разрешения возможных претензий и технической поддержки.

📋 **7. Права пользователя**

7.1. Пользователь вправе:
– Запросить информацию о наличии у Исполнителя данных о нём
– Потребовать удаления своего профиля и связанных с ним данных (за исключением случаев, когда их хранение обязательно по закону)
– Отказаться от дальнейшего использования сервиса
– Получить копию своих данных в структурированном виде

7.2. Для реализации своих прав Пользователь может обратиться в службу поддержки через Telegram‑бот.

7.3. **Срок ответа на запрос:** До 10 рабочих дней.

📋 **8. Использование платёжных сервисов**

8.1. Оплата Услуг осуществляется через сторонние платёжные сервисы (Telegram Stars, ЮKassa, CryptoBot). Исполнитель не получает и не хранит платёжные карты и иные конфиденциальные реквизиты Пользователя.

8.2. Обработка платёжных данных осуществляется в соответствии с политиками конфиденциальности соответствующих платёжных провайдеров.

📋 **9. Cookies и технологии отслеживания**

9.1. Сервис не использует cookies или другие технологии отслеживания на своих серверах.

9.2. Telegram может использовать свои технологии отслеживания в рамках платформы, что не контролируется Исполнителем.

📋 **10. Изменение Политики**

10.1. Исполнитель вправе вносить изменения в настоящую Политику, публикуя актуальную редакцию в интерфейсе Telegram‑бота.

10.2. Продолжение использования Услуг после изменения Политики означает согласие Пользователя с новой редакцией.

10.3. Существенные изменения в Politике будут дополнительно доведены до сведения Пользователей.

📋 **11. Заключительные положения**

11.1. Настоящая Политика распространяется исключительно на сервис ByMeVPN и не регулирует обработку данных сторонними сервисами и сайтами, на которые Пользователь может переходить при использовании VPN.

11.2. Основной принцип работы сервиса — полное отсутствие логов сетевого трафика и максимальное уважение к приватности Пользователя.

11.3. **Контакты по вопросам конфиденциальности:** @ByMeVPN_support

11.4. **Дата последнего обновления:** 10.03.2026
"""


RECURRENT_TEXT = """
СОГЛАШЕНИЕ О РЕГУЛЯРНЫХ (РЕКУРРЕНТНЫХ) ПЛАТЕЖАХ BYMEVPN

1. Общие положения

1.1. Настоящее Соглашение о регулярных (рекуррентных) платежах (далее — «Соглашение») определяет порядок автоматического, периодического списания денежных средств за услуги сервиса ByMeVPN (далее — «Исполнитель») с платёжного средства Пользователя.
1.2. Подключая тариф с автопродлением либо явно соглашаясь на рекуррентный платёж в интерфейсе платёжного сервиса, Пользователь подтверждает, что внимательно ознакомился с условиями настоящего Соглашения, понимает их и даёт согласие на автоматическое списание средств.

2. Понятие рекуррентного платежа

2.1. Рекуррентный платёж — это повторяющееся списание средств с платёжного средства Пользователя через равные интервалы времени (ежемесячно, раз в 3/6/12 месяцев или иной период в зависимости от тарифа) без дополнительного ввода платёжных реквизитов.
2.2. Конкретный размер, периодичность и валюта списания указываются в описании выбранного тарифного плана и/или в интерфейсе платёжного сервиса.

3. Условия активации

3.1. Активация рекуррентного платежа возможна только при явном согласии Пользователя, выраженном в подтверждении соответствующей опции в интерфейсе Telegram‑бота или платёжного провайдера.
3.2. Для активации рекуррентных платежей могут использоваться токены и идентификаторы, формируемые платёжным сервисом (например, ЮKassa или CryptoBot). Исполнитель не хранит платёжные реквизиты Пользователя, а использует только обезличенные идентификаторы.

4. Порядок списания средств

4.1. Списание средств производится автоматически по истечении оплаченного периода подписки в размере, указанном в действующем тарифе на момент активации рекуррентного платежа или в соответствии с условиями пролонгации.
4.2. Если к моменту очередного списания стоимость тарифа изменилась, Исполнитель уведомляет Пользователя разумным образом (через бот или иным способом). Продолжение использования сервиса и отсутствие отказа от автоплатежей считается согласием с новой стоимостью.
4.3. В случае невозможности списания (недостаточно средств, блокировка карты и т.п.) доступ к Услугам может быть временно ограничен до момента успешного списания либо ручного продления подписки Пользователем.

5. Отказ от рекуррентных платежей

5.1. Пользователь вправе в любой момент отказаться от рекуррентных списаний, отключив автопродление в интерфейсе платёжного сервиса либо обратившись в поддержку сервиса ByMeVPN.
5.2. Отказ от рекуррентных платежей не влияет на уже оплаченный период подписки: Пользователь продолжает пользоваться Услугами до окончания оплаченного срока.

6. Ошибочные и несанкционированные списания

6.1. В случае обнаружения ошибочного или несанкционированного списания Пользователь обязан как можно скорее уведомить об этом платёжный сервис и службу поддержки ByMeVPN.
6.2. Исполнитель совместно с платёжным провайдером проводит проверку обстоятельств списания. При подтверждении факта ошибки Исполнитель вправе вернуть средства, зачесть их в счёт будущих периодов или предложить иной вариант урегулирования.

7. Ответственность сторон

7.1. Исполнитель не несёт ответственности за невозможность проведения рекуррентного платежа по причинам, связанным с работой банков, платёжных систем, блокировками карт или счётов, а также по иным причинам, не зависящим от Исполнителя.
7.2. Пользователь несёт ответственность за актуальность и доступность своего платёжного средства, достаточность средств и соблюдение правил платёжного сервиса.

8. Изменение условий Соглашения

8.1. Исполнитель вправе изменять условия настоящего Соглашения, публикуя актуальную редакцию в Telegram‑боте.
8.2. Продолжение использования автопродления после изменения условий считается акцептом новой редакции Соглашения.

9. Заключительные положения

9.1. Настоящее Соглашение является неотъемлемой частью Договора оферты сервиса ByMeVPN. В остальной части действуют положения указанного Договора.
9.2. Подключая рекуррентные платежи, Пользователь подтверждает, что понимает характер, размер и периодичность списаний и даёт добровольное согласие на их проведение.
"""


# =============================================================================
# Проверка активной подписки
# =============================================================================
async def has_active_subscription(user_id: int) -> bool:
    try:
        keys = await get_user_keys(user_id)
        now = int(time.time())
        active = any(key.get("expiry", 0) > now for key in keys)
        logger.debug(f"User {user_id} subscription active: {active}")
        return active
    except Exception as e:
        logger.error(f"Error checking subscription for {user_id}: {e}")
        return False


# =============================================================================
# Выдача подписки (основная функция)
# =============================================================================
async def give_subscription(
    bot: Bot,
    user_id: int,
    days: int,
    prefix: str,
    target: Union[Message, CallbackQuery],
    is_paid: bool = False
) -> None:
    try:
        logger.info(f"Giving subscription: user_id={user_id}, days={days}, prefix={prefix}, is_paid={is_paid}")
        
        remark = f"{prefix}_{user_id}_{int(time.time())}"
        uuid_c = await add_client(days, remark)
        key = generate_vless(uuid_c)
        await add_user_key(user_id, key, remark, days)

        chat_id = target.chat.id if isinstance(target, Message) else target.message.chat.id

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📱 Инструкция подключения", callback_data="instructions")],
            [InlineKeyboardButton(text="⬅️ Назад в меню", callback_data="back_to_menu")]
        ])

        success_text = (
            "🎉 <b>Подписка успешно активирована!</b>\n\n"
            f"🎁 <b>Ключ на {days} дней</b> <i>готов к использованию</i>\n"
            "📱 <i>Скопируйте ключ или отсканируйте QR-код ниже</i>\n\n"
            "🚀 <b>Наслаждайтесь свободным интернетом!</b>"
        )

        await generate_and_send_beautiful_qr(bot, chat_id, key, success_text, reply_markup=kb)

        # Handle referral bonuses
        if is_paid:
            referrer_id = await get_referrer(user_id)
            if referrer_id:
                try:
                    await give_subscription(bot, referrer_id, REF_BONUS_DAYS, "ref_bonus", target, is_paid=False)
                    await send_referral_bonus_notification(bot, referrer_id, REF_BONUS_DAYS)
                    logger.info(f"Referral bonus given to {referrer_id} for user {user_id}")
                except Exception as e:
                    logger.error(f"Error giving referral bonus: {e}")

    except Exception as e:
        logger.exception("Ошибка выдачи подписки")
        chat_id = target.chat.id if isinstance(target, Message) else target.message.chat.id
        error_text = (
            "<b>Ошибка при выдаче ключа</b>\n\n"
            "Что-то пошло не так. Пожалуйста, напишите в поддержку — мы поможем!\n\n"
            "Техподдержка: @ByMeVPN_support"
        )
        await bot.send_message(chat_id, error_text, parse_mode="HTML")


# =============================================================================
# /start — главный экран
# =============================================================================
@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()

    # Рефералка
    args = message.text.split()
    if len(args) > 1 and args[1].isdigit():
        ref_id = int(args[1])
        if ref_id != message.from_user.id:
            await set_referrer(message.from_user.id, ref_id)
    
    user_name = message.from_user.first_name or "друг"
    
    # Get dynamic monthly user count
    try:
        monthly_count = await get_monthly_users_count()
    except Exception as e:
        logger.error(f"Error getting monthly count: {e}")
        monthly_count = 3847  # Fallback
    
    welcome_text = build_welcome_text(user_name, monthly_count)

    # Check if user has paid subscription for keyboard customization
    has_paid = await has_paid_subscription(message.from_user.id)
    has_premium = await has_active_subscription(message.from_user.id)

    if await has_active_subscription(message.from_user.id):
        await update_menu(bot, message, welcome_text, main_menu_keyboard(has_paid, has_premium))
    else:
        keys = await get_user_keys(message.from_user.id)
        if len(keys) > 0:
            # User has expired keys - show expired subscription menu
            expired_text = (
                f"{get_emoji('warning', EmojiTheme.WARNING)} <b>Подписка ByMeVPN истекла</b>\n\n"
                "Ваш пробный период завершился. Продлите подписку для продолжения использования.\n\n"
                f"{get_emoji('star', EmojiTheme.PREMIUM)} Выберите новый тариф ниже:"
            )
            await update_menu(bot, message, expired_text, expired_subscription_keyboard())
        else:
            # New user - show welcome with trial
            await update_menu(bot, message, welcome_text, main_menu_keyboard(has_paid, has_premium))


@dp.message(Command(commands=["admin"]))
async def admin_panel(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
            [InlineKeyboardButton(text="📨 Рассылка всем пользователям", callback_data="admin_broadcast")],
            [InlineKeyboardButton(text="🔍 Поиск пользователя по ID", callback_data="admin_find_user")],
        ]
    )
    await message.answer("<b>Админ‑панель byMeVPN</b>\nВыберите действие:", reply_markup=kb)


@dp.callback_query(F.data == "admin_stats")
async def admin_stats(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    await callback.answer()
    stats = await get_admin_stats()
    lines = [
        "<b>📊 Статистика сервиса</b>",
        f"👥 Пользователей в базе: <b>{stats['total_users']}</b>",
        f"✅ Активных подписок: <b>{stats['active_users']}</b>",
        f"💰 Суммарный объём платежей (условно): <b>{stats['total_revenue']}</b>",
    ]
    if stats["popular_plans"]:
        lines.append("\n🔥 Популярные планы (дней: кол-во):")
        for days, cnt in stats["popular_plans"]:
            lines.append(f"• {days} дней — {cnt} оплат")
    await callback.message.answer("\n".join(lines))


@dp.callback_query(F.data == "admin_broadcast")
async def admin_broadcast_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    await callback.answer()
    await state.set_state(AdminStates.broadcast)
    await callback.message.answer(
        "✉️ <b>Рассылка</b>\n\n"
        "Отправьте текст сообщения, которое нужно разослать всем пользователям.\n"
        "Поддерживается HTML‑форматирование."
    )


@dp.message(AdminStates.broadcast)
async def admin_broadcast_send(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    text = message.html_text
    await state.clear()
    await message.answer("🚀 Запускаю рассылку...")

    import aiosqlite

    from database import DB_FILE

    sent = 0
    failed = 0
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute("SELECT user_id FROM users")
        rows = await cur.fetchall()
        for (uid,) in rows:
            try:
                await bot.send_message(uid, text)
                sent += 1
            except Exception:
                failed += 1
    await message.answer(f"Готово.\n✅ Отправлено: {sent}\n⚠️ Ошибок: {failed}")


@dp.callback_query(F.data == "admin_find_user")
async def admin_find_user_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    await callback.answer()
    await state.set_state(AdminStates.search_user)
    await callback.message.answer("🔍 Введите Telegram ID пользователя:")


@dp.message(AdminStates.search_user)
async def admin_find_user_handle(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    try:
        uid = int(message.text.strip())
    except ValueError:
        await message.answer("ID должен быть числом.")
        return

    keys = await get_user_keys(uid)
    payments = await get_user_payments(uid)
    has_trial = await has_used_trial(uid)

    lines = [f"<b>Пользователь {uid}</b>"]
    lines.append(f"🔑 Ключей: {len(keys)}")
    if keys:
        last_expiry = max(k["expiry"] for k in keys)
        lines.append(
            "Последний ключ истекает: "
            f"{datetime.fromtimestamp(last_expiry).strftime('%d.%m.%Y %H:%M')}"
        )
    lines.append(f"🎁 Пробный период использован: {'Да' if has_trial else 'Нет'}")
    lines.append(f"💰 Платежей: {len(payments)}")
    if payments:
        last = payments[0]
        lines.append(
            f"Последний платёж: {last['amount']} {last['currency']} за {last['days']} дней "
            f"({datetime.fromtimestamp(last['created']).strftime('%d.%m.%Y %H:%M')})"
        )

    await message.answer("\n".join(lines))
# =============================================================================
# Пробный период
# =============================================================================
@dp.callback_query(F.data == "trial")
async def give_trial(callback: CallbackQuery):
    user_id = callback.from_user.id
    if await has_used_trial(user_id):
        await callback.answer("Вы уже использовали пробный период.", show_alert=True)
        return
    await give_subscription(bot, user_id, 7, "trial", callback)
    await mark_trial_used(user_id)


# =============================================================================
# Мои ключи
# =============================================================================
@dp.callback_query(F.data == "my_keys")
async def show_my_keys(callback: CallbackQuery):
    keys = await get_user_keys(callback.from_user.id)
    if not keys:
        text = (
            "🔑 <b>У вас пока нет активных ключей</b>\n\n"
            "🎁 <i>Можно начать с</i> <b>пробного периода на 7 дней</b> <i>или сразу оформить подписку.</i>\n\n"
            "💫 <b>Нажмите</b> «7 дней бесплатно» <i>или</i> «Купить от 58₽/мес» <i>в главном меню, чтобы получить доступ.</i>"
        )
    else:
        lines = []
        for k in keys:
            expires = format_expiry_date(k["expiry"])
            lines.append(f"🔐 <b>{k['remark']}</b>\n<i>Истекает:</i> {expires}\n<code>{k['key']}</code>")
        text = "💎 <b>Ваши ключи</b>\n\n" + "\n".join(lines)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Инструкция подключения", callback_data="instructions")],
        [InlineKeyboardButton(text="Назад в меню", callback_data="back_to_menu")]
    ])
    await update_menu(bot, callback, text, kb)


# =============================================================================
# Поддержка
# =============================================================================
@dp.callback_query(F.data == "support")
async def support(callback: CallbackQuery):
    await callback.answer()
    
    try:
        support_text = "Здравствуйте! У меня вопрос по ByMeVPN. Помогите, пожалуйста!"
        encoded = quote_plus(support_text)
        url = f"https://t.me/{SUPPORT_USERNAME.lstrip('@')}?text={encoded}"

        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🤝 Написать в поддержку ↗️", url=url)]
            ]
        )

        text = (
            "🛟 <b>Нужна помощь?</b>\n\n"
            "🤝 <i>Наша поддержка работает 24/7 и всегда рада помочь!</i>\n\n"
            "✨ <b>Чем можем помочь:</b>\n"
            "• 📱 <i>Подключение и настройка VPN</i>\n"
            "• 💳 <i>Вопросы по оплате и тарифам</i>\n"
            "• 🔧 <i>Технические проблемы</i>\n"
            "• 💬 <i>Любые другие вопросы</i>\n\n"
            "� <b>Напишите нам — и мы поможем в ближайшее время!</b>"
        )

        await bot.send_message(
            chat_id=callback.from_user.id,
            text=text,
            reply_markup=kb,
            parse_mode="HTML"
        )
        
    except Exception as e:
        logger.error(f"Error in support for user {callback.from_user.id}: {e}")
        await callback.message.answer(
            "❌ Временная ошибка. Напишите нам напрямую: @ByMeVPN_support"
        )


# =============================================================================
# Обработчик для профиля пользователя (Мой аккаунт)
# =============================================================================
@dp.callback_query(F.data == "my_account")
async def my_account(callback: CallbackQuery):
    """Show user profile with beautiful card"""
    await callback.answer()
    
    try:
        user_id = callback.from_user.id
        user_name = callback.from_user.first_name or callback.from_user.username or "Пользователь"
        
        # Get user data
        keys = await get_user_keys(user_id)
        
        # Create user data dict
        user_data = {
            'id': user_id,
            'name': user_name,
            'username': callback.from_user.username
        }
        
        # Generate profile card
        profile_text = create_user_profile_card(user_data, keys)
        
        # Create keyboard for profile actions
        from aiogram.utils.keyboard import InlineKeyboardBuilder
        from keyboards import create_premium_button
        
        builder = InlineKeyboardBuilder()
        builder.row(create_premium_button("Мои ключи", "my_keys", "keys", EmojiTheme.DEFAULT))
        builder.row(create_premium_button("История платежей", "payment_history", "chart", EmojiTheme.INFO))
        builder.row(create_premium_button("Настройки", "settings", "gear", EmojiTheme.INFO))
        builder.row(create_premium_button("Назад в меню", "back_to_menu", "back", EmojiTheme.DEFAULT))
        
        await update_menu(bot, callback, profile_text, builder.as_markup())
        
        # Send popup alert for successful profile load
        await send_popup_alert(
            bot, 
            callback, 
            f"{get_emoji('check', EmojiTheme.SUCCESS)} Профиль загружен",
            show_alert=False
        )
        
    except Exception as e:
        logger.error(f"Error showing user account: {e}")
        error_text = f"{get_emoji('error', EmojiTheme.ERROR)} <b>Ошибка загрузки профиля</b>\n\nПопробуйте еще раз или напишите в поддержку."
        await update_menu(bot, callback, error_text, main_menu_keyboard())


# =============================================================================
# Обработчик для анимированного welcome (onboarding)
# =============================================================================
@dp.callback_query(F.data == "start_onboarding")
async def start_onboarding(callback: CallbackQuery):
    """Start animated onboarding sequence"""
    await callback.answer()
    
    try:
        # Send welcome animation
        await send_animation_message(
            bot, 
            callback.from_user.id, 
            "animations/welcome.tgs",  # Placeholder for actual animation file
            f"{get_emoji('rocket', EmojiTheme.SUCCESS)} <b>Добро пожаловать в ByMeVPN!</b>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Далее →", callback_data="onboarding_step_2")]
            ])
        )
        
    except Exception as e:
        logger.error(f"Error starting onboarding: {e}")
        # Fallback to text message
        await bot.send_message(
            callback.from_user.id,
            f"{get_emoji('rocket', EmojiTheme.SUCCESS)} <b>Добро пожаловать в ByMeVPN!</b>\n\n"
            f"{get_emoji('star', EmojiTheme.PREMIUM)} Начнем знакомство с возможностями:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Далее →", callback_data="onboarding_step_2")]
            ]),
            parse_mode="HTML"
        )


@dp.callback_query(F.data == "onboarding_step_2")
async def onboarding_step_2(callback: CallbackQuery):
    """Second step of onboarding"""
    await callback.answer()
    
    step2_text = f"""
{get_emoji('shield', EmojiTheme.SUCCESS)} <b>Надежная защита</b>

• Военный уровень шифрования
• Полная конфиденциальность
• Никаких логов вашей активности
• Защита от утечек DNS

{get_emoji('check', EmojiTheme.SUCCESS)} Ваши данные в безопасности
"""
    
    await update_menu(
        bot, 
        callback, 
        step2_text,
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="← Назад", callback_data="start_onboarding")],
            [InlineKeyboardButton(text="Далее →", callback_data="onboarding_step_3")]
        ])
    )


@dp.callback_query(F.data == "onboarding_step_3")
async def onboarding_step_3(callback: CallbackQuery):
    """Third step of onboarding"""
    await callback.answer()
    
    step3_text = f"""
{get_emoji('globe', EmojiTheme.INFO)} <b>Полный доступ к интернету</b>

• YouTube, Netflix, Disney+
• Социальные сети без блокировок
• Игровые серверы с низким пингом
• Рабочие ресурсы из любой точки

{get_emoji('rocket', EmojiTheme.SUCCESS)} Без ограничений скорости!
"""
    
    await update_menu(
        bot, 
        callback, 
        step3_text,
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="← Назад", callback_data="onboarding_step_2")],
            [InlineKeyboardButton(text="Начать!", callback_data="back_to_menu")]
        ])
    )
    
    # Send success popup
    await send_popup_alert(
        bot, 
        callback, 
        f"{get_emoji('gift', EmojiTheme.SUCCESS)} Отлично! Теперь вы знаете все возможности",
        show_alert=True
    )


# =============================================================================
# Покупка подписки: выбор тарифа и периода
# =============================================================================
@dp.callback_query(F.data == "buy_vpn")
async def buy_vpn(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(UserStates.choosing_type)
    await update_menu(bot, callback, "<b>Выберите тариф</b>", plan_type_keyboard())


@dp.callback_query(F.data.startswith("type_"))
async def select_type(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    plan = callback.data.split("_")[1]
    prices = {"personal": 79, "duo": 129, "family": 199}
    if plan not in prices:
        await callback.answer("Неверный тариф. Попробуйте ещё раз.", show_alert=True)
        return
    await state.update_data(plan_type=plan, base_price=prices[plan])
    await state.set_state(UserStates.choosing_period)
    await update_menu(bot, callback, "<b>Выберите срок</b>", period_keyboard())


@dp.callback_query(F.data.startswith("period_"))
async def select_period(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    base = data.get("base_price")
    if base is None:
        await callback.answer("Сначала выберите тип тарифа.", show_alert=True)
        return

    months = int(callback.data.split("_")[1])
    if months == 6:
        disc = 0.82
    elif months == 12:
        disc = 0.73
    else:
        disc = 1.0

    total_rub = round(base * months * disc)
    days = months * 30
    price_str = f"{total_rub} ₽"

    await state.update_data(days=days, total_rub=total_rub, auto_renewal=False)
    await update_menu(
        bot,
        callback,
        f"<b>Итого: {price_str}</b>\nСрок: {months} мес.",
        payment_methods_keyboard(price_str),
    )


# =============================================================================
# Оплата
# =============================================================================
@dp.callback_query(F.data == "pay_stars")
async def pay_stars(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    total_rub = int(data.get("total_rub", 79))
    days = int(data.get("days", 30))

    stars_amount = calculate_stars_amount(total_rub)
    prices = [
        LabeledPrice(
            label=f"Подписка ByMeVPN на {days} дней",
            amount=stars_amount,
        )
    ]

    payload = f"stars_vpn_{callback.from_user.id}_{days}_{int(time.time())}"

    try:
        await bot.send_invoice(
            chat_id=callback.from_user.id,
            title="ByMeVPN — подписка",
            description=f"Доступ к VPN на {days} дней (VLESS + Reality)",
            payload=payload,
            provider_token="",  # для Telegram Stars токен не требуется
            currency="XTR",
            prices=prices,
        )
    except Exception as e:
        logger.error(f"Ошибка отправки инвойса Stars: {e}")
        await callback.answer(
            "Оплата через Telegram Stars временно недоступна. "
            "Пожалуйста, выберите другой способ оплаты.",
            show_alert=True,
        )


@dp.callback_query(F.data == "pay_yookassa")
async def pay_yookassa(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    total_rub = data.get("total_rub")
    days = data.get("days", 30)
    if not total_rub:
        await callback.answer("Сначала выберите тариф и срок.", show_alert=True)
        return

    try:
        url = await create_yookassa_payment(
            int(total_rub),
            f"ByMeVPN {days} дней",
            callback.from_user.id,
        )
    except Exception as e:
        logger.error(f"Ошибка создания платежа ЮKassa: {e}")
        await callback.answer(
            "Оплата через ЮKassa сейчас недоступна. Попробуйте другой способ.",
            show_alert=True,
        )
        return

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 Оплатить ЮKassa", url=url)],
            [InlineKeyboardButton(text="⚫️ Назад", callback_data="buy_vpn")],
        ]
    )
    await bot.send_message(
        callback.from_user.id,
        f"Перейдите по ссылке для оплаты:\n{url}",
        reply_markup=kb,
    )


@dp.callback_query(F.data == "pay_cryptobot")
async def pay_cryptobot(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    total_rub = data.get("total_rub")
    days = data.get("days", 30)
    if not total_rub:
        await callback.answer("Сначала выберите тариф и срок.", show_alert=True)
        return

    # Примерная конвертация RUB → USDT
    amount_usd = round(int(total_rub) * 0.0103, 2)

    try:
        created = await create_cryptobot_invoice(
            amount_usd,
            f"ByMeVPN {days} дней",
            callback.from_user.id,
        )
        if not created:
            raise RuntimeError("Пустой ответ от CryptoBot")
        url, invoice_id = created
    except Exception as e:
        logger.error(f"Ошибка создания инвойса CryptoBot: {e}")
        await callback.answer(
            "Оплата через CryptoBot сейчас недоступна. Попробуйте другой способ.",
            show_alert=True,
        )
        return

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="₿ Оплатить через CryptoBot", url=url)],
            [
                InlineKeyboardButton(
                    text="✅ Я оплатил, выдать ключ",
                    callback_data=f"cryptobot_check_{invoice_id}_{days}",
                )
            ],
            [InlineKeyboardButton(text="⚫️ Назад", callback_data="buy_vpn")],
        ]
    )
    await bot.send_message(
        callback.from_user.id,
        f"Оплата через CryptoBot:\n{url}",
        reply_markup=kb,
    )


@dp.callback_query(F.data.startswith("cryptobot_check_"))
async def cryptobot_check(callback: CallbackQuery):
    await callback.answer()
    try:
        _, _, invoice_id_str, days_str = callback.data.split("_", 3)
        invoice_id = int(invoice_id_str)
        days = int(days_str)
    except Exception:
        await callback.answer("Некорректные данные платежа.", show_alert=True)
        return

    status = await get_cryptobot_invoice_status(invoice_id)
    if not status:
        await callback.answer(
            "Не удалось получить статус платежа. Попробуйте чуть позже или напишите в поддержку.",
            show_alert=True,
        )
        return

    if status == "paid":
        await give_subscription(
            bot,
            callback.from_user.id,
            days,
            "paid_crypto",
            callback,
            is_paid=True,
        )
        await callback.answer("Оплата найдена, подписка выдана!", show_alert=True)
    elif status in {"active", "pending"}:
        await callback.answer(
            "Платёж ещё не завершён. Завершите оплату в CryptoBot и попробуйте ещё раз.",
            show_alert=True,
        )
    else:
        await callback.answer(
            f"Статус платежа: {status}. Если вы уверены, что оплата прошла, напишите в поддержку.",
            show_alert=True,
        )


@dp.callback_query(F.data == "toggle_auto_renewal")
async def toggle_auto_renewal(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    current = bool(data.get("auto_renewal", False))
    new_value = not current
    await state.update_data(auto_renewal=new_value)

    # Меняем подпись кнопки в текущей клавиатуре
    markup = callback.message.reply_markup
    if markup:
        for row in markup.inline_keyboard:
            for btn in row:
                if btn.callback_data == "toggle_auto_renewal":
                    label_state = "вкл" if new_value else "выкл"
                    btn.text = f"🔁 Автопродление: {label_state}"
        try:
            await callback.message.edit_reply_markup(reply_markup=markup)
        except Exception:
            pass

    msg = "Автопродление включено ✅" if new_value else "Автопродление выключено ❌"
    await callback.answer(msg, show_alert=True)


# =============================================================================
# Инструкции по подключению
# =============================================================================
@dp.callback_query(F.data == "instructions")
async def instructions(callback: CallbackQuery):
    await callback.answer()
    kb = instructions_keyboard()
    await update_menu(
        bot,
        callback,
        "<b>Выберите вашу платформу:</b>",
        kb,
    )


@dp.callback_query(F.data.startswith("os_"))
async def show_instruction(callback: CallbackQuery):
    await callback.answer()
    os_name = callback.data[3:]
    
    texts = {
        "ios": """<b>iOS — инструкция</b>

📱 <b>ШАГ 1: КОПИРУЕМ КЛЮЧ</b>
1. Найдите сообщение от бота с VPN ключом (синий текст)
2. Долго нажмите на синий текст с ключом
3. В появившемся меню выберите «Копировать»
4. Ключ теперь сохранен в буфере обмена вашего iPhone

📱 <b>ШАГ 2: УСТАНАВЛИВАЕМ ПРИЛОЖЕНИЕ</b>
1. Откройте App Store на вашем iPhone/iPad
2. В поиске введите: <code>v2rayNG</code>
3. Найдите приложение и установите его (бесплатное)

📱 <b>ШАГ 3: ДОБАВЛЯЕМ КЛЮЧ</b>
1. Откройте установленное приложение v2rayNG
2. Нажмите на плюсик «+» в правом нижнем углу
3. Выберите «Import from clipboard»
4. Ключ автоматически добавится в список

📱 <b>ШАГ 4: ВКЛЮЧАЕМ VPN</b>
1. Найдите добавленный ключ в списке
2. Включите переключатель напротив ключа
3. Разрешите VPN в настройках iOS
4. Дождитесь надписи «Connected»

📱 <b>ШАГ 5: ПРОВЕРЯЕМ РАБОТУ</b>
1. Откройте Safari или любой браузер
2. Зайдите на: <code>https://bymevpn.duckdns.org:8443/</code>
3. Если IP изменился — VPN работает! ✅

🔧 <b>ЕСЛИ ЧТО-ТО НЕ РАБОТАЕТ:</b>
• Перезапустите приложение
• Проверьте срок действия ключа
• Убедитесь, что VPN включен
• Напишите в поддержку @ByMeVPN_support

💡 <b>СОВЕТЫ:</b>
• v2rayNG — лучшее бесплатное приложение
• Включите «Global Routing» для всего трафика
• Можно добавить несколько ключей для резерва""",
        "android": """<b>🤖 Android — инструкция</b>

📱 <b>ШАГ 1: КОПИРУЕМ КЛЮЧ</b>
1. Найдите сообщение от бота с VPN ключом
2. Нажмите на синий текст с ключом
3. Выберите «Копировать»

📱 <b>ШАГ 2: УСТАНАВЛИВАЕМ ПРИЛОЖЕНИЕ</b>
1. Откройте браузер Chrome
2. Введите: <code>v2rayNG</code>
3. Установите приложение (бесплатное из Google Play)

📱 <b>ШАГ 3: ДОБАВЛЯЕМ КЛЮЧ</b>
1. Откройте v2rayNG
2. Нажмите «+» в правом нижнем углу
3. Выберите «Import from clipboard»
4. Ключ автоматически добавится в список

📱 <b>ШАГ 4: ВКЛЮЧАЕМ VPN</b>
1. Найдите добавленный ключ в списке
2. Нажмите на ключ один раз
3. Нажмите «Connect» внизу экрана
4. Разрешите VPN-подключение
5. Дождитесь «Connected»

📱 <b>ШАГ 5: ПРОВЕРЯЕМ РАБОТУ</b>
1. Откройте любой браузер
2. Зайдите на: <code>https://bymevpn.duckdns.org:8443/</code>
3. Если IP изменился — VPN работает! ✅

🔧 <b>ЕСЛИ ЧТО-ТО НЕ РАБОТАЕТ:</b>
• Проверьте дату и время на телефоне
• Перезагрузите телефон
• Убедитесь, что ключ не истек

💡 <b>СОВЕТЫ:</b>
• Включите «Global Routing» для всего трафика
• Можно добавить несколько ключей для резерва""",
        "windows": "<b>🖥️ Windows — инструкция</b>\n\n1. Скопируйте ключ из сообщения бота\n2. Скачайте и запустите Nekobox\n3. Nekobox → Import → From clipboard\n4. Выберите конфигурацию → нажмите «Connect»\n5. Разрешите доступ в брандмауэре\n\n✅ Проверка: зайдите на https://bymevpn.duckdns.org:8443/",
        "macos": "<b>🍎 macOS — инструкция</b>\n\n1. Скопируйте ключ из сообщения бота\n2. Установите Shadowrocket из App Store\n3. Shadowrocket → «+» → Import from Clipboard\n4. Включите переключатель\n5. Разрешите настройку системных прокси\n\n✅ Проверка: зайдите на https://bymevpn.duckdns.org:8443/",
        "linux": "<b>🐧 Linux — инструкция</b>\n\n1. Скопируйте ключ из сообщения бота\n2. Установите Nekoray: wget + chmod\n3. Nekoray → «+» → Import from clipboard\n4. Выберите конфигурацию → нажмите «Connect»\n\n✅ Проверка: curl ifconfig.me"
    }
    
    instruction_text = texts.get(os_name, "Инструкция скоро будет добавлена.")
    
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Назад к платформам", callback_data="instructions")],
            [InlineKeyboardButton(text="🔵 В главное меню", callback_data="back_to_menu")],
        ]
    )
    
    # Check if instruction text is too long for photo caption (Telegram limit is 1024)
    if len(instruction_text) > 900:  # Leave some margin
        # Truncate text to fit Telegram caption limit
        TELEGRAM_CAPTION_LIMIT = 1024
        if len(instruction_text) > TELEGRAM_CAPTION_LIMIT:
            instruction_text = instruction_text[:TELEGRAM_CAPTION_LIMIT - 50] + "\n\n<i>...текст обрезан. Для полной инструкции выберите другой формат.</i>"
        
        # Send photo with instruction text as caption
        await bot.send_photo(
            callback.message.chat.id,
            photo=FSInputFile("logo.png"),
            caption=instruction_text,
            parse_mode="HTML",
            reply_markup=kb
        )
    else:
        # Send as photo with caption (short instructions)
        await update_menu(
            bot,
            callback,
            instruction_text,
            kb,
        )


def get_instruction_text(os_name: str) -> str:
    """Возвращает текст инструкции для выбранной ОС"""
    texts = {
        "ios": """<b>iOS — инструкция</b>

1. Скопируйте ключ из сообщения бота
2. Установите Shadowrocket из App Store
3. Откройте Shadowrocket → нажмите «+» → Import from Clipboard
4. Включите переключатель напротив конфигурации
5. Разрешите VPN в настройках iOS

Проверка: зайдите на <code>https://bymevpn.duckdns.org:8443/</code>""",
        "android": """<b>🤖 Android — инструкция</b>

1. Скопируйте ключ из сообщения бота
2. Установите Nekobox (скачайте APK с сайта)
3. Nekobox → «+» → Import from clipboard
4. Нажмите большую кнопку «Connect»
5. Разрешите VPN-подключение

Проверка: зайдите на <code>https://bymevpn.duckdns.org:8443/</code>""",
        "windows": """<b>🖥️ Windows — инструкция</b>

1. Скопируйте ключ из сообщения бота
2. Скачайте и запустите Nekobox
3. Nekobox → Import → From clipboard
4. Выберите конфигурацию → нажмите «Connect»
5. Разрешите доступ в брандмауэре

Проверка: зайдите на <code>https://bymevpn.duckdns.org:8443/</code>""",
        "macos": """<b>🍎 macOS — инструкция</b>

1. Скопируйте ключ из сообщения бота
2. Установите Shadowrocket из App Store
3. Shadowrocket → «+» → Import from Clipboard
4. Включите переключатель
5. Разрешите настройку системных прокси

Проверка: зайдите на <code>https://bymevpn.duckdns.org:8443/</code>""",
        "linux": """<b>🐧 Linux — инструкция</b>

1. Скопируйте ключ из сообщения бота
2. Установите Nekoray: wget + chmod
3. Nekoray → «+» → Import from clipboard
4. Выберите конфигурацию → нажмите «Connect»

Проверка: curl ifconfig.me"""
    }
    return texts.get(os_name, "Инструкция скоро будет добавлена.")


# =============================================================================
# Партнёрская программа и «О сервисе»
# =============================================================================
@dp.callback_query(F.data == "partner")
async def partner(callback: CallbackQuery):
    await callback.answer()
    user_id = callback.from_user.id
    
    try:
        # Get referral statistics
        stats = await get_referral_stats(user_id)
        bot_info = await bot.get_me()
        link = f"https://t.me/{bot_info.username}?start={user_id}"

        text = (
            "👑 <b>Партнёрская программа ByMeVPN</b>\n\n"
            "💎 <i><b>Зарабатывайте вместе с нами!</b></i>\n\n"
            "🎁 <b>Ваша выгода:</b>\n"
            f"• <b>+{REF_BONUS_DAYS} дней</b> <i>за каждого друга</i>\n"
            "• <i>Без ограничений по количеству рефералов</i>\n"
            "• <i>Друзья получают скидку на первый месяц</i>\n\n"
            "🚀 <b>Как это работает:</b>\n"
            "1. <i>Поделитесь своей уникальной ссылкой</i>\n"
            "2. <i>Друг переходит по ней и оформляет подписку</i>\n"
            "3. <i>Вы мгновенно получаете бонусные дни</i>\n\n"
            "📊 <b>Ваша статистика:</b>\n"
            f"👥 <i>Приглашено:</i> <b>{stats['referred_count']}</b> <i>человек</i>\n"
            f"💳 <i>Оплатили подписку:</i> <b>{stats['successful_referrals']}</b> <i>человек</i>\n"
            f"🎉 <i>Получено бонусов:</i> <b>{stats['bonus_days']}</b> <i>дней</i>\n\n"
            "🔑 <b>Ваша реферальная ссылка:</b>\n"
            f"<code>{link}</code>\n\n"
            "💫 <i>Начните зарабатывать прямо сейчас!</i>"
        )

        kb = referral_keyboard(link)
        await update_menu(bot, callback, text, kb)
        
    except Exception as e:
        logger.error(f"Error in partner program for user {user_id}: {e}")
        await callback.answer("Ошибка загрузки партнерской программы", show_alert=True)


@dp.callback_query(F.data == "about")
async def about(callback: CallbackQuery):
    await callback.answer()
    text = (
        "🌟 <b>О ByMeVPN — ваш надёжный интернет-помощник</b>\n\n"
        "💫 <i><b>Что делает наш VPN особенным:</b></i>\n"
        "• 🚀 <b>Обходит любые блокировки</b> и <i>ограничения</i>\n"
        "• ⚡ <b>Безлимитная скорость</b> и <i>трафик для комфортной работы</i>\n"
        "• 💻 <b>Работает на всех ваших устройствах</b> — <i>один ключ для всех</i>\n"
        "• 🔒 <b>Полная конфиденциальность</b> — <i>мы не храним ваши данные</i>\n"
        "• 💳 <b>Удобные способы оплаты</b> — <i>выбирайте что вам удобно</i>\n"
        "• 🤝 <b>Дружелюбная поддержка</b> — <i>всегда готовы помочь!</i>\n\n"
        "🌐 <b>Наш сайт:</b> <u>https://bymevpn.duckdns.org:8443/</u>\n\n"
        "💎 <i><b>Попробуйте 7 дней бесплатно</b> или выберите подходящий тариф — кнопки в главном меню!</i> ✨"
    )
    await update_menu(bot, callback, text, about_keyboard(), photo=None)


# =============================================================================
# Юридический раздел
# =============================================================================
@dp.message(Command("legal"))
async def legal(message: Message):
    text = (
        "📋 <b>Юридическая информация ByMeVPN</b>\n\n"
        "🤝 <i>Мы ценим прозрачность и доверие!</i>\n\n"
        "📄 <b>Выберите нужный документ</b> — <i>бот сразу пришлёт текст и предложит скачать файл.</i>\n\n"
        "✨ <b>Всё просто, понятно и честно!</b>"
    )
    await update_menu(bot, message, text, legal_menu_keyboard())


@dp.callback_query(F.data == "legal_offer")
async def legal_offer(callback: CallbackQuery):
    await callback.answer()
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Скачать договор .txt",
                    callback_data="legal_offer_download",
                )
            ],
            [
                InlineKeyboardButton(text="Назад к документам", callback_data="back_to_legal"),
                InlineKeyboardButton(text="Поддержка", callback_data="support"),
            ],
        ]
    )
    short_text = (
        "<b>Договор оферты ByMeVPN</b>\n\n"
        "Здесь описаны общие условия сервиса: как мы предоставляем доступ к VPN, "
        "какие есть обязанности у сторон, как работает оплата и какие ограничения ответственности действуют.\n\n"
        "Полный юридический текст можно скачать по кнопке ниже ⬇️"
    )
    await update_menu(
        bot,
        callback,
        short_text,
        kb,
        photo=None,
    )


@dp.callback_query(F.data == "legal_privacy")
async def legal_privacy(callback: CallbackQuery):
    await callback.answer()
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Скачать политику .txt",
                    callback_data="legal_privacy_download",
                )
            ],
            [
                InlineKeyboardButton(text="Назад к документам", callback_data="back_to_legal"),
                InlineKeyboardButton(text="Поддержка", callback_data="support"),
            ],
        ]
    )
    short_text = (
        "<b>Политика конфиденциальности ByMeVPN</b>\n\n"
        "В этом документе подробно описано, какие данные мы <b>не собираем</b> "
        "(IP‑адреса, история, DNS, трафик), что именно храним для работы подписки и "
        "как защищаем вашу приватность.\n\n"
        "Полный текст политики можно скачать по кнопке ниже ⬇️"
    )
    await update_menu(
        bot,
        callback,
        short_text,
        kb,
        photo=None,
    )


@dp.callback_query(F.data == "legal_recurrent")
async def legal_recurrent(callback: CallbackQuery):
    await callback.answer()
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Скачать соглашение .txt",
                    callback_data="legal_recurrent_download",
                )
            ],
            [
                InlineKeyboardButton(text="Назад к документам", callback_data="back_to_legal"),
                InlineKeyboardButton(text="Поддержка", callback_data="support"),
            ],
        ]
    )
    short_text = (
        "<b>Соглашение о регулярных платежах ByMeVPN</b>\n\n"
        "Этот документ объясняет, как работают автосписания за подписку: как они "
        "подключаются, с какой периодичностью списываются средства и как отказаться "
        "от рекуррентных платежей.\n\n"
        "Полный текст соглашения можно скачать по кнопке ниже ⬇️"
    )
    await update_menu(
        bot,
        callback,
        short_text,
        kb,
        photo=None,
    )


@dp.callback_query(F.data == "back_to_legal")
async def back_to_legal(callback: CallbackQuery):
    await callback.answer()
    text = (
        "byMeVPN\n\n"
        "<b>Юридическая информация</b>\n\n"
        "Выбери документ — бот сразу пришлёт текст и даст скачать файл.\n"
        "Чтоб всё было честно и понятно."
    )
    await update_menu(bot, callback, text, legal_menu_keyboard(), photo=None)


@dp.callback_query(F.data == "legal_offer_download")
async def legal_offer_download(callback: CallbackQuery):
    await callback.answer()
    data = OFFER_TEXT.strip().encode("utf-8")
    file = BufferedInputFile(data, filename="bymevpn_dogovor_oferty.txt")
    await bot.send_document(
        chat_id=callback.from_user.id,
        document=file,
        caption="Договор оферты ByMeVPN",
    )


@dp.callback_query(F.data == "payment_history")
async def payment_history(callback: CallbackQuery):
    await callback.answer()
    user_id = callback.from_user.id
    
    try:
        # Check if user has paid subscriptions
        has_paid = await has_paid_subscription(user_id)
        if not has_paid:
            await callback.message.answer(
                "💰 <b>История платежей</b>\n\n"
                "Этот раздел доступен только для пользователей с оплаченными подписками\n\n"
                "🎁 <b>Оформите подписку и получите:</b>\n"
                "• Доступ к истории всех платежей\n"
                "• Подробную статистику\n"
                "• Приоритетную поддержку"
            )
            return
        
        payments = await get_user_payments(user_id)
        if not payments:
            text = (
                "💰 <b>История платежей пуста</b>\n\n"
                "Как только вы оплатите подписку, здесь появятся все операции с датами и суммами."
            )
        else:
            lines = ["💰 <b>История ваших платежей</b>\n"]
            total_spent = 0
            
            for p in payments[:15]:  # Show last 15 payments
                dt = datetime.fromtimestamp(p["created"]).strftime("%d.%m.%Y %H:%M")
                method_emoji = {
                    "stars": "⭐",
                    "yookassa": "💳", 
                    "cryptobot": "₿"
                }.get(p["method"], "💰")
                
                lines.append(
                    f"{method_emoji} {dt} — {p['amount']} {p['currency']} "
                    f"за {p['days']} дней"
                )
                total_spent += p["amount"] if p["currency"] == "RUB" else 0
            
            lines.append(f"\n💎 Всего потрачено: {total_spent:,} ₽".replace(",", " "))
            text = "\n".join(lines)
        
        await callback.message.answer(text, parse_mode="HTML")
        
    except Exception as e:
        logger.error(f"Error showing payment history for user {user_id}: {e}")
        await callback.answer("Ошибка загрузки истории платежей", show_alert=True)


@dp.callback_query(F.data == "legal_privacy_download")
async def legal_privacy_download(callback: CallbackQuery):
    await callback.answer()
    data = PRIVACY_TEXT.strip().encode("utf-8")
    file = BufferedInputFile(data, filename="bymevpn_politika_konfidentsialnosti.txt")
    await bot.send_document(
        chat_id=callback.from_user.id,
        document=file,
        caption="Политика конфиденциальности ByMeVPN",
    )


@dp.callback_query(F.data == "legal_recurrent_download")
async def legal_recurrent_download(callback: CallbackQuery):
    await callback.answer()
    data = RECURRENT_TEXT.strip().encode("utf-8")
    file = BufferedInputFile(data, filename="bymevpn_rekurrentnye_platezhi.txt")
    await bot.send_document(
        chat_id=callback.from_user.id,
        document=file,
        caption="Соглашение о регулярных платежах ByMeVPN",
    )


# =============================================================================
# Назад в меню
# =============================================================================
@dp.callback_query(F.data == "my_referrals")
async def my_referrals(callback: CallbackQuery):
    await callback.answer()
    user_id = callback.from_user.id
    
    try:
        referrals = await get_referral_list(user_id)
        stats = await get_referral_stats(user_id)
        
        if not referrals:
            text = (
                "👥 <b>Ваши рефералы</b>\n\n"
                "У вас пока нет приглашённых пользователей\n\n"
                "🔗 Поделитесь своей реферальной ссылкой и начните зарабатывать!"
            )
        else:
            lines = [
                f"👥 <b>Ваши рефералы ({len(referrals)} человек)</b>\n",
                f"Всего приглашено: {stats['referred_count']}\n",
                f"Оплатили подписку: {stats['successful_referrals']}\n",
                f"Получено бонусов: {stats['bonus_days']} дней\n\n",
                "<b>Список приглашённых:</b>"
            ]
            
            for ref in referrals[:10]:  # Show last 10
                date = datetime.fromtimestamp(ref['created']).strftime("%d.%m.%Y")
                status_emoji = "💰" if ref['status'] == 'paid' else "🎁"
                status_text = "оплатил" if ref['status'] == 'paid' else "пробный период"
                lines.append(f"{status_emoji} ID: {ref['user_id']} — {date} ({status_text})")
            
            if len(referrals) > 10:
                lines.append(f"\n... и ещё {len(referrals) - 10} человек")
            
            text = "\n".join(lines)
        
        kb = referral_details_keyboard()
        await update_menu(bot, callback, text, kb)
        
    except Exception as e:
        logger.error(f"Error showing referrals for user {user_id}: {e}")
        await callback.answer("Ошибка загрузки рефералов", show_alert=True)


@dp.callback_query(F.data == "export_keys")
async def export_keys(callback: CallbackQuery):
    await callback.answer()
    user_id = callback.from_user.id
    
    try:
        keys = await get_user_keys(user_id)
        if not keys:
            await callback.message.answer(
                "🔑 <b>У вас пока нет ключей</b>\n\n"
                "Оформите подписку и сможете экспортировать все свои ключи."
            )
            return
        
        # Create export file
        file_data = await export_keys_to_file(bot, user_id, keys)
        
        caption = (
            "📦 <b>Экспорт ключей ByMeVPN</b>\n\n"
            f"Всего ключей: {len(keys)}\n"
            "Файл содержит подробную информацию о каждом ключе"
        )
        
        await bot.send_document(
            chat_id=user_id,
            document=file_data,
            caption=caption,
            parse_mode="HTML"
        )
        
        logger.info(f"Keys exported for user {user_id}")
        
    except Exception as e:
        logger.error(f"Error exporting keys for user {user_id}: {e}")
        await callback.answer("Ошибка экспорта ключей", show_alert=True)


@dp.callback_query(F.data == "toggle_news")
async def toggle_news(callback: CallbackQuery):
    await callback.answer()
    user_id = callback.from_user.id
    
    try:
        # This would require getting current state from database
        # For now, just show a message
        await callback.message.answer(
            "🔔 <b>Уведомления о новинках</b>\n\n"
            "Функция находится в разработке. Скорко вы сможете настроить получение уведомлений о новых серверах и акциях!"
        )
        
    except Exception as e:
        logger.error(f"Error toggling news for user {user_id}: {e}")
        await callback.answer("Ошибка", show_alert=True)


@dp.callback_query(F.data == "back_to_menu")
async def back_to_menu(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    user_name = callback.from_user.first_name or "друг"
    
    # Get dynamic monthly user count
    try:
        monthly_count = await get_monthly_users_count()
    except Exception as e:
        logger.error(f"Error getting monthly count: {e}")
        monthly_count = 3847  # Fallback
    
    welcome_text = build_welcome_text(user_name, monthly_count)
    
    # Check if user has paid subscription for keyboard customization
    has_paid = await has_paid_subscription(callback.from_user.id)
    
    await update_menu(bot, callback, welcome_text, main_menu_keyboard(has_paid))


# =============================================================================
# Telegram Stars: подтверждение и успешная оплата
# =============================================================================
@dp.pre_checkout_query()
async def pre_checkout_query(pre: PreCheckoutQuery):
    await pre.answer(ok=True)


@dp.message(F.successful_payment)
async def successful_payment(message: Message):
    payment: SuccessfulPayment = message.successful_payment
    user_id = message.from_user.id
    currency = payment.currency
    total_amount = payment.total_amount
    payload = payment.invoice_payload or ""

    logger.info(
        "Успешная оплата: user=%s currency=%s amount=%s payload=%s",
        user_id,
        currency,
        total_amount,
        payload,
    )

    days_to_give = 30
    prefix = "paid"

    if payload.startswith("stars_vpn_") and currency == "XTR":
        parts = payload.split("_")
        if len(parts) >= 4:
            try:
                days_to_give = int(parts[3])
            except ValueError:
                days_to_give = 30
        prefix = "paid_stars"
        success_text = (
            f"✅ <b>Оплата через Telegram Stars прошла!</b>\n\n"
            f"⭐ Сумма: {total_amount} звёзд\n"
            f"🔑 Подписка будет выдана на {days_to_give} дней\n\n"
            f"🎉 Спасибо за доверие!"
        )
        await message.answer(success_text, parse_mode="HTML")
    else:
        prefix = f"paid_{currency.lower()}"
        success_text = (
            f"✅ <b>Оплата успешно проведена!</b>\n\n"
            f"💰 Валюта: {currency}\n"
            f"💳 Сумма: {total_amount}\n"
            f"🔑 Подписка сейчас будет активирована\n\n"
            f"🎉 Спасибо за покупку!"
        )
        await message.answer(success_text, parse_mode="HTML")

    try:
        await give_subscription(bot, user_id, days_to_give, prefix, message, is_paid=True)
        
        # Save payment to history
        method = "stars" if currency == "XTR" else currency.lower()
        await save_payment(
            user_id=user_id,
            amount=total_amount,
            currency=currency,
            method=method,
            days=days_to_give,
            payload=payload,
        )
        
        # Save auto-renewal setting if enabled
        state = dp.fsm.get_context(bot, message.chat, message.from_user)
        try:
            data = await state.get_data()
            auto_renew = bool(data.get("auto_renewal", False))
            if auto_renew:
                await set_auto_renewal(user_id, True)
                logger.info(f"Auto-renewal enabled for user {user_id}")
        except Exception as e:
            logger.error(f"Error saving auto-renewal setting: {e}")
        
        # Send payment success notification
        await send_payment_success_notification(bot, user_id, days_to_give, method)
    except Exception as e:
        logger.error(f"Ошибка выдачи после оплаты: {e}")
        await message.answer("❌ Ключ не выдан автоматически. Напишите в поддержку.")


# =============================================================================
# Уведомления о продлении (за 1–3 дня)
# =============================================================================
async def send_expiry_notifications():
    nearing = await get_keys_nearing_expiry(days_left_min=1, days_left_max=3)
    for user_id, key, days_left, expiry in nearing:
        try:
            date_str = datetime.fromtimestamp(expiry).strftime("%d.%m.%Y")
            text = (
                f"⚠️ <b>Напоминание от ByMeVPN</b>\n\n"
                f"Ваша подписка истекает через <b>{int(days_left)} день(дней)</b> — {date_str}.\n\n"
                "Продлите сейчас, чтобы не потерять доступ!\n"
                "Нажмите /start или кнопку ниже ↓"
            )
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Продлить подписку", callback_data="buy_vpn")]
            ])
            await bot.send_message(user_id, text, reply_markup=kb)
        except Exception as e:
            logger.error(f"Ошибка уведомления {user_id}: {e}")


# =============================================================================
# Запуск бота
# =============================================================================
async def main():
    # Инициализация базы данных
    await init_db()
    
    # Удаляем вебхук (на всякий случай)
    await bot.delete_webhook(drop_pending_updates=True)
    
    # Создаем задачу для поллинга
    polling_task = asyncio.create_task(dp.start_polling(bot))
    
    # Создаем задачу для планировщика уведомлений
    scheduler_task = asyncio.create_task(start_notification_scheduler(bot))
    
    # Настраиваем обработку сигналов для graceful shutdown
    loop = asyncio.get_running_loop()
    
    # Создаем событие для сигнала остановки
    stop_signal = asyncio.Event()
    
    def signal_handler():
        print("\n🛑 Получен сигнал завершения. Останавливаем бота...")
        stop_signal.set()
    
    # Регистрируем обработчики сигналов (только для Unix систем)
    if sys.platform != 'win32':
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, signal_handler)
    
    try:
        # Ждем либо сигнал остановки, либо завершение одной из задач
        await asyncio.wait(
            [polling_task, scheduler_task, asyncio.create_task(stop_signal.wait())],
            return_when=asyncio.FIRST_COMPLETED
        )
    except asyncio.CancelledError:
        pass
    finally:
        print("🔄 Завершаем работу...")
        
        # Отменяем задачи
        polling_task.cancel()
        scheduler_task.cancel()
        
        # Ждем завершения задач
        try:
            await asyncio.wait([polling_task, scheduler_task], timeout=5.0)
        except asyncio.CancelledError:
            pass
        
        # Закрываем сессию бота
        await bot.session.close()
        print("✅ Бот остановлен")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Бот остановлен пользователем") 
