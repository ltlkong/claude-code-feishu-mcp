---
name: random-timer
description: Schedule a one-shot smart reminder at a random time within a specified window. Use when you need unpredictable timing — e.g., posting to social media at natural-looking times, sending heartbeat-style messages that don't look mechanical, or running a task "sometime in the next 2 hours." Uses create_reminder with smart=true and max_runs=1.
---

# Random Timer Skill

Create a one-shot smart reminder at a random time within a specified window. Useful for scheduling tasks at unpredictable intervals — e.g., posting to social media at natural-looking times.

## How It Works

Uses `create_reminder` with `smart=true` and `max_runs=1` to schedule a one-shot task. The cron time is randomly picked within the given window.

## Steps

### 1. Parse the request

Extract from the user's message:
- **task**: what to do when the timer fires (e.g., "post a cat video to Douyin")
- **window_minutes**: max delay in minutes (default: 30)
- **chat_id**: which chat to send the result to (use the current chat_id)

### 2. Calculate random time

Pick a random delay between 1 and `window_minutes` minutes from now.

```python
import random
from datetime import datetime, timedelta, timezone

delay = random.randint(1, window_minutes)
fire_time = datetime.now(timezone.utc) + timedelta(minutes=delay)
cron = f"{fire_time.minute} {fire_time.hour} {fire_time.day} {fire_time.month} *"
```

Note: cron expression must be in **UTC** — the system auto-converts to local time display.

### 3. Create the reminder

```
create_reminder(
  reminder_id="random_timer_{timestamp}",
  cron_expression=cron,        # UTC time
  chat_id=chat_id,
  message=task,                # the prompt for Claude to execute
  smart=true,                  # Claude thinks and acts on the task
  max_runs=1                   # one-shot, auto-deletes after firing
)
```

### 4. Confirm to user

Tell the user when the task will fire (in their local timezone) and what it will do. Don't reveal the exact minute — just say something like "10-30 minutes later" to keep it random-feeling.

## Chaining

To create a recurring random loop:
1. Include in the task prompt: "After completing this task, set another random timer for [X] minutes to do [next task]"
2. Each smart reminder fires → does the task → sets the next random timer
3. This creates an infinite chain of randomly-spaced tasks

## Example

User: "每隔30分钟随机发一条抖音"

→ Set a smart reminder at random time within 30min:
  - prompt: "从 /tmp/ 找一个待发的视频，用抖音策略师写文案，用 sau douyin upload-video 发布。发完后再用 random-timer skill 设一个30分钟内的随机定时器继续发下一条。"
  - max_runs: 1

→ When it fires, Claude posts the video and sets the next timer → loop continues.
