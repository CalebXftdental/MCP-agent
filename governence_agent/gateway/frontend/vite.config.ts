import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

/**
 * Paths forwarded to the gateway by the dev server. Everything else — `/`,
 * `/login`, `/signup`, `/dashboard*` — is left to Vite's own dev index.html:
 * this app owns its sign-in view now (LoginPage.tsx), so none of those need
 * the real gateway's HTML, only its JSON API underneath.
 *
 * `/backend` is the whole JSON API (API_PREFIX in gateway/app.py), so one entry
 * covers every endpoint. The gateway's legacy bare paths — /admin/*, /artifacts/*
 * and the rest — are deliberately NOT proxied: the old app.html is served from
 * port 8020 and calls them same-origin there, never through this dev server.
 *
 * `/legacy` IS proxied, unlike the rest of the HTML paths above — it's the one
 * page route that's supposed to serve the pre-React shell, not this app, so it
 * has to reach the real gateway rather than Vite's SPA fallback.
 *
 * `/mcp` is deliberately absent: that is the governed machine plane, which takes
 * an API key rather than a session cookie, and this app never calls it.
 */
const GATEWAY_PATHS = ['/backend', '/legacy']

export default defineConfig(({ mode }) => {
  // Third arg '' loads unprefixed vars too, so GATEWAY_ORIGIN is visible here
  // while staying out of the browser bundle (only VITE_* gets inlined).
  const env = loadEnv(mode, process.cwd(), '')
  const target = env.GATEWAY_ORIGIN || 'http://localhost:8020'

  return {
    plugins: [react()],
    server: {
      port: Number(env.FRONTEND_PORT) || 5173,
      // Proxying keeps every request same-origin from the browser's point of
      // view, so the HttpOnly SameSite=Strict session cookie is sent normally
      // and the gateway needs no CORS handling at all.
      proxy: Object.fromEntries(
        GATEWAY_PATHS.map((path) => [
          path,
          {
            target,
            changeOrigin: true,
            // /chat/stream is Server-Sent Events; http-proxy streams it through
            // unbuffered, so no extra config is needed for it.
          },
        ]),
      ),
    },
  }
})
