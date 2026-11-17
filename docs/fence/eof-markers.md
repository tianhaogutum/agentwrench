# Why `agentwrench fence` requires canonical EOF markers

A unified diff marks a line without a trailing newline with
`\ No newline at end of file`. Tools disagree on misplaced markers, and a scope
gate must check the same bytes that `git apply` will later produce.

## The disagreement

jsdiff (npm `diff` 9.0.0) scans the final hunk for markers and treats them as
global end-of-file state. Git applies each marker to the line it follows. With
this baseline (no final newline):

```text
function allowed(){return 1;} // trailing
function protectedFn(){return 2;}
```

and this patch, whose markers sit on the first line instead of the last:

```diff
--- a/a.ts
+++ b/a.ts
@@ -1 +1 @@
-function allowed(){return 1;} // trailing
\ No newline at end of file
+function allowed(){return 3;} // trailing
\ No newline at end of file
```

jsdiff reconstructs:

```text
function allowed(){return 3;} // trailing
function protectedFn(){return 2;}
```

while `git apply --unidiff-zero` (observed with Git 2.54) accepts the patch and
joins the marked line with the next one:

```text
function allowed(){return 3;} // trailingfunction protectedFn(){return 2;}
```

The two results have different syntax: Git's output comments out
`protectedFn`. A gate that checked jsdiff's reconstruction would inspect
different code from what Git applies.

## What the gate requires

Before applying a patch in memory, `agentwrench fence` refuses noncanonical EOF
evidence (`invalid_patch`, or `patch_mismatch` when a marker contradicts the
baseline):

- each marker follows exactly one unmarked data line, in the final hunk;
- the line it marks is the actual old or new end-of-file line;
- old-side markers agree with the baseline bytes, and a touched old last line
  without a newline must be marked;
- the resulting candidate's line count and final-newline state agree with the
  new-side evidence.

Git-generated LF, CRLF and no-final-newline combinations reconstruct the exact
candidate bytes. This is a conservative input restriction on top of the
jsdiff-compatible application; ordinary Git output is unaffected.
