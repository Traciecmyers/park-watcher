# Park Watcher

Checks campground availability every ~5 minutes and sends a **Telegram** message when a site opens up (a cancellation or a newly opened booking window). It never books anything and never logs in to reservation sites.

Built on [camply](https://github.com/juftin/camply). Supported systems: Recreation.gov, ReserveCalifornia, Florida State Parks, Arizona State Parks, and GoingToCamp (Washington, Wisconsin, Michigan, Maryland, BC Parks, Parks Canada).

## Setup

Add three repository secrets (Settings → Secrets and variables → Actions):

- `TELEGRAM_BOT_TOKEN` — from @BotFather
- `TELEGRAM_CHAT_ID` — your Telegram user ID
- `WATCHLIST_YAML` — the watch list (kept private; never commit it)

Then run **Actions → Park Watcher → Run workflow** with **test** ticked to confirm Telegram works.

## Watch list format

```yaml
watch:
  - id: example-park          # unique, no spaces
    name: Example Campground
    provider: recgov          # recgov | california | florida | arizona | goingtocamp
    campground_ids: [123456]  # recgov IDs, or use search: + state:
    rv_length: 25             # recgov only
    start: 2027-04-25         # check-in
    end: 2027-05-02           # check-out
    min_nights: 2
    book_url: https://www.recreation.gov
```

Set `paused: true` on an entry to stop watching it. Logs only show counts, never park names or dates.
