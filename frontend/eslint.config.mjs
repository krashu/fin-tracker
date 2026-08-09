// Minimal flat config for v0.1.
// Next.js / React-specific rules will be layered in at v0.6 (dashboards), once
// eslint-config-next's flat-config export stabilises.
export default [
  {
    ignores: [".next/**", "node_modules/**", "next-env.d.ts"],
  },
];
