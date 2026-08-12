// Minimal flat config for v0.1.
//
// Next.js / React-specific rules are NOT layered in yet. Originally deferred pending
// eslint-config-next's flat-config export stabilising — that shipped (16.3.0 exports a
// native Linter.Config[], no FlatCompat shim needed). The actual current blocker,
// verified 2026-08 on typescript@7.0.2: eslint-config-next hard-depends on
// typescript-eslint@^8.46.0, whose typescript-estree submodule reads `ts.Extension` at
// require()-time — an enum TypeScript 7's native/Go-ported package doesn't expose
// (`require('typescript').Extension` is `undefined`). The crash is on *load*, not lint,
// and hits every export path (`.`, `/typescript`, `/core-web-vitals`) since they all pull
// in typescript-eslint. Revisit once typescript-eslint ships TS7 support.
export default [
  {
    ignores: [".next/**", "node_modules/**", "next-env.d.ts"],
  },
];
