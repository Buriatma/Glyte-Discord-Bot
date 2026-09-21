# 🤖 Glyte Discord Bot — Commands Manual & Reference Guide

Welcome to the comprehensive command manual for **Glyte Discord Bot**! This guide details every slash command, required & optional parameters, permissions, and usage examples.

---

## 📑 Table of Contents
- [🗓️ Attendance & Leaves](#️-attendance--leaves)
- [🎮 Gamification, XP & Levels](#-gamification-xp--levels)
- [🏪 Economy & Shop](#-economy--shop)
- [🤝 Team Rituals & Collaboration](#-team-rituals--collaboration)
- [🛡️ Moderation & Administration](#️-moderation--administration)
- [🎂 Community, Reminders & Utilities](#-community-reminders--utilities)
- [📊 Web Dashboard](#-web-dashboard)
- [💡 Troubleshooting "Command is Outdated"](#-troubleshooting-command-is-outdated)

---

## 🗓️ Attendance & Leaves

Keep track of your daily attendance, build streaks, and manage leave requests seamlessly.

| Command | Arguments | Permission | Description |
| :--- | :--- | :--- | :--- |
| `/checkin` | *None* | Everyone | Mark today's attendance. Awards **+25 XP**, **+10 Coins**, and extends your streak 🔥. Streak milestones grant massive bonus coin drops! |
| `/daily` | *None* | Everyone | Claim daily coins based on your active streak multiplier (available once every calendar day). |
| `/mystats` | `[member]` (Optional) | Everyone | Displays a gamified card with XP level progress bar, total coins, streak, messages, and voice time. |
| `/attendance` | `[days]` (Default: 7, Max: 30) | Everyone | Visualizes your past N days of attendance with status marks (✅ Checked in, 🎙️ Voice active, 🌴 Leave, ⬜ Absent). |
| `/leave_apply` | `[days]`, `[start_date]`, `[reason]` | Everyone | Apply for planned time off. If arguments are omitted, opens a dynamic interactive modal form! |
| `/my_leaves` | *None* | Everyone | Lists your recent leave requests and their current approval status (`pending`, `approved`, `rejected`). |
| `/leave_list` | `[status]` (Default: pending) | Manage Server | Displays leave requests with interactive **[Approve]** and **[Reject]** buttons. |
| `/leave_decide` | `[leave_id]`, `<approve>` | Manage Server | Approve or reject a leave request by its unique ID. |

---

## 🎮 Gamification, XP & Levels

Earn XP through chat activity, voice channels, Pomodoro focus sessions, and daily quests.

| Command | Arguments | Permission | Description |
| :--- | :--- | :--- | :--- |
| `/quests` | *None* | Everyone | View today's adaptive quests (e.g. Daily Check-in, Active Chatter, Voice Collaborator, Reactor) and completion status. |
| `/quest_claim` | `<quest_id>` | Everyone | Claim completed quest rewards for extra coins & XP boosts. |
| `/leaderboard` | `[category]` (XP, Coins, Messages, Voice, Check-ins) | Everyone | View the server top 10 members in any activity metric. |
| `/season` | `[month]` (Default: current) | Everyone | View the monthly competitive season standings. Season champions receive **+1,000 Coins** and exclusive champion badges! |
| `/badges` | *None* | Everyone | View all earned milestone badges (First Check-in, 7-Day Streak, Night Owl, Lucky Spinner, Jackpot, etc.). |
| `/spin` | *None* | Everyone | Spin the Daily Lucky Wheel 🎡 for coins, XP, and mystery jackpot badges! First spin every day is 100% free; additional spins cost 25 coins. |
| `/focus` | `<minutes>` (5–120), `[task]` | Everyone | Start a Pomodoro deep work timer 🎯 with live countdown, pause/stop buttons, and bonus completion XP! |
| `/focus_rooms` | *None* | Everyone | Lists designated study and voice lounges that automatically yield **1.5x bonus voice XP** per minute. |

---

## 🏪 Economy & Shop

Spend your hard-earned coins on perks, boosters, custom roles, and fun team rewards.

| Command | Arguments | Permission | Description |
| :--- | :--- | :--- | :--- |
| `/shop` | *None* | Everyone | Interactive shop catalog with a dropdown menu displaying items, descriptions, costs, and instant purchase buttons. |
| `/buy` | `<item_id>` | Everyone | Directly purchase an item from the shop using its ID. |
| `/use` | `<item_id>` | Everyone | Consume or activate an owned item from your inventory (e.g. activate `xp2x` for 2 hours of double XP). |
| `/inventory` | *None* | Everyone | View your currently owned items, power-ups, and tickets. |
| `/shop_add` | `<id>`, `<name>`, `<cost>`, `<description>` | Manage Server | Add or update an item in the server shop. |
| `/shop_remove` | `<id>` | Manage Server | Remove an existing item from the server shop. |

---

## 🤝 Team Rituals & Collaboration

Foster engagement with daily asynchronous standups, peer shoutouts, and coin-backed bounties.

| Command | Arguments | Permission | Description |
| :--- | :--- | :--- | :--- |
| `/standup` | `<yesterday>`, `<today>`, `[blockers]` | Everyone | Submit your daily standup update. Automatically logs to the database and awards **+5 XP**. |
| `/standup_list` | `[date]` (YYYY-MM-DD) | Manage Server | View all submitted standup updates compiled for a given date. |
| `/kudos` | `<member>`, `<reason>` | Everyone | Give a public shout-out to a teammate (+10 coins from server, maximum 3 per day). |
| `/bounty_post` | `<title>`, `<coins>`, `[description]` | Manage Server | Post an open task with coins placed in escrow. |
| `/bounty_list` | *None* | Everyone | View all available open bounties waiting to be claimed. |
| `/bounty_claim` | `<bounty_id>` | Everyone | Claim an open bounty task to work on. |
| `/bounty_approve` | `<bounty_id>` | Manage Server | Approve completed work and release escrowed coins + 25 XP to the claimer! |

---

## 🛡️ Moderation & Administration

Maintain server hygiene, synchronize slash commands, manage roles, and export data.

| Command | Arguments | Permission | Description |
| :--- | :--- | :--- | :--- |
| `/sync` | *None* | Manage Server | **Instant command refresh!** Pushes all guild and global slash commands to Discord immediately, resolving "This command is outdated" errors. |
| `/strike add` | `<member>`, `<reason>` | Manage Messages | Issue a formal warning/strike. **3 strikes automatically apply a 1-hour server timeout ⏳.** |
| `/strike list` | `<member>` | Manage Messages | View the strike history and reasons for a member. |
| `/strike clear` | `<member>` | Manage Messages | Clear all strikes on a member's record. |
| `/slowmode` | `<seconds>`, `[channel]` | Manage Channels | Set text channel message cooldown (0 to disable, up to 21600s). |
| `/give_coins` | `<member>`, `<amount>` | Manage Server | Grant or deduct coins from a member's balance. |
| `/reward_add` | `<level>`, `<role>` | Manage Server | Map an XP level to an automatic role reward. |
| `/reward_list` | *None* | Everyone | View configured level-to-role progression tiers. |
| `/export` | *None* | Manage Server | Export the entire member database as an indented JSON file. |
| `/report` | `[days]` (1–31) | Manage Server | Generates an activity summary embed and downloads a full CSV report. |
| `/eod` | *None* | Manage Server | Posts the End-Of-Day attendance breakdown immediately. |
| `/insights` | *None* | Manage Server | View team activity metrics, quest completion rates, feedback inbox, and bot self-tuning logs. |

---

## 🎂 Community, Reminders & Utilities

Convenience utilities and server celebrations.

| Command | Arguments | Permission | Description |
| :--- | :--- | :--- | :--- |
| `/birthday set` | `<month>`, `<day>` | Everyone | Register your birthday. The bot automatically celebrates you on your day with **+150 Coins** & **+100 XP**! |
| `/birthday list` | *None* | Everyone | Shows upcoming server birthdays ordered chronologically. |
| `/remindme` | `<minutes>`, `<message>` | Everyone | Set a timer (up to 7 days). Delivers a reminder ping in the channel or via DM. |
| `/help` | `[category]` | Everyone | Interactive guide with dropdown category browser and full manual links. |
| `/feedback` | `<text>` | Everyone | Send suggestions or feedback directly to the team and bot learning inbox. |
| `/about` | *None* | Everyone | Information about Glyte bot architecture and GlyteTech. |
| `/ping` | *None* | Everyone | Health check returning WebSocket latency and MongoDB Atlas connection status. |

---

## 📊 Web Dashboard

Access your live web hub without needing to remember any passwords:

1. Run `/dashboard` in any channel where Glyte Bot is present.
2. The bot replies with an ephemeral **Magic Link** valid for 24 hours.
3. Open the link in any browser (mobile, tablet, or desktop) to view:
   - **Personal Hub:** Live XP progression, attendance history, daily quests, inventory, and leaves.
   - **Admin Room (Managers):** Activity charts, live team attendance grid with instant search filter, leave approval workflows, bounty management, coin economy, and real-time bot execution logs.

---

## 💡 Troubleshooting "Command is Outdated"

If Discord displays **"This command is outdated"** when running a slash command:
1. Have an admin run `/sync`.
2. Press **`Ctrl + R`** (Windows/Linux) or **`Cmd + R`** (Mac) in your Discord desktop client to reload the cached command signatures. On iOS / Android, fully close and restart the Discord app.
