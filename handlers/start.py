import time
import logging
from typing import Union

from aiogram import Bot, F, Router
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery

from config import ADMIN_ID, SUPPORT_USERNAME
from database import (
    get_user_keys,
    has_active_subscription,
    has_paid_subscription,
    get_referrer,
    set_referrer,
    get_monthly_users_count,
    has_used_trial,
    mark_trial_used,
)
from emojis import get_emoji, EmojiTheme
from keyboards import (
    main_menu_keyboard,
    expired_subscription_keyboard,
    instructions_keyboard,
    about_keyboard,
    back_to_menu_keyboard,
)
from utils import update_menu
from utils import add_client, generate_vless

logger = logging.getLogger(__name__)

router = Router()


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


async def give_subscription(
    bot: Bot,
    user_id: int,
    days: int,
    prefix: str,
    target: Union[Message, CallbackQuery],
    is_paid: bool = False
) -> None:
    """Выдача подписки пользователю"""
    try:
        logger.info(f"Giving subscription: user_id={user_id}, days={days}, prefix={prefix}, is_paid={is_paid}")
        
        remark = f"{prefix}_{user_id}_{int(time.time())}"
        uuid_c = await add_client(days, remark)
        key = generate_vless(uuid_c)
        from database import add_user_key
        await add_user_key(user_id, key, remark, days)

        chat_id = target.chat.id if isinstance(target, Message) else target.message.chat.id

        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
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

        from utils import generate_and_send_beautiful_qr
        await generate_and_send_beautiful_qr(bot, chat_id, key, success_text, reply_markup=kb)

    except Exception as e:
        logger.exception("Ошибка выдачи подписки")
        chat_id = target.chat.id if isinstance(target, Message) else target.message.chat.id
        error_text = (
            "<b>Ошибка при выдаче ключа</b>\n\n"
            "Что-то пошло не так. Пожалуйста, напишите в поддержку — мы поможем!\n\n"
            "Техподдержка: @ByMeVPN_support"
        )
        await bot.send_message(chat_id, error_text, parse_mode="HTML")


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    """Обработчик команды /start"""
    await state.clear()

    # Рефералка
    args = message.text.split()
    if len(args) > 1 and args[1].isdigit():
        ref_id = int(args[1])
        if ref_id != message.from_user.id and not await get_referrer(message.from_user.id):
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
        await update_menu(message.bot, message, welcome_text, main_menu_keyboard(has_paid, has_premium))
    else:
        keys = await get_user_keys(message.from_user.id)
        if len(keys) > 0:
            # User has expired keys - show expired subscription menu
            expired_text = (
                f"{get_emoji('warning', EmojiTheme.WARNING)} <b>Подписка ByMeVPN истекла</b>\n\n"
                "Ваш пробный период завершился. Продлите подписку для продолжения использования.\n\n"
                f"{get_emoji('star', EmojiTheme.PREMIUM)} Выберите новый тариф ниже:"
            )
            await update_menu(message.bot, message, expired_text, expired_subscription_keyboard())
        else:
            # New user - show welcome with trial
            await update_menu(message.bot, message, welcome_text, main_menu_keyboard(has_paid, has_premium))


@router.callback_query(F.data == "trial")
async def give_trial(callback: CallbackQuery):
    """Выдача пробного периода"""
    user_id = callback.from_user.id
    if await has_used_trial(user_id):
        await callback.answer("Вы уже использовали пробный период.", show_alert=True)
        return
    await give_subscription(callback.bot, user_id, 7, "trial", callback)
    await mark_trial_used(user_id)


@router.callback_query(F.data == "my_keys")
async def show_my_keys(callback: CallbackQuery):
    """Показать ключи пользователя"""
    keys = await get_user_keys(callback.from_user.id)
    if not keys:
        text = (
            "🔑 <b>У вас пока нет активных ключей</b>\n\n"
            "🎁 <i>Можно начать с</i> <b>пробного периода на 7 дней</b> <i>или сразу оформить подписку.</i>\n\n"
            "💫 <b>Нажмите</b> «7 дней бесплатно» <i>или</i> «Купить от 58₽/мес» <i>в главном меню, чтобы получить доступ.</i>"
        )
    else:
        from utils import format_expiry_date
        lines = []
        for k in keys:
            expires = format_expiry_date(k["expiry"])
            lines.append(f"🔐 <b>{k['remark']}</b>\n<i>Истекает:</i> {expires}\n<code>{k['key']}</code>")
        text = "💎 <b>Ваши ключи</b>\n\n" + "\n".join(lines)
    
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📱 Инструкция подключения", callback_data="instructions")],
        [InlineKeyboardButton(text="⬅️ Назад в меню", callback_data="back_to_menu")]
    ])
    await update_menu(callback.bot, callback, text, kb)


@router.callback_query(F.data == "support")
async def support(callback: CallbackQuery):
    """Поддержка"""
    await callback.answer()
    
    try:
        from urllib.parse import quote_plus
        support_text = "Здравствуйте! У меня вопрос по ByMeVPN. Помогите, пожалуйста!"
        encoded = quote_plus(support_text)
        url = f"https://t.me/{SUPPORT_USERNAME.lstrip('@')}?text={encoded}"

        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
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
            "🤝 <b>Напишите нам — и мы поможем в ближайшее время!</b>"
        )

        await callback.bot.send_message(
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


@router.callback_query(F.data == "about")
async def about(callback: CallbackQuery):
    """О сервисе"""
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
    await update_menu(callback.bot, callback, text, about_keyboard(), photo=None)


@router.callback_query(F.data == "instructions")
async def instructions(callback: CallbackQuery):
    """Инструкции по подключению"""
    await callback.answer()
    kb = instructions_keyboard()
    await update_menu(
        callback.bot,
        callback,
        "<b>Выберите вашу платформу:</b>",
        kb,
    )




@router.callback_query(F.data == "back_to_menu")
async def back_to_menu(callback: CallbackQuery, state: FSMContext):
    """Возврат в главное меню"""
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
    
    await update_menu(callback.bot, callback, welcome_text, main_menu_keyboard(has_paid))