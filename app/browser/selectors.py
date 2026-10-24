HOME_URL = "https://gmgn.ai/?chain=sol"
ADDRESS_URL = "https://gmgn.ai/sol/address/{wallet}"

SEARCH_TRIGGER = '[data-sentry-component="Search"]'
SEARCH_INPUT = '[data-sentry-component="SearchModalInput"] input'
SEARCH_MODAL = '[data-sentry-component="SearchModalInput"]'
SEARCH_WALLET = '[data-sentry-component="SearchWallet"]'
LOG_IN_TEXT = "Log In"
WATCHLIST = '[data-sentry-component="WatchlistEntry"]'
USER_AVATAR = '[data-sentry-component="UserAvatar"]'
CONNECT = '[data-sentry-component="Connect"]'
SIGNUP_BTN = "#signUpBtn"
ANNOUNCE_MODAL = ".pi-modal"
ANNOUNCE_NEXT = (
    '.pi-modal button:has-text("Next"), '
    '.pi-modal button:has-text("Got it"), '
    '.pi-modal button:has-text("Done"), '
    '.pi-modal button:has-text("Start"), '
    '.pi-modal button:has-text("Finish"), '
    '.pi-modal button:has-text("完成"), '
    '.pi-modal button:has-text("我知道了")'
)
ADDRESS_CARD = '[data-sentry-component="NewAddressCard"]'
ACTION_TAG = '[data-sentry-component="ActionTag"]'
ACTIVITY = '[data-sentry-component="Activity"]'
TOKEN_HEADER = '[data-sentry-component="TokenHeader"]'
ACTIVITY_ROW = '[data-sentry-component="Activity"] tr.g-table-row'
ACTIVITY_TABLE = (
    '[data-sentry-component="Activity"] .g-table-wrapper, '
    '[data-sentry-component="Activity"] .g-table-content, '
    '[data-sentry-component="Activity"] table'
)
DRAWER_CLOSE = '[data-sentry-component="DrawerCloseIcon"]'
TOKEN_LINK = 'a[href*="/sol/token/"]'
TOKEN_DRAWER = (
    '[data-sentry-component="DrawerToken"], '
    '[data-sentry-component="DrawerTokenStats"], '
    '[data-sentry-component="DrawerMakerHeader"], '
    '[data-sentry-component="DrawerCloseIcon"], '
    '[data-sentry-component="DrawerTable"]'
)
QR_CANDIDATES = "canvas, img[src*='qr'], img[alt*='QR'], img[alt*='qr']"
HEADER_TOP_PX = 120
