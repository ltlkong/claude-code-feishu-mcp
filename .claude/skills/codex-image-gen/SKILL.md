---
name: codex-image-gen
description: Generate or edit images via Codex CLI's built-in image generator (image2). Use when the user asks to 生成图片/做张图/画一个/海报/promo poster/头像/P图/换背景/改风格，or when a request naturally produces an image (e.g. "把老婆P到LV店里"). Supports reference images for face/style continuity and iterative refinement via codex resume.
---

# Codex Image Generation

Generate photorealistic or styled images through Codex CLI's built-in image tool (`image2`). Supports reference images (for face/style continuity), multi-turn refinement, and posters with Chinese text.

## When to Use

- 生成/P/做 a single image: portraits, product shots, posters, memes, scene edits
- User supplies a reference face/product and wants it placed in a new scene
- User asks for a 海报/宣传图/封面 with Chinese typography
- User dislikes first result and wants a refined version (use resume, not fresh call)

**Do NOT use for:** batch OCR (read images directly or use a local OCR like `tesseract`), video generation, pure photo retouching where ImageMagick suffices (see `image-edit` skill).

## Workflow

### First generation

Use `codex exec` with **gpt-5.4**, medium reasoning, full sandbox + network:

```bash
codex exec --skip-git-repo-check \
  --sandbox danger-full-access --full-auto \
  -m gpt-5.4 --config model_reasoning_effort="medium" \
  "<PROMPT>" 2>/dev/null
```

**Prompt template** (tweak for the request):

```
Generate an image: <vivid scene description>.
<style notes: photorealistic / cinematic / poster / illustrated>
<composition: portrait 1024x1536 | square 1024x1024 | landscape 1536x1024>
Reference the face/style from <absolute path>.  ← only if user supplied a reference
Save the generated image to <absolute output path>.
Use whatever image-generation tool you have access to.
Report the final file path.
```

Codex returns text like `Saved to '/tmp/xxx.png'` — trust it, but still `Read` the file to sanity-check.

### Refinement (iterate, don't restart)

When user wants changes ("更真实"/"加个logo"/"换蓝色"), **resume** the same session so face/style continuity holds:

```bash
echo "Regenerate but <specific changes>. Save to <new path>. Report final path." \
  | codex exec --skip-git-repo-check resume --last 2>/dev/null
```

**No flags between `resume` and `--last`** — session inherits model/effort/sandbox.

### Tips for realism

If the first result looks too polished ("AI味"/"有点不真实"), common refinement prompts:
- "Make it look like a candid iPhone snapshot, not a studio portrait"
- "Visible skin texture, minor blemishes, no beauty filter"
- "Asymmetric expression, natural posture, off-center framing"
- "Mixed color temperature store lighting with slight shadows on face"
- "Mild JPEG compression, iPhone color profile"

### Tips for Chinese posters

- Spell out exact Chinese characters to appear, in quotes, inside the prompt
- Specify font feel (宋体/黑体/书法) if it matters
- List elements top-to-bottom: 标题 → 副标题 → 主视觉 → 底部tag
- Give a palette ("gold + deep blue + beige, warm+elegant")
- Codex's image model handles Chinese text fairly well but check output — re-roll via resume if a character is garbled

## Output Conventions

- Save to `/tmp/<descriptive_name>.png` so `reply_image` can send it immediately
- Use v2/v3/v4 suffixes on iterations: `/tmp/wife_lv_bag_v2.png` — keeps old versions recoverable if user wants to revert
- After `Read`-ing to verify, deliver via `reply_image`, then a one-line caption via `reply`

## Example — one-shot portrait composite

User: "把她P到LV店买包"
→ Download user's reference to `/tmp/feishu-channel/...jpg` (already there if from Feishu)
→ Call codex:

```bash
codex exec --skip-git-repo-check --sandbox danger-full-access --full-auto \
  -m gpt-5.4 --config model_reasoning_effort="medium" \
  "Generate a photorealistic portrait: an elegant Asian woman shopping at a Louis Vuitton boutique, holding an LV monogram Alma bag, warm store lighting, LV logo on wall behind. Reference face/style from /tmp/feishu-channel/feishu-img-xxx.jpg. Portrait 1024x1536. Save to /tmp/wife_lv_bag.png. Report final path." 2>/dev/null
```

→ `Read /tmp/wife_lv_bag.png` to verify → `reply_image(chat_id, "/tmp/wife_lv_bag.png")`.

## Example — iterative realism fix

User reacts: "有点不真实"
→ **Resume**, do not start fresh:

```bash
echo "Regenerate as a candid iPhone snapshot — natural skin texture, off-center framing, woman mid-browsing looking down at the bag (not posing). Save to /tmp/wife_lv_bag_v2.png. Report final path." \
  | codex exec --skip-git-repo-check resume --last 2>/dev/null
```

## Example — hotel promo poster

User: "做一张酒店宣传海报"
→ Call codex with poster-specific prompt:

```
Generate a vertical Chinese hotel promotional poster (1024x1536).
Reference photo at <path> shows the actual hotel facade — use it for accurate building/signage.
Top banner: "<酒店名>" in gold/warm color.
Subtitle: "<tagline>".
Hero: building at golden hour.
Bottom: <scenic element silhouettes> + small "<集团名>荣誉出品" tag.
Palette: gold + deep blue + beige.
Save to /tmp/hotel_poster.png. Report final path.
```

## Error Handling

- Codex exits non-zero → re-read its last stderr line; usually sandbox or auth. Retry once with `2>&1` to see errors.
- Output file missing despite "Saved to …" → session lost network; start fresh (no `resume`).
- Resume target not found → the last session expired; start fresh, losing continuity.
- Chinese characters garbled in poster → resume once with "Render the exact Chinese characters: '<string>'. Do not transliterate."

## What NOT to do

- Do not read reference images with Claude just to describe them before calling codex — codex ingests the file path directly, saves tokens.
- Do not expose the codex command or internal tooling in chat (see CLAUDE.md: no internal process leaks). Just deliver the image.
