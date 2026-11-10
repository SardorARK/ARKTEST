"""Раздел «Настройки».

Что здесь есть:
  * Никнейм в боте. Игрок присылает имя сообщением: форматирование (жирный, курсив,
    подчёркнутый, зачёркнутый, моноширинный) и кастомные эмодзи сохраняются. Это имя
    показывается в игре вместо имени из Telegram (главное меню, профиль, исследователи).
    Другим модулям достаточно вызвать name_html(user) (для текста) или name_plain(user)
    (для кнопок и уведомлений).
  * Уведомления: «отряд вернулся из экспедиции». Фоновая задача notifier_loop шлёт
    сообщение, когда экспедиция закончилась. Список ожидающих уведомлений хранится в
    alerts.json рядом с базой, поэтому бот не перебирает всех игроков.
  * Приватность: режим скрытости. Скрытый игрок показывается другим как «Скрытый игрок»
    (public_name_html / public_name_plain), а сам видит своё имя как обычно.
  * Помощь и FAQ: «Как играть», «Что даёт уровень», «Как работают артефакты», «Как получить
    монеты». Числа берутся из настроек игры, поэтому текст не расходится с реальным балансом.
  * Поддержка: прямая ссылка на @IIARKII и «Сообщить о проблеме» (сообщение с ID игрока уходит
    всем админам, с защитой от спама).
  * Информация об игроке: никнейм, ID, дата регистрации, уровень и бои.

Защита никнейма: длина, запрет ссылок и упоминаний, запрет переносов строк, пауза между
сменами, список запрещённых слов (BANNED_PARTS), проверка сохранённого HTML при каждом показе,
запись смен в audit.log. Админ может сбросить чужой никнейм из карточки игрока.

Данные игрока:
    user["nick"]        HTML никнейма (только безопасные теги)
    user["nick_plain"]  тот же никнейм обычным текстом
    user["nick_at"]     время последней смены
    user["tg_name"]     имя из Telegram (запасной вариант, обновляет панель админа)
    user["settings"]    {"notify_exrn": True/False, "hidden": True/False}
    user["support_at"], user["support_day"]  защита обращений в поддержку

Подключение в main.py: settings.setup(...), dp.include_router(settings.router) и запуск
settings.notifier_loop(bot) отдельной задачей. Старый обработчик «Настройки» в main.py нужно убрать.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from html import escape
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.enums import ButtonStyle
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.text_decorations import html_decoration

try:
    from storage import atomic_write_json, ledger as _ledger, read_json_safe
except ImportError:                      # запуск модуля отдельно (например, в тестах)
    _ledger = None
    atomic_write_json = read_json_safe = None

log = logging.getLogger(__name__)
router = Router()

# Куда админ может добавить фото
IMG_TARGETS: dict[str, str] = {
    "settings": "Настройки",
    "settings_nick": "Настройки: никнейм",
    "settings_skin": "Настройки: скин никнейма",
    "settings_notify": "Настройки: уведомления",
    "settings_privacy": "Настройки: приватность",
    "settings_help": "Настройки: помощь (темы)",
    "settings_support": "Настройки: поддержка",
}

# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------

NICK_MIN = 2                     # минимум видимых символов
NICK_MAX = 24                    # максимум видимых символов
NICK_COOLDOWN = 300              # пауза между сменами никнейма, секунд
NICK_PATTERN = re.compile(r"[A-Za-z0-9]+")   # в никнейме только английские буквы и цифры
BANNED_PARTS: tuple[str, ...] = ()   # запрещённые слова (в нижнем регистре), например ("admin",)
RESERVED_BUTTONS = ("Меню", "Арена", "Рынок", "Настройки")   # кнопки нижней клавиатуры

# Какое форматирование сохраняем (остальное, например ссылки, отбрасывается)
ALLOWED_ENTITIES = {"bold", "italic", "underline", "strikethrough", "code", "custom_emoji"}
# Что запрещено в никнейме целиком
FORBIDDEN_ENTITIES = {"url", "text_link", "mention", "text_mention", "email", "phone_number",
                      "hashtag", "cashtag", "bot_command"}

ALERT_TICK = 20                  # как часто проверять уведомления, секунд
ALERTS_PER_TICK = 30             # сколько уведомлений отправлять за один проход
DEFAULT_SETTINGS = {"notify_exrn": True, "hidden": False}
HIDDEN_NAME = "Скрытый игрок"    # так видят игрока с режимом скрытости

SUPPORT_USERNAME = "IIARKII"                       # поддержка в Telegram
SUPPORT_URL = f"https://t.me/{SUPPORT_USERNAME}"
SUPPORT_MAX_LEN = 800            # длина обращения
SUPPORT_COOLDOWN = 60            # пауза между обращениями, секунд
SUPPORT_PER_DAY = 5              # обращений в сутки от одного игрока

# Кастомный эмодзи заголовка (тот же, что на кнопке «Настройки»)
SETTINGS_EMOJI = ("5341715473882955310", "⚙️")

# Эмодзи пунктов настроек: (id, запасной обычный эмодзи)
EM_NICK = ("5197269100878907942", "✍️")        # никнейм
EM_SKIN_ON = ("5390846416530716134", "✅")      # галочка на надетом скине
EM_NOTIF = ("5458603043203327669", "🔔")       # уведомления
EM_PRIV = ("5843826335088120045", "💬")        # приватность
EM_HELP = ("5436113877181941026", "❓")        # помощь
EM_SUPPORT = ("5393061004747750741", "🤩")     # поддержка
EM_ID = ("5323442290708985472", "©")          # ID
EM_DATE = ("5413879192267805083", "🗓")       # «в игре с»
EM_OFF = ("5328093774650293878", "❌")        # «выключено»
EM_USER = ("5275979556308674886", "👤")       # перед именем игрока (меню, профиль)

# ---------------------------------------------------------------------------
# Подключение к боту
# ---------------------------------------------------------------------------

_get_user = None
_save_users = None
_show_rich_card = None
_send_rich_card = None
_custom_emoji = None
_BACK_ID: str | None = None
_INFO = ("5334544901428229844", "ℹ️")
_ALERTS_FILE: Path | None = None
_ALERTS: dict[str, float] = {}   # uid -> когда закончилась (или закончится) экспедиция


_ADMIN_IDS: set[int] = set()
_OPEN_PROFILE = None
_GAME: dict = {}                 # числа игры для раздела «Помощь» (уровни, цены, шансы)


# Кастомные эмодзи других разделов (передаёт main.py): ключ -> (id, запасной эмодзи)
_EMOJI: dict[str, tuple[str, str]] = {}
_FALLBACK = {"pet": "🐾", "level": "🆙", "wins": "⚔️", "losses": "☠️", "mine": "⛏", "boss": "⚔️",
             "explore": "🗺", "treasury": "🧩", "coin": "🪙", "art": "💎", "expedition": "🗺",
             "ok": "✅", "play": "🎮"}


def _ic(key: str) -> str:
    """Эмодзи раздела: кастомный из других модулей, а если его нет, обычный."""
    pair = _EMOJI.get(key)
    if pair and _custom_emoji:
        return _custom_emoji(pair)
    return _FALLBACK.get(key, "")


def setup(*, get_user, save_users, show_rich_card, send_rich_card, custom_emoji,
          BACK_EMOJI_ID, INFO_EMOJI=None, alerts_path=None, admin_ids=(), game=None,
          emoji=None, open_profile=None, **_ignored) -> None:
    """game: числа игры для текстов помощи: MAX_LEVEL, EVO_EVERY, EVO_COST_MULT, STAT_GROWTH,
    EVO_GROWTH, upgrade_cost(level), SLOTS, DUPLICATE_COINS, FRAG_DAMAGE_STEP, FRAG_CHANCE."""
    global _get_user, _save_users, _show_rich_card, _send_rich_card, _custom_emoji
    global _BACK_ID, _INFO, _ALERTS_FILE, _ADMIN_IDS, _GAME, _OPEN_PROFILE
    _OPEN_PROFILE = open_profile          # async (bot, chat_id, user_id, note): показать «Профиль»
    _EMOJI.update(emoji or {})
    _ADMIN_IDS = set(admin_ids)
    _GAME = dict(game or {})
    _get_user, _save_users = get_user, save_users
    _show_rich_card, _send_rich_card = show_rich_card, send_rich_card
    _custom_emoji, _BACK_ID = custom_emoji, BACK_EMOJI_ID
    if INFO_EMOJI:
        _INFO = INFO_EMOJI
    if alerts_path:
        _ALERTS_FILE = Path(alerts_path)
        _load_alerts()


def _led(name: str, /, **fields) -> None:
    if _ledger is None:
        return
    try:
        _ledger(name, **fields)
    except Exception:
        log.exception("не удалось записать в audit.log: %s", name)


# ---------------------------------------------------------------------------
# Никнейм: безопасное имя для показа в других разделах
# ---------------------------------------------------------------------------

_TAG = re.compile(r'<(/?)(b|i|u|s|code|tg-emoji)((?:\s+emoji-id="\d{1,30}")?)>')


def _valid_html(value) -> bool:
    """Проверяет, что в сохранённом HTML только разрешённые теги, они закрыты и вложены верно."""
    if not isinstance(value, str) or not value or len(value) > 2000:
        return False
    stack: list[str] = []
    pos = 0
    for m in _TAG.finditer(value):
        if "<" in value[pos:m.start()] or ">" in value[pos:m.start()]:
            return False
        closing, tag, attrs = m.group(1), m.group(2), m.group(3)
        if closing:
            if attrs or not stack or stack.pop() != tag:
                return False
        else:
            if (tag == "tg-emoji") != bool(attrs):
                return False
            stack.append(tag)
        pos = m.end()
    tail = value[pos:]
    return not stack and "<" not in tail and ">" not in tail


# ---------------------------------------------------------------------------
# Скины никнейма: имя показывается кастомными эмодзи-буквами
# ---------------------------------------------------------------------------

_LETTER_IDS = [
    "5442666386662926782", "5442789489015567725", "5442888268968407239", "5443067296090205241",
    "5442930565806339502", "5445045210134392285", "5442904770232759662", "5444888546907312649",
    "5442948978331134970", "5443056721880718834", "5442889935415718340", "5443093181858100144",
    "5442964612012092458", "5443144742940485489", "5445160306667986067", "5444872745722616155",
    "5444912749048016606", "5443041805459303246", "5443129860878806759", "5443023517488557215",
    "5442904907671714022", "5442786624272378035", "5442817560921810165", "5445399433267162730",
    "5443021812386537711", "5442665196956985605",
]   # A-Z по алфавиту
_DIGIT_IDS = [
    "5445292643200313328", "5445356758472109209", "5445093764739674361", "5445133531841865213",
    "5445156909348857969", "5445069798822159353", "5442808331037096630", "5445321239092568970",
    "5442792065995942039", "5444961204869044774",
]   # 0-9
_DIGIT_FALLBACK = ["0️⃣", "1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣"]

EMOJI_LETTERS: dict[str, tuple[str, str]] = {}
for _i, _id in enumerate(_LETTER_IDS):
    EMOJI_LETTERS[chr(ord("A") + _i)] = (_id, "🔤")
for _i, _id in enumerate(_DIGIT_IDS):
    EMOJI_LETTERS[str(_i)] = (_id, _DIGIT_FALLBACK[_i])

# Донатные наборы: A-Z, затем 0-9 (порядок как в наборе эмодзи)
_SET1_IDS = [
    "5442656319259587462", "5442941067001377271", "5445198733740384645", "5442721276344971174",
    "5443069246005358218", "5444919251628500977", "5445342722518983442", "5445008848941262321",
    "5442984093983745264", "5442956112271815287", "5442956382854755407", "5443110636605188325",
    "5445340394646710021", "5442728418875584162", "5445232964629735264", "5442710757970065562",
    "5443007346936688048", "5445313830273988012", "5443027902650164419", "5444900955067821500",
    "5442879936731849745", "5445017670804087756", "5442727499752579967", "5445408976684492861",
    "5445312747942223346", "5442724531930185855",                                   # A-Z
    "5444879596195458584", "5442915971507466800", "5443124268831387407", "5445381299915240220",
    "5443036063088026470", "5442839744427894778", "5444974545037470548", "5443034929216665764",
    "5445214938651992882", "5445184130851579828",                                   # 0-9
]
_SET2_IDS = [
    "5444855278090626891", "5445049006885479486", "5442892813043805793", "5445161320280271117",
    "5445309397867734511", "5443144047155785124", "5443112096894071108", "5443097781768072422",
    "5442778266266022259", "5445037268739860933", "5442606540588623052", "5442963843212946555",
    "5442953866003921707", "5442837377900917600", "5445374213219199450", "5444998953336614272",
    "5443059891566588262", "5442748081235867022", "5443126725552678443", "5442960806671068172",
    "5444953808935363375", "5444929151528119060", "5445108616736579735", "5443122379045778120",
    "5442892585410539351", "5442904027203416832",                                   # A-Z
    "5442619163497507457", "5442834294114399292", "5443089793128903783", "5445010794561448165",
    "5442601120339917111", "5443147642043413120", "5445103724768829237", "5442642549594438022",
    "5443131119304227142", "5442601704455448469",                                   # 0-9
]

_SET3_IDS = [
    "5442905938463864208", "5442873326777193264", "5442626799949359793", "5445049758504759589",
    "5442839276276460237", "5442662650041382113", "5445161513553798120", "5442864045352856510",
    "5442691202983965929", "5442777407272560880", "5443114884327845929", "5442848995787450838",
    "5442969224806971162", "5442962627737200002", "5442775796659825108", "5442658488218073482",
    "5443126605293596617", "5442780984980317458", "5443025484583583178", "5442652621292744996",
    "5442990394700769286", "5443082616238547471", "5445062639111678606", "5445408036086653841",
    "5445050218066255798", "5443104748205027683",
    # A-Z
    "5442785632134934666", "5442763959729957991", "5445261753795521976", "5442724686549002058",
    "5444867218099708614", "5445084934286911833", "5442617320956539979", "5445280814860380068",
    "5442740526388393291", "5442724768153384890",
    # 0-9
]

_SET4_IDS = [
    "5443136251790141987", "5443088822466292383", "5442929543604119526", "5445104862935164904",
    "5443012750005546924", "5442915052384462351", "5443057194327124056", "5443032485380276474",
    "5442872652467321540", "5445195851817330643", "5442902523964863910", "5442765931119946087",
    "5442802047499939385", "5445053387752125988", "5445383958499994341", "5442918595732485089",
    "5445241769312691916", "5442765256810082801", "5445412580162053830", "5442665227021758452",
    "5445241309751189954", "5445207551308244294", "5444899563498417344", "5442890425041989332",
    "5442649086534659974", "5444922494328809769",
    # A-Z
    "5444864533745151082", "5442607781834175220", "5442960987059696065", "5445262144637545074",
    "5442759497258938360", "5442788458223417407", "5442792349463787231", "5445204944263095564",
    "5442666807569721925", "5445171778525636950",
    # 0-9
]

_SET5_IDS = [
    "5442959372151998138", "5442762276102779803", "5442693517971338489", "5442871278077782867",
    "5442604861256415012", "5444983723382582138", "5445413288831657327", "5442633306824814289",
    "5443149815296864427", "5445093257933528987", "5445382584110461909", "5445186849565880747",
    "5445047546596598375", "5444900091779392489", "5445349907999272056", "5444957678700897089",
    "5442619919411753003", "5442876105621019013", "5443025853950765891", "5442905057995567900",
    "5442961876117924770", "5443053324561590699", "5442714683570170050", "5443022602660525110",
    "5445213705996379703", "5442651719349611263",
    # A-Z
    "5443071685546795971", "5442746973134301641", "5445049122849596653", "5445168776343496183",
    "5443139133713198691", "5442956760811875146", "5445375974155790096", "5442875908052529360",
    "5444967170578621165", "5445071933420905368",
    # 0-9
]

_SET6_IDS = [
    "5445224310270633584", "5442637734936098023", "5444957425297827744", "5445284555776893202",
    "5445190732216312916", "5443149604843464892", "5443013888171876372", "5442670587140946671",
    "5443147002093284912", "5442987452648171049", "5442774825997216466", "5442794243544361330",
    "5445390297871723716", "5442866235786175121", "5445395967228553736", "5445172379821059059",
    "5443115477033335740", "5445161388999748612", "5443148952008438812", "5442727181925031372",
    "5442856426080873163", "5444886910524762279", "5442777660675632547", "5442703052798733465",
    "5443084450189586893", "5443095398061223631",
    # A-Z
    "5442652234745686624", "5444951193300279090", "5442866218606307003", "5445134304935983841",
    "5445231160743468812", "5442885537369208912", "5445353721930230982", "5445034245082885665",
    "5443111100461655750", "5445374183154430410",
    # 0-9
]

_SET7_IDS = [
    "5445200314288353629", "5442693475021664344", "5443007505850477599", "5442697855888305681",
    "5442864152727038702", "5444884475278304457", "5442752024015843263", "5442842905523826587",
    "5442865093324877771", "5442811307449428309", "5445233669004374154", "5444909613721888760",
    "5443025059381817562", "5443063855821400123", "5442722539065358740", "5445091690270469514",
    "5442719274890212267", "5445124873187796164", "5444878273345529005", "5445337538493456575",
    "5442678180643124714", "5445204939968129128", "5442903318533810545", "5445146111801076575",
    "5443071127201033543", "5445341446913696888",
    # A-Z
    "5445207181941057026", "5444907620857065726", "5445124332021921541", "5445380286302958756",
    "5442709151652296747", "5445089963693617599", "5442681002436635833", "5444854234413574288",
    "5445074025069979534", "5442936114904084971",
    # 0-9
]

_SET8_IDS = [
    "5445083014436530583", "5445293304625277379", "5445239024828591708", "5442856846987668037",
    "5442795948646381972", "5445106593806985040", "5442614116910935414", "5442783802478865542",
    "5445347279479284281", "5442685984598699426", "5445132054373117147", "5445205253500743414",
    "5445028768999585013", "5442996914461126089", "5442842329998209242", "5442869590155635454",
    "5442697538060725176", "5443027486038338809", "5444957300743777005", "5445400640152970436",
    "5442914820456235418", "5445395224199210334", "5442919489085680647", "5442997232288706290",
    "5445174626088954180", "5443126347595559730",
    # A-Z
    "5442870307415172699", "5444917404792563239", "5442837669958692633", "5442994088372645819",
    "5442907811069601308", "5445213852025269768", "5443034126057778274", "5442946049163439899",
    "5445208393121837910", "5444958056658017960",
    # 0-9
]

_SET9_IDS = [
    "5444958743852786734", "5442723475368225974", "5442792912104504743", "5443130715577297434",
    "5442995179294337757", "5445021974361327111", "5445402499873812807", "5444984762764666054",
    "5444908497030390362", "5442679984529384507", "5445051622520564027", "5443088191106099999",
    "5442768971956792627", "5445388536935129577", "5442801480564256281", "5442794406753121154",
    "5442999693304968598", "5443002210155809806", "5442829629779911443", "5444948753758856820",
    "5444978913019208104", "5445062961234224707", "5443085030010171754", "5442946324041345567",
    "5443148604116087877", "5445400627268068689",
    # A-Z
    "5445348323156345057", "5442881444265373387", "5442876470693246213", "5442720713704254825",
    "5443120991771339355", "5443066918133083579", "5442630725549470287", "5443000144276535413",
    "5442635793610877049", "5443137944007256718",
    # 0-9
]

_SET10_IDS = [
    "5442889394249841328", "5444958481859781958", "5445372241829210114", "5444884475278304446",
    "5443078647688771153", "5442940908087595884", "5443050395393895203", "5442847406649551853",
    "5444903231400486989", "5444904498415837506", "5443111852080933552", "5442684648863870618",
    "5442763362729503756", "5445177099990115849", "5442842858279184542", "5443161974349281010",
    "5444854599485794785", "5445009725114591249", "5442920184870380369", "5443095814673052023",
    "5442623583018855743", "5443022082969481142", "5442970161109839946", "5443120493555130412",
    "5444941525328896875", "5442914567053157093",
    # A-Z
    "5445122772948789017", "5444903884235514026", "5442852655099590243", "5442788771756025105",
    "5442655357186910873", "5442947320473759566", "5442889905350948494", "5442731416762758751",
    "5442698624687451439", "5442739783359046565",
    # 0-9
]

_SET11_IDS = [
    "5445318305629907794", "5443100560611911748", "5442741351022114680", "5442843433804803405",
    "5442759475784105039", "5443153345759980494", "5442900522510101770", "5444956196937182912",
    "5444867596056832585", "5445170923827145939", "5445361259597834540", "5442887959730763262",
    "5445180355575333312", "5445061062858682679", "5442672988027666272", "5442904542599489051",
    "5442696322584982392", "5442810710448974671", "5442931184281628425", "5442733362382938062",
    "5442749498575073268", "5442815503632476763", "5445199356510644217", "5442882986158632127",
    "5442906453859940041", "5442701236027568466",
    # A-Z
    "5445120127248937952", "5445240613966488010", "5442702966899389894", "5442611041714351631",
    "5442700398508942524", "5445317790233830104", "5443077573946946160", "5442973274961128858",
    "5445251123751463263", "5445389069511078990",
    # 0-9
]

_SET12_IDS = [
    "5442686362555820443", "5443118577999720327", "5442794763235404936", "5442893770821515590",
    "5442611256462715140", "5443090759496546101", "5443129890943577409", "5443043231388447985",
    "5443087817443948046", "5442733366677910277", "5442760485101415490", "5445228141381460979",
    "5442607425351889921", "5442753939571260794", "5442810126333421890", "5445385685076846105",
    "5442893251130469419", "5445404870695759124", "5444890088800560256", "5442986151273082059",
    "5442612081096437943", "5442842141019645584", "5442844108114668307", "5442988869987380977",
    "5444873819464443290", "5442818471454878574",
    # A-Z
    "5442856782563159849", "5443070959697308471", "5445313929058232585", "5443086000672781807",
    "5442918462588501230", "5442757272465880360", "5445385878350373795", "5442929517834319132",
    "5442851989379659051", "5444962338740414901",
    # 0-9
]

_SET13_IDS = [
    "5445104463503205143", "5442993177839579337", "5442681728286110931", "5442975100322228620",
    "5442898143098218286", "5443126635358366887", "5442814910926991434", "5445018808970421229",
    "5443153818206384614", "5442971672938326797", "5442935749831863209", "5444886712956264496",
    "5445026651580704341", "5445183396412170978", "5443127408452482109", "5445147202722770083",
    "5442957658460042130", "5444919582340982123", "5442646977705718673", "5443073699886442312",
    "5445268788951950719", "5442848939952879946", "5442851555587958614", "5443011543119740191",
    "5444973149173101269", "5442915761054069251",
    # A-Z
    "5442622870054282848", "5442675474813724390", "5442988792677969437", "5445172955346674614",
    "5445252279097667059", "5442810435571066636", "5442738460509122856", "5445111554494209602",
    "5442877351161542486", "5442632718414300265",
    # 0-9
]


def _build_map(ids: list[str], letter_fb: str, special: dict[str, str], digit_fb: list[str]) -> dict:
    """символ -> (id эмодзи, запасной символ) для A-Z и 0-9."""
    table = {}
    for i in range(26):
        ch = chr(ord("A") + i)
        table[ch] = (ids[i], special.get(ch, letter_fb))
    for i in range(10):
        table[str(i)] = (ids[26 + i], digit_fb[i])
    return table


SET1_LETTERS = _build_map(_SET1_IDS, "🔤", {"A": "🅰️"}, ["🔤"] * 10)
SET2_LETTERS = _build_map(_SET2_IDS, "🔠", {"A": "🅰️", "B": "🅱️", "O": "⭕️"}, _DIGIT_FALLBACK)
SET3_LETTERS = _build_map(_SET3_IDS, "🔤", {"A": "🅰️"}, _DIGIT_FALLBACK)
SET4_LETTERS = _build_map(_SET4_IDS, "🔤", {"A": "🅰️"}, _DIGIT_FALLBACK)
SET5_LETTERS = _build_map(_SET5_IDS, "🔠", {"A": "🅰️", "B": "🅱️", "C": "©️"}, _DIGIT_FALLBACK)
SET6_LETTERS = _build_map(_SET6_IDS, "🔤", {"A": "🅰️"}, _DIGIT_FALLBACK)
SET7_LETTERS = _build_map(_SET7_IDS, "🔠", {"A": "🅰️", "B": "🅱️"}, _DIGIT_FALLBACK)
SET8_LETTERS = _build_map(_SET8_IDS, "💀", {}, _DIGIT_FALLBACK)
SET9_LETTERS = _build_map(_SET9_IDS, "🔠", {"A": "🅰️", "B": "🅱️"}, _DIGIT_FALLBACK)
SET10_LETTERS = _build_map(_SET10_IDS, "🩸", {"A": "🅰️", "B": "🅱️"}, _DIGIT_FALLBACK)
SET11_LETTERS = _build_map(_SET11_IDS, "🔤", {"A": "🅰️"}, _DIGIT_FALLBACK)
SET12_LETTERS = _build_map(_SET12_IDS, "🚩", {}, _DIGIT_FALLBACK)
SET13_LETTERS = _build_map(_SET13_IDS, "🔤", {}, _DIGIT_FALLBACK)

DEFAULT_SKIN = "plain"
# Новые скины добавляются сюда: "map" это символ -> (id эмодзи, запасной символ).
# Если есть "shop", скин платный: price кристаллов, покупается один раз и навсегда, лимитов нет.
# Скины без "shop" доступны всем.
SKINS: dict[str, dict] = {
    "plain": {"name": "Обычный", "map": None},
    "emoji": {"name": "Эмодзи-буквы", "map": EMOJI_LETTERS},
    "set1": {"name": "Набор 1", "map": SET1_LETTERS, "shop": {"price": 29}},
    "set2": {"name": "Набор 2", "map": SET2_LETTERS, "shop": {"price": 39}},
    "set3": {"name": "Набор 3", "map": SET3_LETTERS, "shop": {"price": 49}},
    "set4": {"name": "Набор 4", "map": SET4_LETTERS, "shop": {"price": 29}},
    "set5": {"name": "Набор 5", "map": SET5_LETTERS, "shop": {"price": 39}},
    "set6": {"name": "Набор 6", "map": SET6_LETTERS, "shop": {"price": 499}},
    "set7": {"name": "Набор 7", "map": SET7_LETTERS, "shop": {"price": 129}},
    "set8": {"name": "Набор 8", "map": SET8_LETTERS, "shop": {"price": 19}},
    "set9": {"name": "Набор 9", "map": SET9_LETTERS, "shop": {"price": 59}},
    "set10": {"name": "Набор 10", "map": SET10_LETTERS, "shop": {"price": 1029}},
    "set11": {"name": "Набор 11", "map": SET11_LETTERS, "shop": {"price": 39}},
    "set12": {"name": "Набор 12", "map": SET12_LETTERS, "shop": {"price": 89}},
    "set13": {"name": "Набор 13", "map": SET13_LETTERS, "shop": {"price": 399}},
}
EM_SKINS_BTN = "5445224310270633584"   # значок кнопки «Скины» в разделе «Никнейм»
SKINS_FIELD = "nick_skins"       # в профиле: список купленных донатных скинов


def owned_skins(user: dict | None) -> list[str]:
    """Скины, которыми игрок может пользоваться: бесплатные и купленные."""
    own = (user or {}).get(SKINS_FIELD)
    own = own if isinstance(own, list) else []
    return [k for k, v in SKINS.items() if not v.get("shop") or k in own]


def owns_skin(user: dict | None, key: str) -> bool:
    return key in owned_skins(user)


def skin_of(user: dict | None) -> str:
    key = (user or {}).get("nick_skin", DEFAULT_SKIN)
    return key if key in SKINS and owns_skin(user, key) else DEFAULT_SKIN


def apply_skin(text: str, skin: str) -> str:
    """Имя обычным текстом -> HTML в выбранном скине. Символы без эмодзи остаются как есть."""
    table = (SKINS.get(skin) or {}).get("map")
    if not table:
        return escape(text)
    out = []
    for ch in text:
        pair = table.get(ch.upper())
        out.append(_custom_emoji(pair) if pair else escape(ch))
    return "".join(out)


def default_name(user: dict) -> str:
    """Имя без никнейма: имя из Telegram (юзернейм не показываем), иначе «Игрок»."""
    return (user.get("tg_name") or "Игрок")[:40]


def name_html(user: dict | None) -> str:
    """Имя игрока для вставки в HTML-сообщения (с форматированием и кастомными эмодзи)."""
    if not user:
        return "Игрок"
    nick = user.get("nick")
    if nick and not _is_legacy_nick(user) and _valid_html(nick):
        skin = skin_of(user)
        plain = user.get("nick_plain")
        if skin != DEFAULT_SKIN and isinstance(plain, str) and plain:
            return apply_skin(plain[:NICK_MAX], skin)
        return nick
    return escape(default_name(user))


def name_plain(user: dict | None) -> str:
    """Имя игрока обычным текстом (для кнопок, подсказок, админки)."""
    if not user:
        return "Игрок"
    plain = user.get("nick_plain")
    if plain and isinstance(plain, str) and user.get("nick") and not _is_legacy_nick(user):
        return plain[:60]
    return default_name(user)


def has_nick(user: dict) -> bool:
    return bool(user.get("nick")) and not _is_legacy_nick(user) and _valid_html(user.get("nick"))


def is_hidden(user: dict | None) -> bool:
    return bool(user) and get_setting(user, "hidden")


def public_name_html(user: dict | None) -> str:
    """Имя игрока для показа ДРУГИМ игрокам (рейтинги, топы боссов): учитывает режим скрытости."""
    return escape(HIDDEN_NAME) if is_hidden(user) else name_html(user)


def public_name_plain(user: dict | None) -> str:
    return HIDDEN_NAME if is_hidden(user) else name_plain(user)


def public_name_html_by_id(uid) -> str | None:
    """Имя для других игроков по ID (HTML). None, если игрока нет в базе.
    Используется модулем босса для топа и ленты боя."""
    try:
        user = _get_user(int(uid)) if _get_user else None
    except (TypeError, ValueError):
        return None
    return public_name_html(user) if user else None


def public_name_plain_by_id(uid) -> str | None:
    try:
        user = _get_user(int(uid)) if _get_user else None
    except (TypeError, ValueError):
        return None
    return public_name_plain(user) if user else None


_MISSING = object()


class NameBook(dict):
    """Словарь имён игроков для рейтингов (например, топ бойцов босса).

    Вместо сохранённого имени отдаёт актуальное: никнейм из настроек, а если он не задан,
    имя из Telegram (не юзернейм). Для игрока с режимом скрытости отдаёт «Скрытый игрок».
    Записи, сделанные модулем босса, по-прежнему принимаются как запасной вариант."""

    @staticmethod
    def _fresh(key) -> str | None:
        if _get_user is None:
            return None
        try:
            user = _get_user(int(key))
        except (TypeError, ValueError):
            return None
        return public_name_plain(user) if user else None

    def get(self, key, default=None):
        fresh = self._fresh(key)
        if fresh:
            return fresh
        raw = dict.get(self, key, default)
        # Старые записи могли быть юзернеймами: их не показываем
        return default if isinstance(raw, str) and raw.startswith("@") else raw

    def __getitem__(self, key):
        value = self.get(key, _MISSING)
        if value is _MISSING:
            raise KeyError(key)
        return value


def install_name_book(stats: dict) -> None:
    """Подключает NameBook к статистике босса: stats["names"] начнёт отдавать никнеймы."""
    stats["names"] = NameBook(stats.get("names") or {})


def reset_nick(user: dict) -> str | None:
    """Убирает никнейм. Возвращает прежний (обычным текстом) или None."""
    old = user.get("nick_plain") if user.get("nick") else None
    for key in ("nick", "nick_plain", "nick_at"):
        user.pop(key, None)
    return old


def _is_legacy_nick(user: dict | None) -> bool:
    """Старый ник с премиум (кастомными) эмодзи: в новой системе ники только обычным текстом."""
    nick = (user or {}).get("nick")
    return isinstance(nick, str) and "<tg-emoji" in nick


def migrate_legacy_nicks(users) -> int:
    """Сбрасывает ники с премиум-эмодзи (новая система скинов). Возвращает, сколько сброшено.
    Вызывается из main.py после setup(). Безопасна при повторном запуске."""
    count = 0
    try:
        items = list(users.items())
    except Exception:                      # хранилище без items(): идём по ключам
        try:
            items = [(k, users.get(k)) for k in list(users.keys())]
        except Exception:
            log.exception("migrate_legacy_nicks: не удалось перебрать игроков")
            return 0
    for uid, user in items:
        if isinstance(user, dict) and _is_legacy_nick(user):
            reset_nick(user)
            count += 1
    if count:
        try:
            if _save_users:
                _save_users()
        except Exception:
            log.exception("migrate_legacy_nicks: не удалось сохранить")
        log.info("migrate_legacy_nicks: сброшено ников с премиум-эмодзи: %d", count)
    return count


def _nest_entities(entities: list) -> list:
    """Оставляет только вложенные или не пересекающиеся куски форматирования.
    Частично пересекающиеся (жирный и курсив внахлёст) ломают разметку, поэтому лишние отбрасываем."""
    kept: list = []
    stack: list[int] = []                     # концы открытых кусков
    for e in sorted(entities, key=lambda x: (x.offset, -x.length)):
        end = e.offset + e.length
        while stack and stack[-1] <= e.offset:
            stack.pop()
        if stack and end > stack[-1]:
            continue                          # пересекается частично: пропускаем
        kept.append(e)
        stack.append(end)
    return kept


def _check_text(message: Message) -> tuple[str | None, str | None, str | None]:
    """Разбирает сообщение с именем. Возвращает (html, обычный текст, ошибка)."""
    text = message.text or ""
    entities = list(message.entities or [])
    if not text.strip():
        return None, None, "Имя не должно быть пустым."
    if "\n" in text or "\r" in text:
        return None, None, "Имя должно быть в одну строку."
    if text.startswith("/"):
        return None, None, "Имя не может начинаться с «/»."
    if not NICK_PATTERN.fullmatch(text):
        return None, None, ("В имени можно использовать только английские буквы и цифры, "
                            "без русских букв, пробелов, эмодзи и символов.")
    if any(e.type in FORBIDDEN_ENTITIES for e in entities):
        return None, None, "В имени нельзя использовать ссылки, упоминания и хэштеги."
    if re.search(r"(https?://|t\.me/|www\.|@\w)", text, re.I):
        return None, None, "В имени нельзя использовать ссылки и упоминания."
    visible = len(text)
    if visible < NICK_MIN:
        return None, None, f"Слишком коротко: минимум {NICK_MIN} символа."
    if visible > NICK_MAX:
        return None, None, f"Слишком длинно: максимум {NICK_MAX} символов, у тебя {visible}."
    low = text.lower()
    if any(part in low for part in BANNED_PARTS):
        return None, None, "Это имя использовать нельзя. Придумай другое."
    if any(ord(ch) < 32 or ch in "\u200b\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
           for ch in text):
        return None, None, "В имени есть недопустимые символы."
    kept = _nest_entities([e for e in entities if e.type in ALLOWED_ENTITIES])
    try:
        html = html_decoration.unparse(text, kept)
    except Exception:                          # битые смещения форматирования
        return None, None, "Не получилось разобрать форматирование. Попробуй другое имя."
    if not _valid_html(html):
        return None, None, "Не получилось разобрать форматирование. Попробуй другое имя."
    return html, text, None


# ---------------------------------------------------------------------------
# Уведомления об экспедициях
# ---------------------------------------------------------------------------

def get_setting(user: dict, key: str) -> bool:
    st = user.get("settings")
    if isinstance(st, dict) and key in st:
        return bool(st[key])
    return DEFAULT_SETTINGS.get(key, False)


def _load_alerts() -> None:
    if _ALERTS_FILE is None or read_json_safe is None:
        return
    try:
        saved = read_json_safe(_ALERTS_FILE)
    except RuntimeError:
        log.critical("alerts.json повреждён и копии нет: ожидающие уведомления начаты с нуля")
        return
    if isinstance(saved, dict):
        for uid, end in saved.items():
            try:
                _ALERTS[str(uid)] = float(end)
            except (TypeError, ValueError):
                continue


def _save_alerts() -> None:
    if _ALERTS_FILE is None or atomic_write_json is None:
        return
    try:
        atomic_write_json(_ALERTS_FILE, _ALERTS, backup_every=300)
    except OSError:
        log.exception("не удалось сохранить alerts.json")


def register_alert(uid, end: float) -> None:
    """Запоминает, что надо сообщить игроку, когда закончится экспедиция."""
    _ALERTS[str(uid)] = float(end)
    _save_alerts()


async def notifier_loop(bot: Bot) -> None:
    """Фоновая задача: шлёт «отряд вернулся», когда экспедиция закончилась."""
    while True:
        await asyncio.sleep(ALERT_TICK)
        try:
            await _notify_due(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("ошибка в цикле уведомлений")


async def _notify_due(bot: Bot) -> None:
    now = time.time()
    due = [uid for uid, end in _ALERTS.items() if end <= now][:ALERTS_PER_TICK]
    if not due:
        return
    for uid in due:
        _ALERTS.pop(uid, None)
        user = _get_user(int(uid)) if uid.isdigit() else None
        ex = user.get("exrn") if user else None
        if not isinstance(ex, dict):
            continue
        trips = ex.get("trips") if isinstance(ex.get("trips"), dict) else {}
        for key, trip in list(trips.items()):
            if not isinstance(trip, dict) or trip.get("notified") or trip.get("end", 0) > now:
                continue
            if not get_setting(user, "notify_exrn"):
                continue
            await _send_return_alert(bot, int(uid), user, key, trip)
            trip["notified"] = True
            _save_users()
            await asyncio.sleep(0.05)
    _save_alerts()


async def _send_return_alert(bot: Bot, uid: int, user: dict, key: str, trip: dict) -> None:
    import exrn      # поздний импорт: exrn тоже использует settings

    g = exrn.BY_KEY.get(key)
    if g is None:
        return
    text = (
        f"<b>{_custom_emoji(exrn.EXPLORE_EMOJI)} Отряд вернулся!</b>\n\n"
        f"{name_html(user)}, {g['icon']} <b>{g['name']}</b> завершили экспедицию на "
        f"{trip.get('hours', '?')} ч. Добыча сложена у входа в лагерь, осталось её забрать.\n\n"
        "<i>Отключить такие сообщения можно в «Настройки» → «Уведомления».</i>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="К лагерю", callback_data="menu:exrn", style=ButtonStyle.SUCCESS,
                             icon_custom_emoji_id=exrn.ICON_SEND),
    ]])
    try:
        await bot.send_message(uid, text, reply_markup=kb)
    except TelegramForbiddenError:
        # Игрок заблокировал бота: больше не пытаемся
        user.setdefault("settings", {})["notify_exrn"] = False
        _save_users()
    except TelegramAPIError as e:
        log.warning("не удалось отправить уведомление игроку %s: %s", uid, e)


# ---------------------------------------------------------------------------
# Экраны
# ---------------------------------------------------------------------------

class NickStates(StatesGroup):
    waiting = State()


def _em(pair) -> str:
    return _custom_emoji(pair)


def _tree(rows: list[tuple[str, str]], title: str = "") -> str:
    lines = []
    for n, (label, value) in enumerate(rows):
        mark = "└" if n == len(rows) - 1 else "├"
        lines.append(f"{mark} {label}" + (f": <b>{value}</b>" if value != "" else ""))
    head = f"<b>{title}</b><br>" if title else ""
    return f"<p>{head}{'<br>'.join(lines)}</p>"


def _gap() -> str:
    return "<p>&nbsp;</p>"


def _image(with_image: bool) -> str:
    return '<img src="tg://photo?id=pet"/>' if with_image else ""


def _kb(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows if r])


def _btn(text: str, data: str, style: ButtonStyle = ButtonStyle.PRIMARY) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data, style=style)


def _ibtn(text: str, data: str, pair: tuple[str, str],
          style: ButtonStyle = ButtonStyle.PRIMARY) -> InlineKeyboardButton:
    """Кнопка с кастомным эмодзи слева от текста."""
    return InlineKeyboardButton(text=text, callback_data=data, style=style,
                                icon_custom_emoji_id=pair[0])


def _back(data: str, text: str = "Назад") -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data, style=ButtonStyle.PRIMARY,
                                icon_custom_emoji_id=_BACK_ID)


def _date(ts) -> str:
    try:
        return time.strftime("%d.%m.%Y", time.localtime(float(ts))) if float(ts) > 0 else "давно"
    except (TypeError, ValueError, OverflowError):
        return "давно"


def _onoff(value: bool, on: str = "включено", off: str = "выключено") -> str:
    return f"{on} {_ic('ok')}" if value else f"{off} {_em(EM_OFF)}"


def settings_html(user: dict, uid: int, with_image: bool = False, note: str = "") -> str:
    notify = get_setting(user, "notify_exrn")
    profile = [
        (f"{_em(EM_NICK)} Никнейм", name_html(user)),
        (f"{_em(EM_ID)} ID", f"<code>{uid}</code>"),
        (f"{_em(EM_DATE)} В игре с", _date(user.get("joined", 0))),
        (f"{_ic('level')} Уровень питомца", str(user.get("level", 1))),
        (f"{_ic('wins')} Бои", f"{user.get('wins', 0)} побед · {user.get('losses', 0)} поражений"),
    ]
    alerts = [
        (f"{_ic('expedition')} Возвращение экспедиции", _onoff(notify)),
        (f"{_em(EM_PRIV)} Режим скрытости", _onoff(is_hidden(user), "включён", "выключен")),
    ]
    return (
        f"{_image(with_image)}"
        f"<p><b>{_em(SETTINGS_EMOJI)} НАСТРОЙКИ</b></p>"
        "<blockquote><i>Здесь ты можешь настроить уведомления "
        "и посмотреть данные своего профиля.</i></blockquote>"
        + _tree(profile, "Профиль")
        + _gap()
        + _tree(alerts, "Параметры")
        + (f"<p>{note}</p>" if note else "")
    )


def settings_kb() -> InlineKeyboardMarkup:
    return _kb(
        [_ibtn("Уведомления", "set:notif", EM_NOTIF, ButtonStyle.PRIMARY),
         _ibtn("Приватность", "set:priv", EM_PRIV, ButtonStyle.PRIMARY)],
        [_ibtn("Помощь", "set:help", EM_HELP, ButtonStyle.PRIMARY),
         _ibtn("Поддержка", "set:sup", EM_SUPPORT, ButtonStyle.SUCCESS)],
        [_btn("Закрыть", "set:close", ButtonStyle.DANGER)],
    )


def nick_html_screen(user: dict, with_image: bool = False, note: str = "") -> str:
    left = max(0, int(user.get("nick_at", 0) + NICK_COOLDOWN - time.time()))
    return (
        f"{_image(with_image)}"
        f"<p><b>{_em(EM_NICK)} ИЗМЕНИТЬ НИК</b></p>"
        "<blockquote><i>Отправь мне новый ник одним сообщением.</i></blockquote>"
        + _p(f"Английские буквы и цифры, от {NICK_MIN} до {NICK_MAX} символов."
             + (f"<br><i>Менять ник можно раз в {NICK_COOLDOWN // 60} мин, подожди ещё {left} с.</i>"
                if left else ""))
        + (f"<p>{note}</p>" if note else "")
    )


def _p(text: str) -> str:
    return f"<p>{text}</p>"


def nick_kb(user: dict) -> InlineKeyboardMarkup:
    rows = []
    if has_nick(user):
        rows.append([_btn("Сбросить к имени Telegram", "set:nick_reset", ButtonStyle.DANGER)])
    rows.append([_back("menu:player")])
    return _kb(*rows)


def _rbtn(label_html: str, data: str, style: str) -> str:
    """Кнопка прямо в теле сообщения (callback)."""
    return (f'<tg-button type="callback_data" data="{escape(data, quote=True)}" style="{style}">'
            f"{label_html}</tg-button>")


SKIN_PAGE_SIZE = 7   # на странице: ник без эмодзи отдельным рядом, 3 ряда по 2 скина и один скин в конце


def skin_pages() -> list[list[str]]:
    """Страницы скинов. На каждой первым идёт обычный ник, дальше по SKIN_PAGE_SIZE скинов."""
    rest = [k for k in SKINS if k != DEFAULT_SKIN]
    return [rest[i:i + SKIN_PAGE_SIZE] for i in range(0, len(rest), SKIN_PAGE_SIZE)] or [[]]


def skin_page_of(key: str) -> int:
    """Номер страницы (с 1), на которой лежит скин."""
    for n, page in enumerate(skin_pages(), 1):
        if key in page:
            return n
    return 1


def _skin_page_no(page: int) -> int:
    return max(1, min(int(page), len(skin_pages())))


def skin_html(user: dict, with_image: bool = False, page: int = 1) -> str:
    """Скины по страницам. Надет: зелёный, куплен: синий, не куплен: красный (нажатие открывает покупку)."""
    cur = skin_of(user)
    sample = ((has_nick(user) and (user.get("nick_plain") or "")) or "Zoro")[:NICK_MAX]
    page = _skin_page_no(page)

    def one(k: str) -> str:
        if k == cur:
            return _rbtn(f"{_em(EM_SKIN_ON)} {apply_skin(sample, k)}", f"set:skin:{k}", "success")
        if owns_skin(user, k):
            return _rbtn(apply_skin(sample, k), f"set:skin:{k}", "success")
        return _rbtn(apply_skin(sample, k), f"donate:skin:{k}", "danger")

    keys = skin_pages()[page - 1]
    pairs, last = (keys[:-1], keys[-1]) if len(keys) % 2 else (keys, None)
    rows = "<tg-button-row>" + one(DEFAULT_SKIN) + "</tg-button-row>"
    rows += "".join("<tg-button-row>" + "".join(one(k) for k in pairs[i:i + 2]) + "</tg-button-row>"
                    for i in range(0, len(pairs), 2))
    if last:
        rows += "<tg-button-row>" + one(last) + "</tg-button-row>"
    return (
        f"{_image(with_image)}"
        f"<p><b>{_em((EM_SKINS_BTN, '🔤'))} СКИНЫ НИКА</b></p>"
        "<blockquote><i>Нажми на скин, чтобы надеть его. "
        "Зелёные доступны тебе, галочка стоит на надетом, красные не куплены: "
        "нажми на красный, чтобы увидеть цену и купить.</i></blockquote>"
        + _tree([("Сейчас", name_html(user) if has_nick(user) else escape(default_name(user)))],
                "Твой ник")
        + _gap()
        + rows
    )


def skin_kb(user: dict, page: int = 1) -> InlineKeyboardMarkup:
    """Скины лежат кнопками в самом сообщении, внизу переключатель страниц и «Назад»."""
    page = _skin_page_no(page)
    total = len(skin_pages())
    nav = [_btn("‹", f"set:skinp:{(page - 2) % total + 1}"),
           _btn(f"{page}/{total}", "set:skinnoop"),
           _btn("›", f"set:skinp:{page % total + 1}")] if total > 1 else []
    return _kb(nav, [_back("menu:player")])


def notif_html(user: dict, with_image: bool = False) -> str:
    notify = get_setting(user, "notify_exrn")
    return (
        f"{_image(with_image)}"
        f"<p><b>{_em(EM_NOTIF)} УВЕДОМЛЕНИЯ</b></p>"
        "<blockquote><i>Бот может сообщить, когда твой отряд вернётся из экспедиции, "
        "чтобы добыча не ждала в лагере.</i></blockquote>"
        + _tree([(f"{_ic('expedition')} Возвращение экспедиции", _onoff(notify))], "Сейчас")
        + _p("<i>Сообщение приходит один раз, когда экспедиция завершилась.</i>")
    )


def notif_kb(user: dict) -> InlineKeyboardMarkup:
    notify = get_setting(user, "notify_exrn")
    return _kb(
        [_btn("Выключить: возвращение отряда" if notify else "Включить: возвращение отряда",
              "set:notif_toggle", ButtonStyle.DANGER if notify else ButtonStyle.SUCCESS)],
        [_back("set:main")],
    )


# ---------------------------------------------------------------------------
# Обработчики
# ---------------------------------------------------------------------------

def _touch(user: dict, tg) -> None:
    """Обновляет имя из Telegram (запасное имя, пока нет никнейма)."""
    name = (tg.full_name or "")[:64]
    if name and user.get("tg_name") != name:
        user["tg_name"] = name
        _save_users()


async def _need_user(callback: CallbackQuery):
    user = _get_user(callback.from_user.id)
    if user is None:
        await callback.answer("Сначала выбери питомца: /start", show_alert=True)
        return None
    _touch(user, callback.from_user)
    return user


async def _show(callback: CallbackQuery, key: str, html_fn, kb: InlineKeyboardMarkup,
                force_new: bool = False) -> None:
    await _show_rich_card(callback, key, html_fn, kb, force_new=force_new)


@router.message(F.text == "Настройки")
async def open_settings(message: Message, state: FSMContext):
    await state.clear()
    user = _get_user(message.from_user.id)
    if user is None:
        await message.answer("<i>Сначала выбери питомца: /start</i>")
        return
    _touch(user, message.from_user)
    await _send_rich_card(message.bot, message.chat.id, "settings",
                          lambda wi: settings_html(user, message.from_user.id, wi), settings_kb())


@router.callback_query(F.data == "set:main")
async def on_main(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    user = await _need_user(callback)
    if user is None:
        return
    await callback.answer()
    uid = callback.from_user.id
    await _show(callback, "settings", lambda wi: settings_html(user, uid, wi), settings_kb())


@router.callback_query(F.data == "set:close")
async def on_close(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer()
    try:
        await callback.message.delete()
    except TelegramAPIError:
        pass


@router.callback_query(F.data == "set:nick")
async def on_nick(callback: CallbackQuery, state: FSMContext):
    user = await _need_user(callback)
    if user is None:
        return
    await callback.answer()
    await state.set_state(NickStates.waiting)
    await _show(callback, "settings_nick", lambda wi: nick_html_screen(user, wi), nick_kb(user))


@router.callback_query(F.data == "set:skin")
async def on_skin(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    user = await _need_user(callback)
    if user is None:
        return
    await callback.answer()
    await _show(callback, "settings_skin", lambda wi: skin_html(user, wi, 1), skin_kb(user, 1))


@router.callback_query(F.data == "set:skinnoop")
async def on_skin_noop(callback: CallbackQuery):
    await callback.answer()


@router.callback_query(F.data.regexp(r"^set:skinp:\d+$"))
async def on_skin_page(callback: CallbackQuery):
    user = await _need_user(callback)
    if user is None:
        return
    page = _skin_page_no(int(callback.data.split(":")[2]))
    await callback.answer()
    await _show(callback, "settings_skin", lambda wi: skin_html(user, wi, page), skin_kb(user, page))


@router.callback_query(F.data.regexp(r"^set:skin:\w+$"))
async def on_skin_pick(callback: CallbackQuery):
    user = await _need_user(callback)
    if user is None:
        return
    key = callback.data.split(":")[2]
    if key not in SKINS:
        await callback.answer("Такого скина нет", show_alert=True)
        return
    if not owns_skin(user, key):
        await callback.answer("Этот скин не куплен: нажми на него в списке, чтобы купить.", show_alert=True)
        return
    if not has_nick(user):
        await callback.answer("Сначала задай никнейм: скин применяется к нему.", show_alert=True)
        return
    if skin_of(user) == key:
        await callback.answer("Этот скин уже выбран")
        return
    old = skin_of(user)
    user["nick_skin"] = key
    _save_users()
    _led("nick_skin", uid=str(callback.from_user.id), old=old, new=key)
    await callback.answer(f"Скин: {SKINS[key]['name']}")
    page = skin_page_of(key)
    await _show(callback, "settings_skin", lambda wi: skin_html(user, wi, page), skin_kb(user, page))


@router.callback_query(F.data == "set:nick_reset")
async def on_nick_reset(callback: CallbackQuery, state: FSMContext):
    user = await _need_user(callback)
    if user is None:
        return
    old = reset_nick(user)
    _save_users()
    if old:
        _led("nick_reset", uid=str(callback.from_user.id), old=old)
    await state.set_state(NickStates.waiting)
    await callback.answer("Никнейм сброшен: теперь показывается имя из Telegram")
    await _show(callback, "settings_nick", lambda wi: nick_html_screen(user, wi), nick_kb(user))


def _retry_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_btn("Ввести имя заново", "set:nick")]])


@router.message(NickStates.waiting, F.text)
async def nick_receive(message: Message, state: FSMContext):
    text = message.text or ""
    if text in RESERVED_BUTTONS or text.startswith("/"):
        await state.clear()          # игрок нажал другую кнопку или команду: выходим из ввода имени
        raise SkipHandler
    user = _get_user(message.from_user.id)
    if user is None:
        await state.clear()
        return
    _touch(user, message.from_user)

    left = int(user.get("nick_at", 0) + NICK_COOLDOWN - time.time())
    if left > 0:
        await state.clear()          # ввод имени ждём только один раз
        await message.answer(f"<i>Имя можно менять раз в {NICK_COOLDOWN // 60} мин. "
                             f"Подожди ещё {left} с.</i>")
        return
    html, plain, error = _check_text(message)
    if error:
        await state.clear()          # имя не подошло: ввод закончен, начинаем заново кнопкой
        await message.answer(f"<i>{escape(error)} Нажми кнопку и введи имя заново.</i>",
                             reply_markup=_retry_kb())
        return

    old = name_plain(user) if has_nick(user) else None
    user["nick"], user["nick_plain"], user["nick_at"] = html, plain, time.time()
    _save_users()
    _led("nick_change", uid=str(message.from_user.id), old=old, new=plain, html=html)
    await state.clear()
    try:
        await message.delete()       # убираем сообщение игрока, чтобы чат оставался чистым
    except TelegramAPIError:
        pass
    uid = message.from_user.id
    note = f"<b>{_ic('ok')} Ник изменён!</b>"
    if _OPEN_PROFILE is not None:
        await _OPEN_PROFILE(message.bot, message.chat.id, uid, note)
        return
    await _send_rich_card(
        message.bot, message.chat.id, "settings",
        lambda wi: settings_html(user, uid, wi, note), settings_kb())


@router.message(NickStates.waiting, ~F.text)
async def nick_not_text(message: Message, state: FSMContext):
    await state.clear()              # ввод имени ждём только один раз
    await message.answer("<i>Имя нужно прислать текстом. Нажми кнопку и введи имя заново.</i>",
                         reply_markup=_retry_kb())


@router.callback_query(F.data == "set:notif")
async def on_notif(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    user = await _need_user(callback)
    if user is None:
        return
    await callback.answer()
    await _show(callback, "settings_notify", lambda wi: notif_html(user, wi), notif_kb(user))


@router.callback_query(F.data == "set:notif_toggle")
async def on_notif_toggle(callback: CallbackQuery):
    user = await _need_user(callback)
    if user is None:
        return
    st = user.get("settings")
    if not isinstance(st, dict):
        st = user["settings"] = {}
    st["notify_exrn"] = not get_setting(user, "notify_exrn")
    _save_users()
    _led("settings_toggle", uid=str(callback.from_user.id), key="notify_exrn", value=st["notify_exrn"])
    await callback.answer("Уведомления включены" if st["notify_exrn"] else "Уведомления выключены")
    await _show(callback, "settings_notify", lambda wi: notif_html(user, wi), notif_kb(user))


# ---------------------------------------------------------------------------
# Приватность
# ---------------------------------------------------------------------------

def priv_html(user: dict, with_image: bool = False) -> str:
    hidden = is_hidden(user)
    return (
        f"{_image(with_image)}"
        f"<p><b>{_em(EM_PRIV)} ПРИВАТНОСТЬ</b></p>"
        "<blockquote><i>Режим скрытости прячет твоё имя от других игроков. В рейтингах и топах "
        "вместо ника будет написано «Скрытый игрок». Сам ты видишь своё имя как обычно.</i></blockquote>"
        + _tree([(f"{_em(EM_PRIV)} Режим скрытости", _onoff(hidden, "включён", "выключен"))], "Сейчас")
        + _gap()
        + _tree([
            ("Ты видишь себя", name_html(user)),
            ("Другие видят тебя", public_name_html(user)),
        ], "Как это выглядит")
        + _p("<i>Скрытость не влияет на прогресс: награды и место в рейтинге сохраняются.</i>")
    )


def priv_kb(user: dict) -> InlineKeyboardMarkup:
    hidden = is_hidden(user)
    return _kb(
        [_btn("Выключить скрытость" if hidden else "Включить скрытость", "set:priv_toggle",
              ButtonStyle.DANGER if hidden else ButtonStyle.SUCCESS)],
        [_back("set:main")],
    )


@router.callback_query(F.data == "set:priv")
async def on_priv(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    user = await _need_user(callback)
    if user is None:
        return
    await callback.answer()
    await _show(callback, "settings_privacy", lambda wi: priv_html(user, wi), priv_kb(user))


@router.callback_query(F.data == "set:priv_toggle")
async def on_priv_toggle(callback: CallbackQuery):
    user = await _need_user(callback)
    if user is None:
        return
    st = user.get("settings")
    if not isinstance(st, dict):
        st = user["settings"] = {}
    st["hidden"] = not is_hidden(user)
    _save_users()
    _led("settings_toggle", uid=str(callback.from_user.id), key="hidden", value=st["hidden"])
    await callback.answer("Режим скрытости включён" if st["hidden"] else "Режим скрытости выключен")
    await _show(callback, "settings_privacy", lambda wi: priv_html(user, wi), priv_kb(user))


# ---------------------------------------------------------------------------
# Помощь и FAQ
# ---------------------------------------------------------------------------

HELP_SECTIONS: list[tuple[str, str, str]] = [
    ("play", "pet", "Как играть"),
    ("level", "level", "Что даёт уровень"),
    ("art", "art", "Как работают артефакты"),
    ("coins", "coin", "Как получить монеты"),
    ("arena", "wins", "Как работает арена"),
]       # второй элемент: ключ эмодзи темы (см. _ic)


def _g(name: str, default):
    return _GAME.get(name, default)


def _num(n) -> str:
    return f"{int(n):,}".replace(",", " ")


def _help_texts() -> dict[str, tuple[str, list[tuple[str, list[tuple[str, str]]]]]]:
    """Содержимое разделов помощи: ключ -> (вступление, [(заголовок блока, строки)])."""
    max_level = _g("MAX_LEVEL", 100)
    evo_every = _g("EVO_EVERY", 10)
    evo_mult = _g("EVO_COST_MULT", 7)
    growth = round(_g("STAT_GROWTH", 0.04) * 100)
    evo_growth = round(_g("EVO_GROWTH", 0.15) * 100)
    cost = _g("upgrade_cost", lambda level: (level, level + 1))
    m5, f5 = cost(5)
    me, fe = cost(evo_every - 1)
    slots = _g("SLOTS", 3)
    dup = _g("DUPLICATE_COINS", {})
    step = _g("FRAG_DAMAGE_STEP", 5000)
    chance = round(_g("FRAG_CHANCE", 0.5) * 100)
    dup_line = " · ".join(f"{k.lower()} {_num(v)}" for k, v in dup.items()) if dup else ""

    import arena      # поздний импорт: arena тоже использует settings

    leagues = " → ".join(f"{icon} {name}" for _, name, icon in arena.LEAGUES)
    return {
        "arena": (
            "Арена: автоматические бои питомцев разных игроков. Побеждай, расти в лигах и получай награды.",
            [
                ("Как проходит бой", [
                    ("Соперник", "система подбирает питомца по кубкам и силе, шанс победы виден до боя"),
                    ("Ход боя", "быстрый питомец бьёт первым, в ход идут три навыка, критические удары и увороты"),
                    ("Стихии", "Вода сильнее Огня, Огонь сильнее Земли, Земля сильнее Воды"),
                    ("Сила питомца", "уровень, эволюции и артефакты из «Снаряжения» решают исход"),
                ]),
                ("Прогресс", [
                    ("Кубки", "растут за победы и падают за поражения, чем сильнее соперник, тем больше награда"),
                    ("Лиги", leagues),
                    ("Сезон", f"длится {arena.SEASON_LENGTH // 86400} дней: в конце награда за лучшую лигу и мягкий сброс"),
                ]),
                ("Энергия и награды", [
                    ("Энергия", f"бой стоит 1, максимум {arena.ENERGY_MAX}, +1 каждые {arena.ENERGY_REGEN_SEC // 60} мин"),
                    ("Монеты", f"за каждый бой, а серия побед даёт до +{round(arena.STREAK_BONUS * arena.STREAK_MAX * 100)}%"),
                    ("Задания дня", "бои и победы приносят дополнительные монеты"),
                ]),
            ],
        ),
        "play": (
            "Коротко о главном: что делать в начале и как устроена игра.",
            [
                ("С чего начать", [
                    ("1. Выбери питомца", "у каждого своя стихия, характеристики и способность"),
                    ("2. Забирай запасы", "в «Запасах» видны монеты, мясо и фрукты"),
                    ("3. Качай питомца", "в «Мой питомец» → «Прокачать», тратится мясо и фрукты"),
                ]),
                ("Разделы меню", [
                    (f"{_ic('mine')} Шахты", "приносят доход в фоне, даже пока тебя нет"),
                    (f"{_ic('explore')} Исследование", "отряды уходят в экспедиции и приносят монеты, еду и артефакты"),
                    (f"{_ic('boss')} Босс", "бей боссов и получай за урон фрагменты"),
                    (f"{_ic('treasury')} Сокровищница", "сундуки за фрагменты, из них падают артефакты"),
                    (f"{_ic('wins')} Арена", "автоматические бои питомцев с другими игроками, рейтинг и лиги"),
                ]),
                ("Игровой круг", [
                    ("Добыча", "шахта и экспедиции дают монеты, мясо и фрукты"),
                    ("Прокачка", "еда повышает уровень и силу питомца"),
                    ("Бои", "сильный питомец наносит больше урона боссам"),
                    ("Награды", "фрагменты → сундуки → артефакты → ещё сильнее"),
                ]),
            ],
        ),
        "level": (
            f"Уровень делает питомца сильнее. Максимум: {max_level} уровень.",
            [
                ("Что даёт уровень", [
                    ("Каждый уровень", f"характеристики питомца +{growth}%"),
                    (f"Каждый {evo_every}-й уровень", f"эволюция: характеристики +{evo_growth}%"),
                    ("Чем выше уровень", "тем больше урон и здоровье в боях с боссами"),
                ]),
                ("Сколько стоит", [
                    ("Обычный уровень", "мяса столько же, какой у тебя уровень, фруктов на одно больше"),
                    ("Например, с 5-го уровня", f"{m5} мяса и {f5} фруктов"),
                    ("Эволюция", f"стоит в {evo_mult} раз больше еды"),
                    (f"Например, с {evo_every - 1}-го уровня", f"{me} мяса и {fe} фруктов"),
                ]),
                ("Совет", [
                    ("Копи еду заранее", "перед эволюцией нужно много мяса и фруктов"),
                    ("Еду приносят", "экспедиции исследователей"),
                ]),
            ],
        ),
        "art": (
            "Артефакты дают процентные бонусы. Их можно надеть и усилить себя.",
            [
                ("Виды артефактов", [
                    (f"{_ic('pet')} Питомца", "усиливают характеристики и боевые умения, раздел «Снаряжение»"),
                    (f"{_ic('mine')} Шахты", "усиливают шахту, раздел «Шахты»"),
                    (f"{_ic('expedition')} Исследователей", "богаче добыча, короче путь, выше шанс редкого"),
                ]),
                ("Как пользоваться", [
                    ("Слоты", f"надеть можно до {slots} артефактов в каждом разделе"),
                    ("Бонусы", "складываются: чем больше надето, тем сильнее эффект"),
                    ("Как надеть", "открой раздел → «Артефакты» или «Снаряжение» → выбери → «Надеть»"),
                ]),
                ("Редкости", [
                    ("Пять редкостей", "обычный, редкий, эпический, мифический, легендарный"),
                    ("Чем реже", "тем сильнее бонус"),
                ]),
                ("Где взять", [
                    ("Сундуки", f"за фрагменты: за каждые {_num(step)} урона по боссу есть попытка "
                                f"получить фрагмент (шанс {chance}%)"),
                    ("Экспедиции", "исследователи находят артефакты до мифических"),
                    ("Дубликат", "если такой артефакт уже есть, ты получишь монеты"
                                 + (f": {dup_line}" if dup_line else "")),
                ]),
            ],
        ),
        "coins": (
            "Монеты нужны для найма отрядов, времени экспедиций и других улучшений.",
            [
                ("Основные источники", [
                    (f"{_ic('mine')} Шахта", "доход каждый час: шахтёры и кирки повышают его"),
                    (f"{_ic('expedition')} Экспедиции", "чем сильнее отряд и дольше путь, тем больше монет"),
                    (f"{_ic('art')} Дубликаты", "повторный артефакт из сундука заменяется монетами"),
                ]),
                ("Как получать больше", [
                    ("Серия дней", "заходи каждый день: бонус к добыче экспедиций до +30%"),
                    ("Уровень отряда", "каждый поход прокачивает отряд, добыча растёт"),
                    ("События в пути", "иногда экспедиции находят сундук или тайник"),
                    ("Артефакты", "бонусы к монетам и ко всей добыче"),
                ]),
                ("Совет", [
                    ("Не забывай забирать добычу", "новый поход можно начать только после того, как заберёшь прошлую добычу"),
                    ("Включи уведомления", "бот напомнит, когда отряд вернётся"),
                ]),
            ],
        ),
    }


def help_section_html(key: str, with_image: bool = False) -> str:
    icon_key, title = next(((i, t) for k, i, t in HELP_SECTIONS if k == key), ("play", "Помощь"))
    icon = _ic(icon_key)
    intro, blocks = _help_texts()[key]
    body = _gap().join(_tree(rows, head) for head, rows in blocks)
    keys = [k for k, _, _ in HELP_SECTIONS]
    return (
        f"{_image(with_image)}"
        f"<p><b>{icon} {title.upper()}</b> · тема {keys.index(key) + 1} из {len(keys)}</p>"
        f"<blockquote><i>{intro}</i></blockquote>" + body
    )


ARROW_PREV_ID = "5255703720078879038"    # стрелка «назад»
ARROW_NEXT_ID = "5253767677670862169"    # стрелка «вперёд»


def help_section_kb(key: str) -> InlineKeyboardMarkup:
    """Две кнопки со стрелками: к предыдущей и к следующей теме (по кругу)."""
    keys = [k for k, _, _ in HELP_SECTIONS]
    i = keys.index(key)
    prev_key, next_key = keys[i - 1], keys[(i + 1) % len(keys)]
    return _kb(
        [InlineKeyboardButton(text="Назад", callback_data=f"set:help:{prev_key}",
                              style=ButtonStyle.PRIMARY, icon_custom_emoji_id=ARROW_PREV_ID),
         InlineKeyboardButton(text="Далее", callback_data=f"set:help:{next_key}",
                              style=ButtonStyle.SUCCESS, icon_custom_emoji_id=ARROW_NEXT_ID)],
        [_ibtn("Поддержка", "set:sup", EM_SUPPORT, ButtonStyle.PRIMARY)],
        [_back("set:main", "К настройкам")],
    )


@router.callback_query(F.data == "set:help")
async def on_help(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer()
    first = HELP_SECTIONS[0][0]           # «Помощь» сразу открывает первую тему: «Как играть»
    await _show_rich_card(callback, "settings_help", lambda wi: help_section_html(first, wi),
                          help_section_kb(first))


@router.callback_query(F.data.regexp(r"^set:help:\w+$"))
async def on_help_section(callback: CallbackQuery):
    key = callback.data.split(":")[2]
    if key not in {k for k, _, _ in HELP_SECTIONS}:
        await callback.answer("Такой темы нет", show_alert=True)
        return
    await callback.answer()
    await _show_rich_card(callback, "settings_help", lambda wi: help_section_html(key, wi),
                          help_section_kb(key))


# ---------------------------------------------------------------------------
# Поддержка
# ---------------------------------------------------------------------------

class SupportStates(StatesGroup):
    waiting = State()


def support_html(user: dict, with_image: bool = False, note: str = "") -> str:
    return (
        f"{_image(with_image)}"
        f"<p><b>{_em(EM_SUPPORT)} ПОДДЕРЖКА</b></p>"
        "<blockquote><i>Что-то работает не так или есть вопрос? Напиши нам: мы читаем каждое "
        "обращение.</i></blockquote>"
        + _tree([
            ("💬 Написать напрямую", f"@{SUPPORT_USERNAME}"),
            ("📨 Сообщить о проблеме", "обращение уйдёт администраторам вместе с твоим ID"),
        ], "Как связаться")
        + _gap()
        + _tree([
            ("Что случилось", "опиши проблему своими словами"),
            ("Где", "раздел и кнопка, на которой это произошло"),
            ("Скриншот", "можно приложить фото к сообщению"),
        ], "Что указать")
        + (f"<p>{note}</p>" if note else "")
    )


def support_kb() -> InlineKeyboardMarkup:
    return _kb(
        [InlineKeyboardButton(text=f"Написать @{SUPPORT_USERNAME}", url=SUPPORT_URL,
                              style=ButtonStyle.SUCCESS)],
        [_btn("📨 Сообщить о проблеме", "set:sup_write", ButtonStyle.PRIMARY)],
        [_back("set:main")],
    )


@router.callback_query(F.data == "set:sup")
async def on_support(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    user = await _need_user(callback)
    if user is None:
        return
    await callback.answer()
    await _show(callback, "settings_support", lambda wi: support_html(user, wi), support_kb())


@router.callback_query(F.data == "set:sup_write")
async def on_support_write(callback: CallbackQuery, state: FSMContext):
    user = await _need_user(callback)
    if user is None:
        return
    await callback.answer()
    await state.set_state(SupportStates.waiting)
    await _show(
        callback, "settings_support",
        lambda wi: (
            f"{_image(wi)}<p><b>📨 СООБЩИТЬ О ПРОБЛЕМЕ</b></p>"
            "<blockquote><i>Опиши, что случилось, одним сообщением. Можно приложить скриншот: "
            "отправь фото с подписью.</i></blockquote>"
            + _p(f"<i>Максимум {SUPPORT_MAX_LEN} символов. Твой ID и ник мы добавим сами.</i>")
        ),
        _kb([_back("set:sup", "Отмена")]),
    )


def _support_header(user: dict, uid: int, tg) -> str:
    handle = f"@{escape(tg.username)}" if tg.username else "без юзернейма"
    return (
        "<b>📨 Обращение в поддержку</b>\n"
        f"Игрок: {name_html(user)} · {handle}\n"
        f"ID: <code>{uid}</code> · уровень {user.get('level', 1)}"
    )


@router.message(SupportStates.waiting, F.text | F.photo)
async def support_receive(message: Message, state: FSMContext):
    text = message.text or ""
    if text in RESERVED_BUTTONS or text.startswith("/"):
        await state.clear()          # игрок выбрал другую кнопку или команду: отмена обращения
        raise SkipHandler
    user = _get_user(message.from_user.id)
    if user is None:
        await state.clear()
        return
    body = (message.text or message.caption or "").strip()
    if not body:
        await message.answer("<i>Добавь описание проблемы текстом (для фото напиши подпись).</i>")
        return
    if len(body) > SUPPORT_MAX_LEN:
        await message.answer(f"<i>Слишком длинно: максимум {SUPPORT_MAX_LEN} символов, у тебя {len(body)}.</i>")
        return

    now = time.time()
    left = int(user.get("support_at", 0) + SUPPORT_COOLDOWN - now)
    if left > 0:
        await message.answer(f"<i>Следующее обращение можно отправить через {left} с.</i>")
        return
    today = time.strftime("%Y-%m-%d")
    day = user.get("support_day")
    count = day[1] if isinstance(day, list) and len(day) == 2 and day[0] == today else 0
    if count >= SUPPORT_PER_DAY:
        await message.answer(f"<i>На сегодня лимит обращений исчерпан ({SUPPORT_PER_DAY}). "
                             f"Если срочно, напиши напрямую: @{SUPPORT_USERNAME}.</i>")
        return

    uid = message.from_user.id
    header = _support_header(user, uid, message.from_user)
    report = f"{header}\n\n{escape(body)}"
    link = (f"https://t.me/{message.from_user.username}" if message.from_user.username
            else f"tg://user?id={uid}")
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Карточка игрока", callback_data=f"adm:u:{uid}", style=ButtonStyle.PRIMARY),
        InlineKeyboardButton(text="Написать игроку", url=link, style=ButtonStyle.SUCCESS),
    ]])
    delivered = 0
    for admin_id in _ADMIN_IDS:
        try:
            if message.photo:
                await message.bot.send_photo(admin_id, message.photo[-1].file_id,
                                             caption=report[:1024], reply_markup=kb)
            else:
                await message.bot.send_message(admin_id, report, reply_markup=kb)
            delivered += 1
        except TelegramAPIError as e:
            log.warning("не удалось доставить обращение админу %s: %s", admin_id, e)

    user["support_at"] = now
    user["support_day"] = [today, count + 1]
    _save_users()
    _led("support_report", uid=str(uid), text=body[:300], photo=bool(message.photo), delivered=delivered)
    await state.clear()
    note = (f"<b>{_ic('ok')} Обращение отправлено!</b><br><i>Мы ответим в Telegram, как только прочитаем.</i>"
            if delivered else
            f"<b>⚠️ Не удалось доставить обращение.</b><br><i>Напиши напрямую: @{SUPPORT_USERNAME}.</i>")
    await _send_rich_card(message.bot, message.chat.id, "settings_support",
                          lambda wi: support_html(user, wi, note), support_kb())
