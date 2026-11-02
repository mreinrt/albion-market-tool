#!/usr/bin/env python3
"""Albion Online market browser for the terminal.

Prices and trade history come from the Albion Online Data Project
(https://www.albion-online-data.com). Standard library only.
"""

import gzip
import http.client
import json
import os
import re
import shutil
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

SERVERS = [
    ("Americas", "https://west.albion-online-data.com"),
    ("Asia", "https://east.albion-online-data.com"),
    ("Europe", "https://europe.albion-online-data.com"),
]
CITIES = [
    "Bridgewatch", "Caerleon", "Fort Sterling", "Lymhurst",
    "Martlock", "Thetford", "Brecilien", "Black Market",
]
TIER_NAMES = {1: "Beginner", 2: "Novice", 3: "Journeyman", 4: "Adept",
              5: "Expert", 6: "Master", 7: "Grandmaster", 8: "Elder"}
QUALITIES = {1: "Normal", 2: "Good", 3: "Outstanding", 4: "Excellent", 5: "Masterpiece"}

ITEMS_URL = "https://raw.githubusercontent.com/ao-data/ao-bin-dumps/master/formatted/items.txt"
CACHE_DIR = os.path.join(
    os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "albion-market"
)
ITEMS_MAX_AGE = 7 * 86400   # re-download the item list weekly
RESPONSE_TTL = 60           # seconds to reuse an API response (r forces a refetch)
PAGE_SIZE = 20
USER_AGENT = "albion-market-cli/1.0"

# Top flips scan (key t)
FLIP_COUNT = 20
VOLUME_WINDOW_HOURS = 7     # an item must have sold in the sell city within this window
FLIP_MAX_PRICE_AGE = 4      # hours; older orders are ignored (stale data makes fake flips)
SALES_TAX = 0.04            # with premium; use 0.08 without
SETUP_FEE = 0.025           # charged when listing a sell order
MIN_MARGIN = 0.05
FLIP_SELL_SHARE = 0.10      # share of the sell city's recent volume you could realistically sell yourself
SCAN_TTL = 600              # seconds before t rescans automatically
SCAN_WORKERS = 4

# Our own ceiling on AODP requests, about 2/3 of the API's published limits
# (180/minute, 300/5 minutes). Every API request waits for a free slot.
RATE_LIMITS = ((60, 120), (300, 240))   # (window in seconds, max requests)
FLIP_EXCLUDE = re.compile(r"NONTRADABLE|QUESTITEM|_MAP|TOKEN|UNIQUE|_EVENT|SKIN|VANITY|JOURNAL|TUTORIAL")
# Item categories for filtering top flips (key f), matched on the item ID in this order.
# Anything that matches none of them is "Other".
FLIP_CATEGORIES = [
    ("Weapons", re.compile(r"^T\d_(MAIN|2H)_(?!TOOL_)")),
    ("Off-hands", re.compile(r"^T\d_OFF_")),
    ("Armor", re.compile(r"^T\d_(HEAD|ARMOR|SHOES)_")),
    ("Bags & capes", re.compile(r"^T\d_(BAG|BACKPACK|CAPE|CAPEITEM)(_|@|$)")),
    ("Mounts", re.compile(r"^T\d_MOUNT")),
    ("Consumables", re.compile(r"^T\d_(POTION|MEAL)_")),
    ("Resources", re.compile(r"^T\d_(ORE|WOOD|HIDE|FIBER|ROCK|METALBAR|PLANKS|LEATHER|CLOTH|STONEBLOCK)(_|@|$)")),
    ("Farming & ingredients", re.compile(r"^T\d_(FARM|FISH|MEAT|MILK|BUTTER|EGG|BREAD|ALCOHOL|ALCHEMY|TURNIP"
                                         r"|CABBAGE|POTATO|CORN|PUMPKIN|BURDOCK|TEASEL|FOXGLOVE|MULLEIN|YARROW)")),
    ("Artifacts & runes", re.compile(r"^T\d_(ARTEFACT|RUNE|SOUL|RELIC|SHARD|ESSENCE)(_|@|$)")),
    ("Tools", re.compile(r"_TOOL_")),
]
# Route filters for top flips (key z), in the order z cycles through them.
RED_ZONE_CITIES = {"Caerleon", "Black Market"}   # reached only through red zones
FLIP_ROUTE_MODES = {
    "all": "all routes",
    "safe": "safe routes (no Caerleon or Black Market)",
    "bm": "Black Market routes only",
}
FLIP_PER_CATEGORY = 80        # each category's most promising items always get a volume check

CITY_CODES = {
    "Bridgewatch": "BW", "Caerleon": "CL", "Fort Sterling": "FS", "Lymhurst": "LH",
    "Martlock": "ML", "Thetford": "TF", "Brecilien": "BC", "Black Market": "BM",
}

# --------------------------------------------------------------------------- output

USE_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
BOLD, DIM, RED, GREEN, YELLOW, CYAN = "1", "2", "31", "32", "33", "36"


def c(text, code):
    return f"\033[{code}m{text}\033[0m" if USE_COLOR else text


def clear():
    if sys.stdout.isatty():
        print("\033[2J\033[H", end="")


def term_width():
    return shutil.get_terminal_size((80, 24)).columns


def fmt_num(n):
    return f"{round(n):,}" if n else "-"


def fmt_silver(n):
    """Compact silver amount: 950, 12.4k, 3.1M, 1.2B."""
    if not n:
        return "-"
    for size, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if n >= size:
            return f"{n / size:.1f}{suffix}"
    return f"{round(n)}"


def getch():
    """Read one keypress without waiting for Enter (falls back to a line on pipes)."""
    if os.name == "nt":
        import msvcrt
        ch = msvcrt.getwch()
        if ch == "\x03":
            raise KeyboardInterrupt
        return ch
    if not sys.stdin.isatty():
        line = sys.stdin.readline()
        if not line:
            raise EOFError
        return line.strip()[:1] or "\n"
    import termios
    import tty
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        return sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)

# --------------------------------------------------------------------------- time


def utcnow():
    return datetime.now(timezone.utc)


def parse_ts(s):
    """API timestamps are UTC without an offset; 0001-01-01 means 'never'."""
    if not s or s.startswith("0001"):
        return None
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def age(dt):
    if dt is None:
        return "-"
    secs = (utcnow() - dt).total_seconds()
    if secs < 60:
        return "<1m"
    if secs < 3600:
        return f"{int(secs // 60)}m"
    if secs < 86400:
        return f"{int(secs // 3600)}h"
    return f"{int(secs // 86400)}d"


def age_colored(dt, width):
    text = f"{age(dt):>{width}}"
    if dt is None:
        return c(text, DIM)
    stale = (utcnow() - dt).total_seconds() > 6 * 3600
    return c(text, YELLOW) if stale else text

# --------------------------------------------------------------------------- http


class ApiError(Exception):
    pass


class RateLimited(ApiError):
    pass


class NetworkError(ApiError):
    pass


_cache = {}
_conns = threading.local()   # per thread: host -> open keep-alive connection


_rate_lock = threading.Lock()
_rate_notice = [0.0]
RATE_FILE = os.path.join(CACHE_DIR, "requests.json")


def _load_request_times():
    try:
        with open(RATE_FILE) as f:
            return [float(t) for t in json.load(f)]
    except (OSError, ValueError, TypeError):
        return []


