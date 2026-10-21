import { fileURLToPath, URL } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv } from "vite";

const projectRoot = fileURLToPath(new URL("..", import.meta.url));

function parsePort(name: string, raw: string | undefined, fallback: number): number {
  if (raw === undefined || raw.trim() === "") {
    return fallback;
  }
  const port = Number(raw);
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    throw new Error(`${name} 必须是 1-65535 之间的整数，当前值为 ${JSON.stringify(raw)}`);
  }
  return port;
}

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, projectRoot, "");
  const backendPort = parsePort("APP_BACKEND_PORT", env.APP_BACKEND_PORT, 8000);
  const frontendPort = parsePort("APP_FRONTEND_PORT", env.APP_FRONTEND_PORT, 5173);
  const configuredHost = env.APP_BACKEND_HOST?.trim() || "127.0.0.1";
  // 浏览器代理只连接本机回环地址；后端监听 0.0.0.0 时仍通过 127.0.0.1 访问。
  const backendHost = configuredHost === "0.0.0.0" ? "127.0.0.1" : configuredHost;

  return {
    plugins: [react()],
    envDir: projectRoot,
    server: {
      host: "127.0.0.1",
      port: frontendPort,
      strictPort: true,
      proxy: {
        "/api": {
          target: `http://${backendHost}:${backendPort}`,
          changeOrigin: false,
        },
      },
    },
    preview: {
      host: "127.0.0.1",
      strictPort: true,
    },
  };
});
