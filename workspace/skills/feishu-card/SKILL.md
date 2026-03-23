# Feishu Card Skill

Build rich Feishu cards with interactive components, data charts, and structured layouts.

Use this skill when creating dynamic visual content: dashboards, reports, option panels, forms, or data visualizations.

## Limits & Pitfalls

**Size:** Card JSON must stay under **28 KB**. If content is too rich, split into multiple `reply()` calls.

**CardKit incompatibilities** — these fields cause silent failures via CardKit streaming API:
- **`row_height`** on tables — do NOT set it; omit the field entirely
- Keep cards reasonable: ~15 top-level elements max, ≤2 charts, table rows ≤10

## Card Structure (V2 Only)

All cards MUST use V2 format. Pass the JSON as the `text` argument to `reply(request_id, text)`.

```json
{
  "schema": "2.0",
  "config": { "wide_screen_mode": true },
  "header": { "title": { "tag": "plain_text", "content": "Title" }, "template": "blue" },
  "body": { "elements": [ ... ] }
}
```

Header colors: `blue`, `green`, `orange`, `red`, `purple`, `indigo`, `turquoise`, `grey`

## Display Components

### Markdown
```json
{ "tag": "markdown", "content": "**Bold** *italic* [link](url) <font color='red'>colored</font>" }
```

Markdown extensions:
- Color: `<font color='red'>text</font>` (red, green, blue, orange, purple, grey)
- @all: `<at id=all></at>` / @user: `<at id=ou_xxx></at>`
- Images: `![alt](image_url)`

### Image
```json
{ "tag": "img", "img_key": "img_v2_xxx", "alt": { "tag": "plain_text", "content": "description" } }
```

### Multi-Image
```json
{ "tag": "img_combination", "combination_mode": "bisect", "img_list": [
  { "img_key": "img_v2_xxx" }, { "img_key": "img_v2_yyy" }
]}
```

### Person / Person List
```json
{ "tag": "person", "user_id": "ou_xxx", "size": "medium" }
{ "tag": "person_list", "persons": [{ "id": "ou_xxx" }, { "id": "ou_yyy" }], "size": "small" }
```

### Table
```json
{
  "tag": "table",
  "page_size": 5,
  "columns": [
    { "name": "name", "display_name": "Name", "data_type": "text", "width": "auto" },
    { "name": "score", "display_name": "Score", "data_type": "number" }
  ],
  "rows": [
    { "name": "Alice", "score": 95 },
    { "name": "Bob", "score": 87 }
  ]
}
```

### Divider
```json
{ "tag": "hr" }
```

## Interactive Components

**NEVER use `"tag": "action"` wrapper.** V2 does not support it. Place all interactive components directly in `elements`.

**NEVER use `"tag": "note"`.** Removed in V2. Use markdown with grey color instead.

### Button
```json
{ "tag": "button", "text": { "tag": "plain_text", "content": "Click me" }, "type": "primary", "behaviors": [{ "type": "callback", "value": { "action": "do_something" } }] }
```
Types: `primary` (blue), `danger` (red), `default` (grey). Use `behaviors` for callbacks (not `value` at root).

### Input
```json
{ "tag": "input", "name": "user_input", "placeholder": { "tag": "plain_text", "content": "Type here..." }, "input_type": "text" }
```
Types: `text`, `multiline_text`, `password`

### Select (Single)
```json
{
  "tag": "select_static",
  "name": "choice",
  "placeholder": { "tag": "plain_text", "content": "Select..." },
  "options": [
    { "text": { "tag": "plain_text", "content": "Option A" }, "value": "a" },
    { "text": { "tag": "plain_text", "content": "Option B" }, "value": "b" }
  ]
}
```

### Multi-Select
```json
{ "tag": "multi_select_static", "name": "choices", "placeholder": { "tag": "plain_text", "content": "Select..." }, "options": [...] }
```

### Person Picker
```json
{ "tag": "select_person", "name": "assignee", "placeholder": { "tag": "plain_text", "content": "Pick user..." } }
{ "tag": "multi_select_person", "name": "reviewers" }
```

### Date / Time Pickers
```json
{ "tag": "date_picker", "name": "due_date", "placeholder": { "tag": "plain_text", "content": "Pick date..." } }
{ "tag": "picker_time", "name": "start_time" }
{ "tag": "picker_datetime", "name": "deadline" }
```

### Checker (Checkbox)
```json
{ "tag": "checker", "name": "task_done", "checked": false, "text": { "tag": "plain_text", "content": "Mark as complete" } }
```

### Overflow Menu
```json
{
  "tag": "overflow",
  "options": [
    { "text": { "tag": "plain_text", "content": "Edit" }, "value": "edit" },
    { "text": { "tag": "plain_text", "content": "Delete" }, "value": "delete" }
  ]
}
```

## Containers

### Column Layout
```json
{
  "tag": "column_set",
  "columns": [
    { "tag": "column", "width": "weighted", "weight": 1, "elements": [ ... ] },
    { "tag": "column", "width": "weighted", "weight": 1, "elements": [ ... ] }
  ]
}
```

### Form
Batches all inputs into one callback:
```json
{
  "tag": "form",
  "name": "my_form",
  "elements": [
    { "tag": "input", "name": "title", "placeholder": { "tag": "plain_text", "content": "Title" }, "input_type": "text" },
    { "tag": "input", "name": "desc", "placeholder": { "tag": "plain_text", "content": "Description" }, "input_type": "multiline_text" },
    { "tag": "button", "text": { "tag": "plain_text", "content": "Submit" }, "type": "primary", "name": "submit" }
  ]
}
```

