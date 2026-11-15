# trajport merged trajectory

## Sources

```json
[
  {
    "agent": {
      "name": "codex",
      "version": "1"
    },
    "input": 1,
    "session_id": "A&lt;session&gt;"
  },
  {
    "agent": {
      "name": "claude",
      "version": "1"
    },
    "input": 2,
    "session_id": "B`雪"
  }
]
```

## View

```json
{
  "view": "tools"
}
```

Keeps steps with tool calls or nonempty observations from any role; hides messages, reasoning and step metadata.

View and source/time selections are combined; all events are validated.

All declared sources are selected.

No timestamp bounds are active.

## Assistant · 1

### Source / session

```json
{
  "input": 2,
  "line": 2,
  "session_id": "B`雪",
  "step_id": 1
}
```

```
2026-01-01T11:00:00+01:00
```

### Tool call

```
shell [shared-call]
```

```json
{
  "cmd": "printf \"雪\"",
  "html": "&lt;b&gt;"
}
```

### Tool result

```
shared-call
```

`````
雪
````
&lt;done&gt; &amp;
`````

## System · 2

### Source / session

```json
{
  "input": 1,
  "line": 3,
  "session_id": "A&lt;session&gt;",
  "step_id": 2
}
```

```
2026-01-01T10:01:00Z
```

### Tool result (unmatched)

```
missing
```

```
unmatched result
```

## Render diagnostics

```json
{
  "excluded_events": 1,
  "rendered_events": 2,
  "sources": 2,
  "view": "tools"
}
```
