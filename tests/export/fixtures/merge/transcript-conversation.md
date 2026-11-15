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
  "view": "conversation"
}
```

Keeps nonempty user/assistant messages only; hides reasoning, tool activity and step metadata.

View and source/time selections are combined; all events are validated.

All declared sources are selected.

No timestamp bounds are active.

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

## Render diagnostics

```json
{
  "excluded_events": 1,
  "rendered_events": 2,
  "sources": 2,
  "view": "conversation"
}
```
