# XHS Browsing Guide

Timer-triggered guide for browsing xiaohongshu. Read this file each time the timer fires.

## Setup

```bash
agent-browser --cdp 9222 open "https://www.xiaohongshu.com/explore"
```

## Browsing Loop

### 1. Get Feed

```js
// Always wrap in IIFE
(() => {
  const cards = document.querySelectorAll('section.note-item');
  const results = [];
  cards.forEach((card, i) => {
    if (i >= 20) return;
    const titleEl = card.querySelector('.title span, .note-content .title');
    const coverLink = card.querySelector('a.cover');
    const likeEl = card.querySelector('.like-wrapper span.count');
    results.push({ i, title: titleEl?.textContent.trim() || '(no title)', href: coverLink?.href || '', likes: likeEl?.textContent.trim() || '' });
  });
  return JSON.stringify(results, null, 2);
})();
```

### 2. Click Into Posts

```js
(() => {
  const cards = document.querySelectorAll('section.note-item a.cover');
  if (cards[INDEX]) { cards[INDEX].click(); return 'clicked'; }
})();
```

### 3. Read Post Content + Comments

```js
(() => {
  const desc = document.querySelector('.note-detail-mask .desc, .note-scroller .desc');
  const comments = document.querySelectorAll('.note-detail-mask .comment-item, .comment-item');
  const commentTexts = [];
  comments.forEach((c, i) => {
    if (i >= 15) return;
    const author = c.querySelector('.name');
    const content = c.querySelector('.content');
    const likeCount = c.querySelector('.like-count, .like span');
    commentTexts.push({ author: author?.textContent.trim(), content: content?.textContent.trim(), likes: likeCount?.textContent.trim() });
  });
  return JSON.stringify({ desc: desc?.textContent.trim().substring(0, 500), comments: commentTexts }, null, 2);
})();
```

### 4. View Full Content

**Multi-image posts:** Click right arrow or bottom indicators
```js
document.querySelector('.note-detail-mask [class*="right"]')?.click();
```

**Videos:** Seek through keyframes and screenshot each
```js
const video = document.querySelector('.note-detail-mask video');
video.currentTime = N; // seek to N seconds
```

**Always screenshot** to see visual content: `agent-browser --cdp 9222 screenshot /tmp/xhs_NAME.png`

### 5. Interact

**Like post** (use engage-bar, NOT comments section):
```js
document.querySelector('.note-detail-mask .engage-bar .like-wrapper')?.click();
```

**Like a comment:**
```js
// comments have their own .like-wrapper inside .comment-item
document.querySelectorAll('.comment-item .like-wrapper')[INDEX]?.click();
```

**Comment:**
```js
// 1. Focus input
document.querySelector('.content-input')?.click();
document.querySelector('.content-input')?.focus();
```
Then: `agent-browser --cdp 9222 keyboard inserttext "评论内容"`
Then:
```js
document.querySelector('.btn.submit')?.click();
```

**Reply to a specific comment (sub-comment):**
```js
// Find the comment you want to reply to and click its reply button
const comments = document.querySelectorAll('.note-detail-mask .comment-item');
for (const c of comments) {
  const name = c.querySelector('.name');
  if (name?.textContent.includes('USERNAME')) {
    c.querySelector('.reply')?.click();
    break;
  }
}
```
Then type and submit as usual.

**Expand sub-comment threads:**
```js
document.querySelectorAll('.note-detail-mask .show-more')[INDEX]?.click();
```

**Scroll comment section:**
```js
document.querySelector('.note-detail-mask .note-scroller').scrollTop += 500;
```

**Close post:** `document.querySelector('.close-circle')?.click();`

### 6. Search

```js
const searchBox = document.querySelector('input[placeholder*="搜索"]');
searchBox.click(); searchBox.focus(); searchBox.value = '';
searchBox.dispatchEvent(new Event('input', {bubbles: true}));
```
Then: `agent-browser --cdp 9222 keyboard inserttext "搜索词"` + `press Enter`

## What To Do Each Session

1. **Browse 2-3 posts** — mix of feed + search for topics you're curious about
2. **Screenshot everything** — images AND video frames (multiple keyframes)
3. **Study comments** — note what gets high likes, real vs AI patterns
4. **Like freely** — if it's good content, hit like
5. **Comment occasionally** — match the post energy, keep it short and natural (see HUMAN_BEHAVIOR.md for patterns)
6. **Search varied topics** — cats, food, local life, tech humor, daily life, travel. Don't repeat the same searches
7. **Share sparingly** — only truly interesting stuff to group chat `oc_528c720d12dc73a64eab6790e2157838`, and only if boss or mama would genuinely care
8. **Update HUMAN_BEHAVIOR.md** — when you notice new comment patterns or social behaviors worth recording
9. **Use websearch** — if you encounter slang/memes/cultural references you don't understand

## Image Comments

Comments can contain images (memes, selfies, screenshots). To identify:
```js
// Images in comments have class "inner" (not "avatar-item" which is the user avatar)
const commentImgs = c.querySelectorAll('img.inner');
```

To view image comments, screenshot the comment section after scrolling.

## Comment Style (Quick Reference)

- Short > long (under 30 chars ideal)
- Specific details > generic praise
- Build on existing funny comments, don't start new threads
- Creative comparisons and unexpected angles
- Match post energy: funny→funny, serious→genuine
- Never: "确实", paragraphs, emoji stacking, unsolicited advice
- Natural vocab: 绝了、笑死、我去、离谱、救命

## Timer Setup

After each session, set the next one:
```
CronCreate(cron="M H D Mo *", recurring=false, prompt="Read XHS.md then follow the browsing guide. Browse xiaohongshu for 5-10 minutes.")
```
Use 5-10 minute intervals to keep the loop going consistently.