def _save_request_times(times):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(RATE_FILE + ".tmp", "w") as f:
            json.dump(times, f)
        os.replace(RATE_FILE + ".tmp", RATE_FILE)
    except OSError:
        pass


def wait_for_request_slot():
    """Block until sending one more request keeps us inside RATE_LIMITS.

    Request times are kept in RATE_FILE (wall-clock), so the limit also holds
    across restarts and copies of the tool running at the same time.
    """
    longest = max(window for window, _ in RATE_LIMITS)
    while True:
        with _rate_lock:
            now = time.time()
            times = [t for t in _load_request_times() if 0 <= now - t < longest]
            wait = 0.0
            for window, limit in RATE_LIMITS:
                recent = sorted(t for t in times if now - t < window)
                if len(recent) >= limit:
                    wait = max(wait, recent[-limit] + window - now)
            if wait <= 0:
                times.append(now)
                _save_request_times(times)
                return
            if time.monotonic() - _rate_notice[0] > 10:
                _rate_notice[0] = time.monotonic()
                print(f"\n Pausing about {wait:.0f}s to stay under the API rate limit...", flush=True)
        time.sleep(min(wait, 5) + 0.05)


def requests_sent(window=60):
    now = time.time()
    with _rate_lock:
        return sum(1 for t in _load_request_times() if 0 <= now - t < window)


def http_get(url, force=False, limited=False):
    now = time.monotonic()
    hit = _cache.get(url)
    if hit and not force and now - hit[0] < RESPONSE_TTL:
        return hit[1]
    if limited:
        wait_for_request_slot()
    body = _fetch(url)
    _cache[url] = (now, body)
    return body


