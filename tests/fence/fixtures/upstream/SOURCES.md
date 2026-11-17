# Fixture provenance

These are unmodified real upstream TypeScript source files downloaded on 2026-11-11. Tests produce candidate patches with a Python port of jsdiff `createTwoFilesPatch` (`tests/fence/support.py`) and, for integration tests, real Git.

* `typescript-path.ts`: Microsoft TypeScript **v5.9.3**, `src/compiler/path.ts`, https://raw.githubusercontent.com/microsoft/TypeScript/v5.9.3/src/compiler/path.ts . Apache-2.0; accompanying `TypeScript-LICENSE.txt`. Contains production compiler path utilities, imports, regexes, comments, exports, overloads, optional parameters and nested logic.
* `rxjs-pipe.ts`: ReactiveX RxJS **7.8.2**, `src/internal/util/pipe.ts`, https://raw.githubusercontent.com/ReactiveX/rxjs/7.8.2/src/internal/util/pipe.ts . Apache-2.0; accompanying `RxJS-LICENSE.txt`. Contains generic overload signatures, function declarations, arrow functions and a nested reducer.
* `query-client-provider.tsx` and `hydration-boundary.tsx`: **@tanstack/react-query 5.90.7**, `package/src/QueryClientProvider.tsx` and `package/src/HydrationBoundary.tsx`, extracted without modification from https://registry.npmjs.org/@tanstack/react-query/-/react-query-5.90.7.tgz . MIT; accompanying `TanStack-LICENSE.txt`. The published source includes `'use client'`, type-only imports, generic hooks, object destructuring, optional chaining, effects, JSX member tags and semicolon-free code. The temporary 733 KiB-unpacked package archive was deleted after extracting these fixtures and the license.

Small adversarial source strings in tests are authored for this project and are marked as synthetic in test names/descriptions. No third-party project code is executed.
