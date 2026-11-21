# Newz 213 autopost

Posts one carousel to @newz.213 at a time, without anyone in the loop:

1. **News card**: the article's photo full bleed, the full logos (wordmarks) of the papers that reported it, in white tags (up to 3, then +N), the headline as white highlighted blocks, one detail, the section and date, the app icon top corner. French or Arabic depending on the papers. The photo comes from the feed, or from the article page's og:image when the feed has none or a small one. With no usable photo (at least 600×400) the story is skipped: no text-only cards (user, 21 Nov 2026).
2. **App promo slide**: `public/promo-fr.jpg` or `public/promo-ar.jpg`, the green slide picked on 20 Nov 2026.

A GitHub Actions job runs `autopost.py` every 30 minutes. GitHub's own schedule proved unreliable on this repo (most slots were skipped), so the real trigger is a **Hostinger cron job** (account u369108729, every 5 minutes) that calls GitHub's workflow_dispatch API every 5 minutes. It reads its key from `/home/u369108729/newz213-github.txt` (one line, `Authorization: Bearer <fine-grained token>`, token `newz213-hostinger-cron`: only this repo, Actions read/write, no expiry). GitHub's schedule stays on as a backup. Most runs post nothing. A run posts only when all of these are true:

| Rule | Default | Setting |
|---|---|---|
| Fewer than 95 posts today (Algiers time) | 95 | `DAILY_CAP` |
| Meta's own rolling 24 h quota (100) still has more than 3 posts left, read live from `content_publishing_limit` | 3 | `QUOTA_MARGIN` |
| Last post more than 9 minutes ago | 9 | `MIN_GAP_MIN` |
| Between 07:00 and 01:00 Algiers time | 7 to 1 | `ACTIVE_FROM`, `ACTIVE_TO` |
| A new story from at least 1 paper in the last 24 hours (stories covered by more papers go first) | 1, 24 | `MIN_OUTLETS`, `MAX_AGE_H` |
| The story passes the keyword blocklist and the model's own check | | `BLOCK_PATTERNS` in `autopost.py` |

**21 Nov 2026: do not chase Meta's 100/day limit.** At about 6 posts an hour (13 in two hours) Meta blocked publishing (`action is blocked`, error 2207051) and restricted the business portfolio for "automation". The 100 is a technical ceiling, not a safe pace for a new account. Default now: 5 posts a day at fixed peak slots, stories from 2+ papers. For fixed peak-time slots instead, set `POST_SLOTS` (e.g. `"12:30,13:15,18:30,20:00"`) and `DAILY_CAP: "10"`: one post per slot.

The settings are in `.github/workflows/autopost.yml`. On quiet news days it posts fewer than 10: it never posts a single-paper story unless you set `MIN_OUTLETS: "1"`.

## How a run works

`fetch 12 RSS feeds → group same-story articles (port of the app's StoryClusterer) → keep new stories with 2+ papers → blocklist → Claude Haiku 5.5 writes headline, detail, caption and can refuse → render the card (headless Chrome) → commit the JPEG to public/ → Instagram API publishes the carousel → record it in state.json`

- **AI use**: one short call to Claude Haiku 5.5 per story it considers (at most 3 per run, usually 1). At ~100 posts a day that's still about $0.03 to $0.05 a day. Without `ANTHROPIC_API_KEY` it falls back to a template that quotes the first paper's headline. The template works but reads like a robot and has no topic check beyond the keyword list, so keep the key.
- **Cost**: Haiku 5.5 is $0.10 per million input tokens and $0.50 per million output tokens. A call is about 1,500 tokens in and 300 out, so roughly $0.0003 per post, well under a cent a day. GitHub Actions is free for a public repository.
- **Never posted**: politics, elections, the president, the army, crimes and arrests, court cases, named private people, deaths, accidents, disasters with victims, tensions with other countries (see `BLOCK_PATTERNS` and the `SYSTEM` prompt in `autopost.py`).
- **Images**: the card reuses the biggest photo among the papers' articles, credited bottom-right ("Photo : TSA"). Press photos belong to the papers, so a paper can ask for a post to be removed; delete it from the app if that happens. Paper logos come from the app (`templates/assets/logos/`). Images older than 7 days are deleted from `public/` (Instagram keeps its own copy).

