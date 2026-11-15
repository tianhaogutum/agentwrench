# agentwrench export merged trajectory

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

## Timestamp selection

```json
{
  "since": "2026-01-01T11:00:00+01:00",
  "until": "2026-01-01T10:00:00.000001Z"
}
```

Since is inclusive; until is exclusive. A null endpoint is unbounded. Time and source selections are combined; all events are validated.

All declared sources are selected.

## User · 1

### Source / session

```json
{
  "input": 1,
  "line": 2,
  "session_id": "A&lt;session&gt;",
  "step_id": 1
}
```

```
2026-01-01T10:00:00.000Z
```

````
Hello 雪 literalseparator end &lt;script&gt; &amp; ```
````

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

```
Independent reply 👋
```

### Reasoning summary

```
Recorded summary
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

## Render diagnostics

```json
{
  "excluded_events": 1,
  "rendered_events": 2,
  "since": "2026-01-01T11:00:00+01:00",
  "sources": 2,
  "until": "2026-01-01T10:00:00.000001Z"
}
```
