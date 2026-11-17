# Real formatter-generated candidate

`compiler-prettier.patch` was generated on 2026-11-11 by:

1. Reading the unmodified TypeScript **v5.9.3** `src/compiler/path.ts` fixture in `../upstream/` (Apache-2.0, license there).
2. Changing only `isUrl`'s expression from `< 0` to `<= -1`.
3. Formatting that edited source using **Prettier 3.6.2**'s real `format` API with `{ parser: 'typescript', useTabs: true, singleQuote: false, semi: true, trailingComma: 'none', endOfLine: 'lf' }`.
4. Producing an existing-file unified diff with jsdiff 9.0.0 `createTwoFilesPatch`, path `compiler/path.ts`.

The candidate therefore combines an authorized function edit with a genuine whole-file formatter spill. It is expected to reject while recording `isUrl` as accepted. `provenance.json` contains source/candidate/patch SHA-256 hashes and the observed diagnostic count. Diagnostic details may evolve; the scope verdict is the regression contract.

Prettier's published source came from https://registry.npmjs.org/prettier/-/prettier-3.6.2.tgz . Its temporary 8.46 MB-unpacked package was removed after generation. No Prettier runtime dependency or source code is retained. The patch includes derived TypeScript code, covered by the upstream Apache license included with the base fixture.