def _fetch(url):
    """GET over a kept-alive HTTPS connection, one per host and thread.

    Opening a connection costs a TLS handshake (over a second to the AODP servers
    from some places), so a scan reuses connections instead of opening one per request.
    """
    parts = urllib.parse.urlsplit(url)
    pool = _conns.__dict__.setdefault("pool", {})
    path = parts.path + (f"?{parts.query}" if parts.query else "")
    headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"}
    for attempt in range(2):
        reused = parts.netloc in pool
        conn = pool.pop(parts.netloc, None) or http.client.HTTPSConnection(parts.netloc, timeout=20)
        try:
            conn.request("GET", path, headers=headers)
            resp = conn.getresponse()
            body = resp.read()
        except (http.client.HTTPException, OSError):
            conn.close()
            # The server may have closed an idle connection; retry once on a fresh one.
            if reused and attempt == 0:
                continue
            raise
        if resp.will_close:
            conn.close()
        else:
            pool[parts.netloc] = conn
        if resp.status >= 400:
            raise urllib.error.HTTPError(url, resp.status, resp.reason, resp.headers, None)
        if resp.getheader("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
        return body


def api_get(base, path, params, force=False):
    query = urllib.parse.urlencode(params, safe=",", quote_via=urllib.parse.quote)
    try:
        return json.loads(http_get(f"{base}{path}?{query}", force, limited=True))
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise RateLimited("Rate limited by the API. Wait a minute, then press r.") from e
        raise ApiError(f"API returned HTTP {e.code}.") from e
    except (urllib.error.URLError, http.client.HTTPException, OSError) as e:
        raise NetworkError(f"Network error: {getattr(e, 'reason', e)}") from e
    except ValueError as e:
        raise ApiError("API returned invalid data.") from e


def item_path(kind, uid):
    return f"/api/v2/stats/{kind}/{urllib.parse.quote(uid, safe='@,')}.json"


def api_get_retry(base, path, params):
    """api_get that waits out rate limiting and network hiccups (used by the bulk scan).

    Every retry is a new request, so it goes through the rate limiter like any other.
    """
    for attempt in range(3):
        try:
            return api_get(base, path, params)
        except RateLimited:
            if attempt == 2:
                raise
            time.sleep(30)
        except NetworkError:
            if attempt == 2:
                raise
            time.sleep(3 * (attempt + 1))


def batches(ids, max_len=3800):
    """Split item IDs into comma-joined groups that keep the URL a safe length."""
    batch, size = [], 0
    for uid in ids:
        if batch and size + len(uid) + 1 > max_len:
            yield batch
            batch, size = [], 0
        batch.append(uid)
        size += len(uid) + 1
    if batch:
        yield batch

# --------------------------------------------------------------------------- items


def load_items():
    path = os.path.join(CACHE_DIR, "items.txt")
    fresh = os.path.exists(path) and time.time() - os.path.getmtime(path) < ITEMS_MAX_AGE
    if not fresh:
        print("Downloading item list...")
        try:
            data = http_get(ITEMS_URL, force=True)
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(path + ".tmp", "wb") as f:
                f.write(data)
            os.replace(path + ".tmp", path)
        except (urllib.error.URLError, http.client.HTTPException, OSError) as e:
            if not os.path.exists(path):
                sys.exit(f"Could not download the item list: {e}")
            print("Download failed, using the cached item list.")
        _cache.pop(ITEMS_URL, None)

    items = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            # "  2888: T4_BAG                    : Adept's Bag"
            parts = line.split(":", 2)
            if len(parts) < 3:
                continue
            uid, name = parts[1].strip(), parts[2].strip()
            if uid and name:
                items.append((uid, name))
    return items


def tier_of(uid):
    m = re.match(r"T(\d)_", uid)
    return int(m.group(1)) if m else None


def enchant_of(uid):
    m = re.search(r"@(\d)$", uid)
    return int(m.group(1)) if m else 0


def tier_label(uid):
    tier = tier_of(uid)
    return f"T{tier}.{enchant_of(uid)}" if tier else ""


def fold(text):
    """Lowercase and drop apostrophes, so journeymans / journeyman's / journeyman’s all match."""
    return re.sub(r"['’‘`´]", "", text.lower())


def search_items(items, query):
    """Every word must appear in the name or ID. Also accepts t4, 4.1, t6.2 and @3 filters."""
    tier = enchant = None
    words = []
    for tok in fold(query).split():
        m = re.fullmatch(r"t?([1-8])\.([0-4])", tok)
        if m:
            tier, enchant = int(m.group(1)), int(m.group(2))
            continue
        m = re.fullmatch(r"t([1-8])", tok)
        if m:
            tier = int(m.group(1))
            continue
        m = re.fullmatch(r"@([0-4])", tok)
        if m:
            enchant = int(m.group(1))
            continue
        words.append(tok)
    if not words:
        return []

    results = []
    for uid, name in items:
        hay = fold(f"{name} {uid}")
        if not all(w in hay for w in words):
            continue
        if tier is not None and tier_of(uid) != tier:
            continue
        if enchant is not None and enchant_of(uid) != enchant:
            continue
        results.append((uid, name))

    def rank(entry):
        uid, name = entry
        base = re.sub(r"^T\d_", "", uid.split("@")[0])
        in_name = all(w in fold(name) for w in words)
        return (not in_name, len(base), base, uid)

    return sorted(results, key=rank)

# --------------------------------------------------------------------------- flips


def flip_category(uid):
    for name, pattern in FLIP_CATEGORIES:
        if pattern.search(uid):
            return name
    return "Other"


def route_allowed(route, mode):
    if mode == "safe":
        return not RED_ZONE_CITIES & {route["buy_city"], route["sell_city"]}
    if mode == "bm":
        return route["sell_city"] == "Black Market"
    return True


def is_fresh(dt):
    return dt is not None and utcnow() - dt <= timedelta(hours=FLIP_MAX_PRICE_AGE)


def item_routes(by_city):
    """Every profitable buy-city -> sell-city route for one item.

    Buying means taking the cheapest sell order. Selling is either an instant sale
    into the highest buy order, or listing a sell order 1 silver under the cheapest one.
    """
    routes = []
    for buy_city, b in by_city.items():
        cost, buy_ts = b["sell_price_min"], parse_ts(b["sell_price_min_date"])
        if buy_city == "Black Market" or not cost or not is_fresh(buy_ts):
            continue
        for sell_city, s in by_city.items():
            if sell_city == buy_city:
                continue
            options = []
            ts = parse_ts(s["buy_price_max_date"])
            if s["buy_price_max"] and is_fresh(ts):
                price = s["buy_price_max"]
                options.append(("instant sell", price, price * (1 - SALES_TAX), ts))
            ts = parse_ts(s["sell_price_min_date"])
            if sell_city != "Black Market" and s["sell_price_min"] > 1 and is_fresh(ts):
                price = s["sell_price_min"] - 1
                options.append(("list order", price, price * (1 - SALES_TAX - SETUP_FEE), ts))
            for method, price, net, ts in options:
                profit = net - cost
                if profit > 0 and profit / cost >= MIN_MARGIN:
                    routes.append({
                        "buy_city": buy_city, "buy_price": cost, "buy_ts": buy_ts,
                        "sell_city": sell_city, "sell_price": price, "sell_ts": ts,
                        "method": method, "net": net, "profit": profit, "margin": profit / cost,
                    })
    return routes

# --------------------------------------------------------------------------- support splash

DONATE_URL = "https://www.paypal.com/ncp/payment/GA5RGE8JVRFH2"
DONATE_MESSAGE = """\
Currently SofaLord is an American living in the Philippines, where the cost of living
is cheap but unfortunately, he has no income.

He's not asking for much. Just enough to keep the lights on, the internet connected,
and the Python code running.

If you enjoy this application, please consider throwing a few pesos (or dollars) his way.

Remember: Every donation helps keep an unemployed American's dreams of becoming
a full-time keyboard warrior alive."""

# QR code for DONATE_URL (version 4, error correction M), one string per module row,
# "#" = dark. Python has no QR encoder built in, so it is stored here pre-generated.
# If DONATE_URL changes, regenerate it, e.g. with: python3 -m pip install segno, then
# [''.join('#' if m else '.' for m in row) for row in segno.make(URL, error='m').matrix]
DONATE_QR = [
    "#######.#.##..#.#.####.#..#######",
    "#.....#.##.##.#.##...#.#..#.....#",
    "#.###.#...##.##.#.#.#.#...#.###.#",
    "#.###.#.###....#..##.##...#.###.#",
    "#.###.#......#..#.#....##.#.###.#",
    "#.....#......#.##.#...#...#.....#",
    "#######.#.#.#.#.#.#.#.#.#.#######",
    "........##.##..#####..#.#........",
    "#.##.###..#...####...#..#.#..#.##",
    "#.#..#.###.#.#..########..##.####",
    ".######..##..#....#..###..####.##",
    ".#.#.#...#......##....######.#.##",
    ".#..########..###...####.#...#.##",
    "...##..#..##..#.....##.##.##...#.",
    ".#..#.#.#.##.##..####.#...###....",
    "###.##.###.###.#.....######...#..",
    "...#####.##...#.#.#.###..##.###..",
    "######..#......#....##.#..#.##.##",
    "....###.#.#....##.#..#...#.##.##.",
    "####.....#..##.#####....##..#..##",
    "..#.#.##..#....##....##....#.##..",
    "##.#.#..#.#.##.###..###....#.#..#",
    "....#.##.##...##..###..#.###..###",
    ".#.###.###.##.##....#.#...#.##.#.",
    "#...#.#....#.#...#.#..#######.#..",
    "........#.######...##...#...##...",
    "#######.###.#...####.####.#.#....",
    "#.....#.#.##..##.##.##.##...####.",
    "#.###.#....####.#####..######.##.",
    "#.###.#.#....#..#..#..###..#.#..#",
    "#.###.#.#.#....####..#.#####.#...",
    "#.....#..##..##..##.#.#.#####...#",
    "#######.####.#...###.#......#....",
]


def qr_lines(rows, border=4):
    """Draw a QR code with half blocks, two module rows per line, black on white."""
    width = len(rows[0]) + 2 * border
    grid = ["." * width] * border + ["." * border + r + "." * border for r in rows] + ["." * width] * (border + 1)
    lines = []
    for top, bottom in zip(grid[0::2], grid[1::2]):
        line = "".join(" ▄▀█"[(t == "#") * 2 + (b == "#")] for t, b in zip(top, bottom))
        lines.append(f"\x1b[30;107m{line}\x1b[0m" if USE_COLOR else line)
    return lines


def support_splash():
    clear()
    print()
    for line in DONATE_MESSAGE.splitlines():
        print(f"  {line}")
    print()
    print("  " + c("☕ Support SofaLord. Help keep the lights on!", BOLD + ";" + YELLOW))
    print()
    for line in qr_lines(DONATE_QR):
        print(f"  {line}")
    print()
    print(f"  Scan to donate with PayPal, or open: {c(DONATE_URL, CYAN)}")
    print()
    print("  Thanks for stopping by!")
    print()
    print(c("  Press any key to continue...", DIM))
    getch()


# --------------------------------------------------------------------------- charts


def fit_columns(n, label_w):
    """Columns per data point so the chart fills the terminal (1..4)."""
    return max(1, min(4, (term_width() - label_w - 3) // max(n, 1)))


def line_chart(values, height, label_w, col_width):
    """ASCII line chart; None values are gaps. Returns lines."""
    pts = [v for v in values if v is not None]
    if not pts:
        return [" " * label_w + "   (no trades recorded)"]
    lo, hi = min(pts), max(pts)
    span = hi - lo
    if span == 0:
        height = 1

    def row(v):
        return 0 if span == 0 else round((v - lo) / span * (height - 1))

    cols = [v for v in values for _ in range(col_width)]
    grid = [[" "] * len(cols) for _ in range(height)]
    for x, v in enumerate(cols):
        if v is None:
            continue
        y = row(v)
        prev = cols[x - 1] if x else None
        if prev is None or row(prev) == y:
            grid[height - 1 - y][x] = "─"
            continue
        y0 = row(prev)
        up = y > y0
        grid[height - 1 - y0][x] = "╯" if up else "╮"
        grid[height - 1 - y][x] = "╭" if up else "╰"
        for mid in range(min(y0, y) + 1, max(y0, y)):
            grid[height - 1 - mid][x] = "│"

    lines = []
    for r in range(height - 1, -1, -1):
        value = lo if span == 0 else lo + span * r / (height - 1)
        label = f"{round(value):,}"
        lines.append(f"{label:>{label_w}} ┤" + c("".join(grid[height - 1 - r]), CYAN))
    return lines


def bar_chart(values, height, label_w, col_width, label):
    """Vertical bars with 1/8-block resolution. None/0 values are empty."""
    blocks = " ▁▂▃▄▅▆▇█"
    mx = max((v for v in values if v), default=0)
    lines = []
    for r in range(height - 1, -1, -1):
        cells = []
        for v in values:
            ch = " "
            if v and mx:
                fill = v / mx * height * 8 - r * 8
                ch = blocks[max(0, min(8, round(fill)))]
                if r == 0 and ch == " ":
                    ch = "▁"   # anything sold should be visible
            cells.append(ch * max(1, col_width - 1) + (" " if col_width > 1 else ""))
        text = label if r == height - 1 else ""
        lines.append(f"{text:>{label_w}} │" + c("".join(cells), DIM))
    return lines


def x_axis(times, every, fmt, label_w, col_width):
    lines = [" " * label_w + " └" + "─" * (len(times) * col_width)]
    line = [" "] * (label_w + 2 + len(times) * col_width + 10)
    last_end = -1
    for i, t in enumerate(times):
        if i % every:
            continue
        text = t.astimezone().strftime(fmt)
        pos = label_w + 2 + i * col_width
        if pos <= last_end:
            continue
        line[pos:pos + len(text)] = text
        last_end = pos + len(text)
    lines.append("".join(line).rstrip())
    return lines

# --------------------------------------------------------------------------- app


class App:
    def __init__(self, items):
        self.items = items
        self.server = None       # index into SERVERS
        self.item = None         # (uid, name)
        self.quality = 1
        self.prices = {}         # city -> price row
        self.hourly = {}         # city -> hourly history records (last ~2 days)
        self.error = None
        self.flips = None        # (monotonic time, server index, results)
        self.flip_filter = None  # category name, or None for all
        self.flip_routes = "all"  # key into FLIP_ROUTE_MODES

    @property
    def base(self):
        return SERVERS[self.server][1]

    def header(self):
        server = SERVERS[self.server][0] if self.server is not None else "-"
        print(c(" ALBION MARKET", BOLD) + c(f"  ·  {server} server  ·  data: albion-online-data.com", DIM))
        print()

    def item_title(self):
        uid, name = self.item
        tier = tier_label(uid)
        return f"{c(name, BOLD)}{'  ' + tier if tier else ''}  {c(uid, DIM)}  ·  Quality: {QUALITIES[self.quality]}"

    # ---- server

    def choose_server(self):
        while True:
            clear()
            self.header()
            print(" Select server:\n")
            for i, (name, _) in enumerate(SERVERS, 1):
                mark = c("  (current)", DIM) if self.server == i - 1 else ""
                print(f"   {c(str(i), BOLD)}  {name}{mark}")
            print()
            print(c(" [1-3] select  [q] quit" + ("  [b] back" if self.server is not None else ""), DIM))
            k = getch().lower()
            if k in ("1", "2", "3"):
                self.server = int(k) - 1
                return
            if k == "b" and self.server is not None:
                return
            if k == "q":
                raise SystemExit

    # ---- search

    # ---- home

    def home_screen(self):
        while True:
            clear()
            self.header()
            if self.item:
                print(" Current item: " + self.item_title())
                print()
            print(" What do you want to do?\n")
            options = [("s", "Search for an item"), ("t", "Top flips (buy in one city, sell in another)"),
                       ("c", "Change server")]
            if self.item:
                options.insert(1, ("i", "Back to current item"))
            options.append(("q", "Quit"))
            for key, label in options:
                print(f"   {c(key, BOLD)}  {label}")
            print()
            k = getch().lower()
            if k == "s":
                return "search"
            if k == "t":
                return "flips"
            if k == "i" and self.item:
                return "market"
            if k == "c":
                self.choose_server()
            elif k == "q":
                return "quit"

    def search_screen(self):
        flash = None
        while True:
            clear()
            self.header()
            if flash:
                print(" " + c(flash, YELLOW) + "\n")
            print(c(" Examples: bag  ·  t6 broadsword  ·  4.1 hide  ·  avalonian energy", DIM))
            try:
                query = input(" Search item (blank = back): ").strip()
            except EOFError:
                return "quit"
            if not query:
                return "market" if self.item else "home"
            results = search_items(self.items, query)
            if not results:
                flash = f'No items match "{query}".'
                continue
            results, label = self.narrow_by_tier(query, results)
            picked = self.pick_result(label, results)
            if picked:
                self.item = picked
                self.quality = 1
                self.prices, self.hourly = {}, {}
                return "market"
            flash = None

    def narrow_by_tier(self, query, results):
        """Offer a tier filter when the results span more than one tier."""
        tiers = sorted({t for t in (tier_of(uid) for uid, _ in results) if t})
        if len(tiers) < 2:
            return results, query
        clear()
        self.header()
        print(f' Found {len(results)} items for "{query}" across tiers {", ".join(f"T{t}" for t in tiers)}.\n')
        print(" Narrow the results by tier?  [y] yes   [n] no, show all")
        while (k := getch().lower()) not in ("y", "n"):
            pass
        if k == "n":
            return results, query

        print()
        for t in tiers:
            count = sum(1 for uid, _ in results if tier_of(uid) == t)
            print(f"   {c(str(t), BOLD)}  T{t} {TIER_NAMES[t]:<12}{c(f'{count} item' + ('s' if count != 1 else ''), DIM)}")
        print()
        print(f" Which tier?  [{tiers[0]}-{tiers[-1]}] select   [a] show all")
        while True:
            k = getch().lower()
            if k == "a":
                return results, query
            if k.isdigit() and int(k) in tiers:
                t = int(k)
                return [r for r in results if tier_of(r[0]) == t], f"{query}, T{t}"

    def pick_result(self, query, results):
        page = 0
        pages = (len(results) - 1) // PAGE_SIZE + 1
        while True:
            clear()
            self.header()
            print(f' Results for "{query}": {len(results)} items' + (f"  (page {page + 1}/{pages})" if pages > 1 else ""))
            print()
            start = page * PAGE_SIZE
            for i, (uid, name) in enumerate(results[start:start + PAGE_SIZE], start + 1):
                print(f"  {i:>4}  {tier_label(uid):<5} {name:<40.40} {c(uid, DIM)}")
            print()
            nav = "  [n]ext/[p]rev page" if pages > 1 else ""
            try:
                choice = input(f" Number to open{nav}, blank = new search: ").strip().lower()
            except EOFError:
                raise SystemExit
            if not choice:
                return None
            if choice == "n" and page < pages - 1:
                page += 1
            elif choice == "p" and page > 0:
                page -= 1
            elif choice.isdigit() and 1 <= int(choice) <= len(results):
                return results[int(choice) - 1]

    # ---- market overview

    def load_market(self, force=False):
        print(c(" Loading market data...", DIM))
        uid = self.item[0]
        locs = ",".join(CITIES)
        since = (utcnow() - timedelta(days=2)).strftime("%m-%d-%Y")
        try:
            prices = api_get(self.base, item_path("prices", uid),
                             {"locations": locs, "qualities": self.quality}, force)
            hist = api_get(self.base, item_path("history", uid),
                           {"locations": locs, "qualities": self.quality, "time-scale": 1, "date": since}, force)
        except ApiError as e:
            self.error = str(e)
            self.prices, self.hourly = {}, {}
            return
        self.error = None
        self.prices = {p["city"]: p for p in prices if p.get("quality") == self.quality}
        self.hourly = {h["location"]: h["data"] for h in hist if h.get("quality") == self.quality}

    @staticmethod
    def stats_24h(records):
        cutoff = utcnow() - timedelta(hours=24)
        recent = [r for r in records if parse_ts(r["timestamp"]) >= cutoff]
        sold = sum(r["item_count"] for r in recent)
        avg = sum(r["item_count"] * r["avg_price"] for r in recent) / sold if sold else None
        return sold, avg, recent

    def render_market(self):
        clear()
        self.header()
        print(" " + self.item_title())
        print()
        if self.error:
            print(" " + c(self.error, RED) + "\n")

        # Best prices: cheapest sell order (Black Market has no real sell side), highest buy order.
        sells = [(p["sell_price_min"], city) for city, p in self.prices.items()
                 if p["sell_price_min"] and city != "Black Market"]
        buys = [(p["buy_price_max"], city) for city, p in self.prices.items() if p["buy_price_max"]]
        best_sell = min(sells)[1] if sells else None
        best_buy = max(buys)[1] if buys else None

        print(c(f"  {'#':>2}  {'City':<15}{'Sell min':>10}{'age':>5}{'Buy max':>11}{'age':>5}"
                f"{'24h avg':>11}{'24h sold':>10}", BOLD))
        print(c("  " + "─" * 69, DIM))
        for i, city in enumerate(CITIES, 1):
            p = self.prices.get(city, {})
            sold, avg, _ = self.stats_24h(self.hourly.get(city, []))
            sell = f"{fmt_num(p.get('sell_price_min')):>10}"
            buy = f"{fmt_num(p.get('buy_price_max')):>11}"
            if city == best_sell:
                sell = c(sell, GREEN)
            if city == best_buy:
                buy = c(buy, GREEN)
            print(f"  {c(f'{i:>2}', BOLD)}  {city:<15}{sell}{age_colored(parse_ts(p.get('sell_price_min_date')), 5)}"
                  f"{buy}{age_colored(parse_ts(p.get('buy_price_max_date')), 5)}"
                  f"{fmt_num(avg):>11}{(f'{sold:,}' if sold else '-'):>10}")
        print()
        has_orders = any(p["sell_price_min"] or p["buy_price_max"] for p in self.prices.values())
        if not self.error and not has_orders and not any(self.hourly.values()):
            print(c(f"  No current orders uploaded for this item on the {SERVERS[self.server][0]} server. Orders only"
                    f"\n  show up after someone running the Albion Data Client opens this market in game, and"
                    f"\n  expire if nobody sees them again. Press [v] for past trade volume, or run the client"
                    f"\n  yourself and open the item in a market to upload fresh prices.", YELLOW))
            print()
        print(c("  green = best price · yellow age = older than 6h · 24h = trades in the last 24 hours", DIM))
        print()
        print(" [1-8] city details  [v] volume  [s] search  [t] top flips")
        print(" [k] quality  [c] server  [r] refresh  [q] quit")

    def market_screen(self):
        self.load_market()
        while True:
            self.render_market()
            k = getch().lower()
            if k and k in "12345678":
                return ("detail", int(k) - 1)
            if k == "s":
                return "search"
            if k == "t":
                return "flips"
            if k == "v":
                return "volume"
            if k == "k":
                self.quality = self.quality % 5 + 1
                self.load_market()
            elif k == "c":
                self.choose_server()
                self.load_market()
            elif k == "r":
                self.load_market(force=True)
            elif k == "q":
                return "quit"

    # ---- city detail

    def load_detail(self, city, force=False):
        uid = self.item[0]
        params = {"locations": city, "qualities": self.quality}
        try:
            hourly = api_get(self.base, item_path("history", uid),
                             {**params, "time-scale": 1,
                              "date": (utcnow() - timedelta(days=7)).strftime("%m-%d-%Y")}, force)
            daily = api_get(self.base, item_path("history", uid),
                            {**params, "time-scale": 24,
                             "date": (utcnow() - timedelta(days=31)).strftime("%m-%d-%Y")}, force)
        except ApiError as e:
            return None, None, str(e)

        def pick(rows):
            for h in rows:
                if h.get("location") == city and h.get("quality") == self.quality:
                    return sorted(h["data"], key=lambda r: r["timestamp"])
            return []
        return pick(hourly), pick(daily), None

    def render_detail(self, city, hourly, daily, error):
        clear()
        self.header()
        print(f" {c(city, BOLD + ';' + CYAN)}  ·  " + self.item_title())
        print()
        if error:
            print(" " + c(error, RED) + "\n")

        p = self.prices.get(city, {})

        def when(key):
            dt = parse_ts(p.get(key))
            return f"({age(dt)} ago)" if dt else ""
        print(c(" Current orders", BOLD))
        print(f"   Sell   lowest {fmt_num(p.get('sell_price_min')):>12} {when('sell_price_min_date')}"
              f"   highest {fmt_num(p.get('sell_price_max')):>12} {when('sell_price_max_date')}")
        print(f"   Buy   highest {fmt_num(p.get('buy_price_max')):>12} {when('buy_price_max_date')}"
              f"    lowest {fmt_num(p.get('buy_price_min')):>12} {when('buy_price_min_date')}")
        print()

        # Hourly: 24 one-hour buckets ending now, or ending at the last recorded trade if
        # nothing traded in the last 24h (AODP only knows about trades its users saw).
        sold, avg, recent = self.stats_24h(hourly)
        by_hour = {parse_ts(r["timestamp"]): r for r in hourly}
        anchor = utcnow().replace(minute=0, second=0, microsecond=0)
        note = ""
        if not recent and hourly:
            last = parse_ts(hourly[-1]["timestamp"])
            anchor = last
            note = c(f"  (no trades in the last 24h; showing the 24h up to the last recorded trade, {age(last)} ago)", YELLOW)
        hours = [anchor - timedelta(hours=23 - i) for i in range(24)]
        prices_h = [by_hour[h]["avg_price"] if h in by_hour else None for h in hours]
        counts_h = [by_hour[h]["item_count"] if h in by_hour else None for h in hours]
        window = [v for v in prices_h if v is not None]
        window_sold = sum(v for v in counts_h if v)

        if recent:
            summary = f"{sold:,} sold · avg {fmt_num(avg)} · low {fmt_num(min(window))} · high {fmt_num(max(window))}"
        elif window:
            summary = f"{window_sold:,} sold in window · low {fmt_num(min(window))} · high {fmt_num(max(window))}"
        else:
            summary = "no trades recorded"
        print(c(" Last 24 hours", BOLD) + f"  {summary}" + c("   (hourly average price / items sold)", DIM))
        if note:
            print(note)

        label_w = max([len(fmt_num(v)) for v in window] + [len(f"{max(v for v in counts_h if v):,}") if window else 1, 6])
        cw = fit_columns(24, label_w)
        for line in line_chart(prices_h, 8, label_w, cw):
            print(line)
        if window:
            top = max(v for v in counts_h if v)
            for line in bar_chart(counts_h, 3, label_w, cw, f"{top:,}"):
                print(line)
        for line in x_axis(hours, 6, "%H:%M", label_w, cw):
            print(line)
        print()

        # Daily: last 30 days.
        by_day = {parse_ts(r["timestamp"]): r for r in daily}
        today = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        days = [today - timedelta(days=29 - i) for i in range(30)]
        prices_d = [by_day[d]["avg_price"] if d in by_day else None for d in days]
        known = [v for v in prices_d if v is not None]
        sold_30 = sum(by_day[d]["item_count"] for d in days if d in by_day)
        summary = (f"{sold_30:,} sold · low {fmt_num(min(known))} · high {fmt_num(max(known))}"
                   if known else "no trades recorded")
        print(c(" Last 30 days", BOLD) + f"  {summary}" + c("   (daily average price)", DIM))
        label_w = max([len(fmt_num(v)) for v in known] + [6])
        cw = fit_columns(30, label_w)
        for line in line_chart(prices_d, 6, label_w, cw):
            print(line)
        for line in x_axis(days, 7, "%b %d", label_w, cw):
            print(line)
        print()

        print(c(" Recent trades (hourly totals, local time)", BOLD))
        if hourly:
            print(c(f"   {'Time':<14}{'Sold':>8}{'Avg price':>13}", DIM))
            for r in reversed(hourly[-8:]):
                t = parse_ts(r["timestamp"]).astimezone().strftime("%a %d %H:%M")
                print(f"   {t:<14}{r['item_count']:>8,}{fmt_num(r['avg_price']):>13}")
        else:
            print("   none recorded in the last 7 days")
        print()
        print(" [b] back  [1-8] other city  [v] volume  [s] search  [t] top flips")
        print(" [k] quality  [c] server  [r] refresh  [q] quit")

    def detail_screen(self, idx):
        force = False
        while True:
            city = CITIES[idx]
            print(c(" Loading trade history...", DIM))
            hourly, daily, error = self.load_detail(city, force)
            force = False
            self.render_detail(city, hourly or [], daily or [], error)
            k = getch().lower()
            if k and k in "12345678":
                idx = int(k) - 1
            elif k in ("b", "\x7f", "\x08"):
                return "market"
            elif k == "s":
                return "search"
            elif k == "t":
                return "flips"
            elif k == "v":
                return "volume"
            elif k == "k":
                self.quality = self.quality % 5 + 1
                self.load_market()
            elif k == "c":
                self.choose_server()
                self.load_market()
            elif k == "r":
                force = True
                self.load_market(force=True)
            elif k == "q":
                return "quit"

    # ---- volume by city

    def volume_rows(self, daily, hourly_7d):
        now = utcnow()
        week_start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=6)
        rows = []
        for city in CITIES:
            hourly = hourly_7d.get(city, [])
            # Daily totals sometimes lag days behind; fill missing days from the hourly records.
            by_day = {d["timestamp"][:10]: dict(d) for d in daily.get(city, [])}
            filled = {}
            for r in hourly:
                if r["timestamp"][:10] not in by_day:
                    day = filled.setdefault(r["timestamp"][:10], {"item_count": 0, "silver": 0})
                    day["item_count"] += r["item_count"]
                    day["silver"] += r["item_count"] * r["avg_price"]
            for key, d in filled.items():
                by_day[key] = {"timestamp": key + "T00:00:00", "item_count": d["item_count"],
                               "avg_price": d["silver"] / d["item_count"] if d["item_count"] else 0}
            days = sorted(by_day.values(), key=lambda d: d["timestamp"])
            week = [d for d in days if parse_ts(d["timestamp"]) >= week_start]
            sold_7d = sum(d["item_count"] for d in week)
            rows.append({
                "city": city, "days": days, "hourly": hourly,
                "7h": sum(r["item_count"] for r in hourly
                          if parse_ts(r["timestamp"]) >= now - timedelta(hours=VOLUME_WINDOW_HOURS)),
                "24h": self.stats_24h(hourly)[0],
                "7d": sold_7d,
                "30d": sum(d["item_count"] for d in days),
                "avg_7d": sum(d["item_count"] * d["avg_price"] for d in week) / sold_7d if sold_7d else None,
                "silver": sum(d["item_count"] * d["avg_price"] for d in days),
            })
        week_total = sum(r["7d"] for r in rows)
        for r in rows:
            r["share"] = r["7d"] / week_total if week_total else 0
        return rows

    def render_volume(self, rows, error):
        clear()
        self.header()
        print(c(" Trade volume by city", BOLD) + "  ·  " + self.item_title())
        print()
        if error:
            print(" " + c(error, RED) + "\n")

        print(c(f"  {'#':>2}  {'City':<14}{f'{VOLUME_WINDOW_HOURS}h':>6}{'24h':>7}{'7 days':>8}{'7d share':>9}"
                f"{'30 days':>9}{'7d avg price':>13}{'30d silver':>11}", BOLD))
        print(c("  " + "─" * 79, DIM))
        top = max((r["7d"] for r in rows), default=0)
        n = lambda r, key: f"{r[key]:,}" if r[key] else "-"
        for i, r in enumerate(rows, 1):
            line = (f"{n(r, '7h'):>6}{n(r, '24h'):>7}{n(r, '7d'):>8}{r['share']:>9.0%}{n(r, '30d'):>9}"
                    f"{fmt_num(r['avg_7d']):>13}{fmt_silver(r['silver']):>11}")
            print(f"  {c(f'{i:>2}', BOLD)}  {r['city']:<14}" + (c(line, GREEN) if top and r["7d"] == top else line))
        print(c("  " + "─" * 79, DIM))
        total = lambda key: f"{sum(r[key] for r in rows):,}" if any(r[key] for r in rows) else "-"
        print(c(f"      {'All markets':<14}{total('7h'):>6}{total('24h'):>7}{total('7d'):>8}{'':>9}{total('30d'):>9}"
                f"{'':>13}{fmt_silver(sum(r['silver'] for r in rows)):>11}", BOLD))
        print()
        print(c(" Volume is what Albion Data Client users saw, so real totals are higher. Green = busiest market.", DIM))
        print()
        print(f" Select a city {c('[1-8]', BOLD)} to see its volume chart.")
        print()
        print(" [b] back  [s] search  [t] top flips")
        print(" [k] quality  [c] server  [r] refresh  [q] quit")

    def render_city_volume(self, idx, rows, error):
        r = rows[idx]
        clear()
        self.header()
        print(f" {c(r['city'], BOLD + ';' + CYAN)} trade volume  ·  " + self.item_title())
        print()
        if error:
            print(" " + c(error, RED) + "\n")

        rank = sorted(range(len(rows)), key=lambda i: rows[i]["7d"], reverse=True).index(idx) + 1
        n = lambda key: f"{r[key]:,}" if r[key] else "-"
        print(f"   Items sold   {VOLUME_WINDOW_HOURS}h {c(n('7h'), BOLD)}   24h {c(n('24h'), BOLD)}"
              f"   7 days {c(n('7d'), BOLD)}   30 days {c(n('30d'), BOLD)}")
        share = f"{r['share']:.0%}"
        print(f"   7-day share  {c(share, BOLD)} of all markets"
              + (f" (#{rank} of {len(rows)})" if r["7d"] else ""))
        print(f"   7d avg price {c(fmt_num(r['avg_7d']), BOLD)}   30d silver traded {c(fmt_silver(r['silver']), BOLD)}")
        print()

        # Daily items sold, last 30 days. Days with no record are gaps, not zeros.
        today = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        days = [today - timedelta(days=29 - i) for i in range(30)]
        by_day = {parse_ts(d["timestamp"]): d["item_count"] for d in r["days"]}
        daily = [by_day.get(d) for d in days]
        known = [v for v in daily if v is not None]
        print(c(" Items sold per day, last 30 days", BOLD))
        label_w = max([len(f"{v:,}") for v in known] + [6])
        cw = fit_columns(30, label_w)
        for line in line_chart(daily, 8, label_w, cw):
            print(line)
        for line in x_axis(days, 7, "%b %d", label_w, cw):
            print(line)
        print()

        now = utcnow().replace(minute=0, second=0, microsecond=0)
        hours = [now - timedelta(hours=23 - i) for i in range(24)]
        by_hour = {parse_ts(h["timestamp"]): h["item_count"] for h in r["hourly"]}
        hourly = [by_hour.get(h) for h in hours]
        known = [v for v in hourly if v is not None]
        print(c(" Items sold per hour, last 24 hours", BOLD))
        label_w = max([len(f"{v:,}") for v in known] + [6])
        cw = fit_columns(24, label_w)
        for line in line_chart(hourly, 6, label_w, cw):
            print(line)
        for line in x_axis(hours, 6, "%H:%M", label_w, cw):
            print(line)
        print()
        print(" [b] back to all cities  [1-8] other city  [p] price details  [s] search")
        print(" [t] top flips  [k] quality  [c] server  [r] refresh  [q] quit")

    def volume_screen(self):
        force, reload, city = False, True, None
        while True:
            if reload:
                print(c(" Loading trade volume...", DIM))
                error = None
                daily, hourly = {}, {}
                params = {"locations": ",".join(CITIES), "qualities": self.quality}
                try:
                    for scale, days, out in ((24, 30, daily), (1, 7, hourly)):
                        rows = api_get(self.base, item_path("history", self.item[0]),
                                       {**params, "time-scale": scale,
                                        "date": (utcnow() - timedelta(days=days)).strftime("%m-%d-%Y")}, force)
                        out.update({h["location"]: h["data"] for h in rows if h.get("quality") == self.quality})
                except ApiError as e:
                    error = str(e)
                rows = self.volume_rows(daily, hourly)
                force, reload = False, False
            if city is None:
                self.render_volume(rows, error)
            else:
                self.render_city_volume(city, rows, error)
            k = getch().lower()
            if k and k in "12345678":
                city = int(k) - 1
            elif k in ("b", "\x7f", "\x08"):
                if city is None:
                    return "market"
                city = None
            elif k == "p" and city is not None:
                return ("detail", city)
            elif k == "s":
                return "search"
            elif k == "t":
                return "flips"
            elif k == "q":
                return "quit"
            elif k == "k":
                self.quality = self.quality % 5 + 1
                self.load_market()
                reload = True
            elif k == "c":
                self.choose_server()
                self.load_market()
                reload = True
            elif k == "r":
                self.load_market(force=True)
                force = reload = True

    # ---- market-wide scans

    def bulk_fetch(self, label, kind, groups, params):
        """Fetch many item batches in parallel, showing progress."""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        rows, done = [], 0
        with ThreadPoolExecutor(SCAN_WORKERS) as ex:
            futures = [ex.submit(api_get_retry, self.base, item_path(kind, ",".join(g)), params)
                       for g in groups]
            for f in as_completed(futures):
                rows.extend(f.result())
                done += 1
                print(f"\r {label} {done}/{len(groups)}", end="", flush=True)
        print()
        return rows

    # ---- top flips

    def scan_flips(self):
        names = {uid: name for uid, name in self.items}
        pool = [uid for uid in names if re.match(r"T[4-8]_", uid) and not FLIP_EXCLUDE.search(uid)]
        locs = ",".join(CITIES)
        run = self.bulk_fetch

        clear()
        self.header()
        print(f" Scanning {len(pool):,} items on the {SERVERS[self.server][0]} server...\n")
        by_item = {}
        for r in run("Prices", "prices", list(batches(pool)), {"locations": locs, "qualities": 1}):
            if r.get("quality") == 1:
                by_item.setdefault(r["item_id"], {})[r["city"]] = r
        candidates = {uid: routes for uid, rows in by_item.items() if (routes := item_routes(rows))}

        # Only fetch history for the most promising items; it is the slower call. Besides the
        # overall top 600, each category's best items are checked so every filter has results.
        ranked = sorted(candidates, key=lambda u: max(r["profit"] for r in candidates[u]), reverse=True)
        top, per_cat = ranked[:600], {}
        for uid in ranked[600:]:
            cat = flip_category(uid)
            if per_cat.get(cat, 0) < FLIP_PER_CATEGORY:
                per_cat[cat] = per_cat.get(cat, 0) + 1
                top.append(uid)
        cutoff = utcnow() - timedelta(hours=VOLUME_WINDOW_HOURS)
        since = (utcnow() - timedelta(days=1)).strftime("%m-%d-%Y")
        volume = {}
        for h in run("Trade volume", "history", list(batches(top, 2500)),
                     {"locations": locs, "qualities": 1, "time-scale": 1, "date": since}):
            if h.get("quality") == 1:
                volume[(h["item_id"], h["location"])] = sum(
                    d["item_count"] for d in h["data"] if parse_ts(d["timestamp"]) >= cutoff)

        results = []
        for uid in top:
            routes = []
            for r in candidates[uid]:
                sold = volume.get((uid, r["sell_city"]), 0)
                if sold:
                    # Ranked by expected profit: profit per item × how many you could realistically
                    # sell, a share of what the sell city sold recently (at least 1).
                    units = max(1, int(sold * FLIP_SELL_SHARE))
                    routes.append({**r, "sell_volume": sold, "buy_volume": volume.get((uid, r["buy_city"]), 0),
                                   "units": units, "score": r["profit"] * units})
            if routes:
                routes.sort(key=lambda r: r["score"], reverse=True)
                results.append({"uid": uid, "name": names[uid], "category": flip_category(uid),
                                "routes": routes})
        results.sort(key=lambda x: x["routes"][0]["score"], reverse=True)
        self.flips = (time.monotonic(), self.server, results)

    def choose_flip_filter(self, results):
        counts = {}
        for f in results:
            counts[f["category"]] = counts.get(f["category"], 0) + 1
        names = [name for name, _ in FLIP_CATEGORIES] + ["Other"]
        clear()
        self.header()
        print(c(" Show top flips for:", BOLD) + "\n")
        print(f"   {c(' 0', BOLD)}  All items{'':<17}{len(results):>5} found")
        for i, name in enumerate(names, 1):
            mark = c("  (current)", DIM) if name == self.flip_filter else ""
            print(f"   {c(f'{i:>2}', BOLD)}  {name:<26}{counts.get(name, 0):>5} found{mark}")
        print()
        try:
            choice = input(" Category number (blank = keep current): ").strip()
        except EOFError:
            return
        if choice == "0":
            self.flip_filter = None
        elif choice.isdigit() and 1 <= int(choice) <= len(names):
            self.flip_filter = names[int(choice) - 1]

    def flips_screen(self, rescan=False):
        error = None
        while True:
            stale = (not self.flips or self.flips[1] != self.server
                     or time.monotonic() - self.flips[0] > SCAN_TTL)
            if rescan or stale:
                rescan = False
                try:
                    self.scan_flips()
                    error = None
                except ApiError as e:
                    error = str(e)
            found = []
            for f in (self.flips[2] if self.flips and self.flips[1] == self.server else []):
                routes = [r for r in f["routes"] if route_allowed(r, self.flip_routes)]
                if routes:
                    found.append({**f, "routes": routes})
            found.sort(key=lambda x: x["routes"][0]["score"], reverse=True)
            results = [f for f in found if self.flip_filter in (None, f["category"])][:FLIP_COUNT]

            clear()
            self.header()
            scanned = age(utcnow() - timedelta(seconds=time.monotonic() - self.flips[0])) if self.flips else "-"
            print(c(" Top flips", BOLD) + c(f"  ·  {self.flip_filter or 'all items'}"
                                           f"  ·  {FLIP_ROUTE_MODES[self.flip_routes]}", BOLD + ";" + CYAN)
                  + c(f"  ·  scanned {scanned} ago  ·  Normal quality, T4-T8", DIM))
            print(c(f" Ranked by expected profit: profit per item × {FLIP_SELL_SHARE:.0%} of the items sold"
                    f" in the sell city in the last {VOLUME_WINDOW_HOURS}h.", DIM))
            print(c(f" Orders older than {FLIP_MAX_PRICE_AGE}h ignored.", DIM))
            print()
            if error:
                print(" " + c(error, RED) + "\n")
            if results:
                print(c(f"  {'#':>2}  {'Item':<24}{'Buy':>4}{'Sell':>5}{'Buy at':>9}{'Sell at':>9}"
                        f"{'Profit':>8}{'Margin':>7}{f'{VOLUME_WINDOW_HOURS}h sold':>8}{'Expected':>10}", BOLD))
                print(c("  " + "─" * 85, DIM))
                for i, f in enumerate(results, 1):
                    r = f["routes"][0]
                    label = f"{tier_label(f['uid'])} {f['name']}"
                    print(f"  {c(f'{i:>2}', BOLD)}  {label:<24.24}{CITY_CODES[r['buy_city']]:>4}"
                          f"{CITY_CODES[r['sell_city']]:>5}{fmt_num(r['buy_price']):>9}{fmt_num(r['sell_price']):>9}"
                          f"{fmt_num(r['profit']):>8}{r['margin']:>7.0%}{r['sell_volume']:>8,}"
                          + c(f"{fmt_num(r['score']):>10}", BOLD + ";" + GREEN))
                print()
                legend = [f"{code}={city}" for city, code in CITY_CODES.items()]
                print(c("  " + "  ".join(legend[:4]), DIM))
                print(c("  " + "  ".join(legend[4:]), DIM))
            elif not error and (self.flip_filter or self.flip_routes != "all"):
                what = f"{self.flip_filter.lower()} " if self.flip_filter else ""
                print(f" No profitable {what}flips on {FLIP_ROUTE_MODES[self.flip_routes]} with recent trade volume"
                      " right now. Press f or z to change the filters.")
            elif not error:
                print(" No profitable flips with recent trade volume right now. Press r to rescan later.")
            print()
            try:
                choice = input(" Number for details, [f] category, [z] routes, [r] rescan, [s] search, [q] quit,"
                               " blank = back: ").strip().lower()
            except EOFError:
                return "quit"
            if not choice or choice == "b":
                return "market" if self.item else "home"
            if choice in ("s", "q"):
                return {"s": "search", "q": "quit"}[choice]
            if choice == "r":
                rescan = True
            elif choice == "f":
                self.choose_flip_filter(found)
            elif choice == "z":
                modes = list(FLIP_ROUTE_MODES)
                self.flip_routes = modes[(modes.index(self.flip_routes) + 1) % len(modes)]
            elif choice.isdigit() and 1 <= int(choice) <= len(results):
                nxt = self.flip_detail(results[int(choice) - 1])
                if nxt != "flips":
                    return nxt

    def flip_detail(self, flip):
        best = flip["routes"][0]
        clear()
        self.header()
        tier = tier_label(flip["uid"])
        print(f" {c(flip['name'], BOLD)}{'  ' + tier if tier else ''}  {c(flip['uid'], DIM)}  ·  Quality: Normal")
        print()
        print(f" {c('BUY ', BOLD + ';' + GREEN)}  in {c(best['buy_city'], BOLD):<13}  at {fmt_num(best['buy_price']):>10}"
              f"   cheapest sell order, {age(best['buy_ts'])} old"
              f"   ({best['buy_volume']:,} sold there in {VOLUME_WINDOW_HOURS}h)")
        how = ("instant sell into the highest buy order" if best["method"] == "instant sell"
               else "list a sell order 1 silver under the cheapest")
        print(f" {c('SELL', BOLD + ';' + GREEN)}  in {c(best['sell_city'], BOLD):<13}  at {fmt_num(best['sell_price']):>10}"
              f"   {how}, price {age(best['sell_ts'])} old"
              f"   ({best['sell_volume']:,} sold there in {VOLUME_WINDOW_HOURS}h)")
        fees = f"{SALES_TAX:.0%} tax" + (f" + {SETUP_FEE:.1%} setup fee" if best["method"] == "list order" else "")
        print(f"        after {fees}: {fmt_num(best['net'])}  →  "
              + c(f"profit {fmt_num(best['profit'])} per item ({best['margin']:.0%})", BOLD + ";" + GREEN))
        print(f"        sell about {best['units']:,} ({FLIP_SELL_SHARE:.0%} of {best['sell_volume']:,} sold there in"
              f" {VOLUME_WINDOW_HOURS}h)  →  " + c(f"expected profit {fmt_num(best['score'])}", BOLD + ";" + GREEN)
              + f"  for {fmt_num(best['buy_price'] * best['units'])} invested")
        if RED_ZONE_CITIES & {best["buy_city"], best["sell_city"]}:
            print(c("        Caerleon (and its Black Market) is reached through red zones: you can be attacked"
                    " and lose the goods.", YELLOW))
        print()

        if len(flip["routes"]) > 1:
            print(c(" Other profitable routes", BOLD))
            print(c(f"   {'Buy in':<14}{'Sell in':<14}{'Method':<14}{'Buy at':>9}{'Sell at':>9}{'Profit':>9}"
                    f"{'Margin':>7}{f'{VOLUME_WINDOW_HOURS}h sold':>8}{'Expected':>10}", DIM))
            for r in flip["routes"][1:9]:
                print(f"   {r['buy_city']:<14}{r['sell_city']:<14}{r['method']:<14}{fmt_num(r['buy_price']):>9}"
                      f"{fmt_num(r['sell_price']):>9}{fmt_num(r['profit']):>9}{r['margin']:>7.0%}{r['sell_volume']:>8,}"
                      f"{fmt_num(r['score']):>10}")
            print()
        print(c(" Prices move fast and the API doesn't know order quantities; check in game before buying.", DIM))
        print()
        print(" [b] back to list   [o] open in market view   [s] search   [q] quit")
        while True:
            k = getch().lower()
            if k in ("b", "\x7f", "\x08"):
                return "flips"
            if k == "o":
                self.item = (flip["uid"], flip["name"])
                self.quality = 1
                self.prices, self.hourly = {}, {}
                return "market"
            if k == "s":
                return "search"
            if k == "q":
                return "quit"

    # ---- main loop

    def run(self):
        self.choose_server()
        screen = "home"
        while screen != "quit":
            if screen == "home":
                screen = self.home_screen()
            elif screen == "search":
                screen = self.search_screen()
            elif screen == "flips":
                screen = self.flips_screen()
            elif screen == "market":
                screen = self.market_screen()
            elif screen == "volume":
                screen = self.volume_screen()
            else:
                screen = self.detail_screen(screen[1])


def main():
    try:
        support_splash()
        App(load_items()).run()
    except (KeyboardInterrupt, EOFError, SystemExit):
        pass
    print()


if __name__ == "__main__":
    main()
