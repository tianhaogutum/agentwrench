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
  "rendered_events": 3,
  "sources": 2
}
```
