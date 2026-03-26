# Video Viewer Skill

Watch videos by extracting keyframes with ffmpeg, then viewing them as images.

Use this skill when a user sends a video file (`.mp4`, `.mov`, `.avi`, `.webm`, `.mkv`, etc.) and you need to understand its content.

## Steps

### 1. Get video info

```bash
ffprobe -v quiet -show_entries format=duration,size -show_entries stream=width,height,r_frame_rate,codec_name -of json <video_path>
```

### 2. Extract frames

Choose strategy based on duration:

| Duration | Strategy | Command |
|----------|----------|---------|
| < 5s | 1 frame per second | `fps=1` |
| 5–30s | 1 frame every 3 seconds | `fps=1/3` |
| 30s–2min | 1 frame every 5 seconds, max 12 | `fps=1/5` |
| > 2min | 1 frame every 10 seconds, max 15 | `fps=1/10` |

```bash
ffmpeg -i <video_path> -vf "fps=<rate>,scale=800:-1" -frames:v <max_frames> -q:v 2 /tmp/feishu-channel/vframe_%03d.jpg 2>&1 | tail -3
```

- Always scale to 800px wide (`scale=800:-1`) to keep file sizes manageable.
- Use `-q:v 2` for good quality JPEG output.
- Output to `/tmp/feishu-channel/vframe_NNN.jpg`.

### 3. View frames

Read each extracted frame with the `Read` tool. Describe what you see across the sequence — identify actions, scenes, people, text, changes between frames.

### 4. Clean up

```bash
rm /tmp/feishu-channel/vframe_*.jpg
```

## Notes

- If ffmpeg/ffprobe is not available, tell the user.
- For audio-only content, extract audio info but note you can't listen to it.
- If the video has subtitles, you may see them burned into frames — read them.
- Combine observations across frames to describe the narrative/action, not just individual frames.