## One-time setup

### 1. Instagram API access (done 20 Nov 2026)

New Meta apps can't use "Instagram login" any more. What works is a **system-user token** (never expires):

- Business portfolio **Newz 213 autopost** (id 4161219127518329) owns the Meta app "Newz 213 autopost" (id 1143224305082523) and the Instagram account **@newz.213** (id 17841416781787675).
- System user **autopost** (Employee) has the app ("Develop app") and @newz.213 ("Content").
- Token: System users → autopost → Generate token → app "Newz 213 autopost", expiry **Never**, permissions `instagram_basic` + `instagram_content_publish`. The token starts with `EAA`, so the bot calls graph.facebook.com.
- Business Suite must be opened while signed in with the **Facebook** account. Signed in through Instagram (@ggymapp) it only shows the GGym portfolio.
- If the token ever leaks: same page → Revoke tokens, generate a new one, replace the `IG_TOKEN` secret.

### 2. Anthropic API key

console.anthropic.com → **API keys → Create key**. Add a few dollars of credit under Billing; at this volume that lasts months.

### 3. GitHub repository and secrets

The repository must be **public** so Instagram can fetch the images from `raw.githubusercontent.com` (everything in it ends up on Instagram anyway; the keys stay in encrypted secrets, never in the code).

In the repository: **Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Value |
|---|---|
| `ANTHROPIC_API_KEY` | the key from step 2 |
| `IG_USER_ID` | the Instagram user ID from step 1 |
| `IG_TOKEN` | the token from step 1 |

### 4. First run

Enable **Actions → autopost** (leave **refresh-instagram-token** disabled: the system-user token never expires). Then **Actions → autopost → Run workflow**, tick **force**, run. Read the log: it says which story it picked or why it posted nothing. After that the schedule takes over.

## Day to day

- **Pause**: Actions → autopost → **Disable workflow**. Enable it again to resume.
- **Post right now**: Run workflow with **force** (the daily cap still applies).
- **What was posted**: `state.json` lists every post with its headline, sources and Instagram media ID.
- **Test locally without posting**: `python autopost.py --dry-run` writes cards and captions to `out/`.
- **Change the look**: `templates/card.html`. The promo slides come from `../brand/promo-slide/`.

## Limits to know

- **Repo size**: about 100 card images a day are committed. They are deleted from `public/` after 7 days but stay in git history, roughly 7 to 9 GB a year. Runs stay fast (shallow checkout), but before the repo nears GitHub's ~5 GB guidance, move image hosting to Hostinger or squash history.
- Meta caps API publishing at 100 posts per rolling 24 hours (a carousel counts as one). Each run that posts prints the account's real quota from `content_publishing_limit` ("Meta quota: x used of y").
- The slots are a first guess at when Algerians are on Instagram (lunch, then evening). Once the page has followers, check Insights → Most active times and change `POST_SLOTS`.
- GitHub's own schedule is unreliable (on 20–21 Nov 2026 it skipped almost every slot). The real trigger is a **Hostinger cron job** (account u369108729, job uid `QfjNmAEbZq`, at :03 and :33) that calls GitHub's `workflow_dispatch` API with a fine-grained token (`newz213-hostinger-cron`, Actions read/write on this repo only, no expiry). The token sits in `/home/u369108729/newz213-github.txt` as one line `Authorization: Bearer …`, outside any website folder. The GitHub schedule stays on as a backup; the gap rule prevents double posts. If the token is revoked, generate a new one and replace that line.
- Alt text is not set through the API; Instagram generates its own.
- The model can still get a fact wrong. Every caption names its sources; check the page from time to time and delete anything wrong from the app.