### Collapsible Panel
```json
{
  "tag": "collapsible_panel",
  "expanded": false,
  "header": { "title": { "tag": "plain_text", "content": "Details" } },
  "elements": [ { "tag": "markdown", "content": "Hidden content here" } ]
}
```

### Interactive Container
```json
{
  "tag": "interactive_container",
  "width": "fill",
  "background_style": "default",
  "elements": [ ... ]
}
```

## Charts (VChart)

Use the `chart` tag with a VChart spec in `chart_spec`. Supported types: line, bar, pie, area, scatter, radar, funnel, word cloud, progress.

### Line Chart
```json
{
  "tag": "chart",
  "aspect_ratio": "16:9",
  "color_theme": "brand",
  "chart_spec": {
    "type": "line",
    "data": {
      "values": [
        { "date": "Jan", "value": 120 },
        { "date": "Feb", "value": 200 },
        { "date": "Mar", "value": 150 },
        { "date": "Apr", "value": 300 }
      ]
    },
    "xField": "date",
    "yField": "value"
  }
}
```

### Multi-Series Line
```json
{
  "tag": "chart",
  "aspect_ratio": "16:9",
  "chart_spec": {
    "type": "line",
    "data": {
      "values": [
        { "month": "Jan", "value": 120, "type": "Revenue" },
        { "month": "Jan", "value": 80, "type": "Cost" },
        { "month": "Feb", "value": 200, "type": "Revenue" },
        { "month": "Feb", "value": 100, "type": "Cost" }
      ]
    },
    "xField": "month",
    "yField": "value",
    "seriesField": "type"
  }
}
```

### Bar Chart
```json
{
  "tag": "chart",
  "aspect_ratio": "16:9",
  "chart_spec": {
    "type": "bar",
    "data": {
      "values": [
        { "category": "Product A", "sales": 450 },
        { "category": "Product B", "sales": 320 },
        { "category": "Product C", "sales": 580 }
      ]
    },
    "xField": "category",
    "yField": "sales"
  }
}
```

### Pie Chart
```json
{
  "tag": "chart",
  "aspect_ratio": "1:1",
  "chart_spec": {
    "type": "pie",
    "data": {
      "values": [
        { "type": "Direct", "value": 40 },
        { "type": "Search", "value": 30 },
        { "type": "Referral", "value": 20 },
        { "type": "Social", "value": 10 }
      ]
    },
    "valueField": "value",
    "categoryField": "type"
  }
}
```

### Funnel Chart
```json
{
  "tag": "chart",
  "aspect_ratio": "16:9",
  "chart_spec": {
    "type": "funnel",
    "data": {
      "values": [
        { "stage": "Visits", "count": 1000 },
        { "stage": "Signups", "count": 400 },
        { "stage": "Trials", "count": 200 },
        { "stage": "Paid", "count": 80 }
      ]
    },
    "categoryField": "stage",
    "valueField": "count"
  }
}
```

### Radar Chart
```json
{
  "tag": "chart",
  "aspect_ratio": "1:1",
  "chart_spec": {
    "type": "radar",
    "data": {
      "values": [
        { "skill": "Frontend", "score": 90 },
        { "skill": "Backend", "score": 75 },
        { "skill": "Design", "score": 60 },
        { "skill": "DevOps", "score": 70 },
        { "skill": "PM", "score": 85 }
      ]
    },
    "categoryField": "skill",
    "valueField": "score"
  }
}
```

### Chart Options

| Property | Values | Notes |
|----------|--------|-------|
| `aspect_ratio` | `1:1`, `2:1`, `4:3`, `16:9` | Chart dimensions |
| `color_theme` | `brand`, `rainbow`, `complementary`, `converse`, `primary` | Color palette |
| `preview` | `true`/`false` | Allow fullscreen preview |
| `height` | `"auto"` or `"1"` to `"999"` px | Fixed height |

## Callbacks

When users interact, you receive:
- Button: `[User clicked button (name): action_value]`
- Select: `[User selected (name): option]`
- Multi-select: `[User multi-selected (name): values]`
- Input: `[User input (name): text]`
- Date/time: `[User picked date/time (name): value]`
- Checker: `[User multi-selected (name): values]`
- Form: `[User submitted form (name): field=val, ...]`

## Full Example — Dashboard Card

```json
{
  "schema": "2.0",
  "config": { "wide_screen_mode": true },
  "header": { "title": { "tag": "plain_text", "content": "Weekly Report" }, "template": "indigo" },
  "body": { "elements": [
    { "tag": "markdown", "content": "**Revenue this week:** <font color='green'>$12,500</font> (+15%)" },
    { "tag": "chart", "aspect_ratio": "16:9", "color_theme": "brand", "chart_spec": {
      "type": "bar",
      "data": { "values": [
        { "day": "Mon", "revenue": 1500 },
        { "day": "Tue", "revenue": 2200 },
        { "day": "Wed", "revenue": 1800 },
        { "day": "Thu", "revenue": 2500 },
        { "day": "Fri", "revenue": 2000 },
        { "day": "Sat", "revenue": 1200 },
        { "day": "Sun", "revenue": 1300 }
      ]},
      "xField": "day",
      "yField": "revenue"
    }},
    { "tag": "hr" },
    { "tag": "button", "text": { "tag": "plain_text", "content": "View Full Report" }, "type": "primary", "behaviors": [{ "type": "callback", "value": { "action": "full_report" } }] }
  ]}
}
```
