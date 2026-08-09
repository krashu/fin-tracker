import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Emit a self-contained server (`.next/standalone/server.js`) so the Docker
  // `run` stage ships a lean runtime without the full node_modules tree.
  output: "standalone",
};

export default nextConfig;
