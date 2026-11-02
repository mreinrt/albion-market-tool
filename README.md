# Albion Market

A terminal tool for browsing Albion Online market prices, trade volume and
city-to-city flips, using data from the
[Albion Online Data Project](https://www.albion-online-data.com) (AODP).

The `albiondata-client/` folder holds a command-line build of the Albion Data
Client, which uploads the market data you see in game to AODP.

```
albion-market/
├── albion_market.py              market tool (Python 3, standard library only)
├── README.md
└── albiondata-client/
    ├── headless/main.go          command-line entry point for the data client
    ├── src/                      official client source (you download it, see below)
    ├── albiondata-client-cli     data client, command-line build (you build it)
    └── albiondata-client.log     client log (created when it runs)
```

Only the files at the top of the tree are in this repository. The client
source, the built program and the logs stay on your machine.

## Getting it

```
git clone https://github.com/mreinrt/albion-market-tool.git ~/albion-market
```

The commands below assume it's in `~/albion-market`.

## Market tool

### Running it

```
python3 ~/albion-market/albion_market.py
```

There's nothing to install. On the first run it downloads the item list
(about 1 MB) and saves it to `~/.cache/albion-market/items.txt`. It downloads
the list again once a week.

To start it by typing `albion`:

```
echo "alias albion='python3 ~/albion-market/albion_market.py'" >> ~/.bashrc
source ~/.bashrc
```

### Screens

1. **Support message**: a short note about SofaLord and a request for
   donations, with a PayPal QR code and link. Press any key to continue.
2. **Server**: press `1` Americas, `2` Asia or `3` Europe.
3. **Menu**: press `s` to search, `t` for top flips, `c` to change
   server or `q` to quit. Once you've opened an item, `i` goes
   back to it. Leaving search with a blank line returns here.
4. **Search**: type part of an item name and press Enter. Upper/lower case
   and apostrophes don't matter, and you can narrow by tier and enchantment:
   `bag`, `t6 broadsword`, `4.1 hide`, `@3 cape`. When the results cover
   several tiers, it asks whether to filter by tier. Type a result's number
   to open it.
5. **Item**: prices in the 8 markets (Bridgewatch, Caerleon, Fort Sterling,
   Lymhurst, Martlock, Thetford, Brecilien, Black Market): lowest sell order,
   highest buy order, how old each price is, and the 24-hour average price and
   items sold. Best prices are green. Prices older than 6 hours have a yellow
   age.
6. **City detail** (`1`–`8` on the item screen): current orders, an hourly
   price chart for the last 24 hours with sales volume, a daily price chart
   for the last 30 days, and the latest hourly trades.
7. **Volume** (`v` on the item screen): items sold in each market over the
   last 7 hours, 24 hours, 7 days and 30 days, with each market's share of
   the week, the 7-day average price and the silver traded over 30 days. Press
   `1`–`8` for a city's volume charts (items sold per day and per hour).
8. **Top flips** (`t`): the 20 most profitable routes for buying in one city
   and selling in another. See below.

### Keys

Keys take a single keypress with no Enter, except on the search, results and
top flips screens, which use typed input.

| Key | Action |
|---|---|
| `1`–`8` | Open a city (city detail, or its volume chart in the volume view) |
| `v` | Volume for the current item |
| `p` | Price details for this city (from a city's volume chart) |
| `t` | Top flips |
| `s` | New search |
| `b` | Back |
| `k` | Cycle item quality (Normal → Masterpiece) |
| `c` | Change server, keeping the current item |
| `r` | Refresh, fetching fresh data |
| `q` | Quit (Ctrl+C also works) |

### Top flips

The scan covers about 8,800 tradable T4–T8 items at Normal quality:

- **Buy** from the cheapest sell order in any city except the Black Market.
- **Sell** whichever way pays more: instantly into the highest buy order, or
  by listing a sell order 1 silver below the cheapest one. Fees come off
  both: the sales tax, plus the listing fee when you list an order.
- **Skip** orders older than 4 hours, and items that haven't sold in the
  sell city in the last 7 hours.
- **Rank** by expected profit: profit per item after fees × the number you
  could realistically sell. That number is 10% of the items sold in the sell
  city in the last 7 hours (at least 1), so a big gap on an item that barely
  sells doesn't outrank a smaller gap you can repeat many times, and busy
  items don't win on volume alone. The list shows it in the Expected column;
  the detail screen also shows how many to sell and how much silver that
  ties up.

Type `f` to show only one category of items:

| # | Category | Includes |
|---|---|---|
| 0 | All items | everything (the default) |
| 1 | Weapons | one-handed and two-handed weapons |
| 2 | Off-hands | shields, torches, books |
| 3 | Armor | head, chest and shoes |
| 4 | Bags & capes | bags, gatherer backpacks, capes and faction capes |
| 5 | Mounts | mounts and mount upgrades |
| 6 | Consumables | potions and food |
| 7 | Resources | raw and refined ore, wood, hide, fiber, stone |
| 8 | Farming & ingredients | seeds, animals, fish, crops, herbs, milk, eggs and other cooking and alchemy ingredients |
| 9 | Artifacts & runes | artifacts, runes, souls, relics, shards, essences |
| 10 | Tools | gathering tools |
| 11 | Other | furniture, labourers, tomes of insight, loot bags and the rest |

The menu shows how many profitable flips each category has in the current
scan. Switching categories doesn't send any requests; the filter stays on
until you change it, including after a rescan. A category can have nothing
when its items have no recent prices or sales, which happens often when few
people are uploading.

Type `z` to switch between three route filters:

- **All routes**: the default.
- **Safe routes**: no Caerleon or Black Market. Every other city can be
  reached without entering a red zone.
- **Black Market routes only**: buy in a city and sell instantly into a
  Black Market buy order. The Black Market buys mostly gear, so combine it
  with `f` (Weapons, Armor). It's often empty, because few uploaders open
  the Black Market, so its buy orders are rarely under 4 hours old.

The two filters work together, and both stay on after a rescan. They only
filter the last scan, so switching costs no requests. On an item's detail
screen, the other routes listed follow the route filter too.

Type a result's number to see where to buy, where to sell, the profit after
fees and other profitable routes. Press `o` to open the item in the market
view. Routes through Caerleon or the Black Market show a warning, since you
travel through red zones to reach them.

The API doesn't say how many items an order is for, so check in game before
buying. A very high margin usually comes from one odd order.

### Settings

These are at the top of `albion_market.py`:

| Setting | Default | Meaning |
|---|---|---|
| `SALES_TAX` | `0.04` | Market tax with premium. Use `0.08` without premium. |
| `SETUP_FEE` | `0.025` | Fee for listing a sell order |
| `VOLUME_WINDOW_HOURS` | `7` | Recent-sales window for flips and the volume table |
| `FLIP_MAX_PRICE_AGE` | `4` | Hours before an order counts as too old for flips |
| `MIN_MARGIN` | `0.05` | Lowest margin a flip needs to be listed |
| `FLIP_SELL_SHARE` | `0.10` | Share of the sell city's 7-hour sales you could realistically sell, for expected profit |
| `FLIP_COUNT` | `20` | Number of flips shown |
| `SCAN_TTL` | `600` | Seconds to keep a flip scan before rescanning |
| `FLIP_CATEGORIES` | see above | Item categories for the `f` filter, matched on item ID |
| `FLIP_PER_CATEGORY` | `80` | Items per category that always get a sales check, so each category has results |
| `RESPONSE_TTL` | `60` | Seconds to reuse an API response |
| `RATE_LIMITS` | 120/min, 240/5 min | The tool's own cap on API requests |

### Load on the AODP servers

AODP allows 180 requests per minute and 300 per 5 minutes. The tool caps
itself at about two-thirds of that, 120 per minute and 240 per 5 minutes.
Every API request waits for a free slot, so the cap holds no matter what you
do:

- **Across runs:** request times are saved in
  `~/.cache/albion-market/requests.json`, so the cap also holds after a
  restart or with two copies of the tool open at once.
- **Pauses:** when a request would go over the cap, the tool pauses and says
  so (`Pausing about Ns to stay under the API rate limit...`) instead of
  sending it.
- **Typical costs:**
  - opening an item: 2 requests, plus 2 for the volume view
  - a top flips scan: about 61 to 70 requests, reused for 10 minutes
  - repeat views within 60 seconds use saved answers and cost nothing
- **Slow-down replies:** if the server answers "too many requests" anyway,
  the scan waits 30 seconds before trying again.
- **Network errors:** if a scan request times out or the connection drops,
  it tries again after a few seconds, up to 3 tries. Retries count toward
  the cap like any other request.
- **Connections are reused:** requests share open connections instead of
  setting up a new encrypted connection each time, which takes over a
  second to the AODP servers from some places.

### Missing or empty data

AODP doesn't read the game's servers. It only knows what players running the
Albion Data Client upload while they browse markets in game. So:

- An item can be full of listings in game and still show no data, because
  nobody running the client has opened that market recently. Uploaded orders
  also expire after a while if nobody sees them again.
- Asia has far fewer uploaders than Americas or Europe, so it has more gaps.
- Volume counts only the trades uploaders saw, so real totals are higher.
- Daily totals sometimes lag days behind. The tool fills the missing days
  from hourly records where it can.

If the API fails, the screen shows a red line such as `Network error`,
`API returned HTTP 403` or `Rate limited by the API`. Wait a moment and
press `r`.

## Albion Data Client (command-line build)

The official client opens a dashboard window built with Wails, which needs
WebKitGTK 6 on Linux. This build runs the same capture and upload code with
no window, tray icon or self-updater, so the only library it needs is
libpcap.

### Building it

You need Go (1.25 or newer), git, and the libpcap development files
(`net-libs/libpcap` on Gentoo, `libpcap-dev` on Debian and Ubuntu,
`libpcap` on Arch). Then download the official client source, add the
command-line entry point and build:

```
cd ~/albion-market/albiondata-client
git clone --depth 1 --branch 0.1.61 https://github.com/ao-data/albiondata-client.git src
cp -r headless src/
cd src && go build -trimpath -ldflags "-s -w -X main.version=0.1.61" -o ../albiondata-client-cli ./headless
```

### First-time setup

Allow it to read network traffic. You only need to do this once, and again
after each rebuild:

```
sudo setcap cap_net_raw,cap_net_admin=eip ~/albion-market/albiondata-client/albiondata-client-cli
```

This lets only this one program read network traffic, so it doesn't need to
run as root.

### Running it

Start it before or while you play, and leave it running:

```
cd ~/albion-market/albiondata-client && ./albiondata-client-cli
```

Press Ctrl+C to stop it. Its log appears in the terminal and is also saved to
`albiondata-client.log` in that folder. Run `./albiondata-client-cli -h` to
list its options.

To upload data, open a market in game and browse an item, then click its
price history. The data shows up in the market tool after about a minute.
Uploads are deliberately slowed to stop spam (the client uses up to 25% of
your CPU), so there's a short delay.

### Encrypted market data

Since January 2024, the game sends encrypted live market data to some
accounts, and the client can't read it. Which accounts are affected depends
on things only SBI (the game's developer) knows: character, account, game
server, playtime and so on. If the log says `Market data is encrypted`, this
applies to you, and nothing on your side can fix it. Item history and gold
prices still upload. See the
[AODP client FAQ](https://www.albion-online-data.com/client-faq#encryption).

### Is it allowed?

The client only reads traffic; it never changes or injects anything.
Albion Online's technical lead stated:

> As long as you just look and analyze we are ok with it. The moment you
> modify or manipulate something or somehow interfere with our services we
> will react.

Source:
[Albion Online forum](https://forum.albiononline.com/index.php/Thread/51604-Is-it-allowed-to-scan-your-internet-trafic-and-pick-up-logs/?postID=512670#post512670),
as quoted in the client's README.

### What it sends

- **Captured:** only the game-world connection on port 5056. Login traffic
  goes over a different connection, so the client never sees your password.
- **Uploaded:**
  - market orders: item, city, quality, price, amount, expiry, with no
    player names
  - market history and gold prices
  - map zone data and event timers
  - a random ID that's new for every upload and isn't linked to you
- **Not uploaded:** your skills and market-sale mails. The client only
  sends these to a private server you set with `-p`, which is empty by
  default.
- **Local only:** the log file records your character name and zone. Don't
  post it if you'd rather keep your character name private.

### Updating (rebuilding)

This build doesn't update itself, because the official self-updater would
replace it with the GUI build. To move to a newer release (`X.Y.Z` is the
version number):

```
cd ~/albion-market/albiondata-client
rm -rf src
git clone --depth 1 --branch X.Y.Z https://github.com/ao-data/albiondata-client.git src
cp -r headless src/
cd src && go build -trimpath -ldflags "-s -w -X main.version=X.Y.Z" -o ../albiondata-client-cli ./headless
sudo setcap cap_net_raw,cap_net_admin=eip ../albiondata-client-cli
```

Releases are listed at
<https://github.com/ao-data/albiondata-client/releases>. If a new release
changes `client.NewClient(...).Run()`, update `headless/main.go` to match.

## Support

SofaLord is an American living in the Philippines with no income. If you
enjoy this tool, please consider a donation through PayPal to help keep the
lights on, the internet connected and the Python code running:
<https://www.paypal.com/ncp/payment/GA5RGE8JVRFH2>

The same link is shown as a QR code when the tool starts. The QR code is
stored ready-made in `albion_market.py` (`DONATE_QR`), since Python has no
built-in QR generator. If the link changes, update `DONATE_URL` and
regenerate `DONATE_QR` as described in the comment above it. The startup
message itself is `DONATE_MESSAGE`, in the same place.

## Credits

- Created by **SofaLord** (Albion Online in-game name, Asia server)
- Market data:
  [Albion Online Data Project](https://www.albion-online-data.com)
- Item names: [ao-data/ao-bin-dumps](https://github.com/ao-data/ao-bin-dumps)
- Data client: [ao-data/albiondata-client](https://github.com/ao-data/albiondata-client)
  (MIT License)

This project isn't affiliated with Sandbox Interactive or Albion Online.
